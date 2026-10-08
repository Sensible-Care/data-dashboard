"""Zoom Team Chat -> SharePoint, foldered by team and ISO week.

    python3 -m jobs.chat                          # last CHAT_LOOKBACK_DAYS
    python3 -m jobs.chat --from 2026-09-01 --to 2026-09-30
    python3 -m jobs.chat --dry-run                # plan only, writes nothing
    python3 -m jobs.chat --fixtures --dry-run     # no credentials needed

Separate from jobs/pipeline.py and jobs/meetings.py for the same reason
those are separate from each other: shared infrastructure, nothing shared
about the data.

Two things make chat genuinely different from the other two pipelines, and
both are deliberate:

DUAL DELIVERY. The client mapped 13 of 54 channels to two teams and asked
for the record to appear under both. The meeting and phone pipelines treat
"this id exists in ANY candidate team folder" as already delivered, which is
right when a recording belongs to exactly one team and may have moved
between them. Applied here it would write the first folder, see the id, and
skip the second -- silently, with no error. So chat dedupes PER FOLDER: each
(channel, week, team) is decided on its own.

MUTABLE WEEKS. A transcript is final when it appears. A chat week is not --
messages keep arriving until the week ends. So the file is rewritten while
the week is open and frozen once MANIFEST_SEAL_DAYS have passed. A sealed
week is skipped before any message is fetched, which is what makes
re-running a long range cheap.
"""
import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import pytz

from core import chat as mdl, chat_manifest as mf, config, state
from clients.drive_writer import DriveWriter
from clients.graph_client import GraphClient, GraphError, make_auth
from clients.zoom_chat import ChatClient, FixtureChat
from clients import sharepoint_site
from clients.zoom_client import ZoomError
from teams import channel_source, team_source, team_sync
from teams.channel_map import ChannelMap
from teams.team_map import TeamMap

FAILURE_LOG = state.path("failures.log")

LAST_RUN = {}


def _log_failure(what, exc):
    with open(FAILURE_LOG, "a") as fh:
        fh.write("{}\tchat:{}\t{}\n".format(
            datetime.utcnow().isoformat() + "Z", what, str(exc)[:300]))


def whole_weeks(date_from, date_to):
    """Every whole Melbourne ISO week touched by a date range.

    A week is written as a single file, so a request for Wed..Fri has to
    become the week those days fall in -- otherwise the file would hold
    three days and overwrite a complete copy of that week.
    """
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    start -= timedelta(days=start.isoweekday() - 1)          # back to Monday
    end += timedelta(days=7 - end.isoweekday())              # on to Sunday

    # The padding days belong to the weeks either side, so they would
    # otherwise produce a one-message file for a week we did not actually
    # cover -- and overwrite a complete copy of it with less content. Only
    # the weeks fully inside the aligned span may be written.
    valid, cursor = set(), start
    while cursor <= end:
        iso = cursor.isocalendar()
        valid.add((iso[0], iso[1]))
        cursor += timedelta(days=7)
    return valid


def week_days(iso_year, week):
    """The UTC days that can hold messages for this Melbourne ISO week.

    Monday to Sunday, plus a day either side: Melbourne runs 10-11 hours
    ahead, so the week's first local morning sits in the previous UTC day
    and its last local evening can spill into the next.
    """
    monday = mf.week_end(iso_year, week) - timedelta(days=6)
    return [monday + timedelta(days=n) for n in range(-1, 8)]


class Listings(object):
    """Folder listings, fetched once per run.

    Without this the seal check lists the same dozen team-week folders once
    per channel -- about 270 Graph calls a night to learn the same 48
    answers. Only ADDITIONS happen during a run, so a stale entry can only
    under-report: the caller then re-delivers, compares bytes, and finds it
    unchanged. Wrong in the safe direction.
    """

    def __init__(self, drive):
        self.drive = drive
        self._cache = {}

    def items(self, folder):
        if folder not in self._cache:
            self._cache[folder] = self.drive.existing_items(folder)
        return self._cache[folder]


def deliver(cw, teams, drive, rows=None, dry_run=False, listings=None):
    """Put ONE channel-week into EVERY team folder it belongs to.

    Returns a count of (uploaded, unchanged, failed) across those folders --
    a channel mapped to two teams is two deliveries, and they succeed or
    fail independently.
    """
    uploaded = unchanged = failed = 0
    label = " + ".join(teams)
    body = mdl.render(cw, label)
    filename = mdl.filename_for(cw)

    for team in teams:
        folder = mdl.folder_for(cw, team)
        if dry_run:
            print("  would    {}/{}  ({} msg, {} thread(s))".format(
                folder, filename, len(cw.messages), cw.thread_count))
            uploaded += 1
            continue
        try:
            # Per folder, never across folders: see the module docstring.
            current = drive.download(folder, filename)
            if current == body:
                unchanged += 1
                if rows is not None:
                    look = listings.items(folder) if listings else \
                        drive.existing_items(folder)
                    item = look.get(mdl.short_id(cw.channel_id))
                    rows[(cw.iso_year, cw.week, team)].append(
                        mf.row_for(cw, team, teams, filename, None, item, ""))
                continue
            drive.ensure_folder(folder)
            item = drive.upload(folder, filename, body, len(body))
            uploaded += 1
            if rows is not None:
                rows[(cw.iso_year, cw.week, team)].append(
                    mf.row_for(cw, team, teams, filename, body, item,
                               datetime.utcnow().isoformat() + "Z"))
            print("  uploaded {}/{}  ({} msg, {} thread(s))".format(
                folder, filename, len(cw.messages), cw.thread_count))
        except (GraphError, ZoomError, OSError) as exc:
            if isinstance(exc, GraphError) and exc.status in (401, 403):
                raise
            _log_failure("{}|{}-W{:02d}|{}".format(
                cw.channel_name, cw.iso_year, cw.week, team), exc)
            print("  FAILED   {}\n           {}".format(folder, exc))
            failed += 1
    return uploaded, unchanged, failed


def sealed_everywhere(listings, channel_id, iso_year, week, teams):
    """Is this channel-week already final in every team folder it needs?

    A sealed week cannot change, so if the file is present in all of them
    there is nothing to fetch and nothing to write. Asking this BEFORE any
    message is fetched is what stops a re-run of a year costing a year of
    Zoom calls every night.
    """
    if not mf.is_sealable(iso_year, week):
        return False
    want = mdl.short_id(channel_id)
    for team in teams:
        folder = mdl.week_folder_path(team, iso_year, week)
        if want not in listings.items(folder):
            return False
    return True


def run(date_from, date_to, use_fixtures=False, dry_run=False, drive=None,
        limit=None):
    zoom = ChatClient(FixtureChat("fixtures") if use_fixtures else None)

    if use_fixtures and drive is None and not dry_run:
        raise RuntimeError(
            "--fixtures cannot write to SharePoint. Add --dry-run, or inject "
            "a drive.")
    # Built for a dry run too, unlike the other jobs. The channel map lives
    # in SharePoint, so without a client there is nothing to tell us which
    # channels to archive and --dry-run cannot work at all inside the
    # container. Reading is harmless: every write below is behind `dry_run`.
    if drive is None and not use_fixtures:
        client = GraphClient(make_auth())
        drive = DriveWriter(client, root=sharepoint_site.drive_root(client))

    # The team map is only needed to validate the channel map's team names
    # against reality, but it must be loaded for fixtures too -- leaving it
    # out would make every alias "unrecognised" and file the lot under
    # _Unassigned while the tests still passed.
    if not use_fixtures and drive is not None:
        team_csv = team_source.ensure(drive, notify=not dry_run)
        team_sync.sync(team_csv, config.TEAM_EFFECTIVE_CSV, write=not dry_run)
    teams_map = TeamMap(config.TEAM_EFFECTIVE_CSV)

    channel_csv = (config.CHAT_CHANNEL_CSV if (use_fixtures or drive is None)
                   else channel_source.ensure(drive, notify=not dry_run))
    channels = ChannelMap(channel_csv, known_teams=teams_map.teams())
    print("  teams mapped: {} | channels requested: {}".format(
        len(teams_map), len(channels)))
    for problem in channels.problems:
        print("  ! {}".format(problem))

    resolved, problems = zoom.resolve(channels.names())
    for problem in problems:
        print("  ! {}".format(problem))
    print("  channels resolved: {} of {}".format(len(resolved), len(channels)))
    print()

    valid_weeks = whole_weeks(date_from, date_to)
    seen = uploaded = unchanged = failed = skipped = 0
    rows = defaultdict(list)
    listings = Listings(drive) if drive is not None else None

    for channel in resolved:
        target = channels.teams_for(channel["requested_as"])
        if limit is not None and seen >= limit:
            print("\n  stopping at --limit {}".format(limit))
            break

        # Decide what is still needed BEFORE touching Zoom. A sealed week
        # already present in every team folder cannot change, so its days
        # are never fetched at all.
        needed = []
        for iso_year, week in sorted(valid_weeks):
            if not dry_run and listings is not None and sealed_everywhere(
                    listings, channel["id"], iso_year, week, target):
                skipped += 1
                continue
            needed.append((iso_year, week))
        if not needed:
            continue

        days = sorted({d for wk in needed for d in week_days(*wk)})
        try:
            weeks = zoom.channel_weeks(channel, days)
        except ZoomError as exc:
            _log_failure(channel["name"], exc)
            print("  FAILED   {}\n           {}".format(channel["name"], exc))
            failed += 1
            continue

        wanted = set(needed)
        for cw in weeks:
            # A padding day drags in the neighbouring week, which we have
            # not covered in full. Writing it would replace a complete file
            # with a partial one.
            if cw.iso_week not in wanted:
                continue
            seen += 1
            up, un, fl = deliver(cw, target, drive, rows, dry_run, listings)
            uploaded += up
            unchanged += un
            failed += fl

    if not dry_run and rows:
        _write_manifests(drive, rows)

    LAST_RUN.clear()
    LAST_RUN.update(seen=seen, uploaded=uploaded, unchanged=unchanged,
                    skipped=skipped, failed=failed)
    print("\n  channel-weeks {} | written {} | unchanged {} | sealed-skip {} "
          "| failed {}".format(seen, uploaded, unchanged, skipped, failed))
    if failed:
        print("  failures appended to {}".format(FAILURE_LOG))
    return 1 if failed else 0


def _write_manifests(drive, rows):
    print()
    for (iso_year, week, team), entries in sorted(rows.items()):
        if not mf.is_sealable(iso_year, week):
            print("  manifest {}-W{:02d} {} deferred (week still open)".format(
                iso_year, week, team))
            continue
        _seal_one(drive, iso_year, week, team, entries)


def _seal_one(drive, iso_year, week, team, entries):
    """Write this week's manifest, or a supplement for anything it misses."""
    folder = mdl.week_folder_path(team, iso_year, week)
    present = {i["name"] for i in drive.list_children(folder)}
    sealed = mf.name_for(iso_year, week, team)

    if sealed not in present:
        body = mf.render(entries)
        drive.upload(folder, sealed, body, len(body))
        print("  manifest {} ({} rows)".format(sealed, len(entries)))
        return

    reported = set()
    for name in [sealed] + mf.supplements_in(iso_year, week, team, present):
        reported |= mf.channel_ids(drive.download(folder, name))

    late = [r for r in entries if r.get("channel_id") not in reported]
    if not late:
        return
    name = mf.next_supplement(iso_year, week, team, present)
    body = mf.render(late)
    drive.upload(folder, name, body, len(body))
    print("  manifest {} ({} late row(s))".format(name, len(late)))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", help="YYYY-MM-DD")
    p.add_argument("--lookback", type=int, metavar="N")
    p.add_argument("--fixtures", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, metavar="N",
                   help="stop after N channel-weeks -- for a smoke test")
    args = p.parse_args()

    if args.fixtures and not args.dry_run:
        print("  --fixtures implies --dry-run; synthetic data never reaches "
              "SharePoint")
        args.dry_run = True

    if not args.date_from:
        days = (args.lookback if args.lookback is not None
                else config.CHAT_LOOKBACK_DAYS)
        today = datetime.now(pytz.timezone(config.TIMEZONE)).date()
        args.date_from = str(today - timedelta(days=days))
        args.date_to = str(today)
    elif not args.date_to:
        args.date_to = args.date_from

    print("\n  Zoom chat {} .. {}".format(args.date_from, args.date_to))
    print("  layout: {}/{{team}}/{}\n".format(
        config.CHAT_ROOT_FOLDER, config.CHAT_WEEK_TEMPLATE))
    try:
        return run(args.date_from, args.date_to, args.fixtures, args.dry_run,
                   limit=args.limit)
    except GraphError as exc:
        if exc.status in (401, 403):
            print("\n  {}. The app authenticated but has no grant on the "
                  "site.\n".format(exc.status))
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
