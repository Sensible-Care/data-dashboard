import os
import time

import requests

from core import config
class GraphError(Exception):
    def __init__(self, status, code, message):
        super().__init__("{} {}: {}".format(status, code, message))
        self.status = status
        self.code = code
        self.message = message


class StaticTokenAuth:
    """Dev auth: a token pasted out of Graph Explorer. Expires in ~1 hour."""

    def token(self):
        tok = os.environ.get("GRAPH_DEV_TOKEN")
        if not tok:
            raise RuntimeError("GRAPH_DEV_TOKEN is not set in .env")
        return tok.strip()


class AppOnlyAuth:
    """Prod auth: client_credentials, cached until five minutes before expiry."""

    def __init__(self, tenant_id, client_id, client_secret):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None
        self._expires_at = 0

    def token(self):
        if self._token and time.time() < self._expires_at - 300:
            return self._token
        url = "https://login.microsoftonline.com/{}/oauth2/v2.0/token".format(self.tenant_id)
        resp = requests.post(url, data={
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default",
        }, timeout=30)
        if resp.status_code != 200:
            # Entra's error_description is the only useful part -- it names
            # the actual fault (bad secret, wrong tenant, consent missing).
            try:
                err = resp.json()
                detail = err.get("error_description", "").split("\r\n")[0]
                code = err.get("error", "unknown")
            except ValueError:
                detail, code = resp.text[:200], "unknown"
            raise GraphError(resp.status_code, code,
                             "token request failed: {}".format(detail))
        body = resp.json()
        self._token = body["access_token"]
        self._expires_at = time.time() + body.get("expires_in", 3600)
        return self._token


def make_auth():
    """App-only if the client's Entra app is configured, else the dev token.

    Nothing else in the pipeline needs to know which one it got, so switching
    from development to production is a matter of filling in .env.
    """
    from clients.zoom_client import _refuse_if_offline
    _refuse_if_offline("Microsoft Graph")
    if config.SP_TENANT_ID and config.SP_CLIENT_ID and config.SP_CLIENT_SECRET:
        return AppOnlyAuth(config.SP_TENANT_ID, config.SP_CLIENT_ID,
                           config.SP_CLIENT_SECRET)
    return StaticTokenAuth()


class GraphClient:
    def __init__(self, auth, max_retries=5):
        self.auth = auth
        self.max_retries = max_retries
        self.session = requests.Session()

    def request(self, method, url, **kwargs):
        if url.startswith("/"):
            url = config.GRAPH_BASE + url

        headers = kwargs.pop("headers", {}) or {}
        headers.setdefault("User-Agent", config.USER_AGENT)
        # Preauthenticated upload-session URLs must NOT carry an auth header.
        if "://" not in url or url.startswith(config.GRAPH_BASE):
            headers["Authorization"] = "Bearer " + self.auth.token()

        # A streamed body is consumed as it is sent, so it cannot be replayed.
        # Retrying one would silently upload a truncated file.
        body = kwargs.get("data")
        replayable = body is None or isinstance(body, (bytes, bytearray, str))

        for attempt in range(self.max_retries):
            resp = self.session.request(method, url, headers=headers, timeout=120, **kwargs)

            if resp.status_code in (429, 503):
                if not replayable:
                    raise GraphError(resp.status_code, "throttledMidStream",
                                     "cannot safely retry a streamed upload; "
                                     "the transfer must restart from the source")
                delay = self._retry_delay(resp, attempt)
                print("  throttled ({}), sleeping {}s".format(resp.status_code, delay))
                time.sleep(delay)
                continue

            return resp

        raise GraphError(resp.status_code, "tooManyRetries", "gave up after retries")

    def json(self, method, url, **kwargs):
        """request + raise + decode, for the many small JSON calls.

        Uploads stay on request() so the streamed-body handling above is not
        bypassed; this is only for metadata traffic.
        """
        resp = self.request(method, url, **kwargs)
        self.raise_for_graph(resp)
        return resp.json() if resp.content else {}

    @staticmethod
    def _retry_delay(resp, attempt):
        raw = resp.headers.get("Retry-After")
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
        return min(2 ** attempt, 60)

    @staticmethod
    def raise_for_graph(resp):
        if resp.status_code < 400:
            return
        try:
            err = resp.json().get("error", {})
        except ValueError:
            err = {}
        raise GraphError(resp.status_code, err.get("code", "unknown"), err.get("message", resp.text[:200]))
