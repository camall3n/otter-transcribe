# otter-transcribe

Turn a multi-track recording into one interleaved, speaker-attributed
transcript, using Otter.ai for the transcription and doing the merge locally.

Zoom writes one audio file per participant. Otter transcribes each separately,
so each has its own timeline and its own idea of who is speaking. This stitches
them back into a conversation.

```bash
python -m otter.fetch run track1.m4a track2.m4a -c config.json -o transcript.txt
```

Uploads both, waits for Otter to finish, fetches per-word timings, interleaves,
writes the transcript. In Claude Code, `/transcribe` wraps the same thing.

## Setup

```bash
./install.sh
```

Checks for Python 3.12+, installs the one dependency (via `uv` if you have it,
a plain venv otherwise), walks you through the credential, and verifies it
works. Safe to re-run.

Otter's public API is Enterprise-only, so this uses the private endpoints the
web app uses, which means borrowing your browser's session. `install.sh` walks
you through it, or run it any time:

```bash
python -m otter.fetch login
```

It prints the steps and takes a **Copy as cURL** from developer tools — one
right-click, and unlike `document.cookie` or a bookmarklet it carries the
HttpOnly session cookie, which is the only one that actually authenticates. The
paste is not echoed, lands in `OTTER_COOKIES.txt` (gitignored, mode 600), and is
checked against Otter before it claims success.

No password is involved, the session expires on its own, and you revoke it by
logging out. Three other credential sources — macOS Keychain among them — are
described in `otter/credentials.py`.

```bash
python -m otter.fetch check     # confirm the credential works
python -m otter.fetch list      # recent recordings and their otids
```

## Try it

`examples/` holds a worked example of each recording setup — `separate-mics/`
for one microphone per person, `one-room/` for two devices on a table. Each
comes with its audio, the documents Otter returned, a config and the resulting
transcript, so the merge reproduces offline with no account and no quota.
Running one end to end from the audio costs about a minute of quota and shows
the whole flow, including how the config gets written for you.

## Commands

| | |
|---|---|
| `login` | store a credential from developer tools |
| `run <audio>...` | upload, wait, merge — the whole pipeline |
| `pull <otid>... --wait` | same, when Otter already has the audio |
| `probe <otid>` | is it processed, aligned, and named? |
| `tag <otid> "Speaker 1=Ada"` | name a speaker so Otter knows them next time |
| `list`, `check`, `upload` | |

`pull -d transcripts/` saves each speech document so a merge can be re-run
offline against a changed config, with no network and no re-upload:

```bash
python -m otter.speech transcripts/<otid>.json ... -c config.json -o out.txt
```

## Two devices in one room

The commands above assume one microphone per person — each track hears mostly
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
median time difference — measured at +3.000s on a pair offset by exactly three
seconds. If the tracks do not agree well enough to place a common clock it
refuses to merge rather than inventing a plausible answer.

Reconciling makes two kinds of uncertainty visible instead of guessing:

| | |
|---|---|
| the devices heard different words | `[Goodhart's? \| Goodhurt's?]` |
| they disagree about who spoke | `<ADA? \| BO?>` |

Where only one device caught something, it is simply kept — there is nothing to
decide. Where one device names a speaker and the other could not tell, the name
wins; that is not a disagreement. Only two competing claims get brackets, and
they use different brackets so you can grep them apart.

Leave them for a human, or for an LLM pass. Nothing that has not heard the
audio can settle them, including whatever wrote this.

Any number of devices works — each is folded into the running result, so a
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
picks up whoever sits beside it. A name Otter recognised is never overridden. Either put a name against it in the config — which never leaves your
machine — or run `tag`, which tells Otter and gets that person recognised
automatically in later recordings.

Everything that needed a human decision lives in the config as data — speaker
names, text corrections, section markers — so a transcript can always be rebuilt
from the recording plus the config, and the audit trail at the foot of the
transcript is generated from those same entries rather than written alongside
them.

## Config reference

Every key is optional. A config that only names speakers is a normal config.

| key | what it does |
|---|---|
| `tracks` | `{otid: name}` — the label each recording gets in the output |
| `aliases` | rename speakers. Per-track for interleaving, flat for one room |
| `corrections` | `{pattern, replace, note}` regexes applied at render time |
| `markers` | `{at, text}` — a heading inserted at that second |
| `header` | lines printed above the transcript |
| `footer` | lines printed below, or omit to derive them from `corrections` |
| `gap_seconds` | silence that ends a turn, interleaving only (0.25) |
| `drop` | `{track, start}` — cues to discard, e.g. confirmed mic bleed |
| `auto_drop_bleed` | set `false` to keep both copies of duplicated speech |
| `bleed_max_offset` | how far apart duplicates may be to count as bleed (5.0s) |
| `mode` | force `"interleave"` or `"reconcile"` instead of measuring |
| `offset` | with `mode: "reconcile"`, skip clock estimation and use this |

## How it works

Transcripts come from Otter's `speech` endpoint, not its SRT export. The SRT is
segmented by speaker turn — a single cue ran 11m40s on a two-person call — which
destroys interleaving irrecoverably. `speech` carries a per-word `start`/`end`
for every segment, so turn boundaries are found from silences rather than
inferred, and cues sort into one true-to-life stream.

- `otter/credentials.py` — credential resolution, none of it through a prompt
- `otter/fetch.py` — the Otter API
- `otter/speech.py` — per-word timings into cues, one mic per person
- `otter/reconcile.py` — two devices, one room: find the clock, merge the accounts
- `otter/transcript.py` — model, interleave, render; knows nothing about Otter

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
  speakers) is dropped automatically; genuine simultaneous speech on a single
  mic is lost by Otter before this ever sees it.
