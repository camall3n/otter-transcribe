---
name: otter-cleanup
description: Review a merged transcript and propose corrections — mishearings, wrong names, and the [a? | b?] and <A? | B?> markers left where two recordings disagreed. Use when the user says /otter-cleanup, or asks to clean up, correct, fix or proofread a transcript produced by otter-transcribe.
---

# Otter cleanup

The one step here that costs tokens. `otter-transcribe` does everything else in
Python precisely so this pass can afford to read the transcript.

**You cannot hear the audio.** Everything below follows from that: you can see
what looks wrong, and you can often tell which of two alternatives fits, but you
cannot verify a word. So the output is proposals recorded as data, never a
rewritten transcript.

## Propose corrections as config entries

Read the transcript, then add to `corrections` in its config:

```json
{"pattern": "\\bGodal\\b", "replace": "Gödel", "note": "Cam, ~33:26"}
```

Re-run the merge to apply them:

```bash
uv run python -m otter.speech transcripts/<otid>.json ... -c <cfg> -o <out>
# or, for a one-room recording
uv run python -m otter.reconcile transcripts/<otid>.json ... -c <cfg> -o <out>
```

Why data rather than an edited file: a regex is reviewable, reversible, and
re-runnable against a better transcript later. The footer of the transcript is
generated from these same entries, so the record of what changed cannot drift
from what actually ran. Editing the prose directly would launder guesses into
the transcript with nothing to audit.

Where a correction is a guess rather than a fix, begin its note with
`UNRESOLVED`. Those are listed separately, as open questions rather than
repairs.

## Resolve the disagreement markers

A transcript merged from several recordings of one room carries two kinds of
marker. They are there because the tool refused to guess; you may be able to do
better, from context it does not have.

**`[Goodhart's? | Goodhurt's?]` — the devices heard different words.** Usually
one alternative is a real word or a name that fits the sentence and the other is
not. Record the choice as a correction whose pattern matches the whole marker:

```json
{"pattern": "\\[Goodhart's\\? \\| Goodhurt's\\?\\]", "replace": "Goodhart's",
 "note": "far mic mis-heard; matches the topic"}
```

Some markers genuinely do not matter: `[uh? | um?]`, `[mm? | mm-hmm?]` and
other filler where either transcription is equally true. Say so once and move
on rather than writing an entry for each. Do not put a pair in that bucket
because it looks small — `[our? | are?]` reads minor but the sentence almost
always settles it, and those are worth resolving.

**`<ADA? | BO?>` — the devices disagreed about who spoke.** Judge from the
conversation: who is mid-sentence either side of it, who is being addressed,
whether it is an interjection. If the surrounding turns make it obvious, propose
the name. If they do not, leave it — a wrong speaker is worse than a visible
question mark.

Speaker fixes belong in `aliases` if the whole label is wrong, not in
`corrections`.

## What not to do

- Do not smooth, tidy or summarise. The transcript is verbatim on purpose;
  disfluencies and false starts are content, not noise.
- Do not silently change a word because it is unusual. Jargon, names and
  technical terms are exactly what a transcript is for. Flagging one is
  welcome — ask, or record it with an `UNRESOLVED` note; only quiet
  substitution is the problem.
- Do not resolve a marker by picking the first alternative because it is first.
  If you have no reason, say you have no reason.
- Do not rewrite the transcript file directly, even for a single word.

## Report

Say what you changed and what you left. Something like: *12 corrections
proposed, 3 markers resolved, 9 left as filler, 1 speaker disagreement judged
from context.* Then re-run the merge and confirm the transcript regenerated.

Raise anything that struck you as odd, even where you had no fix — a passage
that reads as though a word is missing, a name spelled two ways, a speaker who
appears to answer their own question. The owner was in the room and can settle
in a second what no amount of reading can.
