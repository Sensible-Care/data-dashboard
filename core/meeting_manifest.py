"""Per-week manifest CSV for meeting transcripts.

Same rules as core/manifest.py -- written once after the week has closed,
never rewritten, late arrivals go into a numbered supplement -- but a
different set of columns, because a meeting has a host and a topic where a
call has a direction, a caller and a callee.

The manifest lives in the WEEK folder, beside the day subfolders:

    Meeting Transcripts/Rostering/Meeting - 2026 - W39/
        2026-09-23/...
        2026-09-24/...
        _manifest_2026-W39_Rostering.csv
"""
import csv
import hashlib
import io

# The naming, supplement numbering and sealing rules are identical, so they
# are reused rather than reimplemented -- one definition of "has this week
# closed" is enough.
from core.manifest import (                                  # noqa: F401
    is_sealable, name_for, next_supplement,
    supplement_prefix, supplements_in, week_end)

COLUMNS = [
    "transcript_id", "meeting_id", "meeting_uuid",
    "started_local", "started_utc", "iso_week", "day", "team",
    "host_email", "topic", "duration_min",
    "filename", "bytes", "sha256", "uploaded_utc",
]


def transcript_ids(body):
    """The transcript ids a manifest CSV already accounts for.

    manifest.recording_ids reads a "recording_id" column, which a meeting
    manifest does not have -- it would return an empty set for every file
    and so write the whole week into a supplement on every run.

    Unreadable or empty content yields an empty set. The caller then treats
    every row as new, which over-reports into a supplement rather than
    dropping a delivery from the record: the safer direction to fail.
    """
    if not body:
        return set()
    try:
        text = body.decode("utf-8-sig") if isinstance(body, bytes) else body
        return {(row.get("transcript_id") or "").strip()
                for row in csv.DictReader(io.StringIO(text))
                if (row.get("transcript_id") or "").strip()}
    except (UnicodeDecodeError, csv.Error):
        return set()


def render(rows):
    """Rows (dicts using COLUMNS) -> CSV bytes."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in sorted(rows, key=lambda r: r.get("started_utc") or ""):
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def row_for(rec, team, filename, body, item, uploaded_utc):
    """One manifest line.

    `body` is the bytes just uploaded. It is None for a transcript an earlier
    run already delivered: the meeting still belongs in the week's manifest,
    so the size comes from SharePoint's own listing and the checksum is left
    blank rather than downloading the file again to hash it.
    """
    iso_year, week = rec.iso_week
    if body is None:
        size, digest = (item or {}).get("size", ""), ""
    else:
        size, digest = len(body), hashlib.sha256(body).hexdigest()
    return {
        "transcript_id": rec.id,
        "meeting_id": rec.meeting_id,
        "meeting_uuid": rec.meeting_uuid,
        "started_local": rec.local_started.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "started_utc": rec.start_time,
        "iso_week": "{}-W{:02d}".format(iso_year, week),
        "day": rec.local_started.strftime("%Y-%m-%d"),
        "team": team,
        "host_email": rec.host_email,
        "topic": rec.topic,
        "duration_min": rec.duration,
        "filename": filename,
        "bytes": size,
        "sha256": digest,
        "uploaded_utc": uploaded_utc,
    }
