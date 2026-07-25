---
name: otter-cleanup
description: Review a transcript — named as an argument, or the most recent — and propose corrections — mishearings, wrong names, and the [a? | b?] and <A? | B?> markers left where two recordings disagreed. Use when the user says /otter-cleanup, or asks to clean up, correct, fix or proofread a transcript produced by otter-transcribe.
---

# Otter cleanup

The one step here that costs tokens. `otter-transcribe` does everything else in
Python precisely so this pass can afford to read the transcript.

**You cannot hear the audio.** Everything below follows from that: you can see
what looks wrong, and you can often tell which of two alternatives fits, but you
cannot verify a word. So the output is proposals recorded as data, never a
rewritten transcript.

## The order

1. Read the transcript the user named, and its config.
2. Work out who the unnamed speakers are.
3. Note mishearings, and resolve what you can of the disagreement markers.
4. **Ask about everything you could not settle.** Then wait.
5. Write all of it into the config — names, corrections — in one pass.
6. Re-merge **once**, writing `transcript-clean.txt` into the same folder.
   `transcript.txt` is never overwritten.
7. Report what changed and what you left.

Nothing is written until step 5 and nothing runs until step 6. Do not merge
after each idea.

## Which transcript

Whichever one the user named — a path, a folder, or "the one you just made".
Only if they named none:

- a transcript produced earlier in this conversation — use that one
- otherwise `ls -td transcripts/*/ | head -3`, take the newest, and say which
  you picked before starting

Each run has its own dated folder holding everything it produced:

```
transcripts/<date>-<id>/
    transcript.txt        what the machine produced
    config.json           every decision, as data
    <otid>.json           what Otter returned, one per recording
```

Read `transcript.txt` and `config.json`; nothing else.

## Name the speakers first

A transcript often arrives with `Speaker 1`, `Speaker 2` — Otter separated the
voices but has no voiceprint for those people. Nothing but the conversation can
say who they are, which is why this is your job and not the tool's.

Read for evidence, strongest first:

- **Someone is addressed by name and answers.** "What do you think, Ada?"
  followed by `Speaker 2` speaking makes `Speaker 2` Ada.
- **Someone introduces themselves**, or is introduced.
- **Someone is referred to in the third person while another speaker talks** —
  that person is *not* the one speaking.
- **Role or content.** The person who opens the meeting, owns the agenda,
  answers questions about their own work.

Write the mapping into `aliases` in the config. Match the shape already there:
nested under each track name, or flat.

```json
"aliases": {"<track>": {"Speaker 1": "Ada", "Speaker 2": "Bo"}}
```

Say which name rests on what, and **leave anyone you cannot place as they
are.** A confidently wrong name is far worse than `Speaker 2`: it is invisible
once applied, and it propagates into anything built on the transcript. If
nobody is named anywhere in the conversation, say so — do not reach for the
filename, the meeting title or a guess.

Cluster numbers belong to one transcription, not to the audio. A mapping
written for one set of documents may not hold for a later re-upload of the same
recording, so read the first line of each speaker rather than reusing an old
config.

Offer this once names are settled: `uv run python -m otter.fetch tag <otid>
"Speaker 1=Ada"` tells Otter, and it then recognises that person in future
recordings without any of this. It queues a re-match taking 10–15 minutes.

## Propose corrections as config entries

Read the transcript, then add to `corrections` in its config:

```json
{"pattern": "\\bGodal\\b", "replace": "Gödel", "note": "Cam, ~33:26"}
```

These accumulate in the config; nothing is applied until the final merge.

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
whether it is an interjection. If the surrounding turns make it obvious, record
it in `speaker_corrections`, which fixes the label rather than the words:

```json
"speaker_corrections": [
  {"pattern": "<ADA\\? \\| BO\\?>", "replace": "Bo",
   "note": "same sentence as the following turn"}
]
```

Write the pattern exactly as the transcript prints it; matching ignores case.
If the surrounding turns do not settle it, leave it — a wrong speaker is worse
than a visible question mark.

A whole label being wrong is an `aliases` fix, not a correction.

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

## Ask before applying

You will finish step 3 with two piles: calls you can defend, and calls you
cannot. Make the first pile. **Bring the second one back to the user**, before
writing anything, and wait for an answer.

Ask about:

- a speaker you cannot place, or can only place weakly — say what the evidence
  was and what it does not settle
- a marker where both alternatives are plausible in the sentence
- anything that reads as wrong where you have no candidate: a passage that
  seems to be missing a word, a name spelled two ways, a speaker who appears to
  answer their own question

Do not ask about the obvious ones. If someone is addressed by name and replies,
that is a speaker named, not a question. Ask once, as a short list, rather than
one at a time — the user was in the room and can answer all of it in a breath.

If they do not answer, or say to skip it, leave those items alone and note them
in the report. `UNRESOLVED` in a correction note is for something you changed
in order to flag it; something you did not change is simply left, and mentioned.

## Then apply it, once

Edit the config, then re-merge offline — no network, no re-upload:

```bash
cd transcripts/<date>-<id>
uv run python -m otter.fetch merge *.json -c config.json -o transcript-clean.txt
```

**Write to `transcript-clean.txt`. Never overwrite `transcript.txt`.** That file is what the
machine produced before anyone touched it, and it is the only way to see what
your changes actually did — `diff` the two, and every difference is something
you proposed. Overwriting it destroys the baseline and makes a wrong correction
undetectable.

That one command handles either kind of recording. Do not reach for
`otter.speech` or `otter.reconcile` directly, and do not use `run` or `pull` —
both would go back to Otter for documents you already have.

## Report

Say what you changed and what you left. Something like: *12 corrections
proposed, 3 markers resolved, 9 left as filler, 1 speaker disagreement judged
from context.* Then re-run the merge and confirm the transcript regenerated.

Raise anything that struck you as odd, even where you had no fix — a passage
that reads as though a word is missing, a name spelled two ways, a speaker who
appears to answer their own question. The owner was in the room and can settle
in a second what no amount of reading can.
