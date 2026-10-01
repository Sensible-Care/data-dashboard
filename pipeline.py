"""Zoom Phone transcripts -> SharePoint, foldered by team and ISO week."""
import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import pytz

import alerts
import config
import manifest
import sharepoint_site
import state
import team_source
import team_sync
import transcript as tfmt
from drive_writer import DriveWriter
from graph_client import GraphClient, GraphError, make_auth
from models import filename_for, folder_for, redact, short_id, week_folders
from team_map import UNMAPPED, TeamMap
from zoom_client import FixtureTransport, ZoomClient, ZoomError
from zoom_directory import ZoomDirectory

FAILURE_LOG = state.path("failures.log")

# Counts from the most recent run(). run() keeps returning an exit code so
# every existing caller is unaffected; backfill.py needs the detail.
LAST_RUN = {}


def _team_of(folder):
    """The team a delivered transcript is filed under, from its folder."""
    return folder.split("/")[0]


def _log_failure(recording_id, exc):
    """Append-only text, not a database. Stops a permanently broken item
    from being retried silently forever."""
    with open(FAILURE_LOG, "a") as fh:
        fh.write("{}\t{}\t{}\n".format(
            datetime.utcnow().isoformat() + "Z", recording_id, str(exc)[:300]))


def deliver(rec, zoom, drive, teams, directory, known, rows=None):
    """Put ONE recording where it belongs. The single copy of that logic.

    Called by the nightly pipeline and by the webhook worker. Two
    implementations of "which folder, and has it already been delivered"
    would drift apart within a month, and the failure mode is silent
    duplicates in the client's library.

    `known` is a {(iso_year, week): {short_id: (folder, item)}} cache of
    what SharePoint already holds, shared across a batch to save listings.
    `rows` collects manifest rows; None when the caller writes no manifest.

    Returns "skipped", "uploaded" or "failed".
    """
    team = teams.resolve(rec, directory)
    folder = folder_for(rec, teams=teams, directory=directory)
    filename = filename_for(rec)
    via = directory.email_for(rec) and "person" or "queue"
    iso_year, week = rec.iso_week
    week_key = rec.iso_week

    if week_key not in known:
        delivered = {}
        for candidate in week_folders(rec, teams):
            for sid, item in drive.existing_items(candidate).items():
                delivered[sid] = (candidate, item)
        known[week_key] = delivered

    already = known[week_key].get(short_id(rec.id))
    if already is not None:
        # Delivered by an earlier run. It still belongs in this week's
        # manifest -- the CSV describes the week, not this run.
        #
        # File it against the folder the transcript is ACTUALLY in, not the
        # team the person is in today. After a move those differ: the
        # transcript correctly stays under the old team, so trusting today's
        # mapping writes a manifest into an empty folder for a team that
        # never took the call, listing it as theirs.
        where, item = already
        actual = _team_of(where)
        if rows is not None:
            rows[(iso_year, week, actual)].append(manifest.row_for(
                rec, actual, item.get("name", filename), None, item, via, ""))
        return "skipped"

    try:
        drive.ensure_folder(folder)
        stream, _ = zoom.download(rec.transcript_download_url)
        raw = stream.read()
        body, _ext = tfmt.render(raw, config.TRANSCRIPT_FORMAT, meta=[
            ("Team", team),
            ("Started (Melbourne)",
             rec.local_started.strftime("%Y-%m-%d %H:%M:%S %Z")),
            ("ISO week", "{}-W{:02d}".format(iso_year, week)),
            ("Direction", rec.direction),
            ("Duration", "{}s".format(rec.duration)),
            ("Recording ID", rec.id)])
        item = drive.upload(folder, filename, body, len(body))
        known[week_key][short_id(rec.id)] = (folder, item)
        if rows is not None:
            rows[(iso_year, week, team)].append(manifest.row_for(
                rec, team, filename, body, item, via,
                datetime.utcnow().isoformat() + "Z"))
        print("  uploaded " + redact("{}/{}".format(folder, item.get("name"))))
        return "uploaded"
    except (GraphError, ZoomError, OSError) as exc:
        if isinstance(exc, GraphError) and exc.status in (401, 403):
            raise
        _log_failure(rec.id, exc)
        print("  FAILED   " + redact(folder) + "\n           {}".format(exc))
        return "failed"


def run(date_from, date_to, use_fixtures=False, dry_run=False,
        write_manifests=True, drive=None, limit=None):
    zoom = ZoomClient(FixtureTransport() if use_fixtures else None)

    if use_fixtures and drive is None and not dry_run:
        # Fixture data must never reach the client's tenant. Without this the
        # combination silently authenticates and writes synthetic recordings
        # into production SharePoint.
        raise RuntimeError(
            "--fixtures cannot write to SharePoint. Add --dry-run, or inject "
            "a drive (test_end_to_end.py does this with FakeDrive).")
    if drive is None and not dry_run:
        client = GraphClient(make_auth())
        drive = DriveWriter(client, root=sharepoint_site.drive_root(client))

    # The mapping may be maintained by the client in SharePoint. Built after
    # the drive because it needs one, and before team_sync because that is
    # what reads it.
    team_csv = team_source.ensure(
        drive, notify=not dry_run and not use_fixtures)

    # Fill in effective dates for any team change the client recorded without
    # them, so a move applies from today rather than rewriting history.
    _rows, team_changes = team_sync.sync(
        team_csv, config.TEAM_EFFECTIVE_CSV, write=not dry_run)
    teams = TeamMap(config.TEAM_EFFECTIVE_CSV)
    directory = ZoomDirectory(
        users=zoom.transport.users() if use_fixtures else None,
        transport=None if use_fixtures else zoom.transport)

    print("  directory: {} users | teams mapped: {} | queues: {}".format(
        len(directory), len(teams), len(teams._by_queue)))
    if team_changes:
        print("\n  team change(s) detected, applied from today:")
        for email, previous, new in team_changes:
            print("    {:<34} {} -> {}".format(email, previous or "(none)", new))
        # Never email from a fixture run -- the alert goes to the client's
        # mailbox, and a test has no business landing there.
        if not dry_run and not use_fixtures:
            try:
                alerts.send("Transcript pipeline: {} team change(s)".format(
                    len(team_changes)), team_sync.describe(team_changes))
            except alerts.AlertError as exc:
                print("    (could not send the alert: {})".format(exc))
        elif use_fixtures:
            print("    (fixture run -- no alert sent)")
    if not teams.loaded:
        print("  note: no {} -- everything files under {}".format(
            config.TEAM_CSV, UNMAPPED))
    print()

    seen = uploaded = skipped = failed = no_transcript = 0
    # (iso_year, week) -> ids already in SharePoint ANYWHERE in that week.
    # Keyed by week rather than by folder so that a recording already
    # delivered under a different team is still recognised; see
    # models.week_folders for why that matters.
    known = {}
    rows = defaultdict(list)           # (iso_year, week, team) -> manifest rows

    for rec in zoom.iter_recordings(date_from, date_to):
        if limit is not None and uploaded >= limit:
            print("\n  stopping at --limit {}".format(limit))
            break
        seen += 1
        if not rec.transcript_download_url:
            no_transcript += 1
            continue

        if dry_run:
            print("  would    " + redact("{}/{}".format(
                folder_for(rec, teams=teams, directory=directory),
                filename_for(rec))))
            continue

        outcome = deliver(rec, zoom, drive, teams, directory, known, rows)
        if outcome == "skipped":
            skipped += 1
        elif outcome == "uploaded":
            uploaded += 1
        elif outcome == "failed":
            failed += 1

    if write_manifests and not dry_run and rows:
        _write_manifests(drive, rows)

    LAST_RUN.clear()
    LAST_RUN.update(seen=seen, uploaded=uploaded, skipped=skipped,
                    no_transcript=no_transcript, failed=failed)
    print("\n  seen {} | uploaded {} | skipped {} | no transcript {} | failed {}"
          .format(seen, uploaded, skipped, no_transcript, failed))
    if failed:
        print("  failures appended to {}".format(FAILURE_LOG))
    return 1 if failed else 0


def _write_manifests(drive, rows):
    """One manifest per team per week, written once the week has closed."""
    print()
    for (iso_year, week, team), entries in sorted(rows.items()):
        if not manifest.is_sealable(iso_year, week):
            print("  manifest {}-W{:02d} {} deferred (week still open)".format(
                iso_year, week, team))
            continue
        _seal_one(drive, iso_year, week, team, entries)


def _seal_one(drive, iso_year, week, team, entries):
    """Write this week's manifest, or a supplement for anything it misses.

    A sealed manifest is never rewritten -- a Purview retention label may
    have made it a record, and rewriting one would fail. So late arrivals go
    into a numbered supplement beside it.

    The catch is knowing what "late" means. Re-running a range that is
    already delivered produces a full set of rows again, and writing those
    out blindly produced a fresh supplement on every single run. So the
    existing files are read back and only genuinely unreported recordings
    are written.
    """
    folder = "{}/{}-W{:02d}".format(team, iso_year, week)
    present = {i["name"] for i in drive.list_children(folder)}
    sealed = manifest.name_for(iso_year, week, team)

    if sealed not in present:
        body = manifest.render(entries)
        drive.upload(folder, sealed, body, len(body))
        print("  manifest {} ({} rows)".format(sealed, len(entries)))
        return

    reported = set()
    for name in [sealed] + manifest.supplements_in(iso_year, week, team, present):
        reported |= manifest.recording_ids(drive.download(folder, name))

    late = [r for r in entries if r.get("recording_id") not in reported]
    if not late:
        return                      # already fully accounted for; stay quiet

    name = manifest.next_supplement(iso_year, week, team, present)
    body = manifest.render(late)
    drive.upload(folder, name, body, len(body))
    print("  manifest {} ({} late row(s))".format(name, len(late)))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", help="YYYY-MM-DD")
    p.add_argument("--lookback", type=int, metavar="N",
                   help="process the last N days (default {}), in {}".format(
                       config.LOOKBACK_DAYS, config.TIMEZONE))
    p.add_argument("--fixtures", action="store_true", help="canned Zoom data")
    p.add_argument("--dry-run", action="store_true", help="plan only")
    p.add_argument("--limit", type=int, metavar="N",
                   help="stop after N uploads -- for a first smoke test")
    p.add_argument("--no-manifest", action="store_true")
    args = p.parse_args()

    if not args.date_from:
        days = args.lookback if args.lookback is not None else config.LOOKBACK_DAYS
        today = datetime.now(pytz.timezone(config.TIMEZONE)).date()
        args.date_from = str(today - timedelta(days=days))
        args.date_to = str(today)
    elif not args.date_to:
        args.date_to = args.date_from

    print("\n  Zoom {} .. {}  ->  {}".format(
        args.date_from, args.date_to, sharepoint_site.cached_root()))
    print("  layout: {}   tz: {}\n".format(config.FOLDER_TEMPLATE, config.TIMEZONE))
    try:
        return run(args.date_from, args.date_to, args.fixtures,
                   args.dry_run, not args.no_manifest, limit=args.limit)
    except GraphError as exc:
        if exc.status == 401:
            print("\n  401. With app-only auth this is a bad client secret or"
                  "\n  tenant id; with the dev token it has simply expired.\n")
            return 2
        if exc.status == 403:
            print("\n  403. The app authenticated but has no grant on the site."
                  "\n  Sites.Selected consent is not enough on its own -- the"
                  "\n  per-site permission grant must also be in place.\n")
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
