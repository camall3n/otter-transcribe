"""Properties every merge must hold, whatever it is given.

Written after a run of one-off fixes: same-named speakers collapsing across
tracks, aliases silently ignored, argument order changing the timestamps, a
config swept into the inputs. Each was found by someone asking a question and
patched where it surfaced. These are the invariants those bugs violated, so
the next one fails here instead.

Everything is synthetic. No account, no network, no real conversation.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SR = 16000


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------

def segment(uuid, speaker_id, cluster, base, words, gap=0.5):
    """One Otter segment; `words` is a list of strings, evenly spaced."""
    align, t = [], 0.0
    for w in words:
        align.append({"word": w, "start": round(t, 2), "end": round(t + 0.3, 2)})
        t += gap
    return {"uuid": uuid, "speaker_id": speaker_id, "speaker_model_label": cluster,
            "start_offset": int(base * SR), "end_offset": int((base + t) * SR),
            "transcript": " ".join(words), "alignment": align}


def doc(title, segments, speakers=None):
    return {"title": title, "duration": 60, "speakers": speakers or [],
            "transcripts": segments}


def separate_mics():
    """One voice per track, alternating -- the interleaving case."""
    a = doc("audioAdaOne1", [segment("a1", None, "1", 0.5, ["one", "two", "three"]),
                             segment("a2", None, "1", 10.0, ["seven", "eight"])])
    b = doc("audioBoTwo2", [segment("b1", None, "1", 5.0, ["four", "five", "six"])])
    return a, b


def one_room(offset=3.0):
    """Both tracks hear everything -- the reconciliation case."""
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
             "golf", "hotel", "india", "juliet", "kilo", "lima"]
    near = doc("near", [segment("n1", None, "1", 0.5, words[:6]),
                        segment("n2", None, "2", 6.0, words[6:])])
    far = doc("far", [segment("f1", None, "1", 0.5 + offset, words[:6]),
                      segment("f2", None, "2", 6.0 + offset, words[6:])])
    return near, far


def with_bleed():
    """Ada's voice leaves Bo's speakers and returns through his microphone.

    Otter transcribes it on both tracks and, because it does not match Bo's
    voiceprint, leaves its copy unattributed. Keeping the wrong one puts Ada's
    words in Bo's mouth.

    Caught when the stray word sits between pauses, which is how it lands in
    a real recording -- someone says one word and stops. A fragment buried
    mid-sentence is not caught; see the README's Limits.
    """
    a = doc("ada", [segment("a1", 1, "1", 0.5, ["cool"]),
                    segment("a2", 1, "1", 3.0, ["shall", "we", "start"])],
            speakers=[{"id": 1, "speaker_name": "Ada"}])
    b = doc("bo", [segment("b0", None, "9", 0.6, ["cool"]),          # the echo
                   segment("b1", 2, "1", 6.0, ["yes", "go", "ahead"])],
            speakers=[{"id": 2, "speaker_name": "Bo"}])
    return a, b


def one_recording():
    """A single track, two speakers, one turn diarised to the wrong person.

    The single-recording case has no second device to disagree with, so a
    misattribution arrives unmarked: Bo's label here is the same string as
    every other Bo turn, and no pattern can pick it out.
    """
    return doc("solo", [segment("s1", 1, "1", 0.5, ["alpha", "bravo", "charlie"]),
                        segment("s2", 2, "2", 5.0, ["delta"]),
                        segment("s3", 1, "1", 8.0, ["echo", "foxtrot"])],
               speakers=[{"id": 1, "speaker_name": "Ada"},
                         {"id": 2, "speaker_name": "Bo"}])


def run_merge(tmp, docs, config=None, output="out.txt"):
    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, d in docs.items():
        p = tmp / f"{name}.json"
        p.write_text(json.dumps(d))
        paths.append(str(p))
    cfg = tmp / "config.json"
    cfg.write_text(json.dumps(config or {}))
    out = tmp / output
    result = subprocess.run(
        [sys.executable, "-m", "otter.fetch", "merge", *paths,
         "-c", str(cfg), "-o", str(out)],
        cwd=ROOT, capture_output=True, text=True)
    assert out.exists(), f"merge failed: {result.stderr[-400:]}"
    return out.read_text(), result.stderr


def turns(text):
    """(speaker, text) per turn, ignoring timestamps."""
    out, pending = [], None
    for line in text.splitlines():
        m = re.match(r"^\[[\d:]+-[\d:]+\]\s+(.+)$", line)
        if m:
            pending = m.group(1).strip()
        elif pending and line.strip() and not line.startswith(("=", "#")):
            out.append((pending, line.strip()))
            pending = None
    return out


def body(text):
    return " ".join(t for _, t in turns(text)).split()


# --------------------------------------------------------------------------
# invariants
# --------------------------------------------------------------------------

@pytest.mark.parametrize("build", [separate_mics, one_room], ids=["separate", "one-room"])
def test_argument_order_does_not_change_the_result(tmp_path, build):
    """Naming the recordings in the other order must not move a single word.

    A glob hands them over alphabetically, which is nobody's recording order.
    """
    a, b = build()
    first, _ = run_merge(tmp_path / "x", {"a": a, "b": b}, output="1.txt")
    second, _ = run_merge(tmp_path / "y", {"b": b, "a": a}, output="2.txt")
    # Compare the whole transcript, timestamps included -- the bug this exists
    # for shifted every time by three seconds without moving a word.
    assert first == second


def test_tracks_starting_together_still_order_deterministically(tmp_path):
    """Zoom writes every track from a common t=0, so ties are the normal case."""
    a, b = one_room(offset=0.0)
    first, _ = run_merge(tmp_path / "x", {"a": a, "b": b}, output="1.txt")
    second, _ = run_merge(tmp_path / "y", {"b": b, "a": a}, output="2.txt")
    assert first == second


@pytest.mark.parametrize("build", [separate_mics, one_room], ids=["separate", "one-room"])
def test_no_words_are_invented_or_lost(tmp_path, build):
    """Merging rearranges; it must never add or drop a word."""
    a, b = build()
    text, _ = run_merge(tmp_path, {"a": a, "b": b})
    spoken = sorted(w for d in (a, b) for s in d["transcripts"]
                    for w in s["transcript"].split())
    got = sorted(w.strip("[]<>?|") for w in body(text) if w.strip("[]<>?|"))
    assert set(got) <= set(spoken), "invented words"
    assert set(spoken) <= set(got), "lost words"


def test_speakers_on_different_tracks_stay_apart(tmp_path):
    """Two unnamed tracks each have a "Speaker 1"; they are different people."""
    a, b = separate_mics()
    text, _ = run_merge(tmp_path, {"a": a, "b": b})
    speakers = {s for s, _ in turns(text)}
    assert len(speakers) > 1, f"collapsed into {speakers}"


@pytest.mark.parametrize("shape", ["flat", "per-track"])
def test_aliases_are_applied_in_either_shape(tmp_path, shape):
    """A config the tool itself writes must be a config the tool can read."""
    near, far = one_room()
    flat = {"Speaker 1": "Ada", "Speaker 2": "Bo"}
    config = {"aliases": flat if shape == "flat" else {"a": flat, "b": flat}}
    text, _ = run_merge(tmp_path, {"a": near, "b": far}, config)
    speakers = {s for s, _ in turns(text)}
    assert any("ADA" in s for s in speakers), f"aliases ignored: {speakers}"


def test_merge_ignores_its_own_config_among_the_inputs(tmp_path):
    """`merge *.json -c config.json` is the natural thing to type.

    The glob hands the config over as an input as well, so it has to be
    skipped rather than rejected -- passing it only via -c tests nothing.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    a, b = one_room()
    for name, d in (("a", a), ("b", b)):
        (tmp_path / f"{name}.json").write_text(json.dumps(d))
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({}))
    out = tmp_path / "out.txt"
    result = subprocess.run(
        [sys.executable, "-m", "otter.fetch", "merge",
         *sorted(str(p) for p in tmp_path.glob("*.json")),   # sweeps the config in
         "-c", str(cfg), "-o", str(out)],
        cwd=ROOT, capture_output=True, text=True)
    assert out.exists(), f"the config was not skipped: {result.stderr[-300:]}"
    assert turns(out.read_text())


def test_corrections_are_applied_and_recorded(tmp_path):
    """What the footer claims and what the text says cannot disagree."""
    a, b = separate_mics()
    config = {"corrections": [{"pattern": r"\bseven\b", "replace": "SEVEN",
                               "note": "test"}]}
    text, _ = run_merge(tmp_path, {"a": a, "b": b}, config)
    assert "SEVEN" in text and "CORRECTIONS APPLIED" in text
    assert "seven" not in " ".join(t for _, t in turns(text))


def test_merging_twice_gives_the_same_answer(tmp_path):
    a, b = one_room()
    first, _ = run_merge(tmp_path / "x", {"a": a, "b": b}, output="1.txt")
    second, _ = run_merge(tmp_path / "y", {"a": a, "b": b}, output="2.txt")
    assert first == second


def test_the_two_topologies_are_told_apart(tmp_path):
    _, err_sep = run_merge(tmp_path / "s", dict(zip("ab", separate_mics())))
    _, err_room = run_merge(tmp_path / "r", dict(zip("ab", one_room())))
    assert "interleave" in err_sep, err_sep
    assert "reconcile" in err_room, err_room


def test_speech_captured_on_two_tracks_appears_once(tmp_path):
    """One voice through another's speakers is transcribed twice; keep one."""
    a, b = with_bleed()
    text, _ = run_merge(tmp_path, {"a": a, "b": b})
    assert body(text).count("cool") == 1, f"duplicated: {body(text)}"


def test_the_kept_copy_is_the_one_otter_attributed(tmp_path):
    """Dropping the named copy would credit Ada's words to Bo."""
    a, b = with_bleed()
    text, _ = run_merge(tmp_path, {"a": a, "b": b})
    who = next(s for s, t in turns(text) if "cool" in t)
    assert "ADA" in who, f"kept the echo instead: {who}"


def test_a_speaker_conflict_can_be_settled_in_the_config(tmp_path):
    """A `<A? | B?>` marker is diagnosable; it has to be fixable too."""
    near, far = one_room()
    # Both devices name the second stretch, and disagree. A cluster mismatch
    # would not do: map_speakers reconciles those, and correctly.
    near["speakers"] = [{"id": 1, "speaker_name": "Ada"}]
    far["speakers"] = [{"id": 2, "speaker_name": "Bo"}]
    for seg in near["transcripts"]:
        seg["speaker_id"] = 1
    for seg in far["transcripts"]:
        seg["speaker_id"] = 2
    config = {}
    before, _ = run_merge(tmp_path / "x", {"a": near, "b": far}, config, output="1.txt")
    marker = [s for s, _ in turns(before) if "?" in s]
    assert marker, f"expected a speaker conflict, got {[s for s, _ in turns(before)]}"

    config["speaker_corrections"] = [
        {"pattern": re.escape(marker[0]), "replace": "Bo", "note": "context"}]
    after, _ = run_merge(tmp_path / "y", {"a": near, "b": far}, config, output="2.txt")
    assert not [s for s, _ in turns(after) if "?" in s], "marker survived"
    assert "SPEAKERS CORRECTED" in after


def test_a_text_rule_cannot_rewrite_a_speaker(tmp_path):
    """The two lists are separate so one cannot reach into the other."""
    a, b = separate_mics()
    config = {"aliases": {"Speaker 1": "Ada"},
              "corrections": [{"pattern": "Ada", "replace": "WRONG", "note": "x"}]}
    text, _ = run_merge(tmp_path, {"a": a, "b": b}, config)
    assert not any("WRONG" in s for s, _ in turns(text)), "a text rule hit a speaker"


def test_a_settled_speaker_joins_the_turn_it_belongs_to(tmp_path):
    """Relabelling a stray word must merge it, not leave three turns.

    Applied at render time the fix looked applied and read wrong: the words
    were right and the same person spoke twice in a row.
    """
    near, far = one_room()
    near["speakers"] = [{"id": 1, "speaker_name": "Ada"}]
    far["speakers"] = [{"id": 2, "speaker_name": "Bo"}]
    for seg in near["transcripts"]:
        seg["speaker_id"] = 1
    for seg in far["transcripts"]:
        seg["speaker_id"] = 2

    before, _ = run_merge(tmp_path / "x", {"a": near, "b": far}, {}, output="1.txt")
    marker = next(s for s, _ in turns(before) if "?" in s)
    config = {"speaker_corrections": [
        {"pattern": re.escape(marker), "replace": "Ada", "note": "context"}]}
    after, _ = run_merge(tmp_path / "y", {"a": near, "b": far}, config, output="2.txt")

    speakers = [s for s, _ in turns(after)]
    assert "?" not in " ".join(speakers)
    assert all(a != b for a, b in zip(speakers, speakers[1:])), \
        f"same speaker twice in a row: {speakers}"


def test_a_speaker_rule_also_applies_when_interleaving(tmp_path):
    """The two merge paths group turns separately; both must honour the rule."""
    a, b = separate_mics()
    plain, _ = run_merge(tmp_path / "x", {"a": a, "b": b}, {}, output="1.txt")
    label = next(s for s, _ in turns(plain))
    config = {"speaker_corrections": [
        {"pattern": re.escape(label), "replace": "Ada", "note": "test"}]}
    fixed, _ = run_merge(tmp_path / "y", {"a": a, "b": b}, config, output="2.txt")
    assert any(s == "ADA" for s, _ in turns(fixed)), \
        f"rule ignored on the interleave path: {[s for s, _ in turns(fixed)]}"


def test_a_time_settles_a_speaker_a_pattern_cannot_reach(tmp_path):
    """One recording gives a misattributed turn no distinguishing label.

    A pattern rule matches the speaker's name, which is identical on every
    turn they took, so settling one of them by pattern relabels all of them.
    Addressing it by time is the only way to say which cue is meant.
    """
    solo = one_recording()
    before, _ = run_merge(tmp_path / "x", {"s": solo}, {}, output="1.txt")
    assert [s for s, _ in turns(before)] == ["ADA", "BO", "ADA"]

    config = {"speaker_corrections": [
        {"at": "00:05", "replace": "Ada", "note": "context"}]}
    after, _ = run_merge(tmp_path / "y", {"s": solo}, config, output="2.txt")
    assert "BO" not in [s for s, _ in turns(after)], "the timed fix missed"
    assert "SPEAKERS CORRECTED" in after and "@00:05" in after


def test_a_timed_fix_joins_the_turn_it_belongs_to(tmp_path):
    """Same invariant as the marker case: relabel, then group -- not the reverse.

    Ada either side of a cue that was really Ada is one turn, not three.
    """
    config = {"speaker_corrections": [{"at": 5.0, "replace": "Ada", "note": "x"}]}
    after, _ = run_merge(tmp_path, {"s": one_recording()}, config)
    spoken = turns(after)
    assert len(spoken) == 1, f"expected one merged turn, got {spoken}"
    assert spoken[0][1].split() == ["alpha", "bravo", "charlie", "delta",
                                    "echo", "foxtrot"]


def test_a_timed_fix_moves_the_whole_turn_not_one_cue(tmp_path):
    """A turn is cut into a cue per silence, and a reader cannot see the cuts.

    Found on a real transcript: "it is right." was two cues, so a fix aimed at
    it moved the word "it" to the other speaker and left "is right." behind --
    a split turn that reads as an interruption that never happened.
    """
    solo = doc("solo", [segment("s1", 1, "1", 0.5, ["alpha", "bravo"]),
                        segment("s2", 2, "2", 5.0, ["it"]),
                        segment("s3", 2, "2", 6.0, ["is", "right"]),
                        segment("s4", 1, "1", 9.0, ["echo"])],
               speakers=[{"id": 1, "speaker_name": "Ada"},
                         {"id": 2, "speaker_name": "Bo"}])
    before, _ = run_merge(tmp_path / "x", {"s": solo}, {}, output="1.txt")
    assert [t for _, t in turns(before)][1] == "it is right", turns(before)

    config = {"speaker_corrections": [{"at": "00:05", "replace": "Ada"}]}
    after, _ = run_merge(tmp_path / "y", {"s": solo}, config, output="2.txt")
    assert len(turns(after)) == 1, f"the turn was split, not moved: {turns(after)}"


def test_a_range_moves_words_out_of_the_middle_of_a_turn(tmp_path):
    """The case no whole-turn fix can reach.

    Otter attributes a whole segment to one person, so when it puts two
    speakers in one segment there is no boundary to move: the other person's
    words sit mid-paragraph. A range cuts the cue where the speaker actually
    changed, which is not where a silence happened to fall.
    """
    solo = doc("solo", [segment("s1", 1, "1", 0.0,
                                ["mine", "mine", "yours", "yours", "mine"],
                                gap=1.0)],
               speakers=[{"id": 1, "speaker_name": "Ada"}])
    before, _ = run_merge(tmp_path / "x", {"s": solo}, {}, output="1.txt")
    assert len(turns(before)) == 1, turns(before)

    # "yours yours" starts at 2.0 and the last of them ends at 3.3.
    config = {"speaker_corrections": [
        {"from": 2.0, "to": 3.4, "replace": "Bo", "note": "test"}]}
    after, _ = run_merge(tmp_path / "y", {"s": solo}, config, output="2.txt")
    assert [(s, t) for s, t in turns(after)] == [
        ("ADA", "mine mine"), ("BO", "yours yours"), ("ADA", "mine")], turns(after)


def test_a_range_written_as_the_transcript_prints_it_covers_the_whole_second(tmp_path):
    """MM:SS is floored on the way out, so it must be generous on the way in.

    A turn printed as ending 00:03 has words running to 00:03.9; a range
    copied off the page that stopped at 00:03.0 would leave them behind.
    """
    solo = doc("solo", [segment("s1", 1, "1", 0.0,
                                ["mine", "yours", "yours"], gap=1.7)],
               speakers=[{"id": 1, "speaker_name": "Ada"}])
    config = {"speaker_corrections": [
        {"from": "00:01", "to": "00:03", "replace": "Bo"}]}   # last word at 3.4
    after, _ = run_merge(tmp_path, {"s": solo}, config)
    assert [(s, t) for s, t in turns(after)] == [
        ("ADA", "mine"), ("BO", "yours yours")], turns(after)


def test_overlapping_ranges_are_an_error(tmp_path):
    """Nose-to-tail ranges share a second, and the later one wins silently.

    `to` covers the whole second -- the transcript floors on the way out, so a
    range copied off the page has to be generous on the way in -- and `from`
    starts at the top of one. So ranges written nose to tail overlap, the third
    rule below takes back the word the second moved, and Bo vanishes from a
    transcript whose config plainly names him.

    Partitioning a block into three ranges is the recommended way to refine a
    decision, which makes nose to tail the natural thing to write. Caught
    before anything is written rather than left to the reader: the alternative
    was noticing a missing speaker in the output, which is exactly the kind of
    attention this tool exists to stop spending.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    solo = doc("solo", [segment("s1", 1, "1", 10.0,
                                ["one", "two", "three", "four", "five", "six"],
                                gap=1.0)],
               speakers=[{"id": 1, "speaker_name": "Cass"}])
    (tmp_path / "s.json").write_text(json.dumps(solo))
    (tmp_path / "config.json").write_text(json.dumps({"speaker_corrections": [
        {"from": "00:10", "to": "00:12", "replace": "Ada"},
        {"from": "00:12", "to": "00:12", "replace": "Bo"},
        {"from": "00:12", "to": "00:15", "replace": "Ada"}]}))
    result = subprocess.run(
        [sys.executable, "-m", "otter.fetch", "merge", str(tmp_path / "s.json"),
         "-c", str(tmp_path / "config.json"), "-o", str(tmp_path / "out.txt")],
        cwd=ROOT, capture_output=True, text=True)
    assert result.returncode != 0, "overlapping ranges were accepted"
    assert "overlap" in result.stderr and "00:12" in result.stderr, result.stderr
    assert not (tmp_path / "out.txt").exists(), \
        "a transcript was written from an ambiguous config"


def test_ranges_on_distinct_seconds_partition_a_block(tmp_path):
    """The correct form of the above: three ranges, three blocks, no overlap.

    This is the pattern the skill recommends for refining a decision about a
    block -- Ada / Bo / Ada rather than a block rule plus an exception -- so it
    has to keep working once overlap is rejected.
    """
    solo = doc("solo", [segment("s1", 1, "1", 10.0,
                                ["one", "two", "three", "four", "five", "six"],
                                gap=1.0)],
               speakers=[{"id": 1, "speaker_name": "Cass"}])
    config = {"speaker_corrections": [
        {"from": "00:10", "to": "00:11", "replace": "Ada"},
        {"from": "00:12", "to": "00:12", "replace": "Bo"},
        {"from": "00:13", "to": "00:15", "replace": "Ada"}]}
    out, _ = run_merge(tmp_path, {"s": solo}, config)
    assert [(s, t) for s, t in turns(out)] == [
        ("ADA", "one two"), ("BO", "three"), ("ADA", "four five six")], turns(out)


def test_a_range_that_holds_no_words_is_an_error(tmp_path):
    """Same rule as a time that names no cue: never claim a fix that did not run."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "s.json").write_text(json.dumps(one_recording()))
    (tmp_path / "config.json").write_text(json.dumps({"speaker_corrections": [
        {"from": "04:00", "to": "04:10", "replace": "Ada"}]}))
    result = subprocess.run(
        [sys.executable, "-m", "otter.fetch", "merge", str(tmp_path / "s.json"),
         "-c", str(tmp_path / "config.json"), "-o", str(tmp_path / "out.txt")],
        cwd=ROOT, capture_output=True, text=True)
    assert result.returncode != 0 and "04:00" in result.stderr
    assert not (tmp_path / "out.txt").exists()


def test_a_time_that_names_no_cue_is_an_error_not_a_silent_pass(tmp_path):
    """A fix that quietly matched nothing would still be claimed in the footer."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "s.json").write_text(json.dumps(one_recording()))
    (tmp_path / "config.json").write_text(json.dumps({"speaker_corrections": [
        {"at": "04:00", "replace": "Ada", "note": "nothing is there"}]}))
    result = subprocess.run(
        [sys.executable, "-m", "otter.fetch", "merge", str(tmp_path / "s.json"),
         "-c", str(tmp_path / "config.json"), "-o", str(tmp_path / "out.txt")],
        cwd=ROOT, capture_output=True, text=True)
    assert result.returncode != 0, "a fix that matched nothing was reported as applied"
    assert "04:00" in result.stderr
    assert not (tmp_path / "out.txt").exists(), "wrote a transcript it could not honour"


def test_saved_documents_carry_no_credentials_or_emails():
    """Otter's speech document embeds live credentials; we save it to disk.

    `audio_url` is a presigned S3 URL holding an AWS temporary access key --
    one reached a public repo on 2026-07-25 and was flagged by GitHub's secret
    scanner. The fetch boundary must drop it, and everything like it.
    """
    from otter.fetch import scrub

    doc = {
        "title": "t", "duration": 1, "transcripts": [], "process_failed": False,
        "speech_processing_state": 4,
        "audio_url": "https://s3/x.mp3?AWSAccessKeyId=ASIA-EXAMPLE-NOT-A-REAL-KEY&Signature=s",
        "download_url": "https://s3/y.mp3?AWSAccessKeyId=ASIA-EXAMPLE-NOT-A-REAL-KEY",
        "pubsub_jwt": "eyJhbGciOiJIUzI1NiJ9.e30.sig",
        "owner": {"email": "someone@example.com"},
        "shared_emails": ["someone@example.com"],
        "calendar_guests": [{"email": "someone@example.com"}],
        "speakers": [{"id": 1, "speaker_name": "Ada",
                      "speaker_email": "ada@example.com", "user_id": 42}],
    }
    blob = json.dumps(scrub(doc))
    for leak in ("ASIA", "AWSAccessKeyId", "Signature=", "eyJhbGci",
                 "@example.com", "pubsub", "user_id"):
        assert leak not in blob, f"{leak!r} survived the scrub: {blob}"

    kept = scrub(doc)
    assert kept["title"] == "t" and kept["duration"] == 1
    assert kept["speakers"] == [{"id": 1, "speaker_name": "Ada"}], \
        "the merge needs speaker id and name; scrub must keep exactly those"


def test_every_saved_speech_document_goes_through_the_scrub():
    """The scrub sits at the save boundary, so every save must use it.

    A source check rather than a behavioural one: the risk is a *new* write
    site added later that forgets, and no fixture can fail for code that does
    not exist yet.
    """
    source = (ROOT / "otter" / "fetch.py").read_text()
    saves = re.findall(r"write_text\(json\.dumps\((.*?)\), encoding", source)
    speech_saves = [s for s in saves if "config" not in s]
    assert speech_saves, "no speech-document save sites found; did they move?"
    unscrubbed = [s for s in speech_saves if not s.startswith("scrub(")]
    assert not unscrubbed, f"save sites bypassing the scrub: {unscrubbed}"


def test_a_single_recording_needs_no_merge(tmp_path):
    """One recording must not be routed to reconciliation.

    The pairwise overlap check asks whether every pair of tracks overlaps.
    With one track there are no pairs, so the answer is vacuously yes and a
    lone recording was sent to `reconcile`, which refuses anything under two.
    """
    from otter.fetch import choose_strategy

    a, _ = separate_mics()
    mode, _, why = choose_strategy({"only": a})
    assert mode == "interleave", f"a single recording chose {mode!r}: {why}"


def test_a_scrubbed_document_still_reads_as_finished():
    """The scrub must not blind the poller to a recording that is done.

    `ready()` gates on four boolean flags. An allowlist that drops them makes
    every recording look permanently unfinished, so `wait()` polls until it
    times out on work Otter finished minutes earlier.
    """
    from otter.fetch import ready, scrub

    finished = {
        "upload_finished": True, "process_finished": True,
        "diarization_finished": True, "realign_finished": True,
        "process_failed": False, "speech_processing_state": "ALL_DONE",
        "duration": 700, "title": "t", "transcripts": [], "speakers": [],
        "audio_url": "https://s3/x.mp3?AWSAccessKeyId=ASIA-EXAMPLE-NOT-A-REAL-KEY",
    }
    assert ready(finished)[0], "fixture is wrong: the raw document is finished"
    done, state = ready(scrub(finished))
    assert done, f"scrub hid the readiness flags; poller would spin: {state}"
