"""One week of one Zoom Team Chat channel, and where it belongs.

Layout, matching meetings exactly:

    Chat Transcripts/{team}/Chat - 2026 - W41/
        <channel name>_<channel id>.txt

One file per channel per ISO week. A channel the client mapped to two teams
is written to BOTH team folders -- see jobs/chat.py for why that needs its
own dedupe rule rather than the one meetings use.

A chat week differs from a transcript in one important way: it is not
finished when we first see it. Messages keep arriving until the week ends,
so the file is rewritten on each run while the week is open and then left
alone for good once it seals. That is why nothing here is keyed on a
delivery id the way a recording is -- the identity of a chat file is
(channel, week), and its content changes until the week closes.
"""
from datetime import datetime

import pytz

from core import config
from core.models import short_id
from teams.team_map import UNMAPPED
from clients.drive_writer import sanitize

# Zoom hands back both an ISO-ish string and an epoch; prefer the string.
ZOOM_TS = "%Y-%m-%dT%H:%M:%SZ"

MAX_CHANNEL = 70

# Zoom channel.type -- 1 and 2 are private, 3 public, 4 an ad-hoc group DM
# named after its participants. 4 is deliberately never archived: those are
# personal conversations, and the client's selection contains none.
ADHOC = 4
TYPE_NAMES = {1: "private", 2: "private", 3: "public", 4: "ad-hoc"}


class ChatMessage(object):
    """A single message, possibly a reply inside a thread."""

    __slots__ = ("id", "sender", "sender_name", "date_time", "timestamp",
                 "text", "reply_to", "reply_ts", "raw")

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    @classmethod
    def from_api(cls, msg):
        return cls(
            id=msg.get("id"),
            sender=(msg.get("sender") or "").strip().lower(),
            sender_name=(msg.get("sender_display_name")
                         or msg.get("sender") or "").strip(),
            date_time=msg.get("date_time"),
            timestamp=msg.get("timestamp") or 0,
            text=(msg.get("message") or ""),
            # Zoom marks a threaded reply by pointing at its parent. Both
            # spellings appear across API versions, so accept either.
            reply_to=(msg.get("reply_main_message_id")
                      or msg.get("main_message_id") or None),
            reply_ts=msg.get("reply_main_message_timestamp"),
            raw=msg,
        )

    @property
    def is_reply(self):
        return bool(self.reply_to)

    @property
    def utc(self):
        try:
            return datetime.strptime(self.date_time, ZOOM_TS)
        except (TypeError, ValueError):
            pass
        if self.timestamp:
            # milliseconds since the epoch
            return datetime.utcfromtimestamp(int(self.timestamp) / 1000.0)
        return datetime(1970, 1, 1)

    @property
    def local(self):
        tz = pytz.timezone(config.TIMEZONE)
        return pytz.utc.localize(self.utc).astimezone(tz)


class ChannelWeek(object):
    """Every message in one channel for one ISO week."""

    __slots__ = ("channel_id", "channel_name", "channel_type",
                 "iso_year", "week", "messages")

    def __init__(self, channel_id, channel_name, channel_type,
                 iso_year, week, messages=None):
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.channel_type = channel_type
        self.iso_year = iso_year
        self.week = week
        self.messages = messages or []

    @property
    def iso_week(self):
        return (self.iso_year, self.week)

    @property
    def thread_count(self):
        return len({m.reply_to for m in self.messages if m.is_reply})

    @property
    def first_utc(self):
        return min((m.utc for m in self.messages), default=None)

    @property
    def last_utc(self):
        return max((m.utc for m in self.messages), default=None)


# ------------------------------------------------------------------ layout

def _segment(value):
    cleaned = sanitize(value)
    if len(cleaned) <= config.MAX_FOLDER_SEGMENT:
        return cleaned
    return cleaned[:config.MAX_FOLDER_SEGMENT].rstrip(" .-") or "truncated"


def week_folder_path(team, iso_year, week):
    """{root}/{team}/Chat - 2026 - W41 -- built without needing a channel."""
    return "/".join(_segment(p) for p in (
        config.CHAT_ROOT_FOLDER, team or UNMAPPED,
        config.CHAT_WEEK_TEMPLATE.format(
            iso_year=iso_year, week="{:02d}".format(week))))


def folder_for(cw, team):
    """The one folder this channel-week lands in for `team`.

    Unlike meetings, there is no "which team does this belong to" question
    to answer here: the client's channel map states it outright, and a
    channel may legitimately belong to two. The caller loops over them.
    """
    return week_folder_path(team, cw.iso_year, cw.week)


def filename_for(cw, extension=".txt"):
    """<channel name>_<channel id>.txt

    The id goes last, after the final underscore, because that is how
    DriveWriter.existing_items recovers it. Two channels can share a display
    name -- the client's own list has a "#Brokerage" and a "Brokerage" -- so
    the id is what actually makes the file unique.
    """
    name = sanitize(cw.channel_name or "channel")[:MAX_CHANNEL].rstrip(" .-")
    return sanitize("{}_{}{}".format(
        name or "channel", short_id(cw.channel_id), extension))


# ------------------------------------------------------------------ render

def render(cw, teams_label):
    """The channel-week as readable text, threads kept together.

    Replies are printed under the message they answer rather than in raw
    arrival order, because a thread read chronologically among unrelated
    messages is close to unreadable.
    """
    lines = [
        "Zoom Team Chat record",
        "=" * 60,
        "Channel:    {}".format(cw.channel_name or "(unnamed)"),
        "Visibility: {}".format(TYPE_NAMES.get(cw.channel_type,
                                               str(cw.channel_type))),
        "Team(s):    {}".format(teams_label),
        "ISO week:   {}-W{:02d}".format(cw.iso_year, cw.week),
        "Messages:   {} ({} thread(s))".format(
            len(cw.messages), cw.thread_count),
        "Times:      {}".format(config.TIMEZONE),
        "=" * 60,
        "",
    ]

    roots = [m for m in cw.messages if not m.is_reply]
    replies = {}
    for m in cw.messages:
        if m.is_reply:
            replies.setdefault(m.reply_to, []).append(m)

    # A reply whose parent fell outside this week would otherwise vanish.
    # Keep it, flagged, rather than dropping a message from the record.
    known = {m.id for m in roots}
    orphans = [m for pid, group in replies.items() if pid not in known
               for m in group]
    for m in orphans:
        replies.pop(m.reply_to, None)

    current_day = None
    for m in sorted(roots, key=lambda x: (x.utc, x.id or "")):
        day = m.local.strftime("%A %d %B %Y")
        if day != current_day:
            lines += ["", "-- {} --".format(day), ""]
            current_day = day
        lines.append(_line(m))
        for r in sorted(replies.get(m.id, []), key=lambda x: (x.utc, x.id or "")):
            lines.append("    | " + _line(r))

    if orphans:
        lines += ["", "-- replies to messages from an earlier week --", ""]
        for m in sorted(orphans, key=lambda x: (x.utc, x.id or "")):
            lines.append("    | " + _line(m))

    if not cw.messages:
        lines.append("(no messages this week)")

    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _line(m):
    return "[{}] {}: {}".format(
        m.local.strftime("%H:%M:%S"),
        m.sender_name or m.sender or "Unknown",
        (m.text or "").replace("\r", "").replace("\n", "\n            "))
