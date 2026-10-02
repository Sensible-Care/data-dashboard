"""Per-week manifest CSV.

One row per transcript delivered, written ONCE after the week has closed
and late transcripts have landed. Writing once rather than daily means the
file is never rewritten -- so a Purview retention label that marks items as
records cannot block it.

Anything arriving after a manifest is sealed goes into a supplement file
beside it rather than editing the sealed original.
"""
import csv
import io
from datetime import date, timedelta

from core import config
COLUMNS = [
    "recording_id", "call_id", "call_element_id",
    "started_local", "started_utc", "iso_week", "team",
    "direction", "duration_sec", "caller", "callee",
    "resolved_via", "filename", "bytes", "sha256", "uploaded_utc",
]


def name_for(iso_year, week, team, supplement=None):
    base = "_manifest_{}-W{:02d}_{}".format(iso_year, week, team)
    if supplement:
        return "{}_supplement_{}.csv".format(base, supplement)
    return base + ".csv"


def supplement_prefix(iso_year, week, team):
    return "_manifest_{}-W{:02d}_{}_supplement_".format(iso_year, week, team)


def supplements_in(iso_year, week, team, present):
    """The supplement files already beside this week's sealed manifest."""
    prefix = supplement_prefix(iso_year, week, team)
    return sorted(n for n in present if n.startswith(prefix))


def next_supplement(iso_year, week, team, present):
    """The next free supplement name.

    Numbered rather than dated. Two late batches on the same day would give
    the same date-stamped name, and the second upload would silently replace
    the first -- losing rows, and failing outright if a retention label has
    made the file a record. A sequence number never collides.
    """
    used = set(supplements_in(iso_year, week, team, present))
    for n in range(1, 100):
        candidate = name_for(iso_year, week, team, "{:02d}".format(n))
        if candidate not in used:
            return candidate
    raise RuntimeError(
        "99 supplements for {}-W{:02d} {} -- something is wrong upstream"
        .format(iso_year, week, team))


def recording_ids(body):
    """The recording ids a manifest CSV already accounts for.

    Unreadable or empty content yields an empty set. The caller then treats
    every row as new, which over-reports into a supplement rather than
    dropping a delivery from the record -- the safer direction to fail.
    """
    if not body:
        return set()
    try:
        text = body.decode("utf-8-sig") if isinstance(body, bytes) else body
        return {(row.get("recording_id") or "").strip()
                for row in csv.DictReader(io.StringIO(text))
                if (row.get("recording_id") or "").strip()}
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


def week_end(iso_year, week):
    """The Sunday that closes this ISO week."""
    return date.fromisocalendar(iso_year, week, 7) if hasattr(date, "fromisocalendar") \
        else _fromisocalendar(iso_year, week, 7)


def _fromisocalendar(iso_year, week, weekday):
    """date.fromisocalendar backport -- it arrived in Python 3.8 but is
    missing on some builds."""
    jan4 = date(iso_year, 1, 4)
    week1_monday = jan4 - timedelta(days=jan4.isoweekday() - 1)
    return week1_monday + timedelta(weeks=week - 1, days=weekday - 1)


def is_sealable(iso_year, week, today=None):
    """Has this week closed long enough for late transcripts to arrive?"""
    today = today or date.today()
    return today >= week_end(iso_year, week) + timedelta(
        days=config.MANIFEST_SEAL_DAYS)


def row_for(rec, team, filename, body, item, resolved_via, uploaded_utc):
    """One manifest line.

    `body` is the bytes we just uploaded. It is None for a transcript an
    earlier run already delivered -- the call still belongs in the week's
    manifest, so the size comes from SharePoint's own listing and the
    checksum is left blank rather than downloading the file again to hash it.
    """
    import hashlib
    iso_year, week = rec.iso_week
    if body is None:
        size = (item or {}).get("size", "")
        digest = ""
    else:
        size = len(body)
        digest = hashlib.sha256(body).hexdigest()
    return {
        "recording_id": rec.id,
        "call_id": rec.call_id,
        "call_element_id": rec.call_element_id,
        "started_local": rec.local_started.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "started_utc": rec.date_time,
        "iso_week": "{}-W{:02d}".format(iso_year, week),
        "team": team,
        "direction": rec.direction,
        "duration_sec": rec.duration,
        "caller": rec.caller_number or rec.caller_name,
        "callee": rec.callee_number or rec.callee_name,
        "resolved_via": resolved_via,
        "filename": filename,
        "bytes": size,
        "sha256": digest,
        "uploaded_utc": uploaded_utc,
    }
