"""Zoom's transcript JSON -> something a person can read and SharePoint can index.

Zoom returns a document shaped like:

    {"type": "zoom_transcript", "ver": 1, "recording_id": "...",
     "recording_start": "...", "recording_end": "...",
     "timeline": [{"ts": "00:00:04.120", "end_ts": "...", "text": "...",
                   "username": "...", "email_address": "...",
                   "word_items": [...]}, ...]}

`word_items` holds per-word timings and accounts for most of the file size.
We drop it: it is useless to a reader and roughly triples storage.
"""
import json

HEADER = "Zoom Phone call transcript"


def parse(raw):
    """Bytes or str of Zoom transcript JSON -> the decoded document."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return json.loads(raw)


def _clock(ts):
    """'00:01:23.456' -> '00:01:23'. Left alone if it is any other shape."""
    if not ts:
        return "00:00:00"
    return str(ts).split(".")[0]


# Zoom is inconsistent about who spoke: sometimes a plain `username` string,
# sometimes a `users` list, and the list entries may be strings or objects.
# Formatting the object itself puts "{'username': 'Agent'}" in the file.
_NAME_KEYS = ("username", "user_name", "display_name", "name", "email_address")


def _as_number(name):
    """Zoom labels external parties by their number, with no leading '+'.

    Bare '61407830697' reads like an internal id; '+61407830697' reads like
    what it is. Names are returned untouched.
    """
    digits = name.replace(" ", "")
    if digits.isdigit() and len(digits) >= 8:
        return "+" + digits
    return name


def _one_name(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in _NAME_KEYS:
            got = (value.get(key) or "").strip()
            if got:
                return got
        return ""
    return str(value).strip()


def speaker_of(entry):
    """A readable speaker name, whatever shape Zoom used."""
    raw = entry.get("username") or entry.get("users") or entry.get("user")
    if isinstance(raw, (list, tuple)):
        names = [n for n in (_one_name(v) for v in raw) if n]
        return ", ".join(_as_number(n) for n in names) if names else "Unknown"
    one = _one_name(raw)
    return _as_number(one) if one else "Unknown"


def to_text(doc, meta=None):
    """Readable plain text. This is what SharePoint full-text indexes."""
    lines = [HEADER, "=" * len(HEADER), ""]

    for label, value in (meta or []):
        lines.append("{}: {}".format(label, value))
    # Label these as UTC. The folder and the filename are Melbourne time, so
    # an unlabelled timestamp ten hours out looks like a misfiled document.
    if doc.get("recording_start"):
        lines.append("Started (UTC): {}".format(doc["recording_start"]))
    if doc.get("recording_end"):
        lines.append("Ended (UTC): {}".format(doc["recording_end"]))
    lines.append("")

    timeline = doc.get("timeline") or []
    if not timeline:
        lines.append("(no speech was transcribed)")
        return "\n".join(lines) + "\n"

    last_speaker = None
    for entry in timeline:
        text = (entry.get("text") or entry.get("raw_text") or "").strip()
        if not text:
            continue
        speaker = speaker_of(entry)
        stamp = _clock(entry.get("ts"))
        if speaker != last_speaker:
            lines.append("")
            last_speaker = speaker
        lines.append("[{}] {}: {}".format(stamp, speaker, text))

    return "\n".join(lines).strip() + "\n"


def to_vtt(doc):
    """WEBVTT, for anyone who wants to play it alongside the audio."""
    out = ["WEBVTT", ""]
    for i, entry in enumerate(doc.get("timeline") or [], 1):
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        start, end = entry.get("ts"), entry.get("end_ts")
        if not start or not end:
            continue
        speaker = speaker_of(entry)
        if speaker == "Unknown":
            speaker = ""
        out.append(str(i))
        out.append("{} --> {}".format(start, end))
        out.append("{}{}".format(speaker + ": " if speaker else "", text))
        out.append("")
    return "\n".join(out)


def strip_word_items(doc):
    """The raw document minus per-word timings -- ~3x smaller, same content."""
    slim = {k: v for k, v in doc.items() if k != "timeline"}
    slim["timeline"] = [
        {k: v for k, v in entry.items() if k not in ("word_items", "avatar_url")}
        for entry in (doc.get("timeline") or [])
    ]
    return slim


def render(raw, fmt, meta=None):
    """Returns (bytes, extension) for the configured output format."""
    doc = parse(raw)
    if fmt == "json":
        body = json.dumps(strip_word_items(doc), indent=1)
        return body.encode("utf-8"), ".json"
    if fmt == "vtt":
        return to_vtt(doc).encode("utf-8"), ".vtt"
    return to_text(doc, meta).encode("utf-8"), ".txt"
