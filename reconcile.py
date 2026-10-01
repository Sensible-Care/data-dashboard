"""Daily reconciliation: did every transcript Zoom had actually land?

Compares Zoom's recording IDs for a day against what is in SharePoint --
not against a local database. SharePoint is the delivery target, so it is
the only thing worth asking.

Runs against a day only after transcription has caught up. Zoom transcribes
asynchronously, so a same-night check reports gaps that are not real; the
default lag is 2 days.

    python3 reconcile.py                      # the day RECONCILE_LAG_DAYS ago
    python3 reconcile.py --day 2026-09-20
    python3 reconcile.py --fixtures           # offline
    python3 reconcile.py --no-alert           # report only, send nothing
"""
import argparse
import os
import sys
from datetime import date, datetime, timedelta

import alerts
import config
import sharepoint_site
import team_source
import team_sync
from drive_writer import DriveWriter
from graph_client import GraphClient, make_auth
from models import short_id, week_folders
from team_map import TeamMap
from zoom_client import FixtureTransport, ZoomClient
from zoom_directory import ZoomDirectory


def run(day, use_fixtures=False, send_alert=True, drive=None):
    zoom = ZoomClient(FixtureTransport() if use_fixtures else None)
    if drive is None:
        client = GraphClient(make_auth())
        drive = DriveWriter(client, root=sharepoint_site.drive_root(client))

    # Read-only: the ingest stage runs first and is what records a change
    # and alerts on it. Reconciliation must not quietly consume one. The
    # exception is bootstrapping, when the derived file does not exist yet.
    team_csv = team_source.ensure(drive, notify=False)
    _rows, _changes = team_sync.sync(
        team_csv, config.TEAM_EFFECTIVE_CSV,
        write=not os.path.exists(config.TEAM_EFFECTIVE_CSV))
    teams = TeamMap(config.TEAM_EFFECTIVE_CSV)
    directory = ZoomDirectory(
        users=zoom.transport.users() if use_fixtures else None,
        transport=None if use_fixtures else zoom.transport)

    # Zoom's view. A window either side, because a UTC day and a Melbourne
    # day do not line up -- we filter to the local date afterwards.
    start = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).date()
    end = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).date()

    expected = {}
    no_transcript = 0
    for rec in zoom.iter_recordings(str(start), str(end)):
        if rec.local_started.strftime("%Y-%m-%d") != day:
            continue
        if not rec.transcript_download_url:
            no_transcript += 1
            continue
        expected[short_id(rec.id)] = rec

    # SharePoint's view. Look in EVERY team folder for the weeks this day
    # touches, not only the folder the current mapping points at: if someone
    # changed team, their already-delivered transcripts sit under the old
    # name and would otherwise be reported missing every night.
    folders = set()
    for rec in expected.values():
        folders.update(week_folders(rec, teams))

    delivered = set()
    for folder in folders:
        delivered |= drive.existing_recording_ids(folder)

    missing = set(expected) - delivered

    print("\n  reconciliation for {}  (local time, {})".format(day, config.TIMEZONE))
    print("  " + "-" * 56)
    print("  recordings with a transcript : {}".format(len(expected)))
    print("  recordings without one       : {}  (ignored)".format(no_transcript))
    print("  folders checked              : {}".format(len(folders)))
    print("  delivered                    : {}".format(len(expected) - len(missing)))
    print("  MISSING                      : {}".format(len(missing)))

    if missing:
        print("\n  missing recording ids:")
        for rid in sorted(missing)[:20]:
            print("    {}".format(rid))
        if len(missing) > 20:
            print("    ... and {} more".format(len(missing) - 20))

        if send_alert and use_fixtures:
            print("\n  (fixture run -- no alert sent)")
        elif send_alert:
            body = alerts.gap_report(day, missing, len(expected), no_transcript)
            try:
                alerts.send("Transcript pipeline: {} gap(s) on {}".format(
                    len(missing), day), body)
                print("\n  alert sent to {}".format(config.ALERT_EMAIL))
            except alerts.AlertError as exc:
                print("\n  ALERT FAILED: {}".format(exc))
                return 2
        else:
            print("\n  (--no-alert: nothing sent)")
        return 1

    print("\n  no gaps.\n")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--day", help="YYYY-MM-DD (default: RECONCILE_LAG_DAYS ago)")
    p.add_argument("--fixtures", action="store_true")
    p.add_argument("--no-alert", action="store_true")
    args = p.parse_args()

    day = args.day or str(date.today() - timedelta(days=config.RECONCILE_LAG_DAYS))
    return run(day, args.fixtures, not args.no_alert)


if __name__ == "__main__":
    sys.exit(main())
