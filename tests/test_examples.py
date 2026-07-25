"""The committed examples must keep reproducing, byte for byte.

Not invented properties -- these outputs were checked against reality: the
separate-mics pair against the four lines the audio actually contains, and the
one-room pair against a known +3.000s offset and a known mis-hearing. Pinning
them catches anything a property test did not think to ask about.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not (ROOT / "examples").is_dir(),
                    reason="examples ship with the distributable repo only")
@pytest.mark.parametrize("folder", ["separate-mics", "one-room"])
def test_example_reproduces(tmp_path, folder):
    here = ROOT / "examples" / folder
    docs = sorted(p for p in here.glob("*.json") if p.name != "config.json")
    out = tmp_path / "out.txt"
    result = subprocess.run(
        [sys.executable, "-m", "otter.fetch", "merge", *map(str, docs),
         "-c", str(here / "config.json"), "-o", str(out)],
        cwd=ROOT, capture_output=True, text=True)
    assert out.exists(), result.stderr[-400:]
    assert out.read_text() == (here / "transcript.txt").read_text()
