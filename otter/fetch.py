#!/usr/bin/env python3
"""Drive Otter.ai from the command line: upload, wait, fetch, merge.

Otter's *official* Public API is Enterprise-only and read-only, so a Business
account has to talk to the same private endpoints the web app uses -- the ones
gmchad/otterai-api reverse-engineered. Those endpoints are spoken directly
here rather than through the package, for two reasons: its download writes to
disk when what we want is an in-memory document, and depending on nothing but
`requests` keeps this installable anywhere without a fork to maintain.

Transcripts come from the `speech` endpoint, which carries per-word timings.
See otter/speech.py for why nothing else will do.

Because these are private endpoints, they can change without notice. Every
call checks its response shape and says so loudly rather than half-working.

`run` and `pull` also decide how to merge. One microphone per person means
interleaving the tracks; several devices recording one room means reconciling
their accounts of it. Which applies is measured, not asked -- see
`choose_strategy`, `otter/speech.py` and `otter/reconcile.py`.

Usage
-----
    python -m otter.fetch login                     # store a credential
    python -m otter.fetch check                     # does it still work
    python -m otter.fetch list                      # recent recordings + otids
    python -m otter.fetch probe <otid>              # ready? aligned? named?
    python -m otter.fetch tag <otid> "Speaker 1=Ada"   # name a speaker

    # the whole thing, one command per recording session
    python -m otter.fetch run track1.m4a track2.m4a -c cfg.json -o merged.txt

    # or, when Otter already has the audio
    python -m otter.fetch pull <otid> <otid> -c cfg.json -o merged.txt -d transcripts/

`pull -d` saves each speech document, so a merge can be re-run offline with a
changed config and no network:

    python -m otter.speech transcripts/<otid>.json ... -c cfg.json -o merged.txt

Credentials never pass through here as arguments -- see otter/credentials.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path

import requests

from otter.credentials import (COOKIE_FILE, CredentialError, Credentials,
                               cookies_from_curl, resolve)
from otter.speech import (GAP_SECONDS, build, is_placeholder,
                          observed_speakers, tracks_from_speeches)
from otter.reconcile import (aliases_for, cues_from_merged, estimate_offset,
                             fold, reconcile, word_stream)
from otter.transcript import correct_speakers, group_turns, render_text, ts

API = "https://otter.ai/forward/api/v1/"
S3_UPLOAD_URL = "https://s3.us-west-2.amazonaws.com/speech-upload-prod"


class OtterError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------

@dataclass
class Otter:
    session: requests.Session
    userid: str
    source: str

    @property
    def csrf(self) -> str:
        token = self.session.cookies.get("csrftoken")
        if not token:
            raise OtterError("no csrftoken cookie; the login did not complete")
        return token


def _userid_from(payload: dict) -> str | None:
    """Otter has moved this field around; accept the shapes we have seen."""
    for path in (("userid",), ("id",), ("data", "userid"), ("data", "id"),
                 ("data", "user", "id"), ("user", "id")):
        node = payload
        for key in path:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if node is not None:
            return str(node)
    return None


def connect(creds: Credentials | None = None) -> Otter:
    creds = creds or resolve()
    session = requests.Session()
    session.headers.update({"Referer": "https://otter.ai/",
                            "Origin": "https://otter.ai"})

    if creds.is_cookie:
        for part in creds.cookies.split(";"):
            if "=" in part:
                name, _, value = part.strip().partition("=")
                session.cookies.set(name, value, domain=".otter.ai")
        response = session.get(API + "user")
        if not response.ok:
            raise OtterError(
                f"cookie auth rejected (HTTP {response.status_code}). "
                "Log in to otter.ai again and re-copy the cookie header.")
    else:
        session.auth = (creds.username, creds.password)
        response = session.get(API + "login", params={"username": creds.username})
        if response.status_code in (401, 403):
            raise OtterError(
                f"login rejected (HTTP {response.status_code}) for the credential "
                f"from {creds.source}. If this account signs in with Google/SSO "
                "there is no password to send -- use the OTTER_COOKIES path.")
        if not response.ok:
            raise OtterError(f"login failed: HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError:
        raise OtterError("login returned non-JSON; the endpoint has likely moved")

    userid = _userid_from(payload)
    if not userid:
        raise OtterError(f"no user id in the login response; keys={list(payload)}")
    return Otter(session, userid, creds.source)


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def speeches(otter: Otter, page_size: int = 45, folder: int = 0) -> list[dict]:
    """Recent recordings. No pagination -- the API's first page only."""
    response = otter.session.get(API + "speeches", params={
        "userid": otter.userid, "folder": folder,
        "page_size": page_size, "source": "owned"})
    if not response.ok:
        raise OtterError(f"list failed: HTTP {response.status_code}")
    payload = response.json()
    items = payload.get("speeches") or payload.get("data", {}).get("speeches") or []
    if not isinstance(items, list):
        raise OtterError(f"unexpected list shape; keys={list(payload)}")
    return items


def speech(otter: Otter, otid: str) -> dict:
    """The full transcript document, including per-word `alignment` timings.

    This is what the merge is built on -- see otter/speech.py.
    """
    response = otter.session.get(API + "speech",
                                 params={"userid": otter.userid, "otid": otid})
    if not response.ok:
        raise OtterError(f"speech {otid} failed: HTTP {response.status_code}")
    doc = response.json().get("speech")
    if not doc:
        raise OtterError(f"no speech in response for {otid}")
    return doc


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------

def upload(otter: Otter, path: str | Path,
           content_type: str = "audio/mp4") -> str:
    """Send audio to Otter for transcription; return the new otid.

    Three steps, mirroring what the web app does: ask for a pre-signed S3
    policy, POST the file to S3, then tell Otter the object is there. Written
    out here rather than taken from otterai-api so the only dependency is
    `requests` -- one less reverse-engineered package to keep installable.
    """
    path = Path(path)
    params = otter.session.get(API + "speech_upload_params",
                               params={"userid": otter.userid})
    if not params.ok:
        raise OtterError(f"upload params failed: HTTP {params.status_code}")
    policy = params.json()["data"]
    bucket_url = policy.pop("form_action", S3_UPLOAD_URL)
    policy["success_action_status"] = str(policy["success_action_status"])

    with path.open("rb") as handle:
        # S3 requires the policy fields to precede the file part.
        posted = requests.post(bucket_url, data=policy,
                               files={"file": (path.name, handle, content_type)})
    if posted.status_code != 201:
        raise OtterError(f"S3 upload failed: HTTP {posted.status_code}\n{posted.text[:300]}")

    root = ET.fromstring(posted.text)
    found = {child.tag.split("}")[-1]: child.text for child in root}
    if not {"Bucket", "Key"} <= set(found):
        raise OtterError(f"S3 reply missing Bucket/Key; got {list(found)}")

    # `appid` is required and was not when otterai-api was written; without it
    # this returns 400 {"message": "missing appid"}. A good canary for drift.
    done = otter.session.get(API + "finish_speech_upload", params={
        "bucket": found["Bucket"], "key": found["Key"], "appid": "web",
        "language": "en", "country": "us", "userid": otter.userid})
    if not done.ok:
        raise OtterError(f"finish_speech_upload failed: HTTP {done.status_code}"
                         f"\n{done.text[:300]}")
    payload = done.json()
    otid = (payload.get("speech", {}) or {}).get("otid") or payload.get("otid")
    if not otid:
        raise OtterError(f"no otid in finish response; keys={list(payload)}")
    return otid


# --------------------------------------------------------------------------
# waiting
# --------------------------------------------------------------------------

def ready(speech: dict) -> tuple[bool, str]:
    """Is this recording finished enough to have word-level alignment?

    `realign_finished` is the one that matters: it gates the `alignment`
    arrays, without which segmentation falls back to padded segment offsets.
    """
    if str(speech.get("process_failed")) == "True":
        raise OtterError("Otter reports process_failed for this recording")
    flags = {k: str(speech.get(k)) == "True" for k in
             ("upload_finished", "process_finished", "diarization_finished",
              "realign_finished")}
    pending = [k for k, done in flags.items() if not done]
    state = speech.get("speech_processing_state", "?")
    return not pending, f"{state}; waiting on {pending}" if pending else str(state)


def transcribed_words(speech: dict) -> int:
    return sum(len(s.get("alignment") or []) for s in speech.get("transcripts") or [])


def wait(otter: Otter, otid: str, deadline: float, interval: float = 15,
         log=lambda m: None) -> dict:
    """Poll until this recording is fully processed, or `deadline` passes.

    `deadline` is an absolute time.monotonic() value rather than a duration,
    so several calls can share one budget instead of each getting the full
    timeout and multiplying it by the number of tracks.

    Every status flag can read done while the transcript is still filling in:
    a 5-minute recording reported process_finished, realign_finished,
    diarization_finished and ALL_DONE with only the first 3m45s transcribed,
    then grew from 517 to 732 words. Flags are necessary and not sufficient,
    so also require the word count to be unchanged across two polls. Otherwise
    a merge silently loses the tail of a recording, which reads as though the
    conversation simply ended.
    """
    settled: int | None = None
    while True:
        doc = speech(otter, otid)
        done, state = ready(doc)
        if done:
            count = transcribed_words(doc)
            if settled == count:
                return doc
            log(f"  {otid}: flags done at {count} words; confirming it has settled")
            settled = count
        left = deadline - time.monotonic()
        if left <= 0:
            raise OtterError(
                f"timed out waiting on {otid} ({state}). The upload survives -- "
                f"resume with:  python -m otter.fetch pull {otid} ... --wait")
        log(f"  {otid}: {state} ({left / 60:.0f} min left)")
        time.sleep(min(interval, left))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def _when(value) -> str:
    """Otter returns creation times as epoch seconds; show a date instead."""
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value or "")[:10]


STEPS = """  Getting the credential — Otter has no public API on this plan, so this
  borrows the session your browser already has.

    1. Log in at https://otter.ai
    2. Open developer tools:  Cmd-Opt-I  (or Menu > Tools > Browser Tools)
    3. Click the Network tab
    4. Reload the page (Cmd-R) — the Network tab records nothing until you do,
       so it looks empty and people assume it is broken
    5. Click any row whose Domain is otter.ai
    6. Right-click that row > Copy > Copy as cURL
       (Firefox may label it "Copy as cURL (POSIX)")
    7. Paste it below

  The paste will not be shown. It goes straight into %s, which is
  gitignored and readable only by you. No password is involved; the session
  expires on its own and you revoke it by logging out."""


def read_paste(prompt: str) -> str:
    """Read a pasted cURL command, however many lines it spans.

    Both Firefox and Chrome break the command across lines with trailing
    backslashes, so the command itself says where it ends -- the same
    continuation rule the shell uses. Reading until a line does not end in a
    backslash means a paste completes on its own, with nothing extra to press.

    Reading only one line would be worse than inconvenient: the remaining
    lines would sit in the terminal and the shell would run them as commands
    the moment this exits.

    Echo is off for the whole read, so the credential stays out of scrollback.
    """
    sys.stderr.write(prompt)
    sys.stderr.flush()
    saved = None
    try:
        import termios
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        quiet = termios.tcgetattr(fd)
        quiet[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSADRAIN, quiet)
    except Exception:
        pass                              # not a terminal; read plainly
    lines: list[str] = []
    try:
        for line in sys.stdin:
            body = line.rstrip("\n")
            lines.append(body.rstrip("\\"))
            if not body.rstrip().endswith("\\"):
                break
    finally:
        if saved is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
        sys.stderr.write("\n")
    return " ".join(lines)


def cmd_login(args) -> int:
    """Turn a copied cURL command into a stored credential."""
    if args.file:
        raw = Path(args.file).read_text(encoding="utf-8")
    else:
        print(STEPS % COOKIE_FILE.name + "\n")
        try:
            raw = read_paste("  Paste here: ")
        except (EOFError, KeyboardInterrupt):
            print("\n  cancelled", file=sys.stderr)
            return 1
    header = cookies_from_curl(raw)
    names = [c.split("=")[0] for c in header.split("; ") if c]
    if "sessionid" not in names:
        raise OtterError(
            f"  that paste has no session cookie in it ({len(names)} cookies "
            "found).\n  Check the row you copied was a request to otter.ai, "
            "and that you\n  used Copy as cURL rather than copying the URL.")

    # Verify before writing, never after. A paste can carry a syntactically
    # perfect cookie header for an expired or wrong session, and storing it
    # first would destroy a working credential to make room for a broken one.
    connect(Credentials("pasted", cookies=header))
    COOKIE_FILE.write_text(header + "\n", encoding="utf-8")
    COOKIE_FILE.chmod(0o600)
    # Say it worked and nothing else. Listing what was stored reads as a leak
    # to someone who was just told the paste would not be shown.
    print(f"  signed in. Saved to {COOKIE_FILE.name}", file=sys.stderr)
    return 0


def cmd_check(args) -> int:
    otter = connect()
    print(f"logged in via {otter.source}; userid={otter.userid}")
    return 0


def cmd_list(args) -> int:
    otter = connect()
    items = speeches(otter, page_size=args.limit)
    print(f"{len(items)} recording(s) via {otter.source}\n")
    for item in items:
        otid = item.get("otid") or item.get("speech_otid") or "?"
        title = (item.get("title") or "(untitled)").strip()[:52]
        seconds = item.get("duration") or 0
        created = _when(item.get("created_at") or item.get("start_time"))
        print(f"  {otid:<28} {ts(seconds):>7}  {created:<12} {title}")
    if not items:
        print("  (none -- if you expect some, the list response shape may have moved)")
    return 0


def cmd_probe(args) -> int:
    """Check a recording carries what the merge needs, before trusting it."""
    otter = connect()
    doc = speech(otter, args.otid)
    done, state = ready(doc)
    segments = doc.get("transcripts") or []
    aligned = [s for s in segments if s.get("alignment")]
    named = {s["id"]: s["speaker_name"] for s in (doc.get("speakers") or [])}
    orphans = [s for s in segments if not s.get("speaker_id")]

    print(f"otid {args.otid}  ({doc.get('title') or 'untitled'})")
    print(f"  state:     {state}{'' if done else '   <-- NOT READY'}")
    print(f"  segments:  {len(segments)}, {len(aligned)} with word alignment")
    print(f"  speakers:  {sorted(named.values()) or '(none named)'}")
    print(f"  orphans:   {len(orphans)} segment(s) Otter would not attribute")
    if not segments:
        return 1
    if len(aligned) < len(segments):
        print(f"\n  !! {len(segments) - len(aligned)} segment(s) lack `alignment`. Those fall"
              "\n     back to padded segment offsets, so their words may sit minutes"
              "\n     from where they were spoken. Re-check once realign_finished.")
    if not named:
        print("\n  !! No named speakers. Name them once in the Otter UI and every"
              "\n     future export of this recording carries the names.")
    return 0 if done and aligned else 1


def cmd_pull(args) -> int:
    """Fetch speech documents and optionally merge them in one step."""
    otter = connect()
    default_paths(args, args.otids)
    prepare_destinations(args)
    deadline = time.monotonic() + args.timeout
    docs = {}
    for otid in args.otids:
        doc = wait(otter, otid, deadline, log=lambda m: print(m, file=sys.stderr)) \
            if args.wait else speech(otter, otid)
        docs[otid] = doc
        if args.dir:
            out = Path(args.dir) / f"{otid}.json"
            out.write_text(json.dumps(doc), encoding="utf-8")
            print(f"wrote {out}", file=sys.stderr)
        print(f"  {otid}: {len(doc.get('transcripts') or [])} segments, "
              f"{ts(float(doc.get('duration') or 0))}", file=sys.stderr)
    return _merge(docs, args)



def create_speaker(otter: Otter, name: str) -> int:
    """Make a new speaker profile and return its id."""
    response = otter.session.post(
        API + "create_speaker", params={"userid": otter.userid},
        headers={"x-csrftoken": otter.csrf}, data={"speaker_name": name})
    if not response.ok:
        raise OtterError(f"create_speaker({name!r}) failed: HTTP {response.status_code}")
    return response.json()["speaker"]["id"]


def set_segment_speaker(otter: Otter, otid: str, uuid: str, name: str,
                        speaker_id: int) -> None:
    response = otter.session.get(API + "set_transcript_speaker", params={
        "speech_otid": otid, "transcript_uuid": uuid, "speaker_name": name,
        "speaker_id": speaker_id, "create_speaker": "false",
        "userid": otter.userid})
    if not response.ok:
        raise OtterError(f"set_transcript_speaker failed: HTTP {response.status_code}")


def redo_speaker_match(otter: Otter, otid: str) -> str:
    """Ask Otter to re-run voiceprint matching over the whole recording.

    Queued, not immediate -- Otter's own reply says 10-15 minutes. It is what
    the web UI's "re-match speakers" button calls, and it is why tagging one
    segment appears to do nothing and then, later, to have worked.
    """
    response = otter.session.post(
        API + "redo_speaker_match", params={"userid": otter.userid},
        headers={"x-csrftoken": otter.csrf}, data={"otid": otid})
    if not response.ok:
        raise OtterError(f"redo_speaker_match failed: HTTP {response.status_code}")
    return response.json().get("message", "accepted")


def tag(otter: Otter, otid: str, assignments: dict[str, str],
        every: bool = True, rematch: bool = True,
        log=lambda m: None) -> dict[str, int]:
    """Name diarisation clusters without opening a browser.

    Sets every segment in the cluster, which is deliberate. Naming a single
    segment in the web UI does not reliably spread to the rest: an already
    trained voiceprint may match the others, but a new person needs a "re-match
    speakers" action that has no endpoint I could find. Writing each segment
    needs no propagation to happen at all, and the calls are cheap.

    `every=False` sets only the first and leans on re-match instead, which is
    queued for 10-15 minutes -- fine overnight, useless in a pipeline.

    Either way `rematch` then asks Otter to re-run matching, which is how
    segments in *other* clusters that are really the same person get picked up.
    That result arrives later; it never blocks the merge.

    `assignments` maps a cluster label ("Speaker 1", or a name Otter already
    matched) to the name it should carry.
    """
    doc = speech(otter, otid)
    known = {s["speaker_name"]: s["id"] for s in doc.get("speakers") or []}
    names = {s["id"]: s["speaker_name"] for s in doc.get("speakers") or []}
    anonymous = not names

    buckets: dict[str, list[dict]] = {}
    for segment in doc.get("transcripts") or []:
        cluster = segment.get("speaker_model_label")
        label = (names.get(segment.get("speaker_id"))
                 or (f"Speaker {cluster}" if cluster else "Unattributed"))
        buckets.setdefault(label, []).append(segment)

    tagged = {}
    for label, name in assignments.items():
        segments = buckets.get(label)
        if not segments:
            raise OtterError(f"no segments labelled {label!r}; have {sorted(buckets)}")
        speaker_id = known.get(name) or create_speaker(otter, name)
        known[name] = speaker_id
        chosen = segments if every else segments[:1]
        for segment in chosen:
            set_segment_speaker(otter, otid, segment["uuid"], name, speaker_id)
        tagged[name] = len(chosen)
        log(f"  {label} -> {name}: set {len(chosen)} of {len(segments)} segment(s)")
    if tagged and rematch:
        log("  " + redo_speaker_match(otter, otid))
    return tagged


# Zoom names each per-participant file audio<TheirName><index><meeting-id>.
_ZOOM_TITLE = re.compile(r"^audio([A-Za-z]+?)\d+$")


def participant_name(title: str | None) -> str | None:
    """The person a Zoom multi-track file is named after, if it is one.

    Returned exactly as Zoom wrote it, spaces already stripped:
    "AdaSmith", not "Ada Smith". Splitting the camel case back into
    words would be guessing -- it turns McDonald into "Mc Donald" -- and the
    original spacing is not recoverable. A name obviously run together beats
    one that is subtly wrong.
    """
    match = _ZOOM_TITLE.match(title or "")
    return match.group(1) if match else None


def _guess_aliases(doc: dict) -> dict[str, str]:
    """Suggest speaker names from a Zoom filename, for the config to confirm.

    Never overrides Otter: a voiceprint match is better evidence than a
    filename, so a named speaker is left alone.

    Where Otter recognised nobody, the filename is the only name available and
    how far it can be trusted depends on what the track holds. One anonymous
    voice on their own track is that person. Several are not -- a participant's
    microphone still picks up whoever is sitting beside them, so a file named
    for one person can hold two voices. There the name is kept as the mic it
    came from rather than claimed for either voice, which at least says whose
    side of the room each speaker was on.
    """
    labels = observed_speakers(doc)
    name = participant_name(doc.get("title"))
    people = [x for x in labels if x != "Unattributed"]
    if not name or not all(is_placeholder(x, {}) for x in people):
        return {label: label for label in labels}
    if len(people) == 1:
        return {label: name for label in labels}
    return {label: (f"{name}-{label}" if label != "Unattributed" else label)
            for label in labels}


def _slug(title: str, otid: str) -> str:
    words = re.findall(r"[A-Z]?[a-z]+", re.sub(r"\d+", " ", title or ""))
    return "-".join(w.lower() for w in words if w.lower() != "audio") or otid[:8]


def scaffold(docs: dict[str, dict], path: Path) -> dict:
    """Write a starter config so a first run never needs one written by hand.

    Every speaker label the recording will actually produce is listed, mapped
    to itself. Replace the values with real names -- for untagged people those
    read "Speaker 1", "Speaker 2" -- and re-run. Nothing is guessed, so a
    config left untouched still merges correctly, just with Otter's labels.
    """
    config = {
        "_comment": f"Scaffolded by otter.fetch. Edit the aliases values, then re-run.",
        "tracks": {otid: _slug(doc.get("title", ""), otid) for otid, doc in docs.items()},
        "gap_seconds": GAP_SECONDS,
        "aliases": {}, "corrections": [],
    }
    for otid, doc in docs.items():
        name = config["tracks"][otid]
        config["aliases"][name] = _guess_aliases(doc)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def choose_strategy(docs: dict[str, dict]) -> tuple[str, float | None, str]:
    """Interleave separate microphones, or reconcile two accounts of one room?

    Decided from how much text the tracks share, which separates the two cases
    by two orders of magnitude. One mic per person: each track hears a
    different voice, so they share almost nothing -- 1.7% on a real Zoom pair,
    and the little they do share is bleed at wildly inconsistent offsets. Two
    devices on a table: both hear everyone, so they share nearly everything --
    95.2%, at one consistent offset.

    That consistency is the real test, not the raw overlap, and it is the same
    check that decides whether a clock offset can be trusted at all.
    """
    streams = {name: word_stream(doc) for name, doc in docs.items()}

    if len(docs) != 2:
        # Reconciliation is pairwise, so more than two tracks always
        # interleaves. That is right for a Zoom recording with four
        # participants and catastrophic for three phones on a table: every
        # sentence would appear once per device, attributed to a different
        # speaker each time, and the transcript would read as plausible. So
        # check the pairs before assuming.
        pairs = list(combinations(streams.items(), 2))
        overlapping = [f"{x} and {y}" for (x, a), (y, b) in pairs
                       if a and b and estimate_offset(a, b).trustworthy]
        if len(overlapping) == len(pairs):
            return ("reconcile", None,
                    f"all {len(docs)} tracks recorded the same room")
        if overlapping:
            return ("refuse", None,
                    f"{len(docs)} tracks, and only some overlap: {'; '.join(overlapping)} "
                    "recorded the same room while the others did not.\n  Reconcile "
                    "the matching ones on their own with `python -m otter.reconcile`, "
                    "then merge that result, or set \"mode\" in the config.")
        return "interleave", None, f"{len(docs)} tracks, none of them overlapping"

    a, b = streams.values()
    if not a or not b:
        return "interleave", None, "a track has no words"
    est = estimate_offset(a, b)
    ok, why = est.verdict
    if ok:
        return "reconcile", est.seconds, f"{why}, so both devices recorded the same room"
    return "interleave", None, f"{why}, so the tracks are separate microphones"


def _merge(docs: dict[str, dict], args) -> int:
    if args.config and not Path(args.config).exists():
        config = scaffold(docs, Path(args.config))
        print(f"\nwrote a starter config to {args.config}:", file=sys.stderr)
        for track, aliases in config["aliases"].items():
            print(f"  {track}: {list(aliases)}", file=sys.stderr)
        print("  ^ put real names on the right-hand side, then re-run the same "
              "command.\n", file=sys.stderr)
    config = json.loads(Path(args.config).read_text()) if args.config else {}
    if not args.output:
        print("\nno -o given; nothing merged. Track names come from config['tracks']:\n  "
              + json.dumps({o: (doc.get("title") or o) for o, doc in docs.items()}, indent=2),
              file=sys.stderr)
        return 0
    mode, offset, why = (config["mode"], config.get("offset"), "set in the config") \
        if config.get("mode") else choose_strategy(docs)
    if mode == "refuse":
        raise OtterError(f"  refusing to merge: {why}")
    print(f"  {mode}: {why}", file=sys.stderr)

    if mode == "reconcile":
        a_doc, b_doc = (list(docs.values()) + [None, None])[:2]
        names = config.get("tracks", {})
        al = lambda otid: aliases_for(config, names.get(otid, otid))
        keys = list(docs)
        streams = [word_stream(d, aliases=al(k)) for k, d in docs.items()]
        if config.get("offset") is not None:      # only an explicit override
            merged = reconcile(word_stream(a_doc, aliases=al(keys[0])),
                               word_stream(b_doc, -config["offset"], al(keys[1])),
                               tuple(docs))
        else:
            # fold, even for a pair: it puts the earliest recording first, so
            # the answer does not depend on the order they were named.
            merged = fold(streams, keys, log=lambda m: print(m, file=sys.stderr))
        turns = group_turns(correct_speakers(
            cues_from_merged(merged, "reconciled"), config))
    else:
        tracks = tracks_from_speeches(docs, config)
        _, turns = build(tracks, config)
    Path(args.output).write_text(render_text(turns, config), encoding="utf-8")
    print(f"wrote {args.output}: {len(turns)} turns, {ts(turns[-1].end)} total",
          file=sys.stderr)
    return 0


SESSION_ROOT = Path("transcripts")


def session_dir(labels: list[str]) -> Path:
    """A stamped folder for one run, so a second never lands on the first.

    Everything a run produces belongs together -- the transcript, the config
    recording every decision, and the documents Otter returned. Flat in one
    directory, a second recording of the same meeting silently overwrote the
    first, and there is no natural key: the same conversation transcribed
    twice is two different things.

    Named by time and a short hash rather than by the recordings, so a folder
    listing never carries a participant's name.
    """
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    seed = "|".join(labels) + stamp + str(time.time_ns())
    digest = hashlib.sha256(seed.encode()).hexdigest()[:6]
    target, n = SESSION_ROOT / f"{stamp}-{digest}", 2
    while target.exists():
        target, n = target.with_name(f"{stamp}-{digest}-{n}"), n + 1
    return target


def default_paths(args, labels: list[str]) -> None:
    """Fill in -o/-c/-d from a stamped folder when the caller gave none."""
    if getattr(args, "output", None):
        return
    where = session_dir(labels)
    args.output = str(where / "transcript.txt")
    args.config = args.config or str(where / "config.json")
    args.dir = args.dir or str(where)
    print(f"  writing to {where}/", file=sys.stderr)


def prepare_destinations(args) -> None:
    """Make the output directories, and fail now rather than after uploading.

    `run` uploads first and writes last, so a missing output directory used to
    surface only once the recordings were already in Otter -- quota spent, and
    a crash instead of a transcript. Anything that would stop the result being
    written is checked here, before the first byte is sent.
    """
    try:
        for target in (getattr(args, "output", None), getattr(args, "config", None)):
            if target:
                Path(target).parent.mkdir(parents=True, exist_ok=True)
        if getattr(args, "dir", None):
            Path(args.dir).mkdir(parents=True, exist_ok=True)
        if getattr(args, "output", None):
            Path(args.output).touch(exist_ok=True)
    except OSError as exc:
        raise OtterError(f"  cannot write there: {exc}")


def cmd_run(args) -> int:
    """Fire and forget: upload every track, wait for all, merge.

    Uploads first and waits second, so Otter transcribes the tracks in
    parallel and the wall clock is the slowest track rather than their sum.
    Every otid is printed as soon as it exists, so a later failure never
    strands work -- `pull <otid> ... --wait` picks up exactly where this left.
    """
    otter = connect()
    default_paths(args, [Path(p).stem for p in args.paths])
    prepare_destinations(args)      # before spending any upload quota
    otids = []
    for path in args.paths:
        otid = upload(otter, path)
        otids.append(otid)
        print(f"uploaded {path} -> {otid}", file=sys.stderr)

    deadline = time.monotonic() + args.timeout
    docs = {}
    for otid in otids:
        docs[otid] = wait(otter, otid, deadline,
                          log=lambda m: print(m, file=sys.stderr))
        print(f"  {otid}: ready", file=sys.stderr)
        if args.dir:
            out = Path(args.dir) / f"{otid}.json"
            out.write_text(json.dumps(docs[otid]), encoding="utf-8")
            print(f"  saved {out}", file=sys.stderr)
    return _merge(docs, args)


def cmd_tag(args) -> int:
    """Name diarisation clusters: `tag <otid> "Speaker 1=Ada" "Speaker 2=Bo"`."""
    otter = connect()
    pairs = dict(a.split("=", 1) for a in args.assign)
    tag(otter, args.otid, pairs, every=args.every,
        rematch=not args.no_rematch, log=lambda m: print(m))
    return 0


def cmd_merge(args) -> int:
    """Re-merge saved speech documents, offline.

    The same choice `run` and `pull` make, without the network: applying a
    changed config should not need re-fetching, and should not need the caller
    to know whether these recordings get interleaved or reconciled.
    """
    # A session folder holds the documents and the config together, so
    # `merge *.json -c config.json` is the natural thing to type. Drop the
    # config rather than failing on it; anything else that is not a speech
    # document is still an error worth reporting.
    config_path = Path(args.config).resolve() if args.config else None
    docs = {}
    for path in args.inputs:
        if config_path and Path(path).resolve() == config_path:
            continue
        doc = json.loads(Path(path).read_text())
        if not isinstance(doc, dict) or not doc.get("transcripts"):
            raise OtterError(f"{path}: not an Otter speech document")
        docs[Path(path).stem] = doc
    if not docs:
        raise OtterError("no speech documents given")
    return _merge(docs, args)


def cmd_upload(args) -> int:
    print(upload(connect(), args.path))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    logger = sub.add_parser("login", help="store a credential from a copied cURL command")
    logger.add_argument("-f", "--file", help="read the cURL command from a file instead")
    logger.set_defaults(func=cmd_login)

    sub.add_parser("check", help="verify the credential works").set_defaults(func=cmd_check)

    lister = sub.add_parser("list", help="recent recordings with their otids")
    lister.add_argument("-n", "--limit", type=int, default=25)
    lister.set_defaults(func=cmd_list)

    prober = sub.add_parser("probe", help="does this recording have what the merge needs?")
    prober.add_argument("otid")
    prober.set_defaults(func=cmd_probe)

    puller = sub.add_parser("pull", help="fetch speech docs; optionally merge")
    puller.add_argument("otids", nargs="+")
    puller.add_argument("-d", "--dir", help="also save the raw speech JSON here")
    puller.add_argument("-c", "--config")
    puller.add_argument("-o", "--output", help="merge to this file")
    puller.add_argument("-w", "--wait", action="store_true",
                        help="poll until processing and word alignment finish")
    puller.add_argument("--timeout", type=float, default=3600)
    puller.set_defaults(func=cmd_pull)

    runner = sub.add_parser("run", help="upload audio, wait, merge -- the whole pipeline")
    runner.add_argument("paths", nargs="+", help="one audio file per track")
    runner.add_argument("-c", "--config")
    runner.add_argument("-o", "--output")
    runner.add_argument("-d", "--dir")
    runner.add_argument("--timeout", type=float, default=3600)
    runner.set_defaults(func=cmd_run)

    tagger = sub.add_parser("tag", help='name clusters: tag <otid> "Speaker 1=Ada"')
    tagger.add_argument("otid")
    tagger.add_argument("assign", nargs="+", metavar="LABEL=NAME")
    tagger.add_argument("--no-rematch", action="store_true",
                        help="skip asking Otter to re-run voiceprint matching")
    tagger.add_argument("--one", dest="every", action="store_false",
                        help="set only the first segment and rely on Otter to spread it")
    tagger.set_defaults(func=cmd_tag)

    merger = sub.add_parser("merge", help="re-merge saved speech documents, offline")
    merger.add_argument("inputs", nargs="+", metavar="SPEECH_JSON")
    merger.add_argument("-c", "--config")
    merger.add_argument("-o", "--output")
    merger.set_defaults(func=cmd_merge)

    uploader = sub.add_parser("upload", help="send audio to Otter to transcribe")
    uploader.add_argument("path")
    uploader.set_defaults(func=cmd_upload)

    args = parser.parse_args()
    try:
        return args.func(args)
    except (OtterError, CredentialError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
