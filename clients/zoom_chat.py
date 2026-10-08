"""Zoom Team Chat -> channel-weeks.

Two awkward facts about Zoom's chat API shape the whole of this module.

1. There is no account-wide "list every channel" endpoint. Channels are
   listed PER USER, and only the channels that user is a member of. The
   app's own account is in 82 of this account's 636 channels, and only 5 of
   the 54 the client asked for -- so the only way to reach them all is to
   walk every active user and union the results.

2. Messages are likewise read in the context of a user who is a member:
   GET /chat/users/{userId}/messages?to_channel=...&date=...
   Asking as a non-member returns 404, not an empty list.

So resolve() builds {channel -> (id, a member who can read it)} once per
run, and everything downstream uses that reader.

Threads: the list endpoint returns replies alongside their parents as long
as exclude_child_message is false, which is the default. Each reply carries
reply_main_message_id pointing at its parent. We ask explicitly rather than
relying on the default staying that way.
"""
import json
import os
import re
from datetime import datetime, timedelta

from core import config
from core.chat import ADHOC, ChannelWeek, ChatMessage
from clients.zoom_client import HttpTransport, ZoomError, _refuse_if_offline


def _norm(value):
    return " ".join((value or "").strip().lower().split())


class FixtureChat(object):
    """Canned chat so the pipeline runs with no credentials."""

    def __init__(self, directory="fixtures"):
        self.directory = directory
        with open(os.path.join(directory, "chat_channels.json")) as fh:
            self._doc = json.load(fh)

    def users(self):
        return self._doc["users"]

    def channels(self, user_id):
        return self._doc["channels_by_user"].get(user_id, [])

    def messages(self, user_id, channel_id, date):
        key = "{}|{}".format(channel_id, date)
        return self._doc["messages"].get(key, [])


class ChatTransport(object):
    """The real thing, on the phone client's token and 429 handling."""

    def __init__(self, http=None):
        self.http = http or HttpTransport(
            config.ZOOM_ACCOUNT_ID, config.ZOOM_CLIENT_ID,
            config.ZOOM_CLIENT_SECRET)

    def _page(self, url, params, key):
        out, token = [], None
        while True:
            p = dict(params)
            if token:
                p["next_page_token"] = token
            resp = self.http._get(url, params=p)
            if resp.status_code == 404:
                return []
            if resp.status_code != 200:
                raise ZoomError("{} failed: {} {}".format(
                    url.rsplit("/", 1)[-1], resp.status_code, resp.text[:200]))
            body = resp.json()
            out.extend(body.get(key) or [])
            token = body.get("next_page_token")
            if not token:
                break
        return out

    def users(self):
        return self._page(config.ZOOM_BASE + "/users",
                          {"page_size": 300, "status": "active"}, "users")

    def channels(self, user_id):
        return self._page(
            config.ZOOM_BASE + "/chat/users/{}/channels".format(user_id),
            {"page_size": 50}, "channels")

    def messages(self, user_id, channel_id, date):
        return self._page(
            config.ZOOM_BASE + "/chat/users/{}/messages".format(user_id),
            {"to_channel": channel_id, "date": date,
             "page_size": config.CHAT_PAGE_SIZE,
             # Replies must come back with their parents, and an edited
             # message should show its current text rather than disappear.
             "exclude_child_message": "false",
             "include_deleted_and_edited_message": "true"},
            "messages")


class ChatClient(object):
    def __init__(self, transport=None):
        if transport is None:
            _refuse_if_offline("Zoom chat")
        self.transport = transport or ChatTransport()
        self._directory = None

    # ------------------------------------------------------------ resolve
    def directory(self, refresh=False):
        """{normalised channel name: [{id, name, type, reader}]}.

        `reader` is a user id that is a member of the channel, and so can
        read its messages. Built by walking every active user once.
        """
        if self._directory is not None and not refresh:
            return self._directory

        found = {}
        users = self.transport.users()
        for user in users:
            uid = user.get("id")
            if not uid:
                continue
            for ch in self.transport.channels(uid) or []:
                cid = ch.get("id")
                if not cid or cid in found:
                    continue
                found[cid] = {
                    "id": cid,
                    "name": (ch.get("name") or "").strip(),
                    "type": ch.get("type"),
                    "reader": uid,
                }

        by_name = {}
        for ch in found.values():
            if ch["name"]:
                by_name.setdefault(_norm(ch["name"]), []).append(ch)
        self._directory = by_name
        print("  zoom chat: {} users, {} channels visible".format(
            len(users), len(found)))
        return by_name

    def resolve(self, wanted_names):
        """Match the client's channel names against Zoom.

        Returns (resolved, problems). Matching is exact on the normalised
        name; an exact match is required because this account really does
        contain both "#Brokerage" (a channel) and "Brokerage" (an ad-hoc
        group DM), and a fuzzy match would archive the wrong one.
        """
        directory = self.directory()
        resolved, problems = [], []
        for name in wanted_names:
            hits = directory.get(_norm(name)) or []
            if not hits:
                problems.append("{}: no such channel is visible to any "
                                "active user".format(name))
                continue
            real = [h for h in hits if h["type"] != ADHOC]
            if len(hits) > 1 and len(real) == 1:
                # "#Brokerage" vs the ad-hoc "Brokerage": prefer the channel.
                hits = real
            if len(hits) > 1:
                problems.append("{}: {} channels share this name -- skipped, "
                                "it cannot be resolved safely".format(
                                    name, len(hits)))
                continue
            ch = hits[0]
            if ch["type"] == ADHOC:
                problems.append("{}: this is an ad-hoc group conversation, "
                                "not a channel -- skipped".format(name))
                continue
            resolved.append(dict(ch, requested_as=name))
        return resolved, problems

    # ----------------------------------------------------------- messages
    @staticmethod
    def days(date_from, date_to):
        start = datetime.strptime(date_from, "%Y-%m-%d").date()
        end = datetime.strptime(date_to, "%Y-%m-%d").date()
        while start <= end:
            yield start
            start += timedelta(days=1)

    def messages_for(self, channel, day):
        """Every message in one channel on one UTC day."""
        raw = self.transport.messages(
            channel["reader"], channel["id"], day.strftime("%Y-%m-%d"))
        return [ChatMessage.from_api(m) for m in raw or []]

    def channel_weeks(self, channel, days):
        """Messages for a channel on `days`, grouped into ISO weeks.

        Takes an explicit list of UTC days rather than a range, because the
        caller skips weeks that are already sealed and must not pay for
        them. Fetching a range and filtering afterwards would make a re-run
        of a year cost a year of API calls every night.

        Zoom indexes chat by UTC day; the archive is organised by Melbourne
        week. A message at 9am Monday in Melbourne is 23:00 Sunday UTC, so
        the day we ASK for and the week we FILE under are not the same
        thing. Grouping on the message's own local timestamp -- not on the
        day we requested -- is what keeps that correct.
        """
        buckets = {}
        for day in days:
            for msg in self.messages_for(channel, day):
                iso = msg.local.isocalendar()
                buckets.setdefault((iso[0], iso[1]), []).append(msg)

        out = []
        for (iso_year, week), msgs in sorted(buckets.items()):
            # One UTC day can be fetched twice at a week boundary; the same
            # message must not be counted twice.
            unique = {m.id: m for m in msgs if m.id}
            unique.update({id(m): m for m in msgs if not m.id})
            out.append(ChannelWeek(
                channel_id=channel["id"], channel_name=channel["name"],
                channel_type=channel["type"], iso_year=iso_year, week=week,
                messages=sorted(unique.values(),
                                key=lambda m: (m.utc, m.id or ""))))
        return out
