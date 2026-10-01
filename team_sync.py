"""Turn a careless edit of teams.csv into a correctly dated team change.

teams.csv is maintained by the client. The correct way to record a move is
two dated rows -- old team up to a date, new team from the next one -- so
that a call is filed under whoever owned it at the time. Nobody outside the
runbook is going to do that; they will open the spreadsheet and change the
team, which reads as "she was ALWAYS in Nursing" and retroactively reassigns
her history.

So the client's file is treated as a statement about TODAY, and the derived
file next to it carries the history:

    teams.csv             what the client edits. Usually undated.
    teams_effective.csv   what the pipeline reads. Dated, accumulated here.

On each run we ask what the effective mapping says a person is today, compare
it with the client's file, and where they differ we close the old span
yesterday and open a new one today. Rows the client dates themselves are
taken verbatim -- an explicit date always beats an inferred one.

Nothing is ever written back to teams.csv.
"""
import csv
import os
from datetime import date, timedelta

COLUMNS = ["email", "team", "valid_from", "valid_to"]


def _parse(value):
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            from datetime import datetime
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _read(path):
    """[{email, team, valid_from, valid_to}] -- empty if the file is absent."""
    if not path or not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8-sig") as fh:
        for raw in csv.DictReader(fh):
            low = {(k or "").strip().lower(): (v or "").strip()
                   for k, v in raw.items()}
            email = low.get("email", "").lower()
            team = low.get("team") or low.get("division", "")
            if not email:
                continue
            rows.append({"email": email, "team": team,
                         "valid_from": low.get("valid_from", ""),
                         "valid_to": low.get("valid_to", "")})
    return rows


def _by_email(rows):
    out = {}
    for row in rows:
        out.setdefault(row["email"], []).append(row)
    return out


def _team_on(spans, when):
    """Which team these spans put someone in on `when`, or '' for none."""
    ordered = sorted(spans, key=lambda s: (_parse(s["valid_from"]) or date.min))
    for span in ordered:
        start, end = _parse(span["valid_from"]), _parse(span["valid_to"])
        if start and when < start:
            continue
        if end and when > end:
            continue
        return span["team"]
    return ""


def sync(client_path, effective_path, today=None, write=True):
    """Reconcile the client's file into the dated one.

    Returns (rows, changes) where changes is a list of
    (email, previous_team, new_team) for moves inferred on this run.

    `write=False` computes the same answer without saving it, so a dry run
    does not quietly consume a pending change that the next real run should
    have noticed and alerted on.
    """
    today = today or date.today()
    yesterday = today - timedelta(days=1)

    client = _by_email(_read(client_path))
    effective = _by_email(_read(effective_path))
    changes = []

    for email, rows in client.items():
        dated = [r for r in rows if r["valid_from"] or r["valid_to"]]
        if dated:
            # The client spelled out the dates. Trust them completely and
            # drop whatever we had inferred -- an explicit statement about
            # history beats anything we guessed.
            effective[email] = dated
            continue

        stated = rows[0]["team"]
        spans = effective.get(email)

        if not spans:
            effective[email] = [{"email": email, "team": stated,
                                 "valid_from": "", "valid_to": ""}]
            continue

        if _team_on(spans, today) == stated:
            continue                       # nothing changed

        previous = _team_on(spans, today)
        for span in spans:
            if not span["valid_to"]:       # the currently open span
                span["valid_to"] = yesterday.isoformat()
        spans.append({"email": email, "team": stated,
                      "valid_from": today.isoformat(), "valid_to": ""})
        changes.append((email, previous, stated))

    rows = [row for email in sorted(effective) for row in effective[email]]
    rows.sort(key=lambda r: (r["email"], _parse(r["valid_from"]) or date.min))

    if write:
        tmp = effective_path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, effective_path)      # a kill cannot leave it half-written

    return rows, changes


def describe(changes):
    """The body of the alert sent when a move is inferred."""
    lines = ["Team changes detected in teams.csv", ""]
    for email, previous, new in changes:
        lines.append("  {}".format(email))
        lines.append("      {}  ->  {}".format(previous or "(none)", new))
    lines += [
        "",
        "These were recorded WITHOUT dates, so they have been applied from",
        "today onwards. Calls taken before today stay filed under the",
        "previous team, which is almost always what is wanted.",
        "",
        "If a change should be backdated instead, edit teams.csv and give",
        "the person two dated rows, for example:",
        "",
        "    name@example.com,Old Team,,2026-10-14",
        "    name@example.com,New Team,2026-10-15,",
        "",
        "Nothing needs to be moved in SharePoint either way -- transcripts",
        "already delivered stay where they are.",
    ]
    return "\n".join(lines)
