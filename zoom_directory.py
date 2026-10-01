import json
import os
import time

import config
import state

CACHE = (os.environ.get("DIRECTORY_CACHE")
         or state.path(".zoom_directory.json"))
CACHE_TTL = 6 * 3600


class ZoomDirectory(object):
    """Zoom user lookup: recording -> person.

    A recording never carries an email. It carries either an owner id (for
    user-owned calls) or an extension number on accepted_by / outgoing_by
    (for queue-owned calls). This resolves both against /phone/users.
    """

    def __init__(self, users=None, transport=None):
        self.transport = transport
        self.users = users if users is not None else self._fetch()
        self.by_id = {u["id"]: u for u in self.users if u.get("id")}
        self.by_ext = {str(u["extension_number"]): u
                       for u in self.users if u.get("extension_number")}

    # -- loading ---------------------------------------------------------

    def _fetch(self):
        cached = self._read_cache()
        if cached is not None:
            return cached
        if self.transport is None:
            return []
        users, page = [], None
        while True:
            params = {"page_size": 100}
            if page:
                params["next_page_token"] = page
            resp = self.transport._get(config.ZOOM_BASE + "/phone/users", params=params)
            if resp.status_code != 200:
                break
            body = resp.json()
            users.extend(body.get("users", []))
            page = body.get("next_page_token")
            if not page:
                break
        if users:
            self._write_cache(users)
        return users

    @staticmethod
    def _read_cache():
        try:
            if time.time() - os.path.getmtime(CACHE) > CACHE_TTL:
                return None
            with open(CACHE) as fh:
                return json.load(fh)
        except (IOError, OSError, ValueError):
            return None

    @staticmethod
    def _write_cache(users):
        try:
            with open(CACHE, "w") as fh:
                json.dump(users, fh)
        except IOError:
            pass

    # -- resolution ------------------------------------------------------

    def email_for(self, rec):
        """The email of whoever this recording belongs to, or "".

        Tried in order of reliability: the owning user, then whoever
        accepted the call, then whoever placed it.
        """
        owner = rec.owner or {}
        if owner.get("type") == "user":
            user = self.by_id.get(owner.get("id"))
            if user and user.get("email"):
                return user["email"].strip().lower()

        for source in (rec.accepted_by, rec.outgoing_by):
            ext = str((source or {}).get("extension_number") or "")
            user = self.by_ext.get(ext)
            if user and user.get("email"):
                return user["email"].strip().lower()

        return ""

    def queue_name_for(self, rec):
        """The owning call queue, for recordings answered on a shared handset."""
        owner = rec.owner or {}
        if owner.get("type") == "callQueue":
            return (owner.get("name") or "").strip()
        return ""

    def __len__(self):
        return len(self.users)
