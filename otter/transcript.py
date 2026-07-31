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
    # This cue's own words, timed absolutely: {"word", "start", "end"}. Kept so
    # a speaker correction can cut a cue at a time rather than only move whole
    # ones -- a misattribution does not have to respect a silence. Empty when
    # the source had no per-word alignment, which is what makes such a cue
    # unsplittable rather than silently split in the wrong place.
    words: list[dict] = field(default_factory=list)


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


def parse_time(value: float | int | str, end: bool = False) -> float:
    """Seconds from a number, or from the MM:SS a transcript actually prints.

    Anyone writing a config is reading timestamps off the rendered transcript,
    so "26:28" has to mean what it looks like. Numbers still work unchanged.

    `end` covers the whole of a written second, because the transcript floors:
    a turn printed as ending 26:29 has words running to 26:29.9, and a range
    copied off the page has to include them. Only for text -- a number is a
    number, and stays exact.
    """
    if isinstance(value, (int, float)):
        return float(value)
    parts = str(value).strip().split(":")
    if not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts) or len(parts) > 3:
        raise ValueError(f"not a time: {value!r} (want seconds, MM:SS or HH:MM:SS)")
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + float(p)
    return seconds + 1.0 if end and "." not in str(value) else seconds


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


MATCH_TOLERANCE = 1.5   # seconds; MM:SS in the transcript is floored, not rounded


def _turn_around(cues: list[Cue], cue: Cue) -> list[Cue]:
    """Every cue that will end up in the same printed turn as this one.

    `at` names a moment in a transcript, where the smallest visible thing is a
    turn -- but a turn is cut into a cue per silence, so "26:28" can land on
    the "it" of "it is right." and move one word. Since group_turns reads this
    same ordered stream, the contiguous same-speaker run around a cue is
    exactly the turn a reader is pointing at.
    """
    i = cues.index(cue)
    lo = hi = i
    while lo > 0 and cues[lo - 1].who == cue.who:
        lo -= 1
    while hi + 1 < len(cues) and cues[hi + 1].who == cue.who:
        hi += 1
    return cues[lo:hi + 1]


def _move_range(cues: list[Cue], fix: dict) -> list[Cue]:
    """Give every word between `from` and `to` to another speaker.

    The general form: a misattribution is a stretch of audio, and nothing
    guarantees it starts or stops where a silence did. So this cuts cues at
    the range edges rather than moving whole ones -- a cue with words either
    side of the boundary becomes two or three, and only the middle changes
    hands. `at` remains for the common case of a whole turn.

    Membership is by where a word starts, so the rule is decidable by reading:
    a word is inside iff its first sound is.
    """
    lo, hi = parse_time(fix["from"]), parse_time(fix["to"], end=True)
    if hi <= lo:
        raise ValueError(f"speaker_corrections: {ts(lo)} is not before {ts(hi)}")
    track, who = fix.get("track"), fix["replace"]
    out: list[Cue] = []
    moved = 0

    for cue in cues:
        if (track and cue.track != track) or cue.end < lo or cue.start > hi:
            out.append(cue)
            continue
        if not cue.words:                    # no alignment: all of it, or none
            if lo <= cue.start and cue.end <= hi:
                out.append(replace(cue, who=who))
                moved += 1
            else:
                raise ValueError(
                    f"speaker_corrections: the cue at {ts(cue.start)} has no "
                    f"word timings, so {ts(lo)}-{ts(hi)} cannot cut it -- give "
                    f"a range that covers it whole")
            continue

        runs: list[tuple[bool, list[dict]]] = []
        for w in cue.words:
            inside = lo <= w["start"] <= hi
            if runs and runs[-1][0] == inside:
                runs[-1][1].append(w)
            else:
                runs.append((inside, [w]))
        for inside, words in runs:
            out.append(replace(
                cue, start=words[0]["start"], end=words[-1]["end"], words=words,
                text=" ".join(w["word"] for w in words).strip(),
                who=who if inside else cue.who))
            moved += inside

    if not moved:
        raise ValueError(f"speaker_corrections: no words between "
                         f"{ts(lo)} and {ts(hi)}"
                         + (f" on track {track}" if track else ""))
    return out


def _cues_at(cues: list[Cue], fix: dict) -> list[Cue]:
    """The cues one `at` entry addresses: the whole turn around that moment.

    Distance is measured to the cue's whole span, not its start, so a time
    copied out of the transcript lands inside the turn it names. Ambiguity is
    an error rather than a guess: silently relabelling the wrong cue is the
    exact failure this mechanism exists to prevent.
    """
    at = parse_time(fix["at"])
    track = fix.get("track")
    pool = [c for c in cues if not track or c.track == track]
    distance = lambda c: 0.0 if c.start <= at <= c.end else min(abs(c.start - at),
                                                                abs(c.end - at))
    near = [c for c in pool if distance(c) <= MATCH_TOLERANCE]
    if not near:
        raise ValueError(f"speaker_corrections: no cue near {ts(at)}"
                         + (f" on track {track}" if track else ""))
    best = min(distance(c) for c in near)
    hits = [c for c in near if distance(c) == best]
    if len(hits) > 1:
        where = ", ".join(f"{c.track} {ts(c.start)} ({c.who})" for c in hits)
        raise ValueError(f"speaker_corrections: {ts(at)} is ambiguous between "
                         f"{where} -- add \"track\", or \"through\" to take both")
    return _turn_around(cues, hits[0])


def correct_speakers(cues: list[Cue], config: dict) -> list[Cue]:
    """Settle disputed speaker labels before turns are grouped, not after.

    Applied at render time this looked right and read wrong: relabelling a
    stray word to the speaker either side of it left three turns where there
    should be one, because grouping had already happened and could not be
    undone. Doing it here means the corrected cue simply belongs to its
    neighbour's turn.

    Three ways to say which words. A `pattern` rewrites the label wherever it
    matches -- which settles a `<ADA? | BO?>` marker, because reconciling two
    recordings gives the disputed cue a label that is unique to it. A single
    recording has no such marker: its labels are just names, identical across
    every turn that speaker took, so a pattern cannot pick one out. Those are
    addressed by time:

        {"from": "26:28", "to": "26:29", "replace": "Bo"}   # these words
        {"at": "26:28", "replace": "Bo"}                    # this turn

    A range is the honest primitive -- a stretch of audio belongs to whoever
    spoke it, whatever the transcript's paragraph breaks say. `at` is the
    shorthand for when the stretch is exactly one turn, which it usually is.
    """
    fixes = config.get("speaker_corrections", [])
    if not fixes:
        return cues
    for f in fixes:
        if ("from" in f) != ("to" in f):
            raise ValueError(f"speaker_corrections: a range needs both "
                             f"\"from\" and \"to\": {f}")
        if not ({"from", "at", "pattern"} & set(f)):
            raise ValueError(f"speaker_corrections needs \"from\"/\"to\", "
                             f"\"at\" or \"pattern\": {f}")
    # Overlapping ranges are the one mistake here that looks right in the
    # config and goes wrong only in the output: the later rule silently takes
    # back words the earlier one moved, and the block between them vanishes
    # from the transcript. Refining a decision by partitioning a block is the
    # recommended pattern, so writing ranges nose to tail is the normal thing
    # to do -- and a `to` covers its whole second while a `from` starts at the
    # top of one, so nose to tail overlaps by a second. Caught before anything
    # is written, rather than left for the reader to spot a missing speaker.
    # Compared as open intervals: `to` runs to the top of the next second, so
    # ranges on adjacent seconds touch at one instant by construction, and
    # that is the partition pattern working as intended, not a mistake.
    ranges = [(parse_time(f["from"]), parse_time(f["to"], end=True), f)
              for f in fixes if "from" in f]
    for i, (lo_a, hi_a, a) in enumerate(ranges):
        for lo_b, hi_b, b in ranges[i + 1:]:
            if not (lo_a < hi_b and lo_b < hi_a):
                continue
            track_a, track_b = a.get("track"), b.get("track")
            if track_a and track_b and track_a != track_b:
                continue
            raise ValueError(
                f"speaker_corrections: the ranges {a['from']}-{a['to']} and "
                f"{b['from']}-{b['to']} overlap, so the later one would take "
                f"back words the earlier one moved and the block between them "
                f"would vanish from the transcript. A time written MM:SS covers "
                f"that whole second, so ranges written nose to tail share one "
                f"-- give them distinct seconds.")

    for fix in [f for f in fixes if "from" in f]:
        cues = _move_range(cues, fix)
    timed = [f for f in fixes if "at" in f and "from" not in f]
    patterns = [f for f in fixes if "pattern" in f and "at" not in f
                and "from" not in f]

    retitled = {}
    for fix in timed:
        for cue in _cues_at(cues, fix):
            retitled[id(cue)] = fix["replace"]
    return [replace(c, who=retitled.get(
        id(c), apply_corrections(c.who or "", patterns, ignore_case=True)))
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

    def label(c: dict) -> str:
        """What the entry addressed -- a pattern's words, or a timestamp."""
        if "from" in c:
            when = f"{ts(parse_time(c['from']))}-{ts(parse_time(c['to']))}"
        elif "at" in c:
            when = ts(parse_time(c["at"]))
        else:
            return show(c["pattern"])
        return f"@{when}" + (f" [{c['track']}]" if c.get("track") else "")
    # One long alternation should not stretch every other row.
    width = min(max((len(label(c))
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
            out.append(f"  {label(c):<{width}}  ->  {c['replace']}{note}")
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
    markers = sorted(config.get("markers", []), key=lambda m: parse_time(m["at"]))

    out: list[str] = list(config.get("header", []))
    if config.get("header"):
        out += ["", "=" * 78, ""]

    pending = list(markers)
    for turn in turns:
        while pending and turn.start > parse_time(pending[0]["at"]):
            m = pending.pop(0)
            out += ["=" * 78, "", f"### {ts(parse_time(m['at']))} - {m['text']}", "",
                    "=" * 78, ""]
        text = apply_corrections(turn.text, corrections)
        out += [f"[{ts(turn.start)}-{ts(turn.end)}]  {(turn.who or '').upper()}",
                "", text, ""]

    # A list is used verbatim; "auto" or nothing at all derives the audit trail.
    footer = config.get("footer", "auto")
    out += list(footer) if isinstance(footer, list) else derive_footer(config)
    return "\n".join(out)
