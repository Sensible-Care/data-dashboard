"""Write the weekly manifests the daily run can never reach.

pipeline.py collects manifest rows from the recordings its window happens to
cover, and the daily window is LOOKBACK_DAYS wide. A week only becomes
sealable MANIFEST_SEAL_DAYS after it closes -- by which point the daily
window has moved on to the following week and contains none of that week's
calls. Simulated over three weeks of consecutive daily runs, the number of
manifests written was zero.

Widening the lookback is not the fix: a window that reaches back far enough
to touch the sealed week still only covers PART of it, and a manifest
listing four days of a seven-day week is worse than no manifest at all.

So sealing is its own pass over the week's exact Monday..Sunday range.
Everything in that range is already delivered, so the run uploads nothing
and simply gathers the rows -- and anything genuinely missing gets
delivered on the way, which makes this a weekly self-heal too.

    python3 seal.py                  # the most recently sealed week
    python3 seal.py --weeks 6        # catch up on a backlog
    python3 seal.py --dry-run        # say what it would do, touch nothing
    python3 seal.py --week 2026-W39  # one specific week

Safe to run daily. A week whose manifests are all present is skipped
without a single Zoom call.
"""
import argparse
import sys
from datetime import date, timedelta

import config
import manifest
import pipeline
import sharepoint_site
import team_source
import team_sync
from drive_writer import DriveWriter
from graph_client import GraphClient, GraphError, make_auth
from team_map import UNMAPPED, TeamMap


def week_bounds(iso_year, week):
    """(monday, sunday) for an ISO week."""
    sunday = manifest.week_end(iso_year, week)
    return sunday - timedelta(days=6), sunday


def sealable_weeks(count, today=None):
    """The `count` most recently sealable ISO weeks, oldest first."""
    today = today or date.today()
    found, probe = [], today
    while len(found) < count and probe > today - timedelta(days=400):
        iso_year, week, _ = probe.isocalendar()
        if manifest.is_sealable(iso_year, week, today) and (iso_year, week) not in found:
            found.append((iso_year, week))
        probe -= timedelta(days=7)
    return list(reversed(found))


def parse_week(text):
    try:
        year, week = text.upper().split("-W")
        return int(year), int(week)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "week must look like 2026-W39, got {!r}".format(text))


def unsealed_teams(drive, iso_year, week, teams):
    """Teams that have transcripts for this week but no manifest yet.

    A team with no folder simply took no calls that week and needs nothing,
    which is what keeps this cheap enough to run every night.
    """
    pending = []
    for team in list(teams.teams()) + [UNMAPPED]:
        folder = "{}/{}-W{:02d}".format(team, iso_year, week)
        names = {i.get("name", "") for i in drive.list_children(folder)}
        transcripts = {n for n in names if not n.startswith("_manifest")}
        if transcripts and manifest.name_for(iso_year, week, team) not in names:
            pending.append(team)
    return pending


def run(weeks=1, only=None, dry_run=False, drive=None, today=None,
        use_fixtures=False):
    if drive is None:
        client = GraphClient(make_auth())
        drive = DriveWriter(client, root=sharepoint_site.drive_root(client))
    team_sync.sync(team_source.ensure(drive, notify=False),
                   config.TEAM_EFFECTIVE_CSV, write=False)
    teams = TeamMap(config.TEAM_EFFECTIVE_CSV)

    targets = [only] if only else sealable_weeks(weeks, today)
    if not targets:
        print("\n  no sealed weeks yet\n")
        return 0

    print("\n  checking {} week(s): {}\n".format(
        len(targets), ", ".join("{}-W{:02d}".format(*t) for t in targets)))

    sealed = failed = 0
    for iso_year, week in targets:
        label = "{}-W{:02d}".format(iso_year, week)
        if not manifest.is_sealable(iso_year, week, today):
            print("  {}  still open -- skipped".format(label))
            continue

        pending = unsealed_teams(drive, iso_year, week, teams)
        if not pending:
            print("  {}  already sealed".format(label))
            continue

        monday, sunday = week_bounds(iso_year, week)
        print("  {}  {} team(s) need a manifest: {}".format(
            label, len(pending), ", ".join(pending)))
        if dry_run:
            print("           would run {} .. {}".format(monday, sunday))
            continue

        print("           running {} .. {}".format(monday, sunday))
        try:
            pipeline.run(str(monday), str(sunday), write_manifests=True,
                         drive=drive, use_fixtures=use_fixtures)
            sealed += 1
        except (GraphError, OSError) as exc:
            print("           FAILED: {}".format(exc))
            failed += 1

    print("\n  {} week(s) sealed, {} failed\n".format(sealed, failed))
    return 1 if failed else 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--weeks", type=int, default=1,
                   help="how many recently sealed weeks to check (default 1)")
    p.add_argument("--week", type=parse_week, metavar="YYYY-Www",
                   help="one specific week, e.g. 2026-W39")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--fixtures", action="store_true", help="canned Zoom data")
    args = p.parse_args()
    return run(weeks=args.weeks, only=args.week, dry_run=args.dry_run,
               use_fixtures=args.fixtures)


if __name__ == "__main__":
    sys.exit(main())
