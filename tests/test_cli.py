import socket
from pathlib import Path

import pytest

from post_radar.cli import main
from post_radar.credentials import WindowsCredentialStore


def test_init_disabled_check_and_no_overwrite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "local.toml"
    assert main(["init", "--config", str(path)]) == 0
    assert main(["check", "--config", str(path)]) == 0
    assert main(["init", "--config", str(path)]) == 1
    assert "configuration_already_exists" in capsys.readouterr().err


def test_doctor_never_reads_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("must_not_read_credentials")

    monkeypatch.setattr(WindowsCredentialStore, "get", forbidden)
    path = tmp_path / "local.toml"
    assert main(["init", "--config", str(path)]) == 0
    assert main(["doctor", "--config", str(path)]) == 0
    assert '"credentials_checked": false' in capsys.readouterr().out


def test_demo_offline_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network_and_credentials_forbidden")

    monkeypatch.setattr(WindowsCredentialStore, "get", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert main(["demo"]) == 0
    output = capsys.readouterr().out
    assert "Mail previews: 1" in output
    assert "no email sent" in output


def test_invalid_config_cli_does_not_echo_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "bad.toml"
    path.write_text('password = "SYNTHETIC-PRIVATE-DETAIL"', encoding="utf-8")
    assert main(["check", "--config", str(path)]) == 1
    assert "SYNTHETIC-PRIVATE-DETAIL" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["secret", "set", "api-key", "SYNTHETIC-PRIVATE-DETAIL"],
        ["secret", "set", "SYNTHETIC-PRIVATE-DETAIL"],
        ["SYNTHETIC-PRIVATE-DETAIL"],
    ],
)
def test_argument_errors_never_echo_secrets(
    args: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as failure:
        main(args)
    assert failure.value.code == 2
    assert "SYNTHETIC-PRIVATE-DETAIL" not in capsys.readouterr().err
