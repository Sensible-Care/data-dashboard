"""One-off historical backfill: every transcript Zoom still holds.

Roughly 150,000 transcripts and 8-12 hours of work, so the job is built to be
killed and restarted. It walks one calendar month at a time (Zoom refuses a
wider query) and records each finished month in .backfill_progress.json. A
restart skips what is already done.

Nothing here tracks individual files. Re-running a month is harmless because
the daily pipeline already treats SharePoint as the ledger: a recording whose
id is present in the destination folder is skipped. The progress file only
saves time, never correctness -- delete it and the job still converges.

    python3 backfill.py --from 2025-07 --to 2026-09 --dry-run
    python3 backfill.py --from 2025-07 --to 2026-09
    python3 backfill.py --status
    python3 backfill.py --from 2025-07 --to 2026-09 --restart

Transcripts only exist from roughly July 2025; earlier recordings have none
and never will, so starting before that just burns API calls.

Run this on the client's infrastructure, not a laptop -- it moves production
data, and the brief forbids that touching our machines.
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

from jobs import pipeline
from core import state
PROGRESS = state.path(".backfill_progress.json")


def month_range(first, last):
    """Yield (label, from_date, to_date) for each month, oldest first."""
    year, month = first
    while (year, month) <= last:
        start = date(year, month, 1)
        nxt = date(year + (month == 12), month % 12 + 1, 1)
        yield "{:04d}-{:02d}".format(year, month), start, nxt - timedelta(days=1)
        year, month = nxt.year, nxt.month


def parse_month(text):
    try:
        parts = text.split("-")
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        raise argparse.ArgumentTypeError(
            "month must look like 2025-07, got {!r}".format(text))


def load_progress():
    if os.path.exists(PROGRESS):
        try:
            with open(PROGRESS) as fh:
                return json.load(fh)
        except ValueError:
            print("  {} is corrupt -- starting over".format(PROGRESS))
    return {"done": {}, "started": None}


def save_progress(state):
    tmp = PROGRESS + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, PROGRESS)        # atomic: a kill cannot truncate it


def show_status(state):
    done = state.get("done", {})
    if not done:
        print("\n  nothing recorded yet\n")
        return 0
    print("\n  month     uploaded  skipped  failed   finished")
    print("  " + "-" * 56)
    totals = [0, 0, 0]
    for label in sorted(done):
        entry = done[label]
        totals = [t + entry.get(k, 0)
                  for t, k in zip(totals, ("uploaded", "skipped", "failed"))]
        print("  {}   {:>7,}  {:>7,}  {:>5,}   {}".format(
            label, entry.get("uploaded", 0), entry.get("skipped", 0),
            entry.get("failed", 0), (entry.get("finished") or "")[:19]))
    print("  " + "-" * 56)
    print("  {} months   {:>7,}  {:>7,}  {:>5,}\n".format(
        len(done), *totals))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="first", type=parse_month, metavar="YYYY-MM")
    parser.add_argument("--to", dest="last", type=parse_month, metavar="YYYY-MM")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be uploaded, write nothing")
    parser.add_argument("--fixtures", action="store_true")
    parser.add_argument("--status", action="store_true", help="progress so far")
    parser.add_argument("--restart", action="store_true",
                        help="forget progress and redo every month")
    parser.add_argument("--pause", type=float, default=2.0, metavar="SEC",
                        help="seconds between months, to stay polite (default 2)")
    args = parser.parse_args()

    if args.fixtures and not args.dry_run:
        print("  --fixtures implies --dry-run; synthetic data never goes to SharePoint")
        args.dry_run = True

    state = load_progress()
    if args.status:
        return show_status(state)
    if args.restart:
        state = {"done": {}, "started": None}
        print("  progress cleared")
    if not args.first or not args.last:
        parser.error("--from and --to are required (or use --status)")
    if args.first > args.last:
        parser.error("--from is after --to")

    months = list(month_range(args.first, args.last))
    todo = [m for m in months if m[0] not in state.get("done", {})]

    print("\n  backfill {} .. {}".format(months[0][0], months[-1][0]))
    print("  {} months, {} already done, {} to go".format(
        len(months), len(months) - len(todo), len(todo)))
    if args.dry_run:
        print("  DRY RUN -- nothing will be written")
    print()

    if not todo:
        print("  nothing left to do\n")
        return 0

    state.setdefault("started", datetime.utcnow().isoformat() + "Z")
    began = time.time()
    failed_months = []

    for index, (label, start, end) in enumerate(todo, 1):
        print("  " + "=" * 62)
        print("  [{}/{}] {}   {} .. {}".format(index, len(todo), label, start, end))
        print("  " + "=" * 62)
        try:
            rc = pipeline.run(str(start), str(end), use_fixtures=args.fixtures,
                              dry_run=args.dry_run, write_manifests=not args.dry_run)
        except KeyboardInterrupt:
            print("\n\n  interrupted. {} months finished; rerun the same command"
                  "\n  to carry on from {}.\n".format(index - 1, label))
            save_progress(state)
            return 130
        except Exception as exc:                      # noqa: BLE001
            # One bad month must not cost the other fifty.
            print("\n  month {} FAILED: {}\n".format(label, exc))
            failed_months.append(label)
            continue

        if not args.dry_run:
            state["done"][label] = {
                "uploaded": pipeline.LAST_RUN.get("uploaded", 0),
                "skipped": pipeline.LAST_RUN.get("skipped", 0),
                "failed": pipeline.LAST_RUN.get("failed", 0),
                "rc": rc,
                "finished": datetime.utcnow().isoformat() + "Z",
            }
            save_progress(state)
        time.sleep(args.pause)

    elapsed = time.time() - began
    print("\n  " + "=" * 62)
    print("  finished {} months in {:.1f} minutes".format(len(todo), elapsed / 60))
    if failed_months:
        print("  months that failed and need rerunning: {}".format(
            ", ".join(failed_months)))
        print("  they were NOT recorded, so the same command retries them.")
    print()
    return 1 if failed_months else 0


if __name__ == "__main__":
    sys.exit(main())
