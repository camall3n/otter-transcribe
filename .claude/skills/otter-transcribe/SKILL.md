---
name: otter-transcribe
description: Transcribe one or more audio recordings via Otter.ai and merge multi-track recordings into a single interleaved transcript. Use when the user says /otter-transcribe, drags audio files in, or asks to transcribe a meeting, call, or Zoom recording. Also covers naming unidentified speakers and applying transcript corrections.
---

# Transcribe

Wraps `otter/` — upload to Otter.ai, wait, fetch per-word timings, interleave
multiple tracks into one transcript.

Cleaning up a finished transcript is a separate skill, `otter-cleanup`.
Offer it once the transcript exists; do not do it as part of this.

**Python moves the words; you never hold them.** A 94-minute call is ~12,000
words, and the point of this skill is that transcribing one costs almost no
context. Run the command, report what it printed, stop.

## When it is done

`run` and `pull` print everything that constitutes success:

```
uploaded <path> -> <otid>
wrote <output>: <N> turns, <MM:SS> total
```

Report that line, plus the speaker labels the config scaffolding printed, and
say the transcript is ready at its path. **Then stop.** Success is the command
exiting 0 and naming its output file.

Do NOT, unless the user asks:

- read the transcript, `wc` it, or count words
- open the saved `.json` documents or print segments, offsets or speaker ids
- re-run the merge to compare, or verify the turn count another way
- summarise what was said

Each of those pulls the conversation into context, which is the one cost this
skill exists to avoid, and none of them can tell you anything the command's own
output did not. If a command fails it says so and exits non-zero — that is the
error path, and reporting its message verbatim is the right response.

## Run it

One audio file per participant track (Zoom writes these to
`~/Documents/Zoom/<meeting>/Audio Record/`). Files must come from the same
recording session — the merge assumes a shared t=0.

Run from the repo root (the directory holding `otter/`). Use `uv run python`
if there is a `uv.lock`, otherwise `./.venv/bin/python`.

```bash
uv run python -m otter.fetch run <track1> <track2> ... \
    -c transcripts/<name>.config.json \
    -o transcripts/<name>.txt
```

If Otter already has the audio, skip the upload:

```bash
uv run python -m otter.fetch list                       # find the otids
uv run python -m otter.fetch pull <otid> <otid> --wait -c <cfg> -o <out> -d transcripts/
```

`-d` saves each speech document, so the merge can be re-run offline against a
changed config with no network and no re-upload:

```bash
uv run python -m otter.speech transcripts/<otid>.json ... -c <cfg> -o <out>
```

## The config writes itself

Point `-c` at a path that does not exist. The first run creates it and prints
the speaker labels the recording actually produced:

```
wrote a starter config to cfg.json:
  julian-marjan-cam: ['Speaker 1', 'Speaker 2', 'Unattributed']
  ^ put real names on the right-hand side, then re-run the same command.
```

`Speaker N` means Otter diarised that person correctly but has no voiceprint
for them. Two ways to fix it, and the user picks:

- **Local only** — edit the `aliases` values in the config. Names never leave
  the machine. Nothing is re-uploaded.
- **Tell Otter** — `uv run python -m otter.fetch tag <otid> "Speaker 1=Ada"`.
  Otter then recognises that person automatically in *future* recordings. It
  also queues a re-match that takes 10–15 minutes, so do not re-`pull` straight
  away and conclude it failed.

An unqualified config still merges correctly; it just uses Otter's labels.

## Two devices in one room

`otter.speech` is for one microphone per person, where each track hears mostly
one voice and the job is interleaving. Two phones on a table both hear
*everyone*, so each track already holds the whole conversation. That is
`otter.reconcile`:

```bash
uv run python -m otter.reconcile a.json b.json -c cfg.json -o transcript.txt
uv run python -m otter.reconcile a.json b.json --offset-only   # just the clock
```

It finds the clock offset itself — phones started by hand do not share a t=0 —
by aligning the two transcripts on their words and reading off the median time
difference. It refuses to merge if the tracks do not agree well enough to place
a common clock, rather than producing a plausible wrong answer; `--offset
SECONDS` overrides.

Then one pass over the alignment, treating two cases differently:

- **only one track has words there** — take them, nothing to decide
- **both have words and they differ** — emit `[a? | b?]` and leave it
- **they disagree about who spoke** — the speaker becomes `<Ada? | Bo?>`

Do not resolve those markers by guessing. You cannot hear the audio, and on a
real recording most of them are function-word noise while a few change the
meaning. Leave them for the correction pass, or for the reader.

## Credentials

Resolved from `OTTER_COOKIES.txt` at the repo root — see `otter/credentials.py`.
**Never print, cat, or echo that file, and never paste a cookie into the
conversation.** If a command fails with `cookie auth rejected`, tell the user
to log in to otter.ai again and refresh the file themselves; do not try to
work around it.

## Notes

- `gap_seconds` (default 0.25) sets where a silence ends a turn when
  interleaving separate microphones. Swept; the optimum is a plateau from
  0.2–0.35. Reconciling one room ignores it — a pause there is just a pause,
  and the same speaker either side of it is still one turn.
- Duplicate speech captured on two tracks (one person's voice through
  another's speakers) is dropped automatically inside the merge.
- These are Otter's private endpoints, not its public API, and they change —
  `finish_speech_upload` gained a required parameter at one point. If a command
  fails with a shape or status error, report the message as-is rather than
  guessing at a workaround.
