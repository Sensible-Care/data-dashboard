"""Where derived state lives.

Five files are produced at runtime rather than shipped with the code:

    teams_effective.csv      team history, accumulated by team_sync
    teams_cached.csv         last teams.csv accepted from SharePoint
    .backfill_progress.json  which months the backfill finished
    failures.log             items that failed permanently
    .site.json               resolved SharePoint drive id (a cache)
    .zoom_directory.json     Zoom user list (a cache)
    queue/                   webhook events awaiting delivery

On a laptop or a VPS they sit next to the code and nobody thinks about it.
In a container the filesystem is thrown away on every restart, which loses
the first three -- and those are not caches:

    teams_effective.csv   losing it does NOT duplicate anything (SharePoint
                          is still the ledger, and cross-folder dedupe is
                          tested against exactly this case), but the history
                          of who moved when is gone and the next run stops
                          alerting on moves it has already forgotten.
    .backfill_progress    losing it restarts a 10-hour job from month one.
    failures.log          losing it means a permanently broken item is
                          retried forever with nobody the wiser.

So STATE_DIR points at something that outlives the container: an Azure Files
share mounted into it, or any persistent directory elsewhere. Unset -- the
default -- every path is exactly the bare filename it has always been, so
nothing changes locally.

This module deliberately imports nothing from the project. config imports
it, so it cannot import config.
"""
import os

# Read once at import. Set it in the environment, not in code.
DIR = (os.environ.get("STATE_DIR") or "").strip().rstrip("/")

_made = False


def path(name):
    """Where `name` should be read from and written to.

    Returns the bare name when STATE_DIR is unset, so paths are
    byte-for-byte what they were before this module existed and no test
    comparing filenames has to change.
    """
    if not DIR:
        return name
    _ensure()
    return os.path.join(DIR, name)


def _ensure():
    """Create STATE_DIR on first use.

    A mounted share already exists, so this is for a plain directory on a
    VPS. Failing here would be fatal and unhelpful, so let the real open()
    raise instead -- its error names the file.
    """
    global _made
    if _made:
        return
    try:
        os.makedirs(DIR, exist_ok=True)
    except OSError:
        pass
    _made = True


def describe():
    """One line for a banner, so an operator can see where state went."""
    return DIR or "(alongside the code)"
