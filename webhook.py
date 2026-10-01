"""Zoom tells us the moment a transcript is ready, instead of us asking daily.

Zoom gives a webhook three seconds to respond, and downloading a transcript
and uploading it to SharePoint takes longer than that. Miss the deadline and
Zoom treats the delivery as failed, retries at 5, 20 and 60 minutes, then
gives up for good.

So the HTTP handler does the least possible work: check the signature, write
the event to a queue on disk, answer 200. A worker thread drains the queue
afterwards at its own pace. The queue is a directory of small JSON files --
no database, no broker, nothing new to run. It lives in STATE_DIR, so a
container restart resumes instead of dropping whatever was in flight.

Each file is named after its recording id, so the same event arriving twice
(Zoom retries, or an overlap with the nightly run) is one file, not two.

    python3 webhook.py                       # serve on PORT (default 8080)
    python3 webhook.py --drain               # process the queue once, exit
    python3 webhook.py --status              # what is waiting
    python3 webhook.py --self-test           # sign and verify, offline

Two endpoints:
    GET  /healthz   liveness, for the platform's probe
    POST /          Zoom events

Zoom re-runs the URL validation challenge roughly every 72 hours, so the
endpoint must keep answering it for as long as the subscription exists --
it is not just a one-off during setup.
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import state

TRANSCRIPT_EVENT = "phone.recording_transcript_completed"
VALIDATION_EVENT = "endpoint.url_validation"

QUEUE_DIR = state.path("queue")
FAILED_DIR = os.path.join(QUEUE_DIR, "failed")

# A Zoom event is a few KB. Anything far larger is not Zoom.
MAX_BODY = 1024 * 1024
# Reject a replayed request. Zoom signs with a timestamp; five minutes is
# the window Zoom's own examples use.
MAX_SKEW_SECONDS = 300
# After this many failed deliveries the event is set aside rather than
# retried forever. reconcile.py is the backstop that notices the gap.
MAX_ATTEMPTS = 5


# ----------------------------------------------------------------- signing
def sign(secret, timestamp, body):
    """The x-zm-signature value Zoom sends for this request."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    message = "v0:{}:{}".format(timestamp, body)
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return "v0=" + digest


def crc_token(secret, plain_token):
    """The answer to Zoom's URL validation challenge."""
    return hmac.new(secret.encode("utf-8"), plain_token.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def verify(headers, body, secret, now=None):
    """(ok, reason). Never raises -- a malformed request is just rejected."""
    if not secret:
        return False, "ZOOM_SECRET_TOKEN is not set"
    timestamp = headers.get("x-zm-request-timestamp")
    signature = headers.get("x-zm-signature")
    if not timestamp or not signature:
        return False, "missing signature headers"
    try:
        age = (now if now is not None else time.time()) - int(timestamp)
    except (TypeError, ValueError):
        return False, "unparseable timestamp"
    if abs(age) > MAX_SKEW_SECONDS:
        return False, "timestamp is {:.0f}s away -- replay?".format(age)
    # compare_digest, not ==, so a wrong signature cannot be guessed a
    # character at a time from how long the comparison took.
    if not hmac.compare_digest(sign(secret, timestamp, body), signature):
        return False, "signature mismatch"
    return True, ""


# ------------------------------------------------------------------ queue
def _ensure_dirs():
    for path in (QUEUE_DIR, FAILED_DIR):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass


def enqueue(recording, event=None):
    """Write one recording to the queue. Returns its path, or "" if invalid.

    Named after the recording id, so a retried event overwrites rather than
    queueing the same work twice.
    """
    recording_id = (recording or {}).get("id")
    if not recording_id:
        return ""
    _ensure_dirs()
    safe = "".join(c for c in str(recording_id) if c.isalnum() or c in "-_")
    path = os.path.join(QUEUE_DIR, safe + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"recording": recording,
                   "event": event or TRANSCRIPT_EVENT,
                   "queued_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime()),
                   "attempts": 0}, fh)
    os.replace(tmp, path)          # a kill cannot leave a half-written event
    return path


def queued():
    if not os.path.isdir(QUEUE_DIR):
        return []
    return sorted(os.path.join(QUEUE_DIR, n) for n in os.listdir(QUEUE_DIR)
                  if n.endswith(".json"))


def _give_up(path, entry, reason):
    _ensure_dirs()
    entry["gave_up_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry["reason"] = str(reason)[:300]
    dest = os.path.join(FAILED_DIR, os.path.basename(path))
    with open(dest, "w") as fh:
        json.dump(entry, fh, indent=2)
    os.remove(path)


def drain(zoom=None, drive=None, teams=None, directory=None, limit=None):
    """Deliver everything waiting. Returns (delivered, skipped, failed).

    Uses exactly the same deliver() the nightly pipeline uses, so there is
    one definition of which folder a transcript belongs in.
    """
    import pipeline
    import sharepoint_site
    import team_sync
    from drive_writer import DriveWriter
    from graph_client import GraphClient, make_auth
    from models import PhoneRecording
    from team_map import TeamMap
    from zoom_client import ZoomClient
    from zoom_directory import ZoomDirectory

    pending = queued()
    if limit:
        pending = pending[:limit]
    if not pending:
        return 0, 0, 0

    if zoom is None:
        zoom = ZoomClient()
    if teams is None:
        team_sync.sync(config.TEAM_CSV, config.TEAM_EFFECTIVE_CSV, write=False)
        teams = TeamMap(config.TEAM_EFFECTIVE_CSV)
    if directory is None:
        directory = ZoomDirectory(transport=zoom.transport)
    if drive is None:
        client = GraphClient(make_auth())
        drive = DriveWriter(client, root=sharepoint_site.drive_root(client))

    known = {}                      # shared across the batch, saves listings
    delivered = skipped = failed = 0

    for path in pending:
        try:
            with open(path) as fh:
                entry = json.load(fh)
        except (IOError, OSError, ValueError) as exc:
            _give_up(path, {"path": path}, "unreadable: {}".format(exc))
            failed += 1
            continue

        rec = PhoneRecording.from_api(entry.get("recording") or {})
        if not rec.transcript_download_url:
            # Zoom said the transcript was ready but sent no URL. Not
            # retryable; the nightly run will pick it up from the API.
            _give_up(path, entry, "no transcript_download_url in the event")
            failed += 1
            continue

        outcome = pipeline.deliver(rec, zoom, drive, teams, directory, known)
        if outcome in ("uploaded", "skipped"):
            os.remove(path)
            delivered += outcome == "uploaded"
            skipped += outcome == "skipped"
            continue

        entry["attempts"] = entry.get("attempts", 0) + 1
        failed += 1
        if entry["attempts"] >= MAX_ATTEMPTS:
            _give_up(path, entry, "{} delivery attempts failed".format(
                entry["attempts"]))
            continue
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(entry, fh)
        os.replace(tmp, path)

    return delivered, skipped, failed


# ------------------------------------------------------------------ server
class Handler(BaseHTTPRequestHandler):
    server_version = "ZoomTranscriptHook/1.0"
    secret = ""                      # set by serve()

    def log_message(self, fmt, *args):
        print("  {} {}".format(self.address_string(), fmt % args))

    def _reply(self, code, payload=None):
        body = json.dumps(payload or {}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """Zoom only ever POSTs here. GET exists for humans and probes.

        The root answers instead of 404ing: this URL gets pasted into a
        browser by whoever is registering it, and a "not found" page makes a
        working endpoint look broken.
        """
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/healthz", "/health"):
            return self._reply(200, {
                "service": "Zoom Phone transcript webhook",
                "ok": True,
                "queued": len(queued()),
                "expects": "POST from Zoom, signed with x-zm-signature",
                "healthz": "/healthz",
            })
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._reply(400, {"error": "bad Content-Length"})
        if length > MAX_BODY:
            # Read and throw away the body before answering. Replying while
            # the client is still sending leaves it with a broken pipe
            # instead of the 413, and it never learns why it was refused.
            # Bounded, so an absurd Content-Length cannot tie up a thread.
            drained = 0
            while drained < length and drained < MAX_BODY * 10:
                chunk = self.rfile.read(min(65536, length - drained))
                if not chunk:
                    break
                drained += len(chunk)
            if drained < length:
                self.close_connection = True
            return self._reply(413, {"error": "body too large"})
        body = self.rfile.read(length) if length else b""

        ok, reason = verify(self.headers, body, self.secret)
        if not ok:
            print("  rejected: {}".format(reason))
            return self._reply(401, {"error": "unauthorized"})

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._reply(400, {"error": "bad JSON"})

        event = payload.get("event")

        if event == VALIDATION_EVENT:
            plain = ((payload.get("payload") or {}).get("plainToken")) or ""
            print("  URL validation challenge answered")
            return self._reply(200, {
                "plainToken": plain,
                "encryptedToken": crc_token(self.secret, plain)})

        if event != TRANSCRIPT_EVENT:
            # 200, not an error: anything else is a subscription we did not
            # ask for, and a non-2xx would make Zoom retry it three times.
            print("  ignoring event {!r}".format(event))
            return self._reply(200, {"ignored": event})

        obj = (payload.get("payload") or {}).get("object") or {}
        recordings = obj.get("recordings")
        if not isinstance(recordings, list):
            # Older/other shapes put a single recording in `object`.
            recordings = [obj] if obj.get("id") else []

        accepted = [r for r in (enqueue(r, event) for r in recordings) if r]
        print("  queued {} recording(s)".format(len(accepted)))
        # 200 the moment it is on disk. Delivery happens after this returns;
        # holding the connection open for it would blow the 3-second budget.
        self._reply(200, {"queued": len(accepted)})


def _worker(stop, interval):
    while not stop.is_set():
        try:
            if queued():
                delivered, skipped, failed = drain()
                print("  drained: {} delivered, {} already there, {} failed"
                      .format(delivered, skipped, failed))
        except Exception as exc:                       # noqa: BLE001
            # A worker that dies stops all delivery and nothing says so.
            print("  worker error (continuing): {}".format(exc))
        stop.wait(interval)


def serve(port=None, interval=10, start_worker=True):
    port = int(port or os.environ.get("PORT") or 8080)
    Handler.secret = os.environ.get("ZOOM_SECRET_TOKEN", "")
    if not Handler.secret:
        print("\n  ZOOM_SECRET_TOKEN is not set -- every request will be"
              "\n  rejected. Add it to .env before registering the URL.\n")
    _ensure_dirs()

    stop = threading.Event()
    if start_worker:
        threading.Thread(target=_worker, args=(stop, interval),
                         daemon=True).start()

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("\n  listening on 0.0.0.0:{}".format(port))
    print("  queue: {}  ({} waiting)".format(QUEUE_DIR, len(queued())))
    print("  POST / for events, GET /healthz for the probe\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopping")
    finally:
        stop.set()
        httpd.server_close()
    return 0


def status():
    pending, dead = queued(), []
    if os.path.isdir(FAILED_DIR):
        dead = [n for n in os.listdir(FAILED_DIR) if n.endswith(".json")]
    print("\n  queue     : {}".format(QUEUE_DIR))
    print("  waiting   : {}".format(len(pending)))
    print("  gave up   : {}".format(len(dead)))
    for path in pending[:10]:
        try:
            with open(path) as fh:
                entry = json.load(fh)
            print("    {}  attempts={} queued={}".format(
                os.path.basename(path), entry.get("attempts", 0),
                entry.get("queued_utc", "?")))
        except (IOError, OSError, ValueError):
            print("    {}  (unreadable)".format(os.path.basename(path)))
    if len(pending) > 10:
        print("    ... and {} more".format(len(pending) - 10))
    print()
    return 0


def self_test():
    """Prove signing and validation work against this machine's secret."""
    secret = os.environ.get("ZOOM_SECRET_TOKEN", "")
    print("\n  ZOOM_SECRET_TOKEN: {}".format(
        "set ({} chars)".format(len(secret)) if secret else "MISSING"))
    if not secret:
        return 1
    body = json.dumps({"event": TRANSCRIPT_EVENT}).encode()
    ts = str(int(time.time()))
    headers = {"x-zm-request-timestamp": ts, "x-zm-signature": sign(secret, ts, body)}
    ok, reason = verify(headers, body, secret)
    print("  round-trip signature : {}".format("OK" if ok else reason))
    headers["x-zm-signature"] = "v0=" + "0" * 64
    bad, _ = verify(headers, body, secret)
    print("  forged signature     : {}".format("rejected" if not bad else "ACCEPTED -- BUG"))
    print("  CRC for 'abc123'     : {}\n".format(crc_token(secret, "abc123")[:32] + "..."))
    return 0 if ok and not bad else 1


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int)
    p.add_argument("--drain", action="store_true", help="process the queue once")
    p.add_argument("--status", action="store_true")
    p.add_argument("--self-test", dest="selftest", action="store_true")
    p.add_argument("--interval", type=int, default=10,
                   help="seconds between queue sweeps (default 10)")
    args = p.parse_args()

    if args.selftest:
        return self_test()
    if args.status:
        return status()
    if args.drain:
        delivered, skipped, failed = drain()
        print("\n  {} delivered, {} already there, {} failed\n".format(
            delivered, skipped, failed))
        return 1 if failed else 0
    return serve(args.port, args.interval)


if __name__ == "__main__":
    sys.exit(main())
