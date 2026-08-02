---
name: otter-cleanup
description: Review a transcript—named as an argument, or the most recent—and propose corrections—mishearings, wrong names, and the [a? | b?] and <A? | B?> markers left where two recordings disagreed. Use when the user says /otter-cleanup, or asks to clean up, correct, fix or proofread a transcript produced by otter-transcribe.
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
2. Infer what the recording was called, from the config and the folder.
3. Work out who the unnamed speakers are.
4. Note mishearings, and resolve what you can of the disagreement markers.
5. **Ask about everything you could not settle**, and confirm the inferred
   name while you are there. Then wait.
6. Write all of it into the config—names, corrections—in one pass.
7. Re-merge **once**, writing `<recording>.txt` into the same folder.
   `transcript.txt` is never overwritten.
8. Report what changed and what you left.

Nothing is written until step 6 and nothing runs until step 7. Do not merge
after each idea.

## Which transcript

Whichever one the user named—a path, a folder, or "the one you just made".
Only if they named none:

- a transcript produced earlier in this conversation—use that one
- otherwise `ls -td transcripts/*/ | head -3`, take the newest, and say which
  you picked before starting

Each run has its own dated folder holding everything it produced:

```
transcripts/<date>-<id>/
    transcript.txt        what the machine produced
    config.json           every decision, as data
    <otid>.json           what Otter returned, one per recording
    <recording>.m4a       the audio, where it has been moved in beside them
```

Read `transcript.txt` and `config.json`; nothing else.

## Work out what the recording was called

**Infer it; do not ask.** The folder is named for the day the audio was
transcribed, which is not the day the conversation happened, and nothing inside
the transcript says where the audio came from. The name a person gave the
recording usually carries both: `audio-2026-06-01-ada-bo.m4a` holds a date
and a roster that the transcript cannot.

In order of directness:

- `config.json` → `tracks`, whose values are the file stems Otter was given
- an audio file sitting in the run folder beside the transcript
- the folder or transcript path the user named when they invoked you

**There may be more than one.** A room recorded on two phones arrives as two
tracks, and the interesting part is what they share: `...-mic0-seminar-where-do-...`
and `...-mic1-seminar-where-do-...` want the common stem, with the per-device part
dropped. Earlier runs already do this—see
`audio-2026-07-27-web-manifesto-ada-bo.txt`.

**Confirm what you inferred** in the same batch of questions you were going to
ask anyway (below); it costs the user nothing to glance at. Only when the names
are too generic to infer from—`recording.m4a`, `audio1.m4a`, `Zoom_0.mp4`—ask,
and ask for the *output filename* you should use rather than for the recording's.

What a filename settles, and what it does not:

- **The date.** Record it in the config `_comment`, because the folder name
  will disagree and the filename is the one that means anything. A transcript
  nobody can date is much less useful a year later.
- **The roster.** Knowing which names to expect makes the conversational
  evidence easier to recognise when you reach it.
- **Never the mapping.** `ada-bo` does not make the first speaker Ada.
  Order in a filename is arbitrary, tracks come back in whatever order Otter
  returns them, and the two need not agree. Confirm every name against the
  conversation, exactly as below.

## Name the speakers first

A transcript often arrives with `Speaker 1`, `Speaker 2`—Otter separated the
voices but has no voiceprint for those people. Nothing but the conversation can
say who they are, which is why this is your job and not the tool's.

Read for evidence, strongest first:

- **Someone is addressed by name and answers.** "What do you think, Ada?"
  followed by `Speaker 2` speaking makes `Speaker 2` Ada.
- **Someone introduces themselves**, or is introduced.
- **Someone is referred to in the third person while another speaker talks**—that person is *not* the one speaking.
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
nobody is named anywhere in the conversation, say so. A filename or a meeting
title is a hypothesis to confirm against what people say, never a substitute
for it—and a guess is not even that.

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
{"pattern": "\\bGodal\\b", "replace": "Gödel", "note": "Ada, ~33:26"}
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

**`[Goodhart's? | Goodhurt's?]`—the devices heard different words.** Usually
one alternative is a real word or a name that fits the sentence and the other is
not. Record the choice as a correction whose pattern matches the whole marker:

```json
{"pattern": "\\[Goodhart's\\? \\| Goodhurt's\\?\\]", "replace": "Goodhart's",
 "note": "far mic mis-heard; matches the topic"}
```

Some markers genuinely do not matter: `[uh? | um?]`, `[mm? | mm-hmm?]` and
other filler where either transcription is equally true. Say so once and move
on rather than writing an entry for each. Do not put a pair in that bucket
because it looks small—`[our? | are?]` reads minor but the sentence almost
always settles it, and those are worth resolving.

**`<ADA? | BO?>`—the devices disagreed about who spoke.** Judge from the
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
If the surrounding turns do not settle it, leave it—a wrong speaker is worse
than a visible question mark.

**Words attributed to the wrong person with no marker at all.** One recording
has nothing to disagree with it, so a slip arrives unflagged, and the label is
the same string as every other turn that speaker took—a pattern would move all
of them. Name the time instead, copied from the transcript:

```json
"speaker_corrections": [
  {"from": "26:28", "to": "26:29", "replace": "Bo",
   "note": "finishing their own sentence"}
]
```

The range moves exactly the words inside it, splitting a turn where it cuts
one—which is the only way to fix the case where someone else's words sit in
the middle of a paragraph with no break around them. Where the stretch is a
whole turn, `{"at": "26:28", "replace": "Bo"}` says that in one timestamp,
and merges it into the turn either side if that was the same speaker—so expect
the turn count to drop.

Both are worth looking for in a single-recording transcript: a short turn that
answers itself or completes the previous speaker's sentence, and a paragraph
that changes voice partway through.

A whole label being wrong is an `aliases` fix, not a correction.

## Do not let one rule feed another

Rules run in order, each over the text the one before it produced. So a rule
whose replacement some other rule matches will fire twice, and the transcript
shows no sign of it:

```json
{"pattern": "Ada", "replace": "Bo"}
{"pattern": "Bo",  "replace": "Cass"}
```

Ada's turns come out as Cass. The same holds for word corrections.

Before writing, read your own list and check that no `replace` value appears in
another rule's `pattern`. Where two rules want the same word, write each against
what the transcript actually says, not against what another rule leaves behind—
one rule per outcome. If that cannot be arranged, the rules are fighting over
something you have not settled yet; settle it, then write one rule.

### Refining a block: say what each part is

The case this comes up in: a long `Unattributed` block you decide is Ada, and
then a "yeah yeah" inside it that is obviously Bo. Do not write "the block is
Ada" and carve an exception out of it—that is a rule that only works if another
rule ran first. Say what each part is, and let the three entries stand alone:

```json
"speaker_corrections": [
  {"from": "41:02", "to": "41:37", "replace": "Ada"},
  {"from": "41:38", "to": "41:39", "replace": "Bo", "note": "\"yeah yeah\""},
  {"from": "41:40", "to": "42:15", "replace": "Ada"}
]
```

Three entries, three blocks in the transcript, each one checkable on its own
against what you read. Expect the turn count to rise, the way `at` makes it
fall.

**Adjacent ranges must not share a second.** A `to` written as MM:SS covers all
of that second and a `from` starts at the top of it, so ranges written nose to
tail overlap—and the later one would take the words back, leaving one block
where you wrote three. The merge refuses to run on overlapping ranges and says
which two, so this is a message you will see rather than a transcript you have
to check. Leave a second between them, as above. Where the speaker changes
mid-second and that will not do, write the boundary as a decimal number of
seconds: numbers are matched exactly, MM:SS is not.

Do not reach for `aliases` to settle this instead. It renames a placeholder
everywhere it appears in the recording, so it answers "who is Speaker 2" and
not "who spoke here"—and if another `Unattributed` stretch elsewhere is someone
else, it will quietly claim that too.

## What not to do

- Do not smooth, tidy or summarise. The transcript is verbatim on purpose;
  disfluencies and false starts are content, not noise.
- Do not silently change a word because it is unusual. Jargon, names and
  technical terms are exactly what a transcript is for. Flagging one is
  welcome—ask, or record it with an `UNRESOLVED` note; only quiet
  substitution is the problem.
- Do not resolve a marker by picking the first alternative because it is first.
  If you have no reason, say you have no reason.
- Do not rewrite the transcript file directly, even for a single word.

## Ask before applying

You will finish step 3 with two piles: calls you can defend, and calls you
cannot. Make the first pile. **Bring the second one back to the user**, before
writing anything, and wait for an answer.

Ask about:

- a speaker you cannot place, or can only place weakly—say what the evidence
  was and what it does not settle
- a marker where both alternatives are plausible in the sentence
- anything that reads as wrong where you have no candidate: a passage that
  seems to be missing a word, a name spelled two ways, a speaker who appears to
  answer their own question

Do not ask about the obvious ones. If someone is addressed by name and replies,
that is a speaker named, not a question. Ask once, as a short list, rather than
one at a time—the user was in the room and can answer all of it in a breath.

If they do not answer, or say to skip it, leave those items alone and note them
in the report. `UNRESOLVED` in a correction note is for something you changed
in order to flag it; something you did not change is simply left, and mentioned.

## Then apply it, once

Edit the config, then re-merge offline—no network, no re-upload:

```bash
cd transcripts/<date>-<id>
uv run python -m otter.fetch merge *.json -c config.json -o <recording>.txt
```

Name the output after the recording, with no suffix—
`audio-2026-06-01-ada-bo.txt`—so it matches the audio it came from and says
on its face which conversation it is and when. The folder name cannot: it is
the transcription date. Fall back to `transcript-clean.txt` only when there was
nothing to infer a name from and the user did not give you one.

**Never overwrite `transcript.txt`,** whatever you call the output. That file
is what the machine produced before anyone touched it, and it is the only way
to see what your changes actually did—`diff` the two, and every difference is
something you proposed. Overwriting it destroys the baseline and makes a wrong
correction undetectable.

That one command handles either kind of recording. Do not reach for
`otter.speech` or `otter.reconcile` directly, and do not use `run` or `pull`—both would go back to Otter for documents you already have.

## Report

Say what you changed and what you left. Something like: *12 corrections
proposed, 3 markers resolved, 9 left as filler, 1 speaker disagreement judged
from context.* Then re-run the merge and confirm the transcript regenerated.

Raise anything that struck you as odd, even where you had no fix—a passage
that reads as though a word is missing, a name spelled two ways, a speaker who
appears to answer their own question. The owner was in the room and can settle
in a second what no amount of reading can.
