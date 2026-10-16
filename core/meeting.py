"""One Zoom meeting transcript, and where it belongs in SharePoint.

Deliberately separate from core/models.py. A phone call and a meeting share
almost nothing: a call has a direction, a caller and a callee number, and
exactly one owner; a meeting has a host, a topic, and any number of
participants. Forcing both through one model would mean a PhoneRecording
full of fields that are meaningless half the time.

Layout, as specified by the client:

    Meeting Transcripts/{team}/Meeting - 2026 - W35/
        HHMMSS_topic_host_<id>.txt

The team is the HOST's team. A meeting has many participants spread across
teams, so there has to be one rule, and the host is the only party Zoom
guarantees to name.
"""
from datetime import datetime

import pytz

from core import config
from core.models import ID_LENGTH, short_id
from teams.team_map import UNMAPPED
from clients.drive_writer import sanitize

ZOOM_TS = "%Y-%m-%dT%H:%M:%SZ"

# Keep the topic recognisable without letting it eat the 400-character path
# budget. SharePoint counts the whole decoded path, not just the filename.
MAX_TOPIC = 60


class MeetingTranscript(object):
    """A meeting's transcript file, paired with the meeting it came from."""

    __slots__ = ("id", "meeting_id", "meeting_uuid", "host_id", "host_email",
                 "topic", "start_time", "duration", "file_size",
                 "download_url", "file_extension", "raw", "download_token")

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    @classmethod
    def from_api(cls, meeting, recording_file):
        """Build from one entry of `meetings[]` plus its TRANSCRIPT file.

        The id is the FILE's id, not the meeting's. A meeting can be recorded
        more than once, and the file id is what makes each transcript unique
        in the destination folder.
        """
        return cls(
            id=recording_file.get("id"),
            meeting_id=meeting.get("id"),
            meeting_uuid=meeting.get("uuid"),
            host_id=meeting.get("host_id"),
            host_email=(meeting.get("host_email") or "").strip().lower(),
            topic=(meeting.get("topic") or "").strip(),
            # The file's own start is more accurate than the meeting's when a
            # session was recorded in parts; fall back to the meeting's.
            start_time=(recording_file.get("recording_start")
                        or meeting.get("start_time")),
            duration=meeting.get("duration") or 0,
            file_size=recording_file.get("file_size") or 0,
            download_url=recording_file.get("download_url"),
            file_extension=(recording_file.get("file_extension") or "VTT"),
            raw=recording_file,
            # Only ever set for a transcript that arrived by webhook. Zoom's
            # webhook download URLs refuse an OAuth token and require this
            # short-lived one instead; the batch endpoint is the opposite.
            # See MeetingsClient.download.
            download_token=recording_file.get("download_token"),
        )

    @property
    def started_utc(self):
        try:
            return datetime.strptime(self.start_time, ZOOM_TS)
        except (TypeError, ValueError):
            return datetime(1970, 1, 1)

    @property
    def local_started(self):
        """Melbourne local time. Week folders and filenames both use this."""
        tz = pytz.timezone(config.TIMEZONE)
        return pytz.utc.localize(self.started_utc).astimezone(tz)

    @property
    def iso_week(self):
        iso = self.local_started.isocalendar()
        return (iso[0], iso[1])

    @property
    def host_name(self):
        """The local part of the host's address -- a readable short name."""
        return (self.host_email or "").split("@")[0] or "unknown-host"


def team_for(rec, teams):
    """The host's team on the day of the meeting, or _Unassigned."""
    if not teams or not rec.host_email:
        return UNMAPPED
    return teams.for_email(rec.host_email, rec.local_started.date()) or UNMAPPED


def folder_for(rec, teams=None, team=None):
    """{root}/{team}/Meeting - 2026 - W35

    Transcripts sit directly in the week folder, exactly as the phone
    pipeline does it. The week is computed in Melbourne local time, so a
    meeting at 9am Monday belongs to that week rather than the previous one
    as its UTC timestamp would suggest.
    """
    iso_year, week = rec.iso_week
    resolved = team if team is not None else team_for(rec, teams)
    week_folder = config.MEETING_WEEK_TEMPLATE.format(
        iso_year=iso_year, week="{:02d}".format(week))
    return "/".join(_segment(p) for p in (
        config.MEETING_ROOT_FOLDER, resolved, week_folder))


def week_folder_path(team, iso_year, week):
    """The week container for a team, built without needing a recording.

    The manifest writer works from collected rows rather than from the
    transcripts themselves, so it has a team and a week but no object to ask.
    """
    return "/".join(_segment(p) for p in (
        config.MEETING_ROOT_FOLDER, team,
        config.MEETING_WEEK_TEMPLATE.format(
            iso_year=iso_year, week="{:02d}".format(week))))


def week_folder_for(rec, teams=None, team=None):
    """The folder a transcript lands in. Manifests live here too."""
    return folder_for(rec, teams=teams, team=team)


def week_folders(rec, teams=None):
    """Every folder this transcript could occupy inside its ISO week.

    Same reasoning as the phone pipeline: when somebody changes team, an
    already-delivered transcript starts resolving to a different folder.
    Checking only the new one finds nothing and uploads a second copy,
    leaving the original stranded under the old team.
    """
    names = set(teams.teams()) if teams else set()
    names.add(UNMAPPED)
    return sorted({folder_for(rec, team=name) for name in names})


# Kept so the webhook and job can call either name during the layout change.
day_folders = week_folders


def _segment(value):
    cleaned = sanitize(value)
    if len(cleaned) <= config.MAX_FOLDER_SEGMENT:
        return cleaned
    return cleaned[:config.MAX_FOLDER_SEGMENT].rstrip(" .-") or "truncated"


def filename_for(rec, extension=".txt"):
    """HHMMSS_topic_host_<id>.<ext> -- sortable and collision-proof.

    The id goes last and after the final underscore because that is how
    DriveWriter.existing_items recovers it when deciding what has already
    been delivered. Nothing else in the name may end with an underscore.
    """
    stamp = rec.local_started.strftime("%H%M%S")
    topic = sanitize(rec.topic or "meeting")[:MAX_TOPIC].rstrip(" .-")
    host = sanitize(rec.host_name)
    name = "{}_{}_{}_{}{}".format(
        stamp, topic or "meeting", host, short_id(rec.id), extension)
    return sanitize(name)
