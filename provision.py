"""Create the SharePoint document library and its columns, by script.

The brief requires provisioning to be version-controlled rather than clicked
together in the portal, so this file -- not a set of instructions -- is the
record of how the library is configured. It is idempotent: run it as often as
you like, it only creates what is missing.

    python3 provision.py --dry-run     # show what would be created
    python3 provision.py               # create it
    python3 provision.py --show        # describe what is already there

What it does NOT do: apply the Purview retention label. Retention labels are
published from the Purview compliance portal and cannot be created through
Graph. Once the client publishes one, attach it as the library's DEFAULT label
and every file inherits it with no pipeline code. See RUNBOOK.md.
"""
import argparse
import sys

import config
import sharepoint_site
from graph_client import GraphClient, GraphError, make_auth

# Mirrors the manifest columns, so the library is filterable in the browser
# the same way the CSV is sortable in Excel.
COLUMNS = [
    {"name": "Team",        "text": {}},
    {"name": "ISOWeek",     "text": {}, "displayName": "ISO Week"},
    {"name": "RecordingID", "text": {}, "displayName": "Recording ID"},
    {"name": "CallDate",    "dateTime": {"format": "dateOnly"},
     "displayName": "Call Date (Melbourne)"},
    {"name": "Direction",   "text": {}},
    {"name": "DurationSec", "number": {"decimalPlaces": "none"},
     "displayName": "Duration (sec)"},
]


def find_library(client, site_id, name):
    lists = client.json("GET", "/sites/{}/lists?$select=id,displayName,list".format(
        site_id)).get("value", [])
    for item in lists:
        if (item.get("displayName") or "").lower() == name.lower():
            return item
    return None


def create_library(client, site_id, name):
    return client.json("POST", "/sites/{}/lists".format(site_id), json={
        "displayName": name,
        "list": {"template": "documentLibrary"},
    })


def existing_columns(client, site_id, list_id):
    cols = client.json("GET", "/sites/{}/lists/{}/columns?$select=name".format(
        site_id, list_id)).get("value", [])
    return {(c.get("name") or "").lower() for c in cols}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--skip-columns", action="store_true",
                        help="do not attempt the columns; they need fullcontrol "
                             "and nothing in the pipeline populates them")
    args = parser.parse_args()

    name = (config.SP_LIBRARY_NAME or "").strip()
    if not name:
        print("  SP_LIBRARY_NAME is not set in .env")
        return 1

    print("\n  site    : https://{}{}".format(
        config.SP_SITE_HOSTNAME, config.SP_SITE_PATH))
    print("  library : {}\n".format(name))

    client = GraphClient(make_auth())
    try:
        site = client.json("GET", "/sites/{}:{}".format(
            config.SP_SITE_HOSTNAME, config.SP_SITE_PATH))
    except GraphError as exc:
        print("  cannot read the site: {}".format(exc))
        if exc.status == 403:
            print("\n  403 here means the per-site permission grant is missing."
                  "\n  Sites.Selected consent alone grants access to no sites.")
        return 1
    site_id = site["id"]

    library = find_library(client, site_id, name)

    if args.show:
        if not library:
            print("  the library does not exist yet")
            return 1
        have = existing_columns(client, site_id, library["id"])
        print("  exists, id {}".format(library["id"]))
        for col in COLUMNS:
            mark = "ok " if col["name"].lower() in have else "MISSING"
            print("    {:<8} {}".format(mark, col.get("displayName", col["name"])))
        return 0

    if library:
        print("  library already exists -- leaving it alone")
    elif args.dry_run:
        print("  would CREATE the library")
    else:
        try:
            library = create_library(client, site_id, name)
            print("  created the library, id {}".format(library["id"]))
        except GraphError as exc:
            if exc.status != 403:
                raise
            print("  403 -- cannot create a document library on this site.\n")
            print("  Reading the site works, so the app and the per-site grant")
            print("  are both fine. Creating a library additionally needs the")
            print("  grant's role to be 'fullcontrol'; 'write' is not enough.\n")
            print("  Two ways forward:")
            print("    a) ask for the grant to be upgraded to fullcontrol, or")
            print("    b) have someone create the library named {!r} in the".format(name))
            print("       browser once, then re-run this script -- it will add")
            print("       the columns and cache the drive id.\n")
            print("  Run check_access.py for exactly what to send them.")
            return 1

    if library and args.skip_columns:
        print("  columns skipped (--skip-columns)")
    elif library:
        have = existing_columns(client, site_id, library["id"])
        for col in COLUMNS:
            label = col.get("displayName", col["name"])
            if col["name"].lower() in have:
                print("  column {:<22} already present".format(label))
            elif args.dry_run:
                print("  column {:<22} would be created".format(label))
            else:
                try:
                    client.json("POST", "/sites/{}/lists/{}/columns".format(
                        site_id, library["id"]), json=col)
                    print("  column {:<22} created".format(label))
                except GraphError as exc:
                    if exc.status != 403:
                        raise
                    print("  column {:<22} 403 -- needs fullcontrol".format(label))

    if not args.dry_run and library:
        _, drive_id, root = sharepoint_site.resolve(client, name)
        sharepoint_site.save_cache(site_id, drive_id, root, name)
        print("\n  DRIVE_ROOT={}".format(root))
        print("  cached in {}".format(sharepoint_site.CACHE))

    print("\n  Remaining manual step: publish a Purview retention label and set"
          "\n  it as this library's default. See RUNBOOK.md.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
