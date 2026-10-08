"""Zoom meeting transcripts arrive as WebVTT, not as the phone JSON.

Zoom Phone hands us a JSON document with a `timeline` array, which
core/transcript.py renders. Meeting recordings hand us a .vtt file instead:

    WEBVTT

    1
    00:00:01.880 --> 00:00:04.240
    Paula Truscott: Morning everyone, thanks for joining.

    2
    00:00:04.600 --> 00:00:07.100
    Tania Marshall: No problem.

Same job, different shape, so it gets its own parser rather than a second
mode bolted onto the phone one. The output format deliberately matches
core/transcript.to_text so a reader opening either file sees the same thing.
"""
import re

HEADER = "Zoom meeting transcript"

# "00:00:04.240 --> 00:00:07.100", optionally followed by cue settings.
_TIMING = re.compile(r"^\s*(\S+)\s*-->\s*(\S+)")

# Zoom writes the speaker into the cue text as "Name: said something".
# A colon can legitimately appear in speech ("so I said: no"), so only treat
# it as a speaker when the part before it looks like a name rather than a
# sentence -- short, and without sentence punctuation.
_SPEAKER = re.compile(r"^([^:]{1,60}):\s+(.*)$", re.S)


def _clock(stamp):
    """'00:01:23.456' -> '00:01:23'. Anything else is left alone."""
    return str(stamp or "00:00:00").split(".")[0]


def _looks_like_speaker(name):
    """Is the text before a colon a participant's name, or just speech?

    "Alice Anderson: morning"        -> a speaker
    "So I told them this: we cannot" -> not a speaker, and mis-reading it
                                        would drop the first half of the line

    Zoom writes the participant's display name, so every word is capitalised
    ("Tania MARSHALL") or the label is an address. A sentence fragment almost
    always contains a lowercase word, which is what separates the two.
    """
    name = (name or "").strip()
    if not name or "\n" in name:
        return False
    if any(ch in name for ch in ".!?,;"):
        return "@" in name          # an email address is a legitimate label
    words = name.split()
    if not words or len(words) > 4:
        return False
    return all(w[0].isupper() or not w[0].isalpha() for w in words)


def parse(raw):
    """VTT bytes or str -> [{start, end, speaker, text}, ...]."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig", "replace")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    cues = []
    for block in raw.split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        # A block is an optional cue number, a timing line, then the text.
        timing_at = None
        for i, line in enumerate(lines):
            if "-->" in line:
                timing_at = i
                break
        if timing_at is None:
            continue                    # the WEBVTT header, NOTE blocks, etc.
        match = _TIMING.match(lines[timing_at])
        if not match:
            continue
        text = " ".join(lines[timing_at + 1:]).strip()
        if not text:
            continue

        speaker = ""
        said = _SPEAKER.match(text)
        if said and _looks_like_speaker(said.group(1)):
            speaker, text = said.group(1).strip(), said.group(2).strip()

        cues.append({"start": match.group(1), "end": match.group(2),
                     "speaker": speaker or "Unknown", "text": text})
    return cues


def to_text(raw, meta=None):
    """Readable plain text -- this is what SharePoint full-text indexes."""
    lines = [HEADER, "=" * len(HEADER), ""]
    for label, value in (meta or []):
        lines.append("{}: {}".format(label, value))
    lines.append("")

    cues = parse(raw)
    if not cues:
        lines.append("(no speech was transcribed)")
        return "\n".join(lines) + "\n"

    last = None
    for cue in cues:
        if cue["speaker"] != last:
            lines.append("")
            last = cue["speaker"]
        lines.append("[{}] {}: {}".format(
            _clock(cue["start"]), cue["speaker"], cue["text"]))

    return "\n".join(lines).strip() + "\n"


def render(raw, fmt, meta=None):
    """(bytes, extension) for the configured output format.

    'vtt' hands back exactly what Zoom sent. There is nothing to improve on
    in the original, and keeping it byte-identical means the archived file
    can be fed straight to any player or caption tool.
    """
    if fmt == "vtt":
        return (raw if isinstance(raw, bytes) else raw.encode("utf-8")), ".vtt"
    return to_text(raw, meta).encode("utf-8"), ".txt"
