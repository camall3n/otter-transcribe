# Example: one room, two devices

`room-near.m4a` and `room-far.m4a` are the same 26-second exchange captured
twice. The "far" copy is the near one delayed by exactly 3 seconds, quietened,
and low-passed to 2.6 kHz—a stand-in for a phone at the other end of a table.
Both are synthetic; no real conversation.

This is the other topology. In the Ada/Bo pair above, each file holds one
voice and the job is interleaving. Here each file holds the *whole*
conversation, and the job is reconciling two accounts of it.

```bash
python -m otter.reconcile \
    examples/one-room/bh-0915lrdsAsWwQbhNcj6y5EC0.json \
    examples/one-room/4l0p2iUmxB-QKEdyLtEoP63g8UU.json \
    -c examples/one-room/config.json -o /tmp/room.txt
```

Reproduces `transcript.txt`, offline:

```
[00:00-00:03]  ADA          Right. So, where do human values actually come from?
[00:05-00:05]  <ADA? | BO?>   I
[00:06-00:08]  BO           think mostly social conditioning, honestly.
[00:12-00:14]  ADA          Or maybe evolution baked some of them in already.
[00:18-00:20]  BO           [Goodhart's? | Goodhurt's?] law makes that very hard to measure.
```

Three things to notice.

The clock offset was **recovered from the text**, not told to the tool—it
reports `+3.000s` from 31 of 32 matched words, with a 0.01 s spread. Devices
started by hand share no common start time, and this is how that is found.

The far microphone misheard one word, so the transcript says
`[Goodhart's? | Goodhurt's?]` rather than picking one. It cannot know which is
right, and neither can anything downstream that has not heard the audio, so it
records the disagreement instead of hiding it. Leave those for a human or an
LLM pass. Words only *one* device caught are simply kept—there is nothing to
decide about those.

The devices also disagreed about *who* said the word "I", so the speaker is
`<ADA? | BO?>`—angle brackets for a disputed speaker, square brackets for a
disputed word, so the two can be grepped apart. Each device diarises on its own, so its cluster numbers are its
own; they are matched up using the words the two tracks agree on. Where one
device names a speaker and the other just could not tell, the name wins—that
is not a disagreement, it is one device knowing more. Only two competing claims
get brackets.

You do not have to choose between the two modes. `otter.fetch run` and `pull`
work out which applies by measuring how much text the tracks share: separate
microphones overlap by a couple of percent at wildly inconsistent offsets,
while two devices in one room overlap by ~95% at a single offset.
