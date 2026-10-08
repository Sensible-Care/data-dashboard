"""chat_channels.csv, maintained by the client in SharePoint.

Exactly the same arrangement as teams.csv (see teams/team_source.py), and
for the same reason: the client adds and removes chat channels without us
redeploying anything.

    Call Transcripts/_config/chat_channels.csv

The protective rule is the same too, and matters more here than it looks.
This file decides which channels are archived at all. A truncated or
half-saved upload does not crash anything -- it silently stops archiving
the channels that fell off the end, and nobody notices until someone goes
looking for a record that was never written. So a download that loses more
than half the channels is REFUSED and the previous good copy stays in force,
with an alert.

    python3 -m teams.channel_source --show
    python3 -m teams.channel_source --validate Zoom-chat-channels-selected.csv
    python3 -m teams.channel_source --pull
    python3 -m teams.channel_source --push Zoom-chat-channels-selected.csv
"""
import argparse
import os
import sys

from core import config, state
from teams.channel_map import ChannelMap, ChannelMapError, parse

CACHE = state.path("chat_channels_cached.csv")


def remote_name():
    return os.environ.get("CHAT_CHANNEL_CSV", config.CHAT_CHANNEL_CSV)


def remote_folder():
    return (os.environ.get("TEAM_CONFIG_FOLDER")
            or getattr(config, "TEAM_CONFIG_FOLDER", "_config"))


def min_retained_fraction():
    try:
        return float(os.environ.get("CHAT_CSV_MIN_FRACTION", "0.5"))
    except ValueError:
        return 0.5


def count_cached():
    for path in (CACHE, config.CHAT_CHANNEL_CSV):
        if path and os.path.exists(path):
            try:
                with open(path, "rb") as fh:
                    return len(parse(fh.read())[0])
            except (ChannelMapError, IOError, OSError):
                continue
    return 0


def check(text, previous=None):
    """Entries, if this content is safe to adopt. Raises otherwise."""
    entries, problems = parse(text)
    previous = count_cached() if previous is None else previous
    if previous and len(entries) < previous * min_retained_fraction():
        raise ChannelMapError(
            "only {} channels, down from {}. That would silently stop "
            "archiving {} channels, so it has been refused. If the "
            "reduction is real, set CHAT_CSV_MIN_FRACTION=0 for one run."
            .format(len(entries), previous, previous - len(entries)))
    return entries, problems


def pull(drive, notify=False):
    """Download, check and cache. Returns (path, problem)."""
    folder, name = remote_folder(), remote_name()
    try:
        body = drive.download(folder, name)
    except Exception as exc:                                  # noqa: BLE001
        return _fallback("could not reach {}/{}: {}".format(
            folder, name, exc), notify)

    if not body:
        return _fallback(
            "{}/{} is missing from the library".format(folder, name), notify)

    try:
        entries, problems = check(body)
    except ChannelMapError as exc:
        return _fallback("{}/{} was refused: {}".format(folder, name, exc),
                         notify)

    tmp = CACHE + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(body if isinstance(body, bytes) else body.encode("utf-8"))
    os.replace(tmp, CACHE)
    print("  chat_channels.csv: {} channels from {}/{}".format(
        len(entries), folder, name))
    for p in problems:
        print("                     ! {}".format(p))
    return CACHE, ""


def _fallback(problem, notify):
    for path in (CACHE, config.CHAT_CHANNEL_CSV):
        if path and os.path.exists(path):
            print("  chat_channels.csv: {}\n                     using the "
                  "previous copy ({})".format(problem, path))
            if notify:
                _alert(problem, path)
            return path, problem
    raise ChannelMapError(
        "{} -- and there is no previous copy to fall back on. Refusing to "
        "run: without a channel map there is nothing to archive and a run "
        "that quietly did nothing would look like success.".format(problem))


def _alert(problem, using):
    try:
        from core import alerts
        alerts.send("Transcript pipeline: chat_channels.csv was not updated",
                    "The chat channel list in SharePoint could not be used.\n\n"
                    "  {}\n\n"
                    "The previous copy is still in force ({}), so the same\n"
                    "channels continue to be archived. Fix the file at\n"
                    "{}/{} and the next run will pick it up.\n"
                    .format(problem, using, remote_folder(), remote_name()))
    except Exception as exc:                                  # noqa: BLE001
        print("                     (could not send the alert: {})".format(exc))


def ensure(drive=None, notify=False):
    """The channel-map path this run should read."""
    if drive is None:
        return config.CHAT_CHANNEL_CSV
    path, _problem = pull(drive, notify=notify)
    return path


def push(drive, path=None):
    """Put a local file into the library. Used once, at setup."""
    path = path or config.CHAT_CHANNEL_CSV
    with open(path, "rb") as fh:
        body = fh.read()
    entries, problems = parse(body)        # never upload something unusable
    folder, name = remote_folder(), remote_name()
    drive.ensure_folder(folder)
    drive.upload(folder, name, body, len(body))
    print("  uploaded {} ({} channels) to {}/{}".format(
        path, len(entries), folder, name))
    for p in problems:
        print("    ! {}".format(p))
    return len(entries)


def _drive():
    from clients import sharepoint_site
    from clients.drive_writer import DriveWriter
    from clients.graph_client import GraphClient, make_auth
    client = GraphClient(make_auth())
    return DriveWriter(client, root=sharepoint_site.drive_root(client))


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--show", action="store_true")
    p.add_argument("--validate", metavar="FILE")
    p.add_argument("--pull", action="store_true")
    p.add_argument("--push", nargs="?", const=True, metavar="FILE")
    args = p.parse_args()

    if args.validate:
        try:
            m = ChannelMap(args.validate)
        except ChannelMapError as exc:
            print("\n  REJECTED: {}\n".format(exc))
            return 1
        from collections import Counter
        per = Counter(t for e in m for t in e["teams"])
        print("\n  {} channels, {} deliveries, {} to two teams".format(
            len(m), sum(len(e["teams"]) for e in m),
            sum(1 for e in m if len(e["teams"]) > 1)))
        for team, n in per.most_common():
            print("    {:<28} {}".format(team, n))
        for prob in m.problems:
            print("    ! {}".format(prob))
        print()
        return 1 if m.problems else 0

    if args.show:
        print("\n  remote : {}/{}".format(remote_folder(), remote_name()))
        print("  local  : {}".format(config.CHAT_CHANNEL_CSV))
        print("  cache  : {}  ({})".format(
            CACHE, "present" if os.path.exists(CACHE) else "not fetched yet"))
        print("  in use : {} channels\n".format(count_cached()))
        return 0

    if args.pull:
        path, problem = pull(_drive(), notify=False)
        print("\n  using {}{}\n".format(
            path, "  ({})".format(problem) if problem else ""))
        return 1 if problem else 0

    if args.push:
        push(_drive(), None if args.push is True else args.push)
        return 0

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
