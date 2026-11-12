import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_sync_plugin_copies_canonical_mb_events(tmp_path):
    hermes_home = tmp_path / "hermes"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "sync_plugin.py"),
            "--hermes-home",
            str(hermes_home),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    vendored = hermes_home / "plugins" / "megabrain" / "mb_events.py"
    canonical = ROOT / "integrations" / "hermes" / "mb_events.py"
    assert vendored.read_bytes() == canonical.read_bytes()
