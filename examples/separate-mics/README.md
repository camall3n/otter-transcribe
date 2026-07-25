# Example: two independent tracks

`track-a-ada.m4a` and `track-b-bo.m4a` are 26 seconds each, synthesised with
macOS `say` — no real conversation, nothing private. They stand in for what
Zoom writes when it records each participant to their own file.

The two speakers strictly alternate and never overlap, so correct interleaving
is checkable by eye:

| | | |
|---|---|---|
| 0.5 s | Ada | "So the idea is that each microphone gets its own separate track." |
| 6.5 s | Bo | "Wait. One audio file per person?" |
| 13 s | Ada | "Exactly. And then the tool stitches them back together." |
| 19 s | Bo | "Got it. So the timing has to line up between them." |

Each file contains only one voice. If those four lines come out of the merge in
that order, two separate recordings were correctly rebuilt into one conversation.

## Merge it, without spending any quota

A finished run is already here — the two `.json` files are what Otter returned,
so the merge works offline:

```bash
python -m otter.speech \
    examples/separate-mics/vbiVOwB3AhtzwuN3-4lf-i4JeqA.json \
    examples/separate-mics/V0WXFfziZnQX9byM1shDpax4fMc.json \
    -c examples/separate-mics/config.json -o /tmp/out.txt
```

Should print `4 turns, 00:22 total` and reproduce `transcript.txt`. Name the
input files explicitly rather than globbing `examples/separate-mics/*.json` — a glob sweeps
in `config.json`, which is not a transcript.

## Run it for real

To exercise the whole path, upload and merge from the audio. About a minute of
transcription quota:

```bash
python -m otter.fetch run examples/separate-mics/track-a-ada.m4a examples/separate-mics/track-b-bo.m4a \
    -c /tmp/fresh-config.json -o /tmp/out.txt -d /tmp/
```

Point `-c` at a config that does *not* exist and the first run writes one,
printing the speaker labels it found. These are synthetic voices with no
voiceprints in your account, so expect `Speaker 1` on both tracks — the merge
keeps them apart as `TRACK-A-ADA SPEAKER 1` and `TRACK-B-BO SPEAKER 1`, because
Speaker 1 on one recording is not the same person as Speaker 1 on another. Put
real names against them, as `config.json` here does, and merge again with
`otter.speech` — not `run`, which would upload the audio a second time.

Naming two labels identically is how you say they *are* the same person: their
turns then merge. That is the fix if one person ends up on two tracks.
