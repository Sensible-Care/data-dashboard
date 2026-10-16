"""Zoom cloud recordings -> meeting transcripts.

A different endpoint and a different shape from Zoom Phone, so it gets its
own client rather than more branches inside ZoomClient:

    GET /accounts/me/recordings?from=&to=     one month maximum per query
      -> meetings[] -> recording_files[]      MP4, M4A, TRANSCRIPT, CHAT, ...

Only the TRANSCRIPT files are of interest. The rest -- video, audio,
timeline, Zoom's AI summary -- are deliberately ignored: the brief is
transcripts, and an MP4 is 57 MB against a transcript's 33 KB.
"""
import io
import json
import os
from datetime import datetime, timedelta
from urllib.parse import quote

from core import config
from core.meeting import MeetingTranscript
from clients.zoom_client import HttpTransport, OfflineError, ZoomError, _refuse_if_offline

TRANSCRIPT_TYPE = "TRANSCRIPT"


class ZoomDownloadDenied(ZoomError):
    """A download URL refused the credentials we offered (401 or 403).

    Separate from ZoomError so the client can tell "wrong credential, try
    another" apart from "this genuinely failed" and fall back instead of
    giving up.
    """


class FixtureMeetings(object):
    """Canned responses so the pipeline runs with no credentials."""

    def __init__(self, directory="fixtures"):
        self.directory = directory

    def recordings_page(self, params):
        name = ("meetings_page2.json" if params.get("next_page_token")
                else "meetings_page1.json")
        with open(os.path.join(self.directory, name)) as fh:
            return json.load(fh)

    def download(self, url, token=None):
        body = (b"WEBVTT\n\n"
                b"1\n00:00:01.000 --> 00:00:04.000\n"
                b"Alice Anderson: Morning everyone, thanks for joining.\n\n"
                b"2\n00:00:04.500 --> 00:00:08.200\n"
                b"Bob Brown: No problem, happy to be here.\n")
        return io.BytesIO(body), len(body)


class MeetingsTransport(object):
    """The real thing, on top of the phone client's HTTP plumbing.

    HttpTransport already handles the token, 429s and Retry-After, and the
    account uses one set of credentials for both products. Reusing it means
    throttling is handled identically whichever pipeline hits Zoom first.
    """

    def __init__(self, http=None):
        self.http = http or HttpTransport(
            config.ZOOM_ACCOUNT_ID, config.ZOOM_CLIENT_ID, config.ZOOM_CLIENT_SECRET)

    def recordings_page(self, params):
        resp = self.http._get(config.ZOOM_BASE + "/accounts/me/recordings",
                              params=params)
        if resp.status_code != 200:
            raise ZoomError("meeting recordings failed: {} {}".format(
                resp.status_code, resp.text[:300]))
        return resp.json()

    def download(self, url, token=None):
        """Fetch a transcript, with the webhook's token if one was given.

        Two kinds of URL reach this method and they want OPPOSITE
        credentials, which is the trap that broke meeting delivery in
        October 2026:

          * a URL from GET /accounts/me/recordings wants our ordinary
            Server-to-Server OAuth bearer token;
          * a URL from a webhook event REFUSES that token with
            401 errorCode 300, and wants the short-lived `download_token`
            carried in the event body instead.

        So a token, when present, is passed as ?access_token= and the
        Authorization header is deliberately left off -- sending both makes
        Zoom reject the request.
        """
        if token:
            resp = self.http.session.get(
                url, params={"access_token": token}, stream=True, timeout=120)
        else:
            resp = self.http._get(url, stream=True)
        if resp.status_code in (401, 403):
            raise ZoomDownloadDenied(
                "transcript download refused: {} {}".format(
                    resp.status_code, resp.text[:160]))
        if resp.status_code != 200:
            raise ZoomError("transcript download failed: {} {}".format(
                resp.status_code, resp.text[:200]))
        size = resp.headers.get("Content-Length")
        return resp.raw, int(size) if size else None

    def recordings_for(self, meeting_uuid):
        """GET /meetings/{uuid}/recordings -- the authoritative file list.

        Used to mint a fresh download URL when a webhook's token has
        expired. A UUID that starts with "/" or contains "//" must be
        double-encoded or Zoom reads it as extra path segments.
        """
        ident = quote(meeting_uuid or "", safe="")
        if (meeting_uuid or "").startswith("/") or "//" in (meeting_uuid or ""):
            ident = quote(ident, safe="")
        resp = self.http._get(
            "{}/meetings/{}/recordings".format(config.ZOOM_BASE, ident))
        if resp.status_code != 200:
            raise ZoomError("meeting recordings lookup failed: {} {}".format(
                resp.status_code, resp.text[:200]))
        return resp.json()


class MeetingsClient(object):
    def __init__(self, transport=None):
        if transport is None:
            _refuse_if_offline("Zoom meetings")
        self.transport = transport or MeetingsTransport()

    @staticmethod
    def windows(date_from, date_to, days=None):
        days = days or config.MEETING_WINDOW_DAYS
        start = datetime.strptime(date_from, "%Y-%m-%d")
        end = datetime.strptime(date_to, "%Y-%m-%d")
        while start <= end:
            stop = min(start + timedelta(days=days - 1), end)
            yield start.strftime("%Y-%m-%d"), stop.strftime("%Y-%m-%d")
            start = stop + timedelta(days=1)

    def iter_transcripts(self, date_from, date_to):
        """Every meeting transcript in the range, oldest window first."""
        for win_from, win_to in self.windows(date_from, date_to):
            yield from self._window(win_from, win_to)

    def _window(self, win_from, win_to):
        """Page the whole window BEFORE yielding anything.

        Zoom's next_page_token expires after about 15 minutes, and the caller
        spends real time downloading and uploading between yields. Paging
        first keeps the token alive for only a few rapid calls -- the same
        lesson the phone client learned the hard way.
        """
        meetings, token, pages = [], None, 0
        while True:
            params = {"from": win_from, "to": win_to,
                      "page_size": config.MEETING_PAGE_SIZE}
            if token:
                params["next_page_token"] = token
            body = self.transport.recordings_page(params)
            meetings.extend(body.get("meetings") or [])
            pages += 1
            token = body.get("next_page_token")
            if not token:
                break

        found = 0
        for meeting in meetings:
            for rf in meeting.get("recording_files") or []:
                if (rf.get("file_type") or "").upper() != TRANSCRIPT_TYPE:
                    continue
                if not rf.get("download_url"):
                    continue
                found += 1
                yield MeetingTranscript.from_api(meeting, rf)
        print("  {} .. {}: {} meetings, {} transcripts".format(
            win_from, win_to, len(meetings), found))

    def download(self, url, rec=None):
        """Fetch a transcript, trying every credential that could work.

        Order matters and each step exists because the one before it can
        legitimately fail:

        1. the webhook's download_token, when the event carried one -- the
           only thing a webhook URL accepts;
        2. our OAuth bearer, which is what a batch URL accepts;
        3. a freshly minted URL from the recordings API, for when the
           webhook token has expired. Zoom's download tokens last between
           one and twenty-four hours, so any event that sat in the queue
           overnight -- or was retried the next morning -- arrives here with
           a token that is no longer any good. Without this step those
           transcripts could never be delivered by the webhook path at all.
        """
        token = getattr(rec, "download_token", None) if rec is not None else None
        if token:
            try:
                return self.transport.download(url, token=token)
            except ZoomDownloadDenied as exc:
                print("  download token rejected ({}); falling back".format(
                    str(exc)[:80]))

        try:
            return self.transport.download(url)
        except ZoomDownloadDenied:
            fresh = self.fresh_download_url(rec)
            if not fresh:
                raise
            print("  re-resolved the download URL from the recordings API")
            return self.transport.download(fresh)

    def fresh_download_url(self, rec):
        """A current download URL for `rec`, straight from the API.

        Returns "" when it cannot be found, so the caller can raise the
        original error rather than a confusing one about lookup.
        """
        if rec is None or not getattr(rec, "meeting_uuid", None):
            return ""
        try:
            body = self.transport.recordings_for(rec.meeting_uuid)
        except (ZoomError, OSError):
            return ""
        for f in body.get("recording_files") or []:
            if (f.get("file_type") or "").upper() != TRANSCRIPT_TYPE:
                continue
            # Match the exact file where we can; a meeting recorded in parts
            # has several transcripts and delivering the wrong one would be
            # worse than failing.
            if rec.id and f.get("id") and f["id"] != rec.id:
                continue
            if f.get("download_url"):
                return f["download_url"]
        return ""
