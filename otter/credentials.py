"""Resolve Otter credentials without them ever appearing in a transcript.

Otter's public API is Enterprise-only, so a Business account has to use the
same private endpoints the web app uses -- which means a login. The point of
this module is that the secret is typed once, by you, into something that is
not a chat window or a shell argument, and is read only inside the process.

Nothing here prints a secret. `Credentials.__repr__` redacts, no function
writes one to stdout, and nothing is passed on a command line, which would put
it in `ps` output and shell history.

The usual way in is `python -m otter.fetch login`, which takes a "Copy as
cURL" from developer tools, checks it against Otter, and writes the file
below. The rest of this is for when that does not fit.

Resolution order, first hit wins
--------------------------------
1. `OTTER_COOKIES`, or `OTTER_COOKIES.txt` at the repo root -- a cookie header
                        from a logged-in browser, which is what `login` writes.
                        No password exists anywhere in this path; the session
                        expires on its own and you revoke it by logging out.
2. macOS Keychain    -- service `otter-transcribe`. Stored once, interactively:

                            security add-generic-password \\
                                -s otter-transcribe -a you@example.com -w

                        Run that in a real Terminal window (it needs a TTY to
                        prompt). Omitting the value after `-w` makes `security`
                        read it without echo, so it stays out of argv and out
                        of shell history.

3. `OTTER_EMAIL` + `OTTER_PASSWORD` -- exported in the shell you launched from.
                        Fine, though an account on Google or Microsoft SSO has
                        no password to send and must use the cookie path.
4. `OTTER_CREDENTIALS.txt` at the repo root -- two lines, email then password.
                        Gitignored. Plaintext on disk; the weakest of the four.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYCHAIN_SERVICE = "otter-transcribe"
CREDENTIALS_FILE = ROOT / "OTTER_CREDENTIALS.txt"
COOKIE_FILE = ROOT / "OTTER_COOKIES.txt"

# `security find-generic-password` prints attributes as  "acct"<blob>="value"
_ACCT = re.compile(r'"acct"<blob>="(.*)"')


class CredentialError(RuntimeError):
    """No usable credential was found. Message never contains a secret."""


@dataclass
class Credentials:
    """Either a username/password pair, or a browser cookie header."""

    source: str
    username: str | None = None
    password: str | None = None
    cookies: str | None = None

    @property
    def is_cookie(self) -> bool:
        return bool(self.cookies)

    def __repr__(self) -> str:  # keep secrets out of tracebacks and logs
        who = self.username or "<cookie>"
        return f"Credentials(source={self.source!r}, username={who!r}, secret=<redacted>)"

    __str__ = __repr__


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def _from_cookie_env() -> Credentials | None:
    raw = os.environ.get("OTTER_COOKIES", "").strip()
    return Credentials("OTTER_COOKIES", cookies=raw) if raw else None


def _from_cookie_file() -> Credentials | None:
    if not COOKIE_FILE.exists():
        return None
    raw = COOKIE_FILE.read_text(encoding="utf-8").strip()
    return Credentials(COOKIE_FILE.name, cookies=raw) if raw else None


def _from_keychain() -> Credentials | None:
    """Read the login from the macOS Keychain.

    Two calls: one for the account name (which is not secret and is printed by
    `security` as an attribute), one for the password with `-w`. The first read
    may raise a Keychain access dialog; that is macOS asking you, not us.
    """
    attrs = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
        capture_output=True, text=True,
    )
    if attrs.returncode != 0:
        return None
    match = _ACCT.search(attrs.stderr or attrs.stdout)
    if not match:
        return None

    secret = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True,
    )
    if secret.returncode != 0 or not secret.stdout.strip():
        return None
    return Credentials("keychain", username=match.group(1),
                       password=secret.stdout.rstrip("\n"))


def _from_env() -> Credentials | None:
    user = os.environ.get("OTTER_EMAIL", "").strip()
    pw = os.environ.get("OTTER_PASSWORD", "")
    return Credentials("env", username=user, password=pw) if user and pw else None


def _from_file() -> Credentials | None:
    if not CREDENTIALS_FILE.exists():
        return None
    lines = [ln.strip() for ln in
             CREDENTIALS_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    return Credentials(CREDENTIALS_FILE.name, username=lines[0], password=lines[1])


SOURCES = (_from_cookie_env, _from_cookie_file, _from_keychain, _from_env, _from_file)

HELP = f"""No Otter credential found.

  Run:  python -m otter.fetch login

  It walks you through copying one request out of developer tools, checks it
  against Otter, and writes {COOKIE_FILE.name}. No password is involved; the
  session expires on its own and you revoke it by logging out.

Other ways in, if that does not fit:

  keychain   security add-generic-password -s {KEYCHAIN_SERVICE} -a you@example.com -w
             Run in a real Terminal; omit the value after -w so it prompts
             without echo -- that keeps it out of argv and shell history.

  env        export OTTER_EMAIL=... OTTER_PASSWORD=... before launching

  file       {CREDENTIALS_FILE.name} at the repo root: email line 1, password line 2
"""


# curl quotes each header as one argument. Match to the *matching* closing
# quote rather than "any quote": cookie values contain quotes of the other
# kind -- Google's g_state is raw JSON -- and excluding both silently drops
# most of the header and keeps a fragment from further along the command.
_COOKIE_HEADER = re.compile(r"""(?isx)
    (?: -H | --header ) \s*
    (?P<q>['"]) \s* cookie \s* : \s* (?P<h>.*?) (?P=q)
  | (?: -b | --cookie ) \s*
    (?P<q2>['"]) (?P<b>.*?) (?P=q2)
""")


def cookies_from_curl(text: str) -> str:
    """Pull the Cookie header out of a copied curl command.

    DevTools' "Copy as cURL" is one right-click and carries every cookie the
    request actually sent, including the HttpOnly ones that no bookmarklet or
    `document.cookie` can reach -- which is the whole reason this takes a curl
    command rather than something you paste into the browser console.

    A bare cookie header is accepted too, so pasting the wrong half of the
    DevTools panel still works.
    """
    match = _COOKIE_HEADER.search(text)
    header = (match.group("h") or match.group("b") or "") if match else text
    header = header.replace("'\\''", "'")     # curl's escape for a literal quote
    pairs = [p.strip() for p in header.split(";")
             if "=" in p and " " not in p.split("=")[0].strip()]
    return "; ".join(pairs)


def resolve() -> Credentials:
    for source in SOURCES:
        found = source()
        if found:
            return found
    raise CredentialError(HELP)
