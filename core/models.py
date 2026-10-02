import re
from datetime import datetime

import pytz

from core import config
from teams.team_map import UNMAPPED
from clients.drive_writer import sanitize

_TOKEN = re.compile(r"\{(\w+)\}")
ZOOM_TS = "%Y-%m-%dT%H:%M:%SZ"

# The recording id is carried in the filename so SharePoint can act as the
# ledger. Truncated for readability, but long enough that a collision across
# the whole library is vanishingly unlikely (48 bits over ~150k files).
ID_LENGTH = 12


def short_id(recording_id):
    return (recording_id or "noid")[:ID_LENGTH]


class PhoneRecording(object):
    """One Zoom Phone call recording, as returned by GET /phone/recordings."""

    __slots__ = (
        "id", "call_id", "call_log_id", "call_element_id",
        "direction", "duration", "date_time", "end_time", "recording_type",
        "caller_name", "caller_number", "callee_name", "callee_number",
        "owner", "site", "outgoing_by", "accepted_by",
        "transcript_download_url",
        "auto_delete_enable", "auto_delete_policy", "disclaimer_status", "raw",
    )

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    @classmethod
    def from_api(cls, raw):
        return cls(
            id=raw.get("id"),
            call_id=raw.get("call_id"),
            call_log_id=raw.get("call_log_id"),
            call_element_id=raw.get("call_element_id"),
            direction=raw.get("direction") or "unknown",
            duration=raw.get("duration") or 0,
            date_time=raw.get("date_time"),
            end_time=raw.get("end_time"),
            recording_type=raw.get("recording_type"),
            caller_name=raw.get("caller_name") or "",
            caller_number=raw.get("caller_number") or "",
            callee_name=raw.get("callee_name") or "",
            callee_number=raw.get("callee_number") or "",
            owner=raw.get("owner") or {},
            site=raw.get("site") or {},
            outgoing_by=raw.get("outgoing_by") or {},
            accepted_by=raw.get("accepted_by") or {},
            transcript_download_url=raw.get("transcript_download_url"),
            auto_delete_enable=raw.get("auto_delete_enable"),
            auto_delete_policy=raw.get("auto_delete_policy"),
            disclaimer_status=raw.get("disclaimer_status"),
            raw=raw,
        )

    @property
    def started(self):
        if not self.date_time:
            return datetime(1970, 1, 1)
        try:
            return datetime.strptime(self.date_time, ZOOM_TS)
        except ValueError:
            return datetime.strptime(self.date_time[:19], "%Y-%m-%dT%H:%M:%S")

    @property
    def local_started(self):
        """The call start in the configured timezone.

        Zoom returns UTC. Melbourne is 10-11 hours ahead, so a 9am Monday
        call is 11pm Sunday UTC -- a different ISO week. Week folders must
        be computed locally or every Monday morning files a week early.
        """
        naive = self.started
        try:
            tz = pytz.timezone(config.TIMEZONE)
        except Exception:
            return naive
        return pytz.utc.localize(naive).astimezone(tz)

    @property
    def iso_week(self):
        """(iso_year, week) for the LOCAL call date."""
        cal = self.local_started.isocalendar()
        return cal[0], cal[1]

    @property
    def owner_name(self):
        return self.owner.get("name") or self.owner.get("id") or "unknown-owner"

    @property
    def owner_type(self):
        return self.owner.get("type") or "unknown"

    @property
    def site_name(self):
        return self.site.get("name") or self.site.get("id") or "no-site"


def folder_for(rec, template=None, teams=None, directory=None, team=None):
    """Build the destination folder path from the configured template.

    `teams` maps a user's email to their Sensible Care team. Zoom does not
    know about teams, so without that map everything lands in _Unassigned
    rather than being guessed at.

    `team` overrides that lookup with an explicit name. week_folders() uses
    it to enumerate every folder a recording could occupy.
    """
    template = template or config.FOLDER_TEMPLATE
    started = rec.local_started
    iso_year, week = rec.iso_week
    resolved = team if team is not None else (
        teams.resolve(rec, directory) if teams else UNMAPPED)
    tokens = {
        "team": sanitize(resolved),
        "iso_year": str(iso_year),
        "week": "{:02d}".format(week),
        "owner": sanitize(rec.owner_name),
        "owner_type": sanitize(rec.owner_type),
        "direction": sanitize(rec.direction),
        "yyyy": started.strftime("%Y"),
        "mm": started.strftime("%m"),
        "dd": started.strftime("%d"),
    }
    def fill(segment):
        return _TOKEN.sub(
            lambda m: tokens.get(m.group(1), m.group(0)), segment)

    return "/".join(_segment(fill(p)) for p in template.split("/") if p)


def week_folders(rec, teams=None, template=None):
    """Every folder this recording could occupy within its ISO week.

    Deduplication has to look at all of them, not just the one the current
    mapping resolves to. When somebody changes team, an already-delivered
    recording starts resolving to a different folder; checking only that
    folder finds nothing and uploads a second copy, leaving the original
    stranded under the old team.

    Returns a single folder when the template has no {team} token, so the
    caller does not need to special-case that.
    """
    names = set(teams.teams()) if teams else set()
    names.add(UNMAPPED)
    return sorted({folder_for(rec, template, team=name) for name in names})


def _segment(value):
    """One folder level: safe characters, and short enough to leave room for
    the rest of the path. SharePoint caps the whole decoded path at 400."""
    cleaned = sanitize(value)
    if len(cleaned) <= config.MAX_FOLDER_SEGMENT:
        return cleaned
    return cleaned[:config.MAX_FOLDER_SEGMENT].rstrip(" .-") or "truncated"


EXTENSIONS = {"txt": ".txt", "vtt": ".vtt", "json": ".json"}


def filename_for(rec, extension=None):
    """Chronologically sortable, collision-proof, SharePoint-safe.

    Note the caller and callee numbers go in UNMASKED. redact() below is for
    console and log output only -- the archive itself keeps full numbers.
    """
    extension = extension or EXTENSIONS.get(config.TRANSCRIPT_FORMAT, ".txt")
    stamp = rec.local_started.strftime("%H%M%S")
    caller = rec.caller_number or rec.caller_name or "unknown"
    callee = rec.callee_number or rec.callee_name or "unknown"
    name = "{}_{}_{}_to_{}_{}{}".format(
        stamp, rec.direction, caller, callee, short_id(rec.id), extension)
    return sanitize(name)


_PHONE = re.compile(r"(\+?\d{3})(\d{3,})(\d{2})")


def redact(text):
    """Mask phone numbers before anything reaches a log file."""
    if not config.REDACT_LOGS:
        return text
    return _PHONE.sub(lambda m: m.group(1) + "*" * len(m.group(2)) + m.group(3), text)
