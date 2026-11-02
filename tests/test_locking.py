import subprocess
import sys
from pathlib import Path

import pytest

from post_radar.locking import RuntimeLock
from post_radar.models import ExternalError


def test_runtime_lock_prevents_second_owner_and_releases(tmp_path: Path) -> None:
    with (
        RuntimeLock(tmp_path),
        pytest.raises(ExternalError, match="already_running"),
        RuntimeLock(tmp_path),
    ):
        pytest.fail("second runtime acquired the state directory")
    with RuntimeLock(tmp_path):
        pass


def test_separate_state_directories_are_independent(tmp_path: Path) -> None:
    with RuntimeLock(tmp_path / "a"), RuntimeLock(tmp_path / "b"):
        pass


def test_process_death_releases_runtime_lock(tmp_path: Path) -> None:
    code = (
        "import sys; from pathlib import Path; from post_radar.locking import RuntimeLock\n"
        "with RuntimeLock(Path(sys.argv[1])):\n"
        " print('ready', flush=True)\n"
        " sys.stdin.read()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(ExternalError, match="already_running"), RuntimeLock(tmp_path):
            pytest.fail("second process acquired a held lock")
    finally:
        child.terminate()
        child.communicate(timeout=10)
    with RuntimeLock(tmp_path):
        pass
