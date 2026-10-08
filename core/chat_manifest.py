"""Per-week manifest CSV for Team Chat records.

Same sealing rules as core/manifest.py -- written once the week has closed,
never rewritten, late arrivals into a numbered supplement -- with columns
describing a channel-week rather than a recording.

One row per channel per team. A channel mapped to two teams produces a row
in each team's manifest, which is correct: each manifest describes what is
in ITS folder, and both folders really do hold that record.
"""
import csv
import hashlib
import io

from core.manifest import (                                  # noqa: F401
    is_sealable, name_for, next_supplement,
    supplement_prefix, supplements_in, week_end)

COLUMNS = [
    "channel_id", "channel_name", "visibility", "iso_week", "team",
    "all_teams", "messages", "threads", "first_message_utc",
    "last_message_utc", "filename", "bytes", "sha256", "uploaded_utc",
]


def channel_ids(body):
    """The channel ids a manifest already accounts for.

    manifest.recording_ids reads a "recording_id" column a chat manifest
    does not have; using it would report every row as late and write the
    whole week into a supplement on every run.
    """
    if not body:
        return set()
    try:
        text = body.decode("utf-8-sig") if isinstance(body, bytes) else body
        return {(row.get("channel_id") or "").strip()
                for row in csv.DictReader(io.StringIO(text))
                if (row.get("channel_id") or "").strip()}
    except (UnicodeDecodeError, csv.Error):
        return set()


def render(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in sorted(rows, key=lambda r: (r.get("channel_name") or "").lower()):
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def row_for(cw, team, all_teams, filename, body, item, uploaded_utc):
    """One manifest line.

    `body` is None when an earlier run delivered this channel-week and
    nothing has changed: the record still belongs in the manifest, so the
    size comes from SharePoint's listing and the checksum is left blank
    rather than re-hashing a file we did not write.
    """
    from core.chat import TYPE_NAMES
    if body is None:
        size, digest = (item or {}).get("size", ""), ""
    else:
        size, digest = len(body), hashlib.sha256(body).hexdigest()
    return {
        "channel_id": cw.channel_id,
        "channel_name": cw.channel_name,
        "visibility": TYPE_NAMES.get(cw.channel_type, str(cw.channel_type)),
        "iso_week": "{}-W{:02d}".format(cw.iso_year, cw.week),
        "team": team,
        "all_teams": " + ".join(all_teams),
        "messages": len(cw.messages),
        "threads": cw.thread_count,
        "first_message_utc": cw.first_utc.isoformat() + "Z" if cw.first_utc else "",
        "last_message_utc": cw.last_utc.isoformat() + "Z" if cw.last_utc else "",
        "filename": filename,
        "bytes": size,
        "sha256": digest,
        "uploaded_utc": uploaded_utc,
    }
