from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_release.py"


def invoke(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *args],
        cwd=root, capture_output=True, text=True, check=False,
    )


def tracked(root: Path, name: str, content: bytes) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    subprocess.run(["git", "add", "--", name], cwd=root, check=True, capture_output=True)


def test_source_uses_tracked_inventory_and_ignores_local_environment(tmp_path: Path) -> None:
    tracked(tmp_path, "README.md", b"Synthetic project. Contact: reader@example.invalid")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "local.txt").write_bytes(b"local runtime data\0")
    result = invoke(tmp_path)
    assert result.returncode == 0
    assert "1 entries inspected" in result.stdout


def test_source_rejects_tracked_personal_configuration(tmp_path: Path) -> None:
    tracked(tmp_path, "config/local.toml", b"enabled = false")
    result = invoke(tmp_path)
    assert result.returncode == 1
    assert "path_not_allowed" in result.stdout


def test_source_reports_secret_location_without_value(tmp_path: Path) -> None:
    prefix = "sk"
    synthetic_value = prefix + "-" + "Z" * 32
    tracked(tmp_path, "README.md", synthetic_value.encode())
    result = invoke(tmp_path)
    assert result.returncode == 1
    assert "api_key_shape" in result.stdout
    assert synthetic_value not in result.stdout + result.stderr


def test_clean_working_file_cannot_hide_secret_in_index(tmp_path: Path) -> None:
    prefix = "sk"
    synthetic_value = prefix + "-" + "Y" * 32
    tracked(tmp_path, "README.md", synthetic_value.encode())
    (tmp_path / "README.md").write_text("Synthetic clean working copy.", encoding="utf-8")
    result = invoke(tmp_path)
    assert result.returncode == 1
    assert "index:README.md:1: api_key_shape" in result.stdout
    assert synthetic_value not in result.stdout + result.stderr


def test_source_rejects_binary_disguised_as_python(tmp_path: Path) -> None:
    tracked(tmp_path, "src/post_radar/sample.py", b"prefix\0suffix")
    assert "binary_content" in invoke(tmp_path).stdout


def test_source_requires_real_tracked_inventory(tmp_path: Path) -> None:
    result = invoke(tmp_path)
    assert result.returncode == 1
    assert "inventory_unavailable" in result.stdout


def test_distribution_allows_package_and_metadata(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    with zipfile.ZipFile(dist / "post_radar-0.1.0-py3-none-any.whl", "w") as archive:
        archive.writestr("post_radar/__init__.py", '"""Synthetic module."""\n')
        archive.writestr("post_radar-0.1.0.dist-info/METADATA", "Name: post-radar\n")
    result = invoke(tmp_path, "--artifacts", str(dist))
    assert result.returncode == 0
    assert "2 entries inspected" in result.stdout


def test_distribution_rejects_private_files_and_path_traversal(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    with zipfile.ZipFile(dist / "post_radar-0.1.0-py3-none-any.whl", "w") as archive:
        archive.writestr("post_radar/state.db", "synthetic state")
        archive.writestr("post_radar/../../outside.py", "pass")
    result = invoke(tmp_path, "--artifacts", str(dist))
    assert result.returncode == 1
    assert result.stdout.count("member_not_allowed") == 2
    assert not (tmp_path / "outside.py").exists()


def test_source_archive_rejects_symlinks_without_extracting(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    with tarfile.open(dist / "post_radar-0.1.0.tar.gz", "w:gz") as archive:
        info = tarfile.TarInfo("post_radar-0.1.0/README.md")
        data = b"Synthetic release."
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("post_radar-0.1.0/src/post_radar/link.py")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside.py"
        archive.addfile(link)
    result = invoke(tmp_path, "--artifacts", str(dist))
    assert result.returncode == 1
    assert "member_not_allowed" in result.stdout
    assert not (tmp_path / "post_radar-0.1.0").exists()


def test_artifact_directory_cannot_silently_pass_empty(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    result = invoke(tmp_path, "--artifacts", str(dist))
    assert result.returncode == 1
    assert "no_distributions" in result.stdout


@pytest.mark.parametrize("marker", [b"*", b"*\n", b"*\r\n"])
def test_artifact_directory_accepts_exact_uv_marker(tmp_path: Path, marker: bytes) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / ".gitignore").write_bytes(marker)
    with zipfile.ZipFile(dist / "post_radar-0.1.0-py3-none-any.whl", "w") as archive:
        archive.writestr("post_radar/__init__.py", '"""Synthetic module."""\n')
    result = invoke(tmp_path, "--artifacts", str(dist))
    assert result.returncode == 0
    assert "1 entries inspected" in result.stdout


def test_uv_marker_alone_does_not_count_as_distribution(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / ".gitignore").write_bytes(b"*\n")
    result = invoke(tmp_path, "--artifacts", str(dist))
    assert result.returncode == 1
    assert "no_distributions" in result.stdout


@pytest.mark.parametrize("name,content", [
    (".gitignore", b"*\nprivate\n"),
    (".gitignore", b"**"),
    ("notes.txt", b"*\n"),
])
def test_artifact_directory_rejects_changed_marker_and_extra_files(
    tmp_path: Path, name: str, content: bytes,
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / name).write_bytes(content)
    result = invoke(tmp_path, "--artifacts", str(dist))
    assert result.returncode == 1
    assert "unexpected_distribution" in result.stdout


def test_uv_marker_cannot_be_a_symlink(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    target = tmp_path / "synthetic-marker.txt"
    target.write_bytes(b"*\n")
    try:
        (dist / ".gitignore").symlink_to(target)
    except OSError:
        pytest.skip("Creating a symlink is not available on this host")
    result = invoke(tmp_path, "--artifacts", str(dist))
    assert result.returncode == 1
    assert "unexpected_distribution" in result.stdout
