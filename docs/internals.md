# Internals

How the merge decides, every config key, and what using Otter's private API
taught us. None of this is needed to transcribe a recording.

## Two devices in one room

The commands above assume one microphone per person—each track hears mostly
one voice, and the job is interleaving them. Two phones on a table are a
different shape: both hear *everyone*, so each file already holds the whole
conversation and the job is reconciling two accounts of it.

**You do not have to say which you have.** `run` and `pull` work it out by
measuring how much text the two tracks share. Separate microphones overlap by a
couple of percent, at offsets that disagree by hundreds of seconds. Two devices
in one room overlap by ~95%, at a single consistent offset.

That consistency is also the answer to a problem nothing else here can solve:
phones started by hand share no common start time. The offset is recovered from
the text, by aligning the two transcripts on their words and reading off the
median time difference—measured at +3.000s on a pair offset by exactly three
seconds. If the tracks do not agree well enough to place a common clock it
refuses to merge rather than inventing a plausible answer.

Reconciling makes two kinds of uncertainty visible instead of guessing:

| | |
|---|---|
| the devices heard different words | `[Goodhart's? \| Goodhurt's?]` |
| they disagree about who spoke | `<ADA? \| BO?>` |

Where only one device caught something, it is simply kept—there is nothing to
decide. Where one device names a speaker and the other could not tell, the name
wins; that is not a disagreement. Only two competing claims get brackets, and
they use different brackets so you can grep them apart.

Leave them for a human, or for an LLM pass. Nothing that has not heard the
audio can settle them, including whatever wrote this.

Any number of devices works—each is folded into the running result, so a
third recording is reconciled against the outcome of the first two. Where they
disagree the alternatives accumulate side by side, `<Ada? | Bo? | Cy?>`, rather
than nesting; a device that agrees with an existing alternative adds nothing,
and if the alternatives collapse to one the marker disappears.

Mixed sets are refused rather than guessed at. Three tracks where two recorded
one room and the third is somebody's separate microphone have no single right
answer, so it says so and stops.

To run it directly rather than through `run`:

```bash
python -m otter.reconcile a.json b.json -c config.json -o transcript.txt
python -m otter.reconcile a.json b.json --offset-only   # just report the clock
```

## Config

Point `-c` at a path that does not exist and the first run writes it, listing
the speaker labels the recording actually produced. Fill in real names and
re-run. An unedited config still merges correctly, just with Otter's labels.

`Speaker 1` means Otter separated that person correctly but has no voiceprint
for them. For Zoom multi-track files the suggestion is filled in from the
filename, which names the participant: one anonymous voice on their own track
becomes `AdaSmith`, while a track holding two becomes
`AdaSmith-Speaker 1` and `AdaSmith-Speaker 2`, since a microphone
picks up whoever sits beside it. A name Otter recognised is never overridden. Either put a name against it in the config—which never leaves your
machine—or run `tag`, which tells Otter and gets that person recognised
automatically in later recordings.

Everything that needed a human decision lives in the config as data—speaker
names, text corrections, section markers—so a transcript can always be rebuilt
from the recording plus the config, and the audit trail at the foot of the
transcript is generated from those same entries rather than written alongside
them.

## Config reference

Every key is optional. A config that only names speakers is a normal config.

| key | what it does |
|---|---|
| `tracks` | `{otid: name}`—the label each recording gets in the output |
| `aliases` | rename speakers. Per-track for interleaving, flat for one room |
| `corrections` | `{pattern, replace, note}` regexes applied at render time |
| `speaker_corrections` | the same, applied to the speaker label instead of the words—or `{from, to, replace}` to move the words in a time range, splitting a turn where the range cuts it, or `{at, replace}` for a whole turn |
| `markers` | `{at, text}`—a heading inserted at that second |
| `header` | lines printed above the transcript |
| `footer` | lines printed below, or omit to derive them from `corrections` |
| `gap_seconds` | silence that ends a turn, interleaving only (0.25) |
| `drop` | `{track, start}`—cues to discard, e.g. confirmed mic bleed |
| `auto_drop_bleed` | set `false` to keep both copies of duplicated speech |
| `bleed_max_offset` | how far apart duplicates may be to count as bleed (5.0s) |
| `mode` | force `"interleave"` or `"reconcile"` instead of measuring |
| `offset` | with `mode: "reconcile"`, skip clock estimation and use this |

## How it works

Transcripts come from Otter's `speech` endpoint rather than any of its file
exports. Those are segmented by speaker turn—one cue ran 11m40s on a
two-person call—and since interleaving works by sorting cues on start time, a
cue that long collapses the conversation into alternating monologues with
nothing in the file able to undo it. `speech` carries a per-word `start`/`end`
for every segment instead, so turn boundaries are found from silences and cues
sort into one true-to-life stream.

The per-word times are interpolated, not measured—a word's duration is a flat
0.03s per character. The silences *between* runs of speech are real, reaching
115 seconds on a 94-minute call, and silences are all the merge relies on.

- `otter/credentials.py`—credential resolution, none of it through a prompt
- `otter/fetch.py`—the Otter API
- `otter/speech.py`—per-word timings into cues, one mic per person
- `otter/reconcile.py`—two devices, one room: find the clock, merge the accounts
- `otter/transcript.py`—model, interleave, render; knows nothing about Otter

Two things worth knowing, both found the hard way: `finish_speech_upload`
requires `appid=web`, and `redo_speaker_match` is queued for 10–15 minutes, so
tagging a speaker looks like it failed and then later works.

These are private endpoints and they change without notice. Every call checks
its response shape and fails loudly rather than half-working.

## Limits

- Interleaving assumes tracks share a `t=0`, which Zoom guarantees. Separate
  devices do not, and are handled by reconciliation instead.
- Requires an Otter account. Free tier untested; developed against Business.
- Speech captured on two tracks at once (one person's voice through another's
  speakers) is dropped automatically when the stray words sit between pauses,
  which is the usual shape. A fragment buried mid-sentence is not detected and
  will appear twice.
- Genuine simultaneous speech on a single mic is lost by Otter before this
  ever sees it.
