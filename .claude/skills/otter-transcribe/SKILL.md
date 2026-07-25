---
name: otter-transcribe
description: Transcribe one or more audio recordings via Otter.ai and merge multi-track recordings into one speaker-attributed transcript. Use when the user says /otter-transcribe, drags audio files in, or asks to transcribe a meeting, call, or Zoom recording. Also covers naming unidentified speakers.
---

# Transcribe

## Run one command

Run from the repo root—the directory holding `otter/`. Use `uv run python` if
there is a `uv.lock`, otherwise `./.venv/bin/python`.

**Given audio files:**

```bash
uv run python -m otter.fetch run <file> <file> ...
```

**Given otids, or told it is already in Otter** (`otter.fetch list` finds them):

```bash
uv run python -m otter.fetch pull <otid> <otid> --wait
```

That is the whole job. Pass every recording of the same conversation in one
command, exactly as the user gave them. The tool makes its own dated folder
under `transcripts/` and prints where.

## Before running, do nothing

No `ls`. No reading README, config or transcript files. No checking whether it
has been transcribed already. No looking at the audio. The command inspects the
recordings itself and reports what it found—anything you do first is a slower,
worse version of that.

If you think there is a shortcut, run the command anyway and raise the shortcut
afterwards. Someone who hands you audio is asking for that audio transcribed,
not for a judgement about whether it needs doing.

## After running, stop

Success is the command exiting 0 and naming what it wrote:

```
writing to transcripts/<date>-<id>/
uploaded <path> -> <otid>
wrote transcripts/<date>-<id>/transcript.txt: <N> turns, <MM:SS> total
```

Report that, plus any speaker labels the config scaffolding printed, and say
where the transcript is. Then stop.

Do NOT, unless asked: read the transcript, `wc` it, count words, open the saved
`.json` files, print segments or offsets, re-run to compare, or summarise what
was said. A 94-minute call is ~12,000 words; the point of this skill is that
transcribing one costs almost no context. None of those checks can tell you
anything the command's own output did not.

If a command fails it exits non-zero and explains itself. Report its message
verbatim rather than working around it.

## Speakers may come out unnamed

The run writes a config beside the transcript, listing the speakers it found:

```
wrote a starter config to transcripts/<date>-<id>/config.json:
  <track>: ['Speaker 1', 'Speaker 2', 'Unattributed']
```

`Speaker N` means Otter separated that person correctly but has no voiceprint
for them. That is normal and the transcript is complete. Do not try to work out
who they are, and do not edit the config—say the transcript is ready and that
`otter-cleanup` can name them from the conversation.

## Credentials

Resolved from `OTTER_COOKIES.txt` at the repo root. **Never print, cat or echo
that file, and never paste a cookie into the conversation.** On `cookie auth
rejected`, tell the user to run `otter.fetch login` themselves; do not try to
work around it.

## Notes

- A transcript may contain `[a? | b?]` or `<Ada? | Bo?>` where two recordings
  disagreed about a word or a speaker. Leave them—you cannot hear the audio.
  Resolving them is the `otter-cleanup` skill, if the user asks.
- These are Otter's private endpoints and they change. On a shape or status
  error, report it as-is rather than guessing at a workaround.
- Cleaning up a finished transcript is `otter-cleanup`. Offer it once the
  transcript exists; do not do it as part of this.
