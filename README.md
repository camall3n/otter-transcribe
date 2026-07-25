# otter-transcribe

Turns several recordings of one conversation into a single transcript with who
said what.

It handles **multi-track Zoom recordings**, which give very good speaker
isolation for free, and merges them for you programmatically by talking to
Otter's servers directly.

It also handles **multi-phone recordings** — two devices on a table hearing the
same room. It aligns them automatically and uses the pair two ways: to recover
words only one device caught, and, where they genuinely disagree, to show both
readings rather than pick one.

It makes no editorial decisions about content (other than Otter's built-in
transcription priors) unless you explicitly invoke the /otter-cleanup skill
with Claude Code, which uses the LLM to clean up the easy stuff based on
context, and asks for your help on the rest. It reports every edit it makes
at the bottom of the file, so you can check its work.

## Setup

```bash
./install.sh
```

It checks for Python 3.12+, installs the one dependency, and asks for
a credential. Otter has no public API on personal plans, so this borrows your
browser session — it walks you through copying one request out of developer
tools. No password is involved, and logging out revokes it.

To replace it later: `python -m otter.fetch login`.

## Running it

```bash
python -m otter.fetch run track1.m4a track2.m4a
```

Pass every recording of the same conversation. It works out for itself whether
they are separate microphones or several devices in one room.

Each run writes a dated folder under `transcripts/` holding the transcript, the
config, and what Otter returned.

In Claude Code: `/otter-transcribe <files>` transcribes, `/otter-cleanup`
names the speakers and applies corrections.

## Naming speakers

Otter names anyone it has a voiceprint for. Everyone else comes out as
`Speaker 1`, and the run prints which. To give them real names, either:

- put real names in that run's `config.json` under `aliases` and merge again —
  names stay on your machine; or
- run `python -m otter.fetch tag <otid> "Speaker 1=Ada"`, which registers the
  name with Otter so that person is recognised in every later recording.
  Allow 10–15 minutes.

## Correcting a transcript

`/otter-cleanup` in Claude Code does this for you: it reads the transcript,
works out who the unnamed speakers are, proposes corrections, asks about
anything it cannot justify, and rebuilds the file. What follows is the same
thing by hand.

You never edit the transcript. You write rules into that run's `config.json` —
patterns, so a literal `?` or `|` needs a backslash:

```json
{
  "corrections": [
    {"pattern": "\\bGodal\\b", "replace": "Gödel", "note": "~33:26"}
  ],
  "speaker_corrections": [
    {"pattern": "<ADA\\? \\| BO\\?>", "replace": "Bo",
     "note": "starts the sentence Bo finishes"}
  ]
}
```

`corrections` change the words, `speaker_corrections` change who a turn belongs
to. Then rebuild:

```bash
python -m otter.fetch merge <folder>/*.json -c <folder>/config.json \
    -o <folder>/transcript-clean.txt
```

That works from documents you already have — no network, no re-upload. Write to
a new file so the original stays as your baseline.

The new transcript carries its own record: the edits are in the text, and every
rule that fired is listed at the foot, generated from the config rather than
typed alongside it. So the file explains itself to anyone who reads it, and
`diff` against the original confirms it independently.

Where two recordings of the same room disagreed, you will see
`[Goodhart's? | Goodhurt's?]` for a word and `<ADA? | BO?>` for a speaker. The
merge leaves those alone because it cannot hear the audio, but the surrounding
sentence usually settles them — that is what cleanup is for.

## Other commands

| | |
|---|---|
| `list` | recent recordings and their otids |
| `pull <otid>...` | merge something already in Otter |
| `probe <otid>` | is it processed and named? |
| `check` | does the credential still work? |

## More

`examples/` has a worked example of each recording setup, each reproducing
offline with no account needed.

`docs/internals.md` covers how the merge decides between the two kinds of
recording, every config key, and the Otter API's sharper edges.
