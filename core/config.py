import os

from dotenv import load_dotenv

from core import state
load_dotenv()

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# The one line that changes when the client's credentials arrive:
#   dev  -> "/me/drive"
#   prod -> "/drives/{driveId}"
DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/me/drive")

USER_AGENT = os.environ.get("USER_AGENT", "NONISV|AgileKode|ZoomArchiver/0.1")

# Simple upload works up to 250 MB; above that we need an upload session.
SIMPLE_UPLOAD_MAX = 250 * 1024 * 1024

# Chunks MUST be a multiple of 320 KiB. 10 MiB is the documented sweet spot.
CHUNK_SIZE = 320 * 1024 * 32

# SharePoint caps the full decoded path at 400 chars. In dev we're on OneDrive
# personal with a short root, so reserve room for the eventual site + library.
MAX_PATH = 400
PATH_PREFIX_RESERVE = int(os.environ.get("PATH_PREFIX_RESERVE", "120"))

# --- Zoom ---------------------------------------------------------------

ZOOM_BASE = os.environ.get("ZOOM_BASE", "https://api.zoom.us/v2")
ZOOM_OAUTH = "https://zoom.us/oauth/token"

ZOOM_ACCOUNT_ID = os.environ.get("ZOOM_ACCOUNT_ID", "")
ZOOM_CLIENT_ID = os.environ.get("ZOOM_CLIENT_ID", "")
ZOOM_CLIENT_SECRET = os.environ.get("ZOOM_CLIENT_SECRET", "")

# Zoom only returns one month of data per query, and caps a page at 300.
ZOOM_WINDOW_DAYS = 30
ZOOM_PAGE_SIZE = 300

# --- Layout -------------------------------------------------------------

# Tokens: {team} {iso_year} {week} {yyyy} {mm} {dd} {owner} {owner_type}
#         {direction}
# {week} MUST be paired with {iso_year}, never {yyyy}: 2027-01-01 is ISO week
# 53 of 2026, so {yyyy}/W{week} produces a folder that does not exist.
# Change this one line to reshape the whole archive for the client.
FOLDER_TEMPLATE = os.environ.get(
    "FOLDER_TEMPLATE", "{team}/{iso_year}-W{week}")

# Zoom timestamps are UTC. Sensible Care is in Melbourne (UTC+10/+11), so a
# 9am Monday call is 11pm Sunday UTC -- the PREVIOUS ISO week. Week folders
# are therefore computed in local time, not UTC.
TIMEZONE = os.environ.get("TIMEZONE", "Australia/Melbourne")

STATE_DB = os.environ.get("STATE_DB", "state.db")

# A single folder segment (a site or owner name) must not eat the path budget.
MAX_FOLDER_SEGMENT = int(os.environ.get("MAX_FOLDER_SEGMENT", "64"))

# Console output carries caller/callee numbers. Mask them unless asked not to.
REDACT_LOGS = os.environ.get("REDACT_LOGS", "1") != "0"

# --- Transcripts --------------------------------------------------------

# The client asked for transcripts, not call audio (project title:
# "Zoom Phone Transcript Extraction & SharePoint Delivery Pipeline").
FETCH_TRANSCRIPTS = os.environ.get("FETCH_TRANSCRIPTS", "1") != "0"
# Zoom returns transcripts as JSON. Raw JSON is faithful but SharePoint
# cannot full-text index it, so "txt" is the default: readable and searchable.
# "vtt" for subtitle-style, "json" for the raw document minus word timings.
TRANSCRIPT_FORMAT = os.environ.get("TRANSCRIPT_FORMAT", "txt")

# Email -> division. Supplied by the client; see divisions_TEMPLATE.csv.
TEAM_CSV = os.environ.get("TEAM_CSV", "teams.csv")
# Derived from TEAM_CSV by team_sync: the same mapping with effective dates
# filled in for changes the client made without them. This is what the
# pipeline actually reads; TEAM_CSV is only ever read, never written.
# "local" reads TEAM_CSV from disk. "sharepoint" downloads it from
# TEAM_CONFIG_FOLDER in the document library each run, so the client can
# maintain staff changes themselves without a redeploy.
TEAM_CSV_SOURCE = os.environ.get("TEAM_CSV_SOURCE", "local")
TEAM_CONFIG_FOLDER = os.environ.get("TEAM_CONFIG_FOLDER", "_config")

TEAM_EFFECTIVE_CSV = (os.environ.get("TEAM_EFFECTIVE_CSV")
                      or state.path("teams_effective.csv"))

# --- SharePoint site ----------------------------------------------------

SP_TENANT_ID = os.environ.get("SP_TENANT_ID", "")
SP_CLIENT_ID = os.environ.get("SP_CLIENT_ID", "")
SP_CLIENT_SECRET = os.environ.get("SP_CLIENT_SECRET", "")
SP_SITE_HOSTNAME = os.environ.get("SP_SITE_HOSTNAME", "")
SP_SITE_PATH = os.environ.get("SP_SITE_PATH", "")

# Blank means "the site's default document library", which Graph addresses
# as /sites/{siteId}/drive -- so we do not need to know its name.
SP_LIBRARY_NAME = os.environ.get("SP_LIBRARY_NAME", "")

# Alerts: email only. The client confirmed a Teams channel is not required.
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "systems@sensiblecare.com.au")

# Optional: call queue -> team, for the ~1% of calls answered on shared
# handsets that are not Zoom Phone users. Unmapped queues go to _Unassigned.
QUEUE_TEAM_CSV = os.environ.get("QUEUE_TEAM_CSV", "queue_teams.csv")

# Reconciliation runs against a day only once transcription has caught up.
# Zoom transcribes asynchronously, so same-night checks report false gaps.
RECONCILE_LAG_DAYS = int(os.environ.get("RECONCILE_LAG_DAYS", "2"))

# A week's manifest is written once, after late transcripts have landed.
MANIFEST_SEAL_DAYS = int(os.environ.get("MANIFEST_SEAL_DAYS", "3"))

# Alerts: "smtp" or "graph".
ALERT_TRANSPORT = os.environ.get("ALERT_TRANSPORT", "smtp")
ALERT_FROM = os.environ.get("ALERT_FROM", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

# The daily run re-processes a rolling window, so transcripts that Zoom
# generates late are still picked up. Re-processing a day is nearly free:
# already-delivered ids are skipped before anything is downloaded.
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "2"))
