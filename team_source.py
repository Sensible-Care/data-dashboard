"""teams.csv, maintained by the client in SharePoint instead of in the code.

Today the mapping is a file next to the code. That works on a laptop and is
useless in Azure: there is no file for anyone to edit, and a redeploy would
be needed for every staff change.

So the client keeps it in the document library, beside the transcripts:

    Call Transcripts/_config/teams.csv

Each run downloads it, checks it, and caches it in STATE_DIR. Everything
downstream is unchanged -- team_sync still reads a path.

The whole point of the checking is that this file decides where 150,000
transcripts get filed. A bad edit is not a crash; it is every person
silently falling to _Unassigned and the next run re-filing the lot. So a
download that does not look like a team map is REJECTED and the previous
good copy is used instead, with an alert. Refusing to update is always
safer than accepting nonsense.

    python3 team_source.py --show        # what is cached, no network
    python3 team_source.py --validate teams.csv
    python3 team_source.py --pull        # fetch from SharePoint now
    python3 team_source.py --push        # upload the local file (first time)
"""
import argparse
import csv
import io
import os
import sys

import config
import state

CACHE = state.path("teams_cached.csv")
REMOTE_NAME = "teams.csv"

# A new file with fewer than this fraction of the previous row count is
# almost certainly a filtered view someone saved by accident, or a partial
# upload. 73 people becoming 5 must not quietly re-file everyone.
#
# Overridable because the refusal must not become a dead end: inside a
# container there is no cached file to delete by hand, so a genuine large
# reduction needs a way through that does not involve a redeploy.
DEFAULT_MIN_RETAINED_FRACTION = 0.5


def min_retained_fraction():
    try:
        return float(os.environ.get("TEAM_CSV_MIN_FRACTION",
                                    DEFAULT_MIN_RETAINED_FRACTION))
    except ValueError:
        return DEFAULT_MIN_RETAINED_FRACTION


class TeamSourceError(Exception):
    pass


def remote_folder():
    return (os.environ.get("TEAM_CONFIG_FOLDER")
            or getattr(config, "TEAM_CONFIG_FOLDER", "_config"))


def from_sharepoint():
    return (os.environ.get("TEAM_CSV_SOURCE")
            or getattr(config, "TEAM_CSV_SOURCE", "local")).lower() == "sharepoint"


# ------------------------------------------------------------- validation
def parse(text):
    """[{email, team}] from CSV text. Raises TeamSourceError if unusable."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise TeamSourceError("not UTF-8 text -- is it really a CSV?")
    if not text.strip():
        raise TeamSourceError("the file is empty")

    reader = csv.DictReader(io.StringIO(text))
    fields = {(f or "").strip().lower() for f in (reader.fieldnames or [])}
    if "email" not in fields:
        raise TeamSourceError(
            "no 'email' column (found: {})".format(
                ", ".join(sorted(fields)) or "nothing"))
    if not fields & {"team", "division"}:
        raise TeamSourceError("no 'team' column")

    rows = []
    for raw in reader:
        low = {(k or "").strip().lower(): (v or "").strip()
               for k, v in raw.items()}
        email = low.get("email", "").lower()
        team = low.get("team") or low.get("division", "")
        if email and team:
            rows.append({"email": email, "team": team})
    if not rows:
        raise TeamSourceError("no usable rows -- every line is missing an "
                              "email or a team")
    return rows


def count_cached():
    """Rows in the copy we are currently trusting, or 0."""
    for path in (CACHE, config.TEAM_CSV):
        if path and os.path.exists(path):
            try:
                with open(path, "rb") as fh:
                    return len(parse(fh.read()))
            except (TeamSourceError, IOError, OSError):
                continue
    return 0


def check(text, previous=None):
    """Rows, if this content is safe to adopt. Raises otherwise."""
    rows = parse(text)
    previous = count_cached() if previous is None else previous
    if previous and len(rows) < previous * min_retained_fraction():
        raise TeamSourceError(
            "only {} rows, down from {}. That would send {} people to "
            "_Unassigned and re-file their transcripts, so it has been "
            "refused. If the reduction is real, set "
            "TEAM_CSV_MIN_FRACTION=0 for one run to accept it."
            .format(len(rows), previous, previous - len(rows)))
    return rows


# ------------------------------------------------------------------ fetch
def pull(drive, notify=False):
    """Download, check and cache. Returns (path, problem).

    `problem` is a string when the remote copy was refused or unreachable
    and the previous copy is being used instead -- never a reason to stop
    the run, because yesterday's mapping is far better than none.
    """
    folder = remote_folder()
    try:
        body = drive.download(folder, REMOTE_NAME)
    except Exception as exc:                                  # noqa: BLE001
        return _fallback("could not reach {}/{}: {}".format(
            folder, REMOTE_NAME, exc), notify)

    if not body:
        return _fallback(
            "{}/{} is missing from the library".format(folder, REMOTE_NAME),
            notify)

    try:
        rows = check(body)
    except TeamSourceError as exc:
        return _fallback("{}/{} was refused: {}".format(
            folder, REMOTE_NAME, exc), notify)

    tmp = CACHE + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(body if isinstance(body, bytes) else body.encode("utf-8"))
    os.replace(tmp, CACHE)
    print("  teams.csv: {} rows from {}/{}".format(len(rows), folder, REMOTE_NAME))
    return CACHE, ""


def _fallback(problem, notify):
    """Use the last copy we trusted, and say loudly that we did."""
    for path in (CACHE, config.TEAM_CSV):
        if path and os.path.exists(path):
            print("  teams.csv: {}\n             using the previous copy ({})"
                  .format(problem, path))
            if notify:
                _alert(problem, path)
            return path, problem
    raise TeamSourceError(
        "{} -- and there is no previous copy to fall back on. Refusing to "
        "run: without a team map every transcript would file under "
        "_Unassigned.".format(problem))


def _alert(problem, using):
    try:
        import alerts
        alerts.send("Transcript pipeline: teams.csv was not updated",
                    "The team mapping in SharePoint could not be used.\n\n"
                    "  {}\n\n"
                    "The previous copy is still in force ({}), so filing\n"
                    "continues as before and nothing has moved. Fix the file\n"
                    "at {}/{} and the next run will pick it up.\n"
                    .format(problem, using, remote_folder(), REMOTE_NAME))
    except Exception as exc:                                  # noqa: BLE001
        print("             (could not send the alert: {})".format(exc))


def ensure(drive=None, notify=False):
    """The path team_sync should read this run.

    Local mode, or no drive (a dry run), keeps today's behaviour exactly.
    """
    if not from_sharepoint() or drive is None:
        return config.TEAM_CSV
    path, _problem = pull(drive, notify=notify)
    return path


def push(drive, path=None):
    """Put the local file into the library. Used once, at setup."""
    path = path or config.TEAM_CSV
    with open(path, "rb") as fh:
        body = fh.read()
    rows = parse(body)                     # never upload something unusable
    folder = remote_folder()
    drive.ensure_folder(folder)
    drive.upload(folder, REMOTE_NAME, body, len(body))
    print("  uploaded {} ({} rows) to {}/{}".format(
        path, len(rows), folder, REMOTE_NAME))
    return len(rows)


# -------------------------------------------------------------------- CLI
def _drive():
    import sharepoint_site
    from drive_writer import DriveWriter
    from graph_client import GraphClient, make_auth
    client = GraphClient(make_auth())
    return DriveWriter(client, root=sharepoint_site.drive_root(client))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--show", action="store_true", help="what is cached")
    p.add_argument("--validate", metavar="FILE", help="check a file, no network")
    p.add_argument("--pull", action="store_true")
    p.add_argument("--push", action="store_true")
    args = p.parse_args()

    if args.validate:
        try:
            rows = parse(open(args.validate, "rb").read())
        except TeamSourceError as exc:
            print("\n  REJECTED: {}\n".format(exc))
            return 1
        teams = sorted({r["team"] for r in rows})
        print("\n  {} rows, {} teams".format(len(rows), len(teams)))
        for team in teams:
            print("    {:<28} {}".format(
                team, sum(1 for r in rows if r["team"] == team)))
        print()
        return 0

    if args.show:
        print("\n  source     : {}".format(
            "SharePoint {}/{}".format(remote_folder(), REMOTE_NAME)
            if from_sharepoint() else "local file"))
        print("  local file : {}".format(config.TEAM_CSV))
        print("  cache      : {}  ({})".format(
            CACHE, "present" if os.path.exists(CACHE) else "not fetched yet"))
        print("  rows in use: {}\n".format(count_cached()))
        return 0

    if args.pull:
        path, problem = pull(_drive(), notify=False)
        print("\n  using {}{}\n".format(path, "  ({})".format(problem) if problem else ""))
        return 1 if problem else 0

    if args.push:
        push(_drive())
        return 0

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
