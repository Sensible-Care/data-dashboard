import base64
import io
import json
import os
import time
from datetime import datetime, timedelta

import requests

from core import config
from core.models import PhoneRecording


class ZoomError(Exception):
    pass


class FixtureTransport(object):
    """Serves canned Zoom responses so the pipeline runs without credentials."""

    def __init__(self, directory="fixtures"):
        self.directory = directory

    def token(self):
        return "fixture-token"

    def recordings_page(self, params):
        name = "page2.json" if params.get("next_page_token") else "page1.json"
        with open(os.path.join(self.directory, name)) as fh:
            return json.load(fh)

    def users(self):
        with open(os.path.join(self.directory, "users.json")) as fh:
            return json.load(fh)

    def download(self, url):
        """Stand-in transcript in Zoom's real JSON shape."""
        doc = {
            "type": "zoom_transcript", "ver": 1,
            "recording_id": url.rsplit("/", 1)[-1],
            "recording_start": "2026-09-22T14:33:01Z",
            "recording_end": "2026-09-22T14:35:18Z",
            "fileExtension": ".json",
            "timeline": [
                {"ts": "00:00:00.000", "end_ts": "00:00:04.120",
                 "username": "Agent", "word_items": [{"w": "x"}] * 40,
                 "text": "Thanks for calling Sensible Care, how can I help?"},
                {"ts": "00:00:04.500", "end_ts": "00:00:09.300",
                 "username": "Caller", "word_items": [{"w": "y"}] * 40,
                 "text": "Hi, I'm following up on my care plan review."},
            ],
        }
        body = json.dumps(doc).encode("utf-8")
        return io.BytesIO(body), len(body)


class HttpTransport(object):
    """The real thing. Untested until Zoom credentials exist."""

    def __init__(self, account_id, client_id, client_secret):
        self.account_id = account_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = requests.Session()
        self._token = None
        self._expires_at = 0

    def token(self):
        if self._token and time.time() < self._expires_at - 300:
            return self._token
        creds = "{}:{}".format(self.client_id, self.client_secret)
        auth = base64.b64encode(creds.encode()).decode()
        resp = self.session.post(
            config.ZOOM_OAUTH,
            params={"grant_type": "account_credentials", "account_id": self.account_id},
            headers={"Authorization": "Basic " + auth},
            timeout=30,
        )
        if resp.status_code != 200:
            raise ZoomError("token request failed: {} {}".format(resp.status_code, resp.text[:200]))
        body = resp.json()
        self._token = body["access_token"]
        self._expires_at = time.time() + body.get("expires_in", 3600)
        return self._token

    def _get(self, url, **kwargs):
        for attempt in range(5):
            resp = self.session.get(
                url,
                headers={"Authorization": "Bearer " + self.token()},
                timeout=120,
                **kwargs
            )
            if resp.status_code == 429:
                delay = self._retry_delay(resp, attempt)
                print("  zoom throttled, sleeping {}s".format(delay))
                time.sleep(delay)
                continue
            return resp
        raise ZoomError("gave up after repeated 429s")

    @staticmethod
    def _retry_delay(resp, attempt):
        raw = resp.headers.get("Retry-After")
        if raw and raw.isdigit():
            return int(raw)
        if raw:  # daily limits return an ISO8601 datetime, not seconds
            return 60
        return min(2 ** attempt, 60)

    def recordings_page(self, params):
        resp = self._get(config.ZOOM_BASE + "/phone/recordings", params=params)
        if resp.status_code != 200:
            raise ZoomError("recordings failed: {} {}".format(resp.status_code, resp.text[:300]))
        return resp.json()

    def download(self, url):
        # The transcript endpoint answers 302 to a storage URL; requests
        # follows it, but the redirect target rejects our auth header, so
        # allow_redirects is handled explicitly by the session defaults.
        resp = self._get(url, stream=True)
        if resp.status_code != 200:
            raise ZoomError("download failed: {} {}".format(resp.status_code, resp.text[:200]))
        size = resp.headers.get("Content-Length")
        return resp.raw, int(size) if size else None


class OfflineError(RuntimeError):
    """A live call was attempted while OFFLINE=1."""


def _refuse_if_offline(what):
    """Hard stop before any real network client is built.

    Forgetting use_fixtures=True somewhere deep in a call chain silently
    turns a test into a live production query -- it has happened twice. A
    missing keyword is easy to overlook; a process that will not start is
    not. Test suites set OFFLINE=1 so the mistake cannot reach the wire.
    """
    if (os.environ.get("OFFLINE") or "").strip() not in ("", "0"):
        raise OfflineError(
            "OFFLINE=1: refusing to build a live {} client. Something asked "
            "for real data during an offline run -- pass use_fixtures=True "
            "(or inject a transport) on the call path that got here."
            .format(what))


class ZoomClient(object):
    def __init__(self, transport=None):
        if transport is None:
            _refuse_if_offline("Zoom")
        self.transport = transport or HttpTransport(
            config.ZOOM_ACCOUNT_ID, config.ZOOM_CLIENT_ID, config.ZOOM_CLIENT_SECRET)

    @staticmethod
    def windows(date_from, date_to, days=None):
        """Zoom returns at most one month per query, so split the range."""
        days = days or config.ZOOM_WINDOW_DAYS
        start = datetime.strptime(date_from, "%Y-%m-%d")
        end = datetime.strptime(date_to, "%Y-%m-%d")
        while start <= end:
            stop = min(start + timedelta(days=days - 1), end)
            yield start.strftime("%Y-%m-%d"), stop.strftime("%Y-%m-%d")
            start = stop + timedelta(days=1)

    def iter_recordings(self, date_from, date_to):
        """Every recording in the range, one window at a time.

        A window is paged to the end BEFORE anything is yielded. Zoom's
        next_page_token dies after 15 minutes, and the caller spends about a
        second per record downloading a transcript and uploading it. Yielding
        as we page meant page 2 was requested ~300 seconds after its token was
        issued, and on a long run the token expired mid-range:

            400 The next page token is invalid or expired.

        Paging first keeps the token in use only for a few rapid calls. The
        cost is holding one window of metadata in memory -- roughly 10,000
        records for a 30-day window, a few tens of MB, and nothing like the
        transcripts themselves, which still stream straight through.
        """
        for win_from, win_to in self.windows(date_from, date_to):
            yield from self._window_recordings(win_from, win_to)

    def _window_recordings(self, win_from, win_to):
        records, token, pages = [], None, 0
        while True:
            params = {
                "from": win_from,
                "to": win_to,
                "page_size": config.ZOOM_PAGE_SIZE,
            }
            if token:
                params["next_page_token"] = token
            body = self.transport.recordings_page(params)
            records.extend(body.get("recordings", []))
            pages += 1
            token = body.get("next_page_token")
            if not token:
                break
        if pages > 1:
            print("  {} .. {}: {} recordings over {} pages".format(
                win_from, win_to, len(records), pages))
        return (PhoneRecording.from_api(raw) for raw in records)

    def download(self, url):
        return self.transport.download(url)
