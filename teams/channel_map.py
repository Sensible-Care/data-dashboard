"""Which chat channels to archive, and which team(s) each belongs to.

The client supplies this as a spreadsheet they maintain themselves:

    Channel name,Channel owner,Members,Which Team(s)
    CMs + ACCOUNTS,Olivia Akdeniz,40,Care Partner + Accounts

Two things make that file awkward to consume directly, and both are handled
here rather than by asking the client to change how they work:

1. A channel can belong to more than one team, written as "A + B". Those
   records are delivered to BOTH team folders, on the client's explicit
   instruction.

2. Team names are typed by hand and do not match teams.csv exactly. The
   client's own file contains "Accounts", "Accounts Team", "Care Partner",
   "Care Partners", "Care Partner Team", "Nursing Team" for "Nursing", and
   one outright typo ("Care Coorcinators"). Eighteen spellings for twelve
   teams. Normalising them is what makes the file editable by a human
   without breaking delivery.

An unrecognised team name is NOT a crash and NOT a silent drop: it resolves
to _Unassigned and is reported, so a new team added to teams.csv but not yet
aliased here still gets its chat archived somewhere findable.
"""
import csv
import io
import os
import re

from teams.team_map import UNMAPPED

# Hand-written spellings -> the canonical team in teams.csv. Keys are
# compared after lowercasing and collapsing whitespace, so only genuinely
# different wordings need an entry.
ALIASES = {
    "accounts": "Accounts Team",
    "account": "Accounts Team",
    "accounts team": "Accounts Team",
    "allied health": "Allied Health Team",
    "allied health team": "Allied Health Team",
    "care coordination": "Care Coordination",
    "care coordinators": "Care Coordination",
    "care coorcinators": "Care Coordination",      # typo in the client's file
    "care co-ordination": "Care Coordination",
    "care co ordination": "Care Coordination",
    "care partner": "Care Partner Team",
    "care partners": "Care Partner Team",
    "care partner team": "Care Partner Team",
    "human resources": "Human Resources",
    "hr": "Human Resources",
    "intake": "Intake Team",
    "intake team": "Intake Team",
    "management": "Management",
    "marketing": "Marketing",
    "marketing team": "Marketing",
    "nursing": "Nursing",
    "nursing team": "Nursing",
    "rostering": "Rostering",
    "rostering team": "Rostering",
    "service coordination": "Service Coordination",
    "service coordinators": "Service Coordination",
    "service co ordination": "Service Coordination",
    "systems": "Systems Team",
    "systems team": "Systems Team",
}

# Column headings we accept, so the client can rename them slightly without
# the run failing.
NAME_KEYS = ("channel name", "channel", "name")
TEAM_KEYS = ("which team(s)", "which teams", "team(s)", "teams", "team")
OWNER_KEYS = ("channel owner", "owner")


class ChannelMapError(Exception):
    pass


def _norm(value):
    return " ".join((value or "").strip().lower().split())


def canonical_team(label, known=None):
    """One hand-typed team label -> the name used in teams.csv.

    `known` is the set of teams teams.csv actually contains. A label that
    matches one of those exactly wins outright, so a team the client adds
    later works without an alias being added here first.
    """
    cleaned = _norm(label)
    if not cleaned:
        return None
    if known:
        for team in known:
            if _norm(team) == cleaned:
                return team
    alias = ALIASES.get(cleaned)
    if alias:
        return alias
    # "Rostering Teams", "Accounts  Team" and friends: try once more with a
    # trailing "team"/"s" removed before giving up.
    stripped = re.sub(r"\s+teams?$", "", cleaned)
    return ALIASES.get(stripped)


def split_teams(cell, known=None):
    """"Care Partner + Accounts" -> ["Accounts Team", "Care Partner Team"].

    Returns (teams, unresolved). Separators seen in the client's file are
    "+" and "&"; a comma is treated the same way for good measure.
    """
    teams, unresolved = [], []
    for part in re.split(r"[+&,/]", cell or ""):
        part = part.strip()
        if not part:
            continue
        resolved = canonical_team(part, known)
        if resolved:
            teams.append(resolved)
        else:
            unresolved.append(part)
    return sorted(set(teams)), unresolved


class ChannelMap(object):
    """The client's channel selection, parsed and normalised."""

    def __init__(self, path=None, known_teams=None):
        self.path = path
        self.known_teams = set(known_teams or ())
        self.entries = []          # [{name, owner, teams, unresolved}]
        self.problems = []
        if path:
            self.load(path)

    def load(self, path):
        if not os.path.exists(path):
            raise ChannelMapError("no channel map at {}".format(path))
        with open(path, "rb") as fh:
            self.parse(fh.read())
        return self

    def parse(self, text):
        self.entries, self.problems = parse(text, self.known_teams)
        return self

    def names(self):
        return [e["name"] for e in self.entries]

    def teams_for(self, channel_name):
        """Every team folder this channel's records belong in."""
        target = _norm(channel_name)
        for e in self.entries:
            if _norm(e["name"]) == target:
                return e["teams"] or [UNMAPPED]
        return []

    def __len__(self):
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)


def parse(text, known_teams=None):
    """CSV text -> ([entry], [problem]). Raises only if unusable outright."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ChannelMapError("not UTF-8 text -- is it really a CSV?")
    if not text.strip():
        raise ChannelMapError("the file is empty")

    reader = csv.DictReader(io.StringIO(text))
    fields = {_norm(f): f for f in (reader.fieldnames or []) if f}
    name_col = _pick(fields, NAME_KEYS)
    team_col = _pick(fields, TEAM_KEYS)
    owner_col = _pick(fields, OWNER_KEYS)
    if not name_col:
        raise ChannelMapError(
            "no 'Channel name' column (found: {})".format(
                ", ".join(sorted(fields)) or "nothing"))
    if not team_col:
        raise ChannelMapError("no 'Which Team(s)' column")

    entries, problems, seen = [], [], set()
    for row in reader:
        name = (row.get(name_col) or "").strip()
        if not name:
            continue
        if _norm(name) in seen:
            problems.append("{}: listed twice -- the later row is ignored"
                            .format(name))
            continue
        seen.add(_norm(name))
        teams, unresolved = split_teams(row.get(team_col), known_teams)
        if unresolved:
            problems.append("{}: team name(s) not recognised: {}".format(
                name, ", ".join(unresolved)))
        if not teams:
            teams = [UNMAPPED]
        entries.append({
            "name": name,
            "owner": (row.get(owner_col) or "").strip() if owner_col else "",
            "teams": teams,
            "unresolved": unresolved,
        })

    if not entries:
        raise ChannelMapError(
            "no usable rows -- every line is missing a channel name")
    return entries, problems


def _pick(fields, candidates):
    for key in candidates:
        if key in fields:
            return fields[key]
    return None
