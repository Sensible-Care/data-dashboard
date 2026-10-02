import re
from urllib.parse import quote

from core import config
from clients.graph_client import GraphClient, StaticTokenAuth

INVALID_CHARS = re.compile(r'[":<>?/\\|*]')
RESERVED = {
    "con", "prn", "aux", "nul", "desktop.ini", ".lock", "_vti_",
}
RESERVED |= {"com{}".format(i) for i in range(10)}
RESERVED |= {"lpt{}".format(i) for i in range(10)}


def sanitize(name):
    """Make a Zoom-supplied string safe as a SharePoint file or folder name."""
    cleaned = INVALID_CHARS.sub("-", name)
    cleaned = cleaned.strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return "untitled"
    stem = cleaned.split(".")[0].lower()
    if stem in RESERVED or cleaned.lower() in RESERVED or cleaned.startswith("~$"):
        cleaned = "_" + cleaned
    return cleaned[:255]


def budget_path(folder, filename):
    """Keep folder + filename inside SharePoint's 400-char decoded path limit."""
    budget = config.MAX_PATH - config.PATH_PREFIX_RESERVE - len(folder) - 1
    if budget < 20:
        raise ValueError("folder path too long to fit any filename: {}".format(folder))
    if len(filename) <= budget:
        return filename
    if "." in filename:
        stem, ext = filename.rsplit(".", 1)
        ext = "." + ext
    else:
        stem, ext = filename, ""
    return stem[:budget - len(ext)] + ext


def _enc(path):
    return quote(path.strip("/"), safe="/")


class DriveWriter:
    def __init__(self, client=None, root=None):
        self.client = client or GraphClient(StaticTokenAuth())
        self.root = root or config.DRIVE_ROOT
        self._known_folders = set()

    def ensure_folder(self, path):
        """Idempotently create every level of `path`. Returns the path."""
        segments = [s for s in path.strip("/").split("/") if s]
        current = ""
        for segment in segments:
            parent = current
            current = "{}/{}".format(current, segment) if current else segment
            if current in self._known_folders:
                continue
            self._create_folder(parent, segment)
            self._known_folders.add(current)
        return current

    def _create_folder(self, parent, name):
        if parent:
            url = "{}/root:/{}:/children".format(self.root, _enc(parent))
        else:
            url = "{}/root/children".format(self.root)

        resp = self.client.request("POST", url, json={
            "name": name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        })

        if resp.status_code == 409:
            return  # already there -- this is the idempotent path
        GraphClient.raise_for_graph(resp)

    def upload(self, folder, filename, data, size=None):
        """Upload bytes or a stream. Nothing is ever written to local disk.

        Large files go through an upload session, which reads one chunk at a
        time -- peak memory is CHUNK_SIZE, regardless of how big the call is.
        """
        filename = budget_path(folder, sanitize(filename))
        if size is None and isinstance(data, (bytes, bytearray)):
            size = len(data)
        if size is None:
            # Zoom did not send Content-Length. Stream it straight through and
            # let the transfer encoding handle framing.
            return self._upload_simple(folder, filename, data)
        if size <= config.SIMPLE_UPLOAD_MAX:
            return self._upload_simple(folder, filename, data)
        return self._upload_session(folder, filename, data, size)

    def _upload_simple(self, folder, filename, data):
        url = "{}/root:/{}/{}:/content".format(self.root, _enc(folder), _enc(filename))
        resp = self.client.request("PUT", url, data=data,
                                   headers={"Content-Type": "application/octet-stream"})
        GraphClient.raise_for_graph(resp)
        return resp.json()

    def _upload_session(self, folder, filename, stream, size):
        url = "{}/root:/{}/{}:/createUploadSession".format(self.root, _enc(folder), _enc(filename))
        resp = self.client.request("POST", url, json={
            "item": {"@microsoft.graph.conflictBehavior": "replace"},
        })
        GraphClient.raise_for_graph(resp)
        upload_url = resp.json()["uploadUrl"]

        sent = 0
        while sent < size:
            chunk = stream.read(config.CHUNK_SIZE)
            if not chunk:
                break
            end = sent + len(chunk) - 1
            resp = self.client.request("PUT", upload_url, data=chunk, headers={
                "Content-Length": str(len(chunk)),
                "Content-Range": "bytes {}-{}/{}".format(sent, end, size),
            })
            if resp.status_code not in (200, 201, 202):
                GraphClient.raise_for_graph(resp)
            sent = end + 1

        return resp.json() if resp.content else {}

    def download(self, folder, filename):
        """The bytes of one small file, or b"" if it is not there.

        Only used for manifests, which are a few KB. Anything large should
        stream instead of landing in memory.
        """
        url = "{}/root:/{}/{}:/content".format(
            self.root, _enc(folder), _enc(filename))
        resp = self.client.request("GET", url)
        if resp.status_code == 404:
            return b""
        GraphClient.raise_for_graph(resp)
        return resp.content

    def existing_items(self, folder):
        """{recording id: driveItem} for everything already in `folder`.

        SharePoint is the source of truth for what has been delivered -- no
        local database to drift from reality. A file deleted by hand is
        simply re-created on the next run.

        The item is kept, not just the id, so a manifest can report the size
        of a transcript delivered by an earlier run without downloading it
        again.
        """
        found = {}
        for item in self.list_children(folder):
            stem = item.get("name", "").rsplit(".", 1)[0]
            tail = stem.rsplit("_", 1)[-1]
            if tail:
                found[tail] = item
        return found

    def existing_recording_ids(self, folder):
        """Just the ids, for callers that do not need the rest."""
        return set(self.existing_items(folder))

    def list_children(self, folder):
        items, url = [], "{}/root:/{}:/children?$top=200&$select=name,id,size".format(
            self.root, _enc(folder))
        while url:
            resp = self.client.request("GET", url)
            if resp.status_code == 404:
                return []                       # folder not created yet
            GraphClient.raise_for_graph(resp)
            body = resp.json()
            items.extend(body.get("value", []))
            url = body.get("@odata.nextLink")
        return items
