#!/usr/bin/env bash
# Set up otter-transcribe: check Python, install the one dependency, and
# walk through the Otter credential. Safe to re-run.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
say "1. Python"

PY=""
if command -v uv >/dev/null 2>&1; then
    uv sync --quiet
    PY="uv run python"
    ok "uv found; dependencies installed into .venv"
else
    warn "uv not found, falling back to a plain venv (install uv for a faster path)"
    for candidate in python3.13 python3.12 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && \
           "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'; then
            "$candidate" -m venv .venv
            ./.venv/bin/pip install --quiet --upgrade pip
            ./.venv/bin/pip install --quiet requests
            PY="./.venv/bin/python"
            ok "$candidate -> .venv, requests installed"
            break
        fi
    done
    [ -n "$PY" ] || die "need Python 3.12 or newer; none found on PATH"
fi

$PY -c 'import requests' || die "requests did not install"

# --------------------------------------------------------------------------
say "2. Otter credential"

# Otter's public API is Enterprise-only, so this uses the endpoints the web
# app uses, which means borrowing a logged-in browser session. A cookie is
# preferred over a password: it expires by itself, logging out revokes it, and
# accounts on Google/Microsoft SSO have no password to send anyway.
if [ -s OTTER_COOKIES.txt ]; then
    ok "OTTER_COOKIES.txt already present (delete it to re-enter)"
else
    # `login` prints the steps, reads the paste without echoing it, checks the
    # session cookie is actually in there, and verifies against Otter before
    # claiming success.
    $PY -m otter.fetch login || die "no credential stored"
fi

# --------------------------------------------------------------------------
say "3. Verify"

if $PY -m otter.fetch check; then
    ok "the credential works"
else
    die "could not reach Otter. If it says 'cookie auth rejected', log in again
    and re-copy the header, then delete OTTER_COOKIES.txt and re-run this script."
fi

# --------------------------------------------------------------------------
say "Ready"

cat <<EOF
  Transcribe a multi-track recording (one audio file per participant; they
  must come from the same session, since the merge assumes a shared start):

    $PY -m otter.fetch run track1.m4a track2.m4a -c config.json -o transcript.txt

  The first run writes config.json and prints the speaker labels it found.
  Put real names against them and run the same command again.

  Already uploaded to Otter?

    $PY -m otter.fetch list                    # find the otids
    $PY -m otter.fetch pull <otid> <otid> --wait -c config.json -o transcript.txt

  In Claude Code, /transcribe wraps all of this. Skills load at startup, so
  start Claude Code fresh in this directory for it to appear.

  README.md has the rest.
EOF
