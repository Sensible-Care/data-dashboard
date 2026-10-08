"""Zoom meeting transcripts -> SharePoint, foldered by team and ISO week.

    python3 -m jobs.meetings                         # last LOOKBACK_DAYS
    python3 -m jobs.meetings --from 2026-09-01 --to 2026-09-30
    python3 -m jobs.meetings --dry-run               # plan only, writes nothing
    python3 -m jobs.meetings --fixtures --dry-run    # no credentials needed

Separate from jobs/pipeline.py on purpose. The two share every piece of
infrastructure -- SharePoint, the team map, throttling -- but nothing about
their data. Keeping them apart means a change to meetings cannot break the
phone pipeline, which has been running in production since September.

Dedupe works exactly as it does for calls: SharePoint is the ledger. A
transcript whose id already appears in any of the week's team folders is
skipped, so re-running a range is always safe.
"""
import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import pytz

from core import config, meeting as mdl, meeting_manifest as mf, state, vtt
from clients.drive_writer import DriveWriter
from clients.graph_client import GraphClient, GraphError, make_auth
from clients.zoom_meetings import FixtureMeetings, MeetingsClient
from clients import sharepoint_site
from clients.zoom_client import ZoomError
from teams import team_source, team_sync
from teams.team_map import TeamMap

FAILURE_LOG = state.path("failures.log")

LAST_RUN = {}


def _log_failure(transcript_id, exc):
    with open(FAILURE_LOG, "a") as fh:
        fh.write("{}\tmeeting:{}\t{}\n".format(
            datetime.utcnow().isoformat() + "Z", transcript_id, str(exc)[:300]))


def existing_team_folders(drive):
    """Team folder names that actually exist under the meeting root.

    week_folders() can only enumerate the teams teams.csv knows about. If a
    team is removed from that file outright -- the client hand-edits it in
    SharePoint and deletes the last member -- the old folder stops being
    checked and everything in it gets uploaded a second time under the new
    team.

    Listing what is really there closes that hole. It costs one call per run,
    which is nothing at this volume (tens of meetings a month, not hundreds a
    day), and it makes dedupe independent of the team map entirely.
    """
    try:
        children = drive.list_children(config.MEETING_ROOT_FOLDER)
    except GraphError:
        return []
    # Graph returns immediate children ("Rostering"); the test double returns
    # paths relative to the folder ("Rostering/Meeting - 2026 - W39/x.txt").
    # The first segment is the team name in both cases.
    names = {(c.get("name") or "").split("/")[0] for c in children}
    return sorted(n for n in names if n)


def deliver(rec, zoom, drive, teams, known, extra_teams=(), rows=None):
    """Put ONE meeting transcript where it belongs.

    `known` is a {(iso_year, week): {short_id: folder}} cache of what
    SharePoint already holds, shared across the run so each week is listed
    once rather than once per transcript.
    """
    team = mdl.team_for(rec, teams)
    folder = mdl.folder_for(rec, teams=teams)
    # Keyed by the ISO week, which is the folder level files land in.
    week_key = rec.iso_week

    if week_key not in known:
        candidates = set(mdl.week_folders(rec, teams))
        candidates.update(mdl.folder_for(rec, team=name) for name in extra_teams)
        delivered = {}
        for candidate in sorted(candidates):
            for sid, item in drive.existing_items(candidate).items():
                delivered[sid] = (candidate, item)
        known[week_key] = delivered

    already = known[week_key].get(mdl.short_id(rec.id))
    if already is not None:
        # Delivered by an earlier run. It still belongs in this week's
        # manifest -- the CSV describes the week, not this run. File it
        # against the folder the transcript is ACTUALLY in, not the team the
        # host is in today; after a move those differ, and trusting today's
        # mapping writes a row into a week for a team that never held it.
        where, item = already
        actual = where.split("/")[1] if "/" in where else team
        if rows is not None:
            rows[(rec.iso_week[0], rec.iso_week[1], actual)].append(
                mf.row_for(rec, actual, item.get("name", ""), None, item, ""))
        return "skipped"

    try:
        drive.ensure_folder(folder)
        stream, _ = zoom.download(rec.download_url)
        raw = stream.read()
        body, ext = vtt.render(raw, config.TRANSCRIPT_FORMAT, meta=[
            ("Team", team),
            ("Topic", rec.topic or "(no topic)"),
            ("Host", rec.host_email or "(unknown)"),
            ("Started (Melbourne)",
             rec.local_started.strftime("%Y-%m-%d %H:%M:%S %Z")),
            ("ISO week", "{}-W{:02d}".format(*rec.iso_week)),
            ("Duration", "{} min".format(rec.duration)),
            ("Meeting ID", str(rec.meeting_id)),
            ("Transcript ID", rec.id)])
        filename = mdl.filename_for(rec, ext)
        item = drive.upload(folder, filename, body, len(body))
        known[week_key][mdl.short_id(rec.id)] = (folder, item)
        if rows is not None:
            rows[(rec.iso_week[0], rec.iso_week[1], team)].append(
                mf.row_for(rec, team, filename, body, item,
                           datetime.utcnow().isoformat() + "Z"))
        print("  uploaded {}/{}".format(folder, filename))
        return "uploaded"
    except (GraphError, ZoomError, OSError) as exc:
        if isinstance(exc, GraphError) and exc.status in (401, 403):
            raise
        _log_failure(rec.id, exc)
        print("  FAILED   {}\n           {}".format(folder, exc))
        return "failed"


def run(date_from, date_to, use_fixtures=False, dry_run=False, drive=None,
        limit=None):
    zoom = MeetingsClient(FixtureMeetings() if use_fixtures else None)

    if use_fixtures and drive is None and not dry_run:
        raise RuntimeError(
            "--fixtures cannot write to SharePoint. Add --dry-run, or inject "
            "a drive.")
    if drive is None and not dry_run:
        client = GraphClient(make_auth())
        drive = DriveWriter(client, root=sharepoint_site.drive_root(client))

    # Refreshing the map from SharePoint needs the network, so it is skipped
    # for fixtures -- but the map itself is just a local CSV and is always
    # loaded. Leaving it unloaded would quietly file every fixture run under
    # _Unassigned and make the team tests pass for the wrong reason.
    if not use_fixtures and drive is not None:
        team_csv = team_source.ensure(drive, notify=not dry_run)
        team_sync.sync(team_csv, config.TEAM_EFFECTIVE_CSV, write=not dry_run)
    teams = TeamMap(config.TEAM_EFFECTIVE_CSV)
    print("  teams mapped: {}".format(len(teams)))
    print()

    seen = uploaded = skipped = failed = 0
    known = {}
    rows = defaultdict(list)          # (iso_year, week, team) -> manifest rows
    # Listed once, before anything is delivered, so dedupe sees folders for
    # teams that teams.csv no longer mentions.
    extra_teams = [] if (dry_run or drive is None) else existing_team_folders(drive)

    for rec in zoom.iter_transcripts(date_from, date_to):
        if limit is not None and uploaded >= limit:
            print("\n  stopping at --limit {}".format(limit))
            break
        seen += 1
        if dry_run:
            print("  would    {}/{}".format(
                mdl.folder_for(rec, teams=teams), mdl.filename_for(rec)))
            continue
        outcome = deliver(rec, zoom, drive, teams, known, extra_teams, rows)
        if outcome == "uploaded":
            uploaded += 1
        elif outcome == "skipped":
            skipped += 1
        else:
            failed += 1

    if not dry_run and rows:
        _write_manifests(drive, rows)

    LAST_RUN.clear()
    LAST_RUN.update(seen=seen, uploaded=uploaded, skipped=skipped, failed=failed)
    print("\n  seen {} | uploaded {} | skipped {} | failed {}".format(
        seen, uploaded, skipped, failed))
    if failed:
        print("  failures appended to {}".format(FAILURE_LOG))
    return 1 if failed else 0


def _write_manifests(drive, rows):
    """One manifest per team per week, once the week has closed."""
    print()
    for (iso_year, week, team), entries in sorted(rows.items()):
        if not mf.is_sealable(iso_year, week):
            print("  manifest {}-W{:02d} {} deferred (week still open)".format(
                iso_year, week, team))
            continue
        _seal_one(drive, iso_year, week, team, entries)


def _seal_one(drive, iso_year, week, team, entries):
    """Write this week's manifest, or a supplement for anything it misses.

    A sealed manifest is never rewritten: a retention label may have made it
    a record, and rewriting one would fail. Late arrivals go into a numbered
    supplement beside it.

    The catch is knowing what "late" means. Re-running a range that is already
    delivered produces a full set of rows again, so the existing files are read
    back and only genuinely unreported transcripts are written -- otherwise
    every run leaves another near-identical supplement.
    """
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
        reported |= mf.transcript_ids(drive.download(folder, name))

    late = [r for r in entries if r.get("transcript_id") not in reported]
    if not late:
        return                      # already fully accounted for; stay quiet

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
                   help="stop after N uploads -- for a first smoke test")
    args = p.parse_args()

    if args.fixtures and not args.dry_run:
        print("  --fixtures implies --dry-run; synthetic data never reaches "
              "SharePoint")
        args.dry_run = True

    if not args.date_from:
        days = args.lookback if args.lookback is not None else config.LOOKBACK_DAYS
        today = datetime.now(pytz.timezone(config.TIMEZONE)).date()
        args.date_from = str(today - timedelta(days=days))
        args.date_to = str(today)
    elif not args.date_to:
        args.date_to = args.date_from

    print("\n  Zoom meetings {} .. {}".format(args.date_from, args.date_to))
    print("  layout: {}/{{team}}/{}\n".format(
        config.MEETING_ROOT_FOLDER, config.MEETING_WEEK_TEMPLATE))
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
