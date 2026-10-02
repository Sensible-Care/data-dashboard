"""Zoom connectivity and scope check.

Read-only. Prints status codes, counts and field names only -- no emails,
names, phone numbers or transcript content ever reach stdout.

    python3 check_zoom.py
"""
import base64
import json
import sys
from datetime import date, timedelta

import requests

from core import config
OK, BAD, WARN = "\033[32mOK\033[0m", "\033[31mFAIL\033[0m", "\033[33mWARN\033[0m"


def body_of(resp):
    try:
        return resp.json()
    except ValueError:
        return {}


def main():
    missing = [k for k, v in (
        ("ZOOM_ACCOUNT_ID", config.ZOOM_ACCOUNT_ID),
        ("ZOOM_CLIENT_ID", config.ZOOM_CLIENT_ID),
        ("ZOOM_CLIENT_SECRET", config.ZOOM_CLIENT_SECRET),
    ) if not v]
    if missing:
        print("\n{} missing from .env: {}\n".format(BAD, ", ".join(missing)))
        return 1

    print("\n[1] POST /oauth/token   (are the credentials valid?)")
    creds = base64.b64encode(
        "{}:{}".format(config.ZOOM_CLIENT_ID, config.ZOOM_CLIENT_SECRET).encode()).decode()
    resp = requests.post(
        config.ZOOM_OAUTH,
        params={"grant_type": "account_credentials",
                "account_id": config.ZOOM_ACCOUNT_ID},
        headers={"Authorization": "Basic " + creds},
        timeout=30,
    )
    print("    HTTP {}".format(resp.status_code))
    if resp.status_code != 200:
        b = body_of(resp)
        print("    {} {}".format(BAD, b.get("reason") or b.get("error") or resp.text[:200]))
        print("\n    -> credentials are wrong, or the app is not activated in "
              "the Zoom Marketplace.\n")
        return 1

    token = resp.json()["access_token"]
    scopes = resp.json().get("scope", "")
    print("    {}  token acquired ({} chars)".format(OK, len(token)))

    granted = sorted(s for s in scopes.split() if s)
    print("\n[2] scopes granted to this app: {}".format(len(granted)))
    need = {
        "recordings list": ("phone:read:list_call_recordings:admin",
                            "phone_recording:read:admin", "phone:read:admin"),
        "transcript download": ("phone:read:recording_transcript:admin",
                                "phone:read:recording_transcript",
                                "phone_recording:read:admin", "phone:read:admin"),
        "user list": ("phone:read:list_users:admin", "phone:read:admin"),
    }
    for label, options in need.items():
        hit = [o for o in options if o in granted]
        print("    {}  {:<22} {}".format(
            OK if hit else WARN, label, hit[0] if hit else "NONE OF: " + ", ".join(options)))
    if granted:
        print("\n    full list:")
        for s in granted:
            print("      - {}".format(s))

    auth = {"Authorization": "Bearer " + token}

    print("\n[3] GET /phone/users    (can we see the phone users?)")
    resp = requests.get(config.ZOOM_BASE + "/phone/users",
                        headers=auth, params={"page_size": 1}, timeout=60)
    print("    HTTP {}".format(resp.status_code))
    if resp.status_code == 200:
        print("    {}  total phone users: {}".format(OK, resp.json().get("total_records")))
    else:
        b = body_of(resp)
        print("    {} code={} {}".format(BAD, b.get("code"), str(b.get("message"))[:250]))

    to = date.today()
    frm = to - timedelta(days=29)
    print("\n[4] GET /phone/recordings   ({} .. {})".format(frm, to))
    resp = requests.get(config.ZOOM_BASE + "/phone/recordings", headers=auth,
                        params={"from": str(frm), "to": str(to), "page_size": 50}, timeout=90)
    print("    HTTP {}".format(resp.status_code))
    if resp.status_code != 200:
        b = body_of(resp)
        print("    {} code={} {}".format(BAD, b.get("code"), str(b.get("message"))[:250]))
        print()
        return 1

    b = resp.json()
    recs = b.get("recordings", [])
    with_t = sum(1 for x in recs if x.get("transcript_download_url"))
    with_a = sum(1 for x in recs if x.get("download_url"))
    print("    {}  total in window : {}".format(OK, b.get("total_records")))
    print("        this page       : {}".format(len(recs)))
    print("        WITH transcript : {}".format(with_t))
    print("        with audio      : {}".format(with_a))

    if not recs:
        print("\n    {} no recordings in the last 30 days -- try a wider range,"
              "\n         or recording may not be enabled yet.\n".format(WARN))
        return 0

    print("\n[5] schema actually returned (field NAMES only, no values)")
    print("    record : {}".format(json.dumps(sorted(recs[0].keys()))))
    print("    owner  : {}".format(json.dumps(sorted((recs[0].get("owner") or {}).keys()))))
    print("    site   : {}".format(json.dumps(sorted((recs[0].get("site") or {}).keys()))))

    has_email = sum(1 for x in recs if (x.get("owner") or {}).get("email"))
    print("\n    owner.email present on {}/{} records".format(has_email, len(recs)))
    if has_email < len(recs):
        print("    {} some records carry no owner email -- those cannot be mapped"
              "\n         to a division by email alone.".format(WARN))

    types = {}
    for x in recs:
        k = (x.get("owner") or {}).get("type") or "(none)"
        types[k] = types.get(k, 0) + 1
    print("    owner.type breakdown: {}".format(types))

    sites = len({(x.get("site") or {}).get("name") for x in recs})
    print("    distinct sites in sample: {}".format(sites))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
