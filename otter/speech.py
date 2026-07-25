#!/usr/bin/env python3
"""Build transcript cues from Otter's per-word timings.

Each segment of a speech document carries an `alignment` array with a start
and end for every word, relative to the segment, and a `speaker_id` resolving
against the document's `speakers` list. Two things follow, and both matter for
interleaving several microphones into one conversation.

Every segment says who spoke, so nothing has to be carried forward from an
earlier label. And turn boundaries are found rather than assumed: a gap longer
than `gap_seconds` between consecutive words means the speaker stopped, which
on a single-participant track is where somebody else was speaking.

How far the timings can be trusted: not very, word by word. A word's duration
is a flat 0.03s per character, so the position of any one word inside a run of
speech is interpolated rather than measured. The silences between runs are
real -- they reach 115 seconds on a 94-minute call, which no interpolation
produces -- and silences are all the gap test uses.

What it does not fix: a segment Otter would not attribute. Where it recognised
nobody at all, its diarisation clusters still separate the speakers and become
"Speaker 1", "Speaker 2" for `aliases` to name. Where it recognised some
people, a leftover cluster means it declined to place those words, and they
stay unattributed rather than inventing a participant.

Still assumed: tracks share a common t=0, which Zoom guarantees. Separate
devices do not, and are handled by `otter/reconcile.py` instead.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from otter.transcript import (Cue, Track, correct_speakers, find_bleed, group_turns,
                              merge_cues, render_text, ts)

# Otter reports segment offsets in samples at this rate.
SAMPLE_RATE = 16000
# A silence longer than this between consecutive words ends a turn. Small on
# purpose: `group_turns` re-merges consecutive same-speaker cues, so an
# unnecessary split costs nothing while a missing one strands words in the
# wrong place. Swept on a 94-min two-track call: words stranded behind an
# interruption run 46-54 across 0.2-0.35s and climb steeply after (309 at
# 1.0s), while below 0.2s splitting inside phrases makes it worse again.
# 0.25s sits in that plateau and is about one natural inter-word pause.
GAP_SECONDS = 0.25
# What to call a segment Otter would not attribute.
UNATTRIBUTED = "Unattributed"
UNATTRIBUTED_ALIASES = {"unattributed", "unknown"}
_SPEAKER_N = re.compile(r"(?i)^speaker[\s_-]*\d*$")


def is_placeholder(name: str, known: dict[int, str]) -> bool:
    """True for a label that stands in for a person rather than naming one."""
    if name in known.values():
        return False
    return name.lower() in UNATTRIBUTED_ALIASES or bool(_SPEAKER_N.match(name))


def speaker_names(speech: dict) -> dict[int, str]:
    return {s["id"]: s["speaker_name"] for s in (speech.get("speakers") or [])}


def observed_speakers(speech: dict) -> list[str]:
    """The labels this recording will produce, for scaffolding an alias map."""
    names = speaker_names(speech)
    anonymous = not names
    seen = []
    for segment in speech.get("transcripts") or []:
        cluster = segment.get("speaker_model_label")
        label = (names.get(segment.get("speaker_id"))
                 or (f"Speaker {cluster}" if anonymous and cluster else UNATTRIBUTED))
        if label not in seen:
            seen.append(label)
    return seen


def segment_speaker(segment: dict, names: dict[int, str], anonymous: bool) -> str:
    """Who Otter says spoke this segment, falling back to its diarisation.

    Otter names a speaker only when a voiceprint matches. Without one it still
    separates people correctly, exposing an anonymous cluster per person in
    `speaker_model_label`. Only trusted when Otter recognised nobody: once some
    speakers ARE named, a leftover cluster means the diariser declined to place
    those words, and calling it "Speaker 4" would invent a participant.
    """
    cluster = segment.get("speaker_model_label")
    return (names.get(segment.get("speaker_id"))
            or (f"Speaker {cluster}" if anonymous and cluster else UNATTRIBUTED))


def cues_from_speech(speech: dict, track: str, gap_seconds: float = GAP_SECONDS,
                     aliases: dict[str, str] | None = None) -> list[Cue]:
    """Split each segment into utterances at silences, timed by its words.

    Falls back to the segment's own offsets when a segment has no `alignment`
    -- older recordings may predate it -- so the segment keeps its own coarse
    start and end rather than being dropped.
    """
    aliases = aliases or {}
    names = speaker_names(speech)
    segments = speech.get("transcripts") or []
    # Otter names a speaker only when a voiceprint matches. Without one it
    # still diarises correctly, exposing an anonymous cluster per person in
    # `speaker_model_label` -- so a recording of untagged people separates
    # fine and needs only names, which `aliases` can supply locally:
    #     "aliases": {"track": {"Speaker 1": "Ada", "Speaker 2": "Bo"}}
    # Only trusted when Otter recognised nobody. Once some speakers ARE named,
    # a leftover cluster means the diariser declined to place those words, and
    # calling that "Speaker 4" would invent a participant.
    anonymous = not names
    cues: list[Cue] = []

    for segment in segments:
        start = segment["start_offset"] / SAMPLE_RATE
        end = segment["end_offset"] / SAMPLE_RATE
        attributed = bool(segment.get("speaker_id"))
        raw = segment_speaker(segment, names, anonymous)
        # "Speaker 1" and "Unattributed" are track-local: Speaker 1 here is a
        # different person from Speaker 1 on the next track, so they must be
        # qualified or group_turns fuses two strangers into a single turn.
        # The test is whether the *resolved* name is still a placeholder, not
        # whether an alias fired -- a scaffolded config maps "Speaker 1" to
        # itself, which is an alias hit that resolves nothing. Real names pass
        # through untouched, since one person may appear on several tracks.
        who = aliases.get("*") or aliases.get(raw) or raw
        if is_placeholder(who, names):
            who = f"{track} {who}"
        words = segment.get("alignment") or []

        if not words:
            cues.append(Cue(track, len(cues) + 1, start, end,
                            segment["transcript"].strip(), who, attributed, 0))
            continue

        run = [words[0]]
        for previous, current in zip(words, words[1:]):
            if current["start"] - previous["end"] > gap_seconds:
                cues.append(_cue(track, len(cues) + 1, start, run, who,
                                 attributed, len(words)))
                run = []
            run.append(current)
        if run:
            cues.append(_cue(track, len(cues) + 1, start, run, who,
                                 attributed, len(words)))
    return cues


def _cue(track: str, idx: int, base: float, words: list[dict], who: str,
         attributed: bool, segment_words: int) -> Cue:
    text = " ".join(w["word"] for w in words).strip()
    return Cue(track, idx, base + words[0]["start"], base + words[-1]["end"],
               text, who, attributed, segment_words)


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def tracks_from_speeches(speeches: dict[str, dict], config: dict) -> list[Track]:
    """`speeches` maps otid -> speech JSON. Names come from config['tracks']."""
    names = config.get("tracks", {})
    gap = float(config.get("gap_seconds", GAP_SECONDS))
    tracks = []
    for otid, speech in speeches.items():
        name = names.get(otid, otid)
        tracks.append(Track(name, cues_from_speech(
            speech, name, gap, config.get("aliases", {}).get(name, {}))))
    return tracks


def drop_bleed(cues: list[Cue], max_offset: float = 5.0) -> list[Cue]:
    """Remove one copy of the same words captured on two tracks at once.

    When someone's voice comes out of another participant's speakers and back
    into their mic, Otter transcribes it on both tracks, each time crediting
    that track's own speaker -- so keeping the wrong copy puts words in
    someone else's mouth.

    Of the pair, drop the one Otter would not attribute. A voice arriving
    through someone else's speakers is band-limited and does not match that
    mic owner's voiceprint, so the diariser isolates it and leaves it unnamed,
    while the person who actually spoke is named on their own track. On a
    94-minute two-mic call every unattributed word on the single-participant
    track was bleed, and every word it named was that participant.

    When that does not separate them -- both named, or neither -- there is no
    acoustic signal left in the text, so it falls back to segment size and
    then to whichever arrived later. Those cases are guesses; they cost one
    short interjection attributed to the wrong person.
    """
    doomed = set()
    for h in find_bleed_pairs(cues, max_offset):
        a, b = h.a, h.b
        rank = lambda c: (c.attributed, c.segment_words, len(c.text), -c.start)
        loser = a if rank(a) < rank(b) else b
        doomed.add(id(loser))
    return [c for c in cues if id(c) not in doomed]


def find_bleed_pairs(cues: list[Cue], max_offset: float):
    by_track: dict[str, list[Cue]] = {}
    for c in cues:
        by_track.setdefault(c.track, []).append(c)
    return [h for h in find_bleed([Track(n, cs) for n, cs in by_track.items()])
            if h.kind == "duplicate" and abs(h.offset) <= max_offset]


def build(tracks: list[Track], config: dict):
    """Interleave the tracks into one stream and group it into turns."""
    cues = merge_cues(tracks, config.get("drop"))
    if config.get("auto_drop_bleed", True):
        cues = drop_bleed(cues, float(config.get("bleed_max_offset", 5.0)))
    cues.sort(key=lambda c: (c.start, c.track))
    return cues, group_turns(correct_speakers(cues, config))


# --------------------------------------------------------------------------
# CLI (offline: operates on saved speech JSON)
# --------------------------------------------------------------------------

def cmd_merge(args) -> int:
    config = json.loads(Path(args.config).read_text()) if args.config else {}
    speeches = {}
    for path in args.inputs:
        doc = json.loads(Path(path).read_text())
        # A wildcard easily sweeps up the config itself. Say which file and
        # why, rather than failing later on an empty track with an IndexError.
        if not isinstance(doc, dict) or not doc.get("transcripts"):
            raise SystemExit(
                f"{path}: not an Otter speech document (no 'transcripts').\n"
                "Inputs are the .json files written by `otter.fetch pull -d`; "
                "the config goes after -c.")
        speeches[Path(path).stem] = doc
    tracks = tracks_from_speeches(speeches, config)
    for track in tracks:
        print(f"  {track.name}: {len(track.cues)} cues, "
              f"{ts(track.cues[0].start)}-{ts(track.cues[-1].end)}", file=sys.stderr)
    cues, turns = build(tracks, config)
    text = render_text(turns, config)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}: {len(turns)} turns, {ts(turns[-1].end)} total",
              file=sys.stderr)
    else:
        print(text)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="+", help="saved speech JSON files")
    parser.add_argument("-c", "--config")
    parser.add_argument("-o", "--output")
    return cmd_merge(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
