import csv
import os
from datetime import date, datetime

import config

UNMAPPED = "_Unassigned"


def _parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


class TeamMap(object):
    """Which Sensible Care team a recording belongs to.

    Zoom has no concept of a team, so the mapping comes from the client as
    a CSV with optional effective dates:

        email,team,valid_from,valid_to
        sarah@...,Accounts,,2026-09-29
        sarah@...,Nursing,2026-09-30,

    A blank valid_from means "since forever", a blank valid_to "still
    current". The team is resolved against the DATE OF THE CALL, so someone
    who transfers keeps their old transcripts under their old team.

    Resolution order:
      1. the person, at the call date
      2. the call queue it arrived through
      3. _Unassigned
    """

    def __init__(self, path=None, queue_path=None):
        self.path = path or config.TEAM_CSV
        self.queue_path = queue_path or config.QUEUE_TEAM_CSV
        self._by_email = {}
        self._by_queue = {}
        self.loaded = self._load_people()
        self.queues_loaded = self._load_queues()

    def _load_people(self):
        if not self.path or not os.path.exists(self.path):
            return False
        with open(self.path, encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                low = {(k or "").strip().lower(): (v or "").strip()
                       for k, v in row.items()}
                email = low.get("email", "").lower()
                team = low.get("team") or low.get("division", "")
                if not email or not team:
                    continue
                self._by_email.setdefault(email, []).append(
                    (_parse_date(low.get("valid_from")),
                     _parse_date(low.get("valid_to")), team))
        for spans in self._by_email.values():
            spans.sort(key=lambda s: (s[0] or date.min))
        return True

    def _load_queues(self):
        if not self.queue_path or not os.path.exists(self.queue_path):
            return False
        with open(self.queue_path, encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                low = {(k or "").strip().lower(): (v or "").strip()
                       for k, v in row.items()}
                name = low.get("call_queue", "").lower()
                team = low.get("team") or low.get("division", "")
                if name and team:
                    self._by_queue[name] = team
        return True

    # -- lookups ---------------------------------------------------------

    def for_email(self, email, on_date=None):
        """The team this person was in on `on_date` (default: today)."""
        spans = self._by_email.get((email or "").strip().lower())
        if not spans:
            return UNMAPPED
        when = on_date or date.today()
        for valid_from, valid_to, team in spans:
            if valid_from and when < valid_from:
                continue
            if valid_to and when > valid_to:
                continue
            return team
        return UNMAPPED

    def for_queue(self, name):
        if not name:
            return UNMAPPED
        return self._by_queue.get(name.strip().lower(), UNMAPPED)

    def resolve(self, rec, directory):
        if directory is None:
            return UNMAPPED
        on_date = rec.local_started.date()
        team = self.for_email(directory.email_for(rec), on_date)
        if team != UNMAPPED:
            return team
        return self.for_queue(directory.queue_name_for(rec))

    def teams(self):
        known = {t for spans in self._by_email.values() for _, _, t in spans}
        return sorted(known | set(self._by_queue.values()))

    def __len__(self):
        return len(self._by_email)
