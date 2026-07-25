#!/usr/bin/env python3
"""Reconcile two recordings of the same room into one transcript.

Different problem from `otter/speech.py`. There, each track is one person's
microphone, every track hears mostly one voice, and the job is to interleave
them. Here two devices sit in a room and both hear *everyone*, so each track
already holds the whole conversation and the job is to reconcile two accounts
of it.

Measured on five minutes of a real two-microphone meeting, microphones at
opposite ends of a table: 704 words agreed, 26 appeared on only one track,
and 12 disagreed. So neither effect is large, and the two want opposite
treatment:

    only one track has words here   -> take them; there is nothing to decide
    both have words and they differ -> emit [a? | b?] and let a human or an
                                       LLM settle it against the audio

Those are the `delete`/`insert` and `replace` cases of one word alignment, so
a single pass does both. Most of the disagreements are function words and a
few change the meaning; none are filtered, because a spurious marker costs a
glance and a missing one costs a wrong transcript.

The clock
---------
Two phones started by hand do not share a t=0, and nothing else in this
package can detect that. It is recoverable from the text: align the two word
sequences by content, then read off the median time difference between matched
words. On a pair offset by exactly 5.000s that recovered +5.180s from 697 of
732 matched words, with a 30ms spread; on a synthetic pair offset by 3.000s,
+3.000s.

That works despite Otter's per-word timings being *interpolated* rather than
measured -- word duration is a flat 0.03s per character -- because both tracks
interpolate the same way, so the error is common to both and cancels in the
difference. Do not mistake this for the timings being accurate; only the
silences between speech runs are real.

Any number of recordings works: each is folded into the running result, so a
third is reconciled against the outcome of the first two. Alternatives then
accumulate side by side rather than nesting, and collapse back to a single
name if a later device resolves the disagreement.

Usage
-----
    python -m otter.reconcile a.json b.json [c.json ...] -c config.json -o out.txt
    python -m otter.reconcile a.json b.json --offset-only

`otter.fetch run` and `pull` choose between this and `otter.speech`
automatically, from how much text the tracks share.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path

from otter.speech import (SAMPLE_RATE, UNATTRIBUTED, is_placeholder,
                          segment_speaker, speaker_names)
from otter.transcript import (Cue, Track, correct_speakers, group_turns,
                              render_text, ts)

# Minimum matched run length to trust when estimating the offset.
MIN_RUN = 3


@dataclass
class Word:
    text: str        # as transcribed, with punctuation
    key: str         # normalised, for alignment
    start: float
    speaker: str | None


def aliases_for(config: dict, track: str) -> dict[str, str]:
    """The alias map that applies to one track, in either config shape.

    `scaffold` writes them nested under each track, because a label is
    track-local: one recording's "Speaker 1" need not be another's. A
    hand-written config for a single reconciled stream is often flat instead.
    Both are accepted -- silently ignoring one of them meant editing the names,
    re-running, and seeing no change and no error.
    """
    aliases = config.get("aliases", {})
    if aliases and all(isinstance(v, dict) for v in aliases.values()):
        return aliases.get(track, {})
    return aliases


def word_stream(speech: dict, offset: float = 0.0,
                aliases: dict[str, str] | None = None) -> list[Word]:
    """Flatten a speech document to words on an absolute clock.

    Uses the same speaker resolution as the interleaving path, so a recording
    of untagged people still separates them -- `aliases` then supplies real
    names. A flat map here rather than the per-track one `otter.speech` takes,
    because reconciliation produces a single stream.
    """
    aliases = aliases or {}
    names = speaker_names(speech)
    anonymous = not names
    out: list[Word] = []
    for segment in speech.get("transcripts") or []:
        base = segment["start_offset"] / SAMPLE_RATE + offset
        raw = segment_speaker(segment, names, anonymous)
        who = aliases.get(raw, raw)
        for w in (segment.get("alignment") or []):
            key = re.sub(r"[^a-z']", "", w["word"].lower())
            if key:
                out.append(Word(w["word"], key, base + w["start"], who))
    return out


# --------------------------------------------------------------------------
# clock
# --------------------------------------------------------------------------

@dataclass
class Offset:
    seconds: float
    matched: int
    total: int
    spread: float     # p90 - p10 of the per-word differences

    @property
    def share(self) -> float:
        return self.matched / self.total if self.total else 0.0

    @property
    def verdict(self) -> tuple[bool, str]:
        """Consistent enough to shift a whole track onto one clock?

        The discriminating property is *consistency*, not overlap: separate
        microphones do share a few words -- bleed -- but at wildly varying
        offsets, so the spread is hundreds of seconds. Two devices in one room
        agree at a single offset, within a few hundredths of a second.

        Both tests are proportional or physical, so neither depends on how
        long the recording is: 97% shared on a 32-word clip and 95% on a
        732-word one both pass, and an absolute floor on matched words only
        ever misjudged short recordings.
        """
        if self.share <= 0.3:
            return False, f"only {self.share:.0%} of the text is shared"
        if self.spread >= 2.0:
            return False, (f"{self.share:.0%} shared but the offsets disagree by "
                           f"{self.spread:.0f}s, so it is not one clock")
        return True, (f"{self.share:.0%} of the text is shared at a consistent "
                      f"{self.seconds:+.2f}s offset")

    @property
    def trustworthy(self) -> bool:
        return self.verdict[0]


def estimate_offset(a: list[Word], b: list[Word]) -> Offset:
    """How far b's clock runs behind a's, judged from where the words agree."""
    sm = SequenceMatcher(None, [w.key for w in a], [w.key for w in b], autojunk=False)
    blocks = [x for x in sm.get_matching_blocks() if x.size >= MIN_RUN]
    deltas = sorted(b[x.b + k].start - a[x.a + k].start
                    for x in blocks for k in range(x.size))
    if not deltas:
        return Offset(0.0, 0, min(len(a), len(b)), float("inf"))
    spread = deltas[9 * len(deltas) // 10] - deltas[len(deltas) // 10]
    return Offset(statistics.median(deltas), len(deltas), min(len(a), len(b)), spread)


# --------------------------------------------------------------------------
# reconcile
# --------------------------------------------------------------------------

@dataclass
class Merged:
    text: str
    start: float
    speaker: str | None
    source: str      # "both" | "a" | "b" | "speaker-conflict" | "conflict"
    key: str = ""    # normalised, so a merged stream can be reconciled again


def map_speakers(a: list[Word], b: list[Word], opcodes) -> dict[str, str]:
    """Work out which of b's speaker labels is which of a's.

    Each device is diarised independently, so its cluster numbers are its own:
    b's "Speaker 1" need not be a's "Speaker 1", and on a different recording
    it will not be. Taking a's label whenever both have one -- or applying a
    single alias map to both -- silently attributes words to whoever happens to
    sort first.

    The words the two tracks agree on say who is who: for each of b's labels,
    the label a used for the same words. Real names are left alone; a
    voiceprint match means the same person on any recording, which is the whole
    point of naming them.
    """
    votes: dict[str, Counter] = {}
    for tag, i1, i2, j1, j2 in opcodes:
        if tag != "equal":
            continue
        for k in range(i2 - i1):
            them, us = b[j1 + k].speaker, a[i1 + k].speaker
            if them and us and is_placeholder(them, {}):
                votes.setdefault(them, Counter())[us] += 1
    return {label: tally.most_common(1)[0][0] for label, tally in votes.items()}


def alternatives(marker: str, open_: str, close: str) -> list[str]:
    """The distinct claims inside a marker, or the value itself if it is not one."""
    if marker.startswith(open_) and marker.endswith(close):
        inner = marker[len(open_):-len(close)]
        return [x.strip().rstrip("?").strip() for x in inner.split("? |")]
    return [marker]


def combine(p: str, q: str, open_: str, close: str) -> tuple[str, bool]:
    """Union two claims, flattening any that are already markers.

    Folding a third recording in must not wrap the previous disagreement in a
    second set of brackets: a device that agrees with one of the existing
    alternatives adds no information, and one that offers a third belongs
    alongside them, not around them. If the union collapses to a single claim
    the disagreement is resolved and the marker disappears.
    """
    seen: list[str] = []
    for claim in alternatives(p, open_, close) + alternatives(q, open_, close):
        if claim and claim not in seen:
            seen.append(claim)
    if len(seen) == 1:
        return seen[0], False
    return open_ + "? | ".join(seen) + "?" + close, True


def pick_speaker(p: str | None, q: str | None) -> tuple[str | None, bool]:
    """Settle who spoke a word the two devices both heard.

    Only one of these is a real conflict. "Unattributed" is not a rival claim,
    it is the absence of one -- a device that could not place a voice against
    one that could, in which case the one that could is simply right. That is
    the common case: on a real two-microphone meeting all 24 differences were
    a name against Unattributed, and taking the first track's answer threw the
    name away.

    Two actual names, or two different clusters after mapping, are a genuine
    disagreement about who was speaking. Nothing in the text can settle it, so
    it is recorded rather than guessed, the same as a disputed word.
    """
    if p == q:
        return p, False
    if not p or p == UNATTRIBUTED:
        return q, False
    if not q or q == UNATTRIBUTED:
        return p, False
    # Angle brackets, distinct from the [a? | b?] used for disputed words, so
    # the two kinds of uncertainty can be grepped apart.
    return combine(p, q, "<", ">")


def reconcile(a: list[Word], b: list[Word], names: tuple[str, str]) -> list[Merged]:
    """One pass over the alignment, treating gaps and disagreements differently."""
    sm = SequenceMatcher(None, [w.key for w in a], [w.key for w in b], autojunk=False)
    opcodes = sm.get_opcodes()
    mapping = map_speakers(a, b, opcodes)
    if mapping:
        b = [replace(w, speaker=mapping.get(w.speaker, w.speaker)) for w in b]
    out: list[Merged] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            for k in range(i2 - i1):
                x, y = a[i1 + k], b[j1 + k]
                who, conflict = pick_speaker(x.speaker, y.speaker)
                out.append(Merged(x.text, x.start, who,
                                  "speaker-conflict" if conflict else "both", x.key))
        elif tag == "delete":      # only track a has words here
            out += [Merged(w.text, w.start, w.speaker, "a", w.key) for w in a[i1:i2]]
        elif tag == "insert":      # only track b
            out += [Merged(w.text, w.start, w.speaker, "b", w.key) for w in b[j1:j2]]
        else:                      # replace: both spoke, they disagree
            left = " ".join(w.text for w in a[i1:i2])
            right = " ".join(w.text for w in b[j1:j2])
            # Key on the first alternative so a further track can still align
            # here; without a key the marker would look like unseen words and
            # be duplicated rather than matched.
            text, _ = combine(left, right, "[", "]")
            out.append(Merged(text, a[i1].start,
                              a[i1].speaker or b[j1].speaker, "conflict",
                              a[i1].key if i2 > i1 else b[j1].key))
    out.sort(key=lambda m: m.start)
    return out


def as_words(merged: list[Merged]) -> list[Word]:
    """Turn a reconciled stream back into words, so it can be reconciled again."""
    return [Word(m.text, m.key, m.start, m.speaker) for m in merged]


def earliest_first(streams: list[list[Word]],
                   names: list[str]) -> list[list[Word]]:
    """Put the recording whose content starts earliest at the front.

    Reconciliation shifts every track onto the first one's clock, so that
    track's timeline becomes the output's, and passing the same recordings in
    another order must not move a word -- a glob hands them over
    alphabetically, which is nobody's recording order.

    Whichever has the earliest first word leads, so the transcript begins where
    the conversation does rather than after another device's leading silence.
    The filename settles a tie, and ties are the normal case: Zoom writes every
    track from a common t=0. Each recording is saved as its otid, so the name
    identifies it for as long as it exists.
    """
    def rank(pair: tuple[str, list[Word]]) -> tuple:
        name, words = pair
        return ((words[0].start if words else float("inf")), name)

    return [w for _, w in sorted(zip(names, streams), key=rank)]


def fold(streams: list[list[Word]], names: list[str],
         log=lambda m: None) -> list[Merged]:
    """Reconcile any number of recordings of one room, two at a time.

    Each track is folded into the running result, so the second device is
    reconciled against the first, the third against that outcome, and so on.
    Order matters only in that the first track supplies the clock everything
    else is shifted onto.
    """
    if len(streams) < 2:
        raise ValueError("reconciliation needs at least two recordings")
    streams = earliest_first(streams, names)
    acc = streams[0]
    merged: list[Merged] = []
    for n, nxt in enumerate(streams[1:], start=2):
        est = estimate_offset(acc, nxt)
        ok, why = est.verdict
        if not ok:
            raise OtterMismatch(f"track {n} does not match the others: {why}")
        log(f"  track {n}: {why}")
        merged = reconcile(acc, [replace(w, start=w.start - est.seconds) for w in nxt],
                           ("merged", f"track{n}"))
        acc = as_words(merged)
    return merged


class OtterMismatch(RuntimeError):
    pass


def cues_from_merged(merged: list[Merged], track: str) -> list[Cue]:
    """Cut the reconciled stream into cues, one per run of a single speaker.

    No silence threshold. A reconciled transcript is one stream, so a pause is
    just a pause -- the same person either side of it is still the same turn.
    Interleaving needs `gap_seconds` because a pause there is where somebody
    else's words have to land; here nothing lands in it.
    """
    cues: list[Cue] = []
    run: list[Merged] = []

    def flush() -> None:
        if run:
            cues.append(Cue(track, len(cues) + 1, run[0].start, run[-1].start,
                            " ".join(m.text for m in run),
                            run[0].speaker or UNATTRIBUTED))

    for m in merged:
        if run and m.speaker != run[0].speaker:
            flush()
            run = []
        run.append(m)
    flush()
    return cues


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inputs", nargs="+", metavar="SPEECH_JSON",
                   help="two or more recordings of the same room")
    p.add_argument("-c", "--config")
    p.add_argument("-o", "--output")
    p.add_argument("--offset", type=float,
                   help="skip estimation and shift the second track by this many seconds")
    p.add_argument("--offset-only", action="store_true",
                   help="report the estimated clock offset and stop")
    args = p.parse_args()

    docs = []
    for path in args.inputs:
        doc = json.loads(Path(path).read_text())
        if not isinstance(doc, dict) or not doc.get("transcripts"):
            raise SystemExit(f"{path}: not an Otter speech document")
        docs.append(doc)

    config = json.loads(Path(args.config).read_text()) if args.config else {}
    names = [Path(x).stem for x in args.inputs]
    if len(docs) > 2:
        if args.offset is not None:
            raise SystemExit("--offset applies to a single pair; omit it for more")
        merged = fold([word_stream(d, aliases=aliases_for(config, n))
                       for d, n in zip(docs, names)], names,
                      log=lambda m: print(m, file=sys.stderr))
        return _render(merged, args, config)

    a = word_stream(docs[0], aliases=aliases_for(config, names[0]))
    raw_b = word_stream(docs[1], aliases=aliases_for(config, names[1]))
    if args.offset is None:
        est = estimate_offset(a, raw_b)
        print(f"  offset {est.seconds:+.3f}s from {est.matched}/{est.total} matched "
              f"words, spread {est.spread:.2f}s", file=sys.stderr)
        if not est.trustworthy:
            raise SystemExit(
                "  refusing to merge: the tracks do not agree well enough to "
                "locate a common clock.\n  Are they the same recording? Pass "
                "--offset SECONDS to override.")
        shift = est.seconds
    else:
        shift = args.offset

    b = word_stream(docs[1], -shift, aliases_for(config, names[1]))
    merged = reconcile(a, b, (Path(args.inputs[0]).stem, Path(args.inputs[1]).stem))
    counts = {k: sum(1 for m in merged if m.source == k)
              for k in ("both", "a", "b", "conflict")}
    print(f"  {counts['both']} agreed, {counts['a']} only from the first track, "
          f"{counts['b']} only from the second, {counts['conflict']} disagreements",
          file=sys.stderr)
    if args.offset_only:
        return 0
    return _render(merged, args, config)


def _render(merged: list[Merged], args, config: dict) -> int:
    turns = group_turns(correct_speakers(
            cues_from_merged(merged, "reconciled"), config))
    text = render_text(turns, config)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}: {len(turns)} turns, {ts(turns[-1].end)} total",
              file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
