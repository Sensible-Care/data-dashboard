"""Turn the site URL in .env into the drive path the rest of the code uses.

Everything downstream writes to `config.DRIVE_ROOT`, e.g. /drives/{id}. That
id is not something anyone should paste by hand, so resolve it from the site
hostname and path the client gave us and cache it -- it never changes.

    python3 sharepoint_site.py            # resolve and cache (one live call)
    python3 sharepoint_site.py --show     # print what is cached, no network
"""
import json
import os
import sys

from core import config
from core import state
from clients.graph_client import GraphClient, GraphError, make_auth

CACHE = state.path(".site.json")


def resolve(client, library=None):
    """(site_id, drive_id, drive_root) for the configured site and library."""
    if not config.SP_SITE_HOSTNAME or not config.SP_SITE_PATH:
        raise RuntimeError("SP_SITE_HOSTNAME and SP_SITE_PATH must be set in .env")

    site = client.json("GET", "/sites/{}:{}".format(
        config.SP_SITE_HOSTNAME, config.SP_SITE_PATH))
    site_id = site["id"]

    wanted = (library or config.SP_LIBRARY_NAME or "").strip()
    drives = client.json("GET", "/sites/{}/drives".format(site_id)).get("value", [])
    if not drives:
        raise RuntimeError("the site has no document libraries")

    match = None
    if wanted:
        match = next((d for d in drives
                      if (d.get("name") or "").lower() == wanted.lower()), None)
        if match is None:
            have = ", ".join(sorted(d.get("name", "?") for d in drives))
            raise RuntimeError(
                "no library named {!r} on this site. Found: {}. "
                "Run provision.py to create it.".format(wanted, have))
    else:
        match = drives[0]          # the default 'Documents' library

    return site_id, match["id"], "/drives/{}".format(match["id"])


def load_cache():
    """The cached site/drive ids, or {} if there is nothing usable.

    A damaged cache must NEVER be fatal. This file is read by every entry
    point -- the nightly job, the webhook, every manual run -- so a
    truncated or hand-edited copy used to raise JSONDecodeError and stop
    all of them at startup. An empty dict instead means the caller resolves
    the site again from the hostname in .env, costing one Graph call and
    rewriting the cache correctly.
    """
    if not os.path.exists(CACHE):
        return {}
    try:
        with open(CACHE) as fh:
            cached = json.load(fh)
    except (ValueError, OSError) as exc:
        print("  .site.json is unreadable ({}) -- resolving the site again"
              .format(exc))
        return {}
    return cached if isinstance(cached, dict) else {}


def save_cache(site_id, drive_id, drive_root, library):
    """Write the cache atomically.

    The plain open(..., "w") this replaced truncated the file before writing
    it, so a crash or a full disk mid-write left a half-written cache that
    every later run choked on. Writing a temp file and renaming it means the
    cache is either the old value or the new one, never a fragment.
    """
    body = json.dumps({"site_id": site_id, "drive_id": drive_id,
                       "drive_root": drive_root, "library": library}, indent=2)
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(body)
    os.replace(tmp, CACHE)


def cached_root():
    """The resolved root if we already know it, else whatever config says.

    Never touches the network, so it is safe for banners and dry runs.
    """
    return (os.environ.get("DRIVE_ROOT")
            or load_cache().get("drive_root")
            or config.DRIVE_ROOT)


def drive_root(client=None):
    """The value DRIVE_ROOT should hold, from cache or by resolving once."""
    if os.environ.get("DRIVE_ROOT"):
        return os.environ["DRIVE_ROOT"]
    cached = load_cache()
    if cached.get("drive_root"):
        return cached["drive_root"]
    client = client or GraphClient(make_auth())
    site_id, drive_id, root = resolve(client)
    save_cache(site_id, drive_id, root, config.SP_LIBRARY_NAME)
    return root


def main():
    if "--show" in sys.argv:
        cached = load_cache()
        print(json.dumps(cached, indent=2) if cached else "  nothing cached yet")
        return 0

    print("\n  site     : https://{}{}".format(
        config.SP_SITE_HOSTNAME, config.SP_SITE_PATH))
    print("  library  : {}".format(config.SP_LIBRARY_NAME or "(default)"))
    try:
        client = GraphClient(make_auth())
        site_id, drive_id, root = resolve(client)
    except GraphError as exc:
        print("\n  FAILED: {}".format(exc))
        if exc.status == 403:
            print("  403 means the app authenticated but has no grant on this site.")
            print("  Sites.Selected consent alone is not enough -- the client must")
            print("  also POST /sites/{id}/permissions with role 'fullcontrol'.")
        return 1
    except RuntimeError as exc:
        print("\n  {}".format(exc))
        return 1

    save_cache(site_id, drive_id, root, config.SP_LIBRARY_NAME)
    print("\n  site id  : {}".format(site_id))
    print("  drive id : {}".format(drive_id))
    print("  cached   : {}".format(CACHE))
    print("\n  DRIVE_ROOT={}\n".format(root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
