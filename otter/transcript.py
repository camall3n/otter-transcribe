#!/usr/bin/env python3
"""Transcript model and renderer: cues in, interleaved transcript out.

Deliberately knows nothing about where cues came from. `otter/speech.py` builds
them by interleaving separate microphones and `otter/reconcile.py` by
reconciling several recordings of one room; anything else that can produce
(who, start, end, text) can use this unchanged.

The one non-obvious piece is `group_turns`. It runs over cues from every
track sorted into a single global stream, so a speaker's turn breaks wherever
anybody else speaks -- which is what turns parallel recordings of the same
room back into a conversation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher

# Speaker labels that mean "not attributed", rather than naming a person.
UNATTRIBUTED = {"unattributed", "unknown"}


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------

@dataclass
class Cue:
    """One continuous run of speech by one speaker on one track."""
    track: str
    idx: int
    start: float
    end: float
    text: str
    who: str | None = None
    # Did Otter attribute this to a named speaker? Read before any aliasing,
    # because it is the strongest available bleed signal: a voice arriving
    # through someone else's speakers is band-limited, so the diariser puts it
    # in its own cluster and declines to name it.
    attributed: bool = True
    # Words in the Otter segment this cue was cut from -- a weak tiebreak.
    segment_words: int = 0


@dataclass
class Track:
    name: str
    cues: list[Cue]


@dataclass
class Turn:
    who: str
    start: float
    end: float
    text: str
    track: str
    cues: list[Cue] = field(default_factory=list)


def ts(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


# --------------------------------------------------------------------------
# mic bleed
# --------------------------------------------------------------------------

@dataclass
class Bleed:
    a: Cue
    b: Cue
    score: float
    kind: str    # only "duplicate" today; kept so a weaker class can be added

    @property
    def offset(self) -> float:
        return self.a.start - self.b.start


def find_bleed(tracks: list[Track], window: float = 20.0,
               threshold: float = 0.55) -> list[Bleed]:
    """Near-duplicate cues on different tracks.

    One participant's voice leaves another's speakers and returns through
    their mic, so Otter transcribes the same words twice, crediting each to
    that track's own speaker -- so keeping the wrong copy puts words in
    someone else's mouth.
    """
    hits: list[Bleed] = []
    norm = lambda t: " ".join(re.sub(r"[^a-z ]", "", t.lower()).split())
    for i, a_track in enumerate(tracks):
        for b_track in tracks[i + 1:]:
            for a in a_track.cues:
                for b in b_track.cues:
                    if abs(a.start - b.start) > window:
                        continue
                    na, nb = norm(a.text), norm(b.text)
                    if not na or not nb:
                        continue
                    ratio = SequenceMatcher(None, na, nb).ratio()
                    if ratio > threshold:
                        hits.append(Bleed(a, b, ratio, "duplicate"))
    return hits


# --------------------------------------------------------------------------
# assemble
# --------------------------------------------------------------------------

def merge_cues(tracks: list[Track], drop: list[dict] | None = None) -> list[Cue]:
    """Interleave every track by cue start time."""
    dropped = {(d["track"], round(float(d["start"]), 1)) for d in (drop or [])}
    cues = [c for t in tracks for c in t.cues
            if (c.track, round(c.start, 1)) not in dropped]
    return sorted(cues, key=lambda c: (c.start, c.track))


def correct_speakers(cues: list[Cue], config: dict) -> list[Cue]:
    """Settle disputed speaker labels before turns are grouped, not after.

    Applied at render time this looked right and read wrong: relabelling a
    stray word to the speaker either side of it left three turns where there
    should be one, because grouping had already happened and could not be
    undone. Doing it here means the corrected cue simply belongs to its
    neighbour's turn.
    """
    fixes = config.get("speaker_corrections", [])
    if not fixes:
        return cues
    return [replace(c, who=apply_corrections(c.who or "", fixes, ignore_case=True))
            for c in cues]


def group_turns(cues: list[Cue]) -> list[Turn]:
    """Collapse consecutive same-speaker cues into turns.

    Runs over the globally sorted stream, so a turn breaks wherever anyone
    else speaks -- which is what produces true interleaving. Nothing else
    breaks a turn: one person talking either side of a pause is still one
    person talking.
    """
    turns: list[Turn] = []
    for cue in cues:
        if turns and turns[-1].who == cue.who:
            turns[-1].cues.append(cue)
            turns[-1].end = cue.end
            turns[-1].text = re.sub(r"\s+", " ", turns[-1].text + " " + cue.text).strip()
        else:
            turns.append(Turn(cue.who, cue.start, cue.end,
                              re.sub(r"\s+", " ", cue.text).strip(), cue.track, [cue]))
    return turns


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def apply_corrections(text: str, corrections: list[dict],
                      ignore_case: bool = False) -> str:
    """Each: {"pattern": <regex>, "replace": <str>, "note": <str>}

    Speaker labels are matched case-insensitively, because the transcript
    prints them upper-case and that is what anyone reading it will write a
    rule against -- while the label itself is mixed-case.
    """
    flags = re.IGNORECASE if ignore_case else 0
    for c in corrections:
        text = re.sub(c["pattern"], c["replace"], text, flags=flags)
    return text


def derive_footer(config: dict) -> list[str]:
    """Build the audit trail from what was actually applied.

    Written out rather than typed alongside the config, so the record cannot
    quietly disagree with the run that produced it. A correction whose `note`
    begins "UNRESOLVED" is listed separately as an open question rather than a
    fix -- the convention for "changed to flag it, not to settle it".
    """
    corrections = config.get("corrections", [])
    fixed = [c for c in corrections
             if not str(c.get("note", "")).upper().startswith("UNRESOLVED")]
    open_ = [c for c in corrections if c not in fixed]
    speakers = config.get("speaker_corrections", [])
    dropped = config.get("drop", [])
    if not (fixed or open_ or speakers or dropped):
        return []

    def show(pattern: str) -> str:
        r"""The words a pattern matches, not the regex that matches them.

        Corrections that resolve a disagreement marker are mostly punctuation
        once escaped -- \[Goodhart\'s\? \| ... -- which is unreadable in an
        audit trail meant for a person.
        """
        text = re.sub(r"\\b", "", pattern)           # word boundaries are not text
        text = re.sub(r"(?<!\\)\|", " / ", text)      # an alternation reads as "or"
        return re.sub(r"\\(.)", r"\1", text)         # \? and \| are just ? and |
    # One long alternation should not stretch every other row.
    width = min(max((len(show(c["pattern"]))
                     for c in corrections + speakers), default=0), 38)
    out = ["=" * 78, ""]

    if fixed:
        out += [f"CORRECTIONS APPLIED ({len(fixed)})", ""]
        for c in fixed:
            note = f"   ({c['note']})" if c.get("note") else ""
            out.append(f"  {show(c['pattern']):<{width}}  ->  {c['replace']}{note}")
        out.append("")
    if open_:
        out += [f"STILL UNRESOLVED ({len(open_)})", ""]
        for c in open_:
            out.append(f"  {show(c['pattern']):<{width}}  ->  {c['replace']}")
            out.append(f"  {'':<{width}}      {c.get('note', '')}")
        out.append("")
    if speakers:
        out += [f"SPEAKERS CORRECTED ({len(speakers)})", ""]
        for c in speakers:
            note = f"   ({c['note']})" if c.get("note") else ""
            out.append(f"  {show(c['pattern']):<{width}}  ->  {c['replace']}{note}")
        out.append("")
    if dropped:
        out += [f"CUES DROPPED ({len(dropped)})", ""]
        for d in dropped:
            out.append(f"  {d['track']} @{ts(float(d['start']))}"
                       f"{'  -- ' + d['_why'] if d.get('_why') else ''}")
        out.append("")
    return out


def render_text(turns: list[Turn], config: dict) -> str:
    corrections = config.get("corrections", [])
    markers = sorted(config.get("markers", []), key=lambda m: float(m["at"]))

    out: list[str] = list(config.get("header", []))
    if config.get("header"):
        out += ["", "=" * 78, ""]

    pending = list(markers)
    for turn in turns:
        while pending and turn.start > float(pending[0]["at"]):
            m = pending.pop(0)
            out += ["=" * 78, "", f"### {ts(float(m['at']))} - {m['text']}", "",
                    "=" * 78, ""]
        text = apply_corrections(turn.text, corrections)
        out += [f"[{ts(turn.start)}-{ts(turn.end)}]  {(turn.who or '').upper()}",
                "", text, ""]

    # A list is used verbatim; "auto" or nothing at all derives the audit trail.
    footer = config.get("footer", "auto")
    out += list(footer) if isinstance(footer, list) else derive_footer(config)
    return "\n".join(out)
