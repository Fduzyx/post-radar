"""Inspect tracked publication inputs and distributions without printing secret values."""

from __future__ import annotations

import argparse
import re
import subprocess
import tarfile
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_FILES = 10000
ROOT_FILES = {
    "AGENTS.md", "README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md",
    "CHANGELOG.md", "pyproject.toml", "uv.lock", ".gitignore",
}
EXACT_FILES = {
    "config/example.toml", "src/post_radar/example.toml", "scripts/check_release.py",
    ".github/workflows/ci.yml",
}
SECRET_PATTERNS = {
    "api_key_shape": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}"),
    "github_token_shape": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{25,}|"
                                     r"github_pat_[A-Za-z0-9_]{30,})"),
    "cloud_access_key_shape": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "jwt_shape": re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}"
                            r"\.[A-Za-z0-9_-]{15,}\b"),
    "private_key_shape": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "bearer_value": re.compile(r"\bBearer\s+[A-Za-z0-9_.-]{24,}", re.IGNORECASE),
    "url_credential": re.compile(r"https?://[^\s/:]+:[^\s/@]+@", re.IGNORECASE),
    "personal_home_path": re.compile(r"\b[A-Z]:[\\/](?:Users|Documents and Settings)"
                                     r"[\\/][A-Za-z0-9_-]+", re.IGNORECASE),
}
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
EXAMPLE_DOMAINS = {"example.com", "example.org", "example.net"}


def safe_member_name(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return bool(parts) and not (
        "\\" in name or ":" in name or name.startswith("/")
        or any(part in {".", ".."} for part in name.split("/"))
        or any(ord(char) < 32 for char in name)
    )


def allowed_source_path(name: str) -> bool:
    if not safe_member_name(name):
        return False
    if name in ROOT_FILES | EXACT_FILES:
        return True
    path = PurePosixPath(name)
    if any(part.startswith(".") or part == "__pycache__" for part in path.parts):
        return False
    return (
        (name.startswith("src/post_radar/") and path.suffix == ".py")
        or (name.startswith("tests/") and path.suffix == ".py")
        or (name.startswith("docs/") and path.suffix == ".md")
    )


def text_findings(name: str, data: bytes) -> list[str]:
    if len(data) > MAX_FILE_BYTES:
        return [f"{name}: file_too_large"]
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [f"{name}: non_utf8_content"]
    if "\x00" in text:
        return [f"{name}: binary_content"]
    findings = []
    for code, pattern in SECRET_PATTERNS.items():
        match = pattern.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"{name}:{line}: {code}")
    for match in EMAIL_PATTERN.finditer(text):
        domain = match.group(1).lower()
        if domain not in EXAMPLE_DOMAINS and not domain.endswith((".invalid", ".test", ".example")):
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"{name}:{line}: non_example_email")
            break
    return findings


def check_source(root: Path) -> tuple[int, list[str]]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--stage", "-z"], cwd=root,
            capture_output=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return 0, ["source: tracked_file_inventory_unavailable"]
    records = [record for record in result.stdout.decode("utf-8").split("\0") if record]
    if not records:
        return 0, ["source: no_tracked_files"]
    if len(records) > MAX_FILES:
        return 0, ["source: too_many_files"]
    findings: list[str] = []
    total = 0
    root = root.resolve()
    for record in records:
        metadata, name = record.split("\t", 1)
        mode, object_id, stage = metadata.split()
        if not allowed_source_path(name):
            findings.append(f"source:{name}: path_not_allowed")
            continue
        path = root / name
        if mode not in {"100644", "100755"} or stage != "0" or path.is_symlink():
            findings.append(f"source:{name}: non_regular_or_unmerged_file")
            continue
        try:
            if not path.resolve().is_relative_to(root):
                findings.append(f"source:{name}: path_outside_root")
                continue
            size = path.stat().st_size
            total += size
            if total > MAX_TOTAL_BYTES:
                findings.append("source: total_size_limit")
                break
            if size > MAX_FILE_BYTES:
                findings.append(f"source:{name}: file_too_large")
                continue
            data = path.read_bytes()
            findings.extend(text_findings(f"source:{name}", data))
            # A clean working file must not hide a secret still staged for publication.
            index_size = subprocess.run(
                ["git", "cat-file", "-s", object_id], cwd=root,
                capture_output=True, check=True,
            )
            if int(index_size.stdout) > MAX_FILE_BYTES:
                findings.append(f"index:{name}: file_too_large")
                continue
            indexed = subprocess.run(
                ["git", "cat-file", "blob", object_id], cwd=root,
                capture_output=True, check=True,
            ).stdout
            if indexed != data:
                findings.extend(text_findings(f"index:{name}", indexed))
        except (OSError, ValueError, subprocess.CalledProcessError):
            findings.append(f"source:{name}: unreadable_file")
    return len(records), findings


def allowed_package_path(name: str, *, wheel: bool) -> bool:
    if not safe_member_name(name):
        return False
    path = PurePosixPath(name)
    if wheel:
        if name.startswith("post_radar/"):
            return allowed_source_path("src/" + name)
        if re.fullmatch(r"post_radar-[A-Za-z0-9_.+]+\.dist-info", path.parts[0]):
            return "/".join(path.parts[1:]) in {
                "METADATA", "WHEEL", "RECORD", "entry_points.txt", "licenses/LICENSE",
            }
        return False
    if len(path.parts) < 2 or not re.fullmatch(r"post[-_]radar-[A-Za-z0-9_.+]+", path.parts[0]):
        return False
    relative = "/".join(path.parts[1:])
    return relative == "PKG-INFO" or allowed_source_path(relative)


def archive_entries(path: Path) -> Iterable[tuple[str, int, bool, bytes]]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                mode = member.external_attr >> 16
                regular = mode & 0o170000 in {0, 0o100000}
                if member.file_size <= MAX_FILE_BYTES and regular:
                    with archive.open(member) as stream:
                        data = stream.read(MAX_FILE_BYTES + 1)
                else:
                    data = b""
                yield member.filename, member.file_size, regular, data
    else:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                if member.isdir():
                    continue
                data = b""
                if member.isfile() and member.size <= MAX_FILE_BYTES:
                    stream = archive.extractfile(member)
                    if stream is not None:
                        with stream:
                            data = stream.read(MAX_FILE_BYTES + 1)
                yield member.name, member.size, member.isfile(), data


def check_artifacts(directory: Path) -> tuple[int, list[str]]:
    if not directory.is_dir():
        return 0, ["artifacts: directory_missing"]
    paths = sorted(directory.iterdir())
    if not paths:
        return 0, ["artifacts: no_distributions"]
    findings: list[str] = []
    count = 0
    total = 0
    distributions = 0
    for path in paths:
        # uv creates this exact marker in the build output directory.
        if path.name == ".gitignore" and not path.is_symlink() and path.is_file():
            try:
                if path.stat().st_size <= 3 and path.read_bytes() in {b"*", b"*\n", b"*\r\n"}:
                    continue
            except OSError:
                pass
        if path.is_symlink() or not path.is_file() or not (
            path.name.endswith(".tar.gz") or path.suffix == ".whl"
        ):
            findings.append(f"artifact:{path.name}: unexpected_distribution")
            continue
        distributions += 1
        seen: set[str] = set()
        try:
            for name, size, regular, data in archive_entries(path):
                count += 1
                total += size
                location = f"artifact:{path.name}:{name}"
                if count > MAX_FILES or total > MAX_TOTAL_BYTES:
                    return count, [*findings, "artifacts: content_limit"]
                if not regular or not allowed_package_path(name, wheel=path.suffix == ".whl"):
                    findings.append(f"{location}: member_not_allowed")
                    continue
                if name in seen:
                    findings.append(f"{location}: duplicate_member")
                seen.add(name)
                if size > MAX_FILE_BYTES:
                    findings.append(f"{location}: file_too_large")
                else:
                    findings.extend(text_findings(location, data))
        except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile, RuntimeError):
            findings.append(f"artifact:{path.name}: unreadable_distribution")
        if not seen:
            findings.append(f"artifact:{path.name}: no_allowed_members")
    if not distributions:
        findings.append("artifacts: no_distributions")
    return count, findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--source", action="store_true", help="inspect Git-tracked source files")
    parser.add_argument(
        "--artifacts", type=Path, help="inspect all distributions in this directory",
    )
    args = parser.parse_args()
    findings: list[str] = []
    count = 0
    if args.source or args.artifacts is None:
        checked, issues = check_source(args.root)
        count += checked
        findings.extend(issues)
    if args.artifacts is not None:
        checked, issues = check_artifacts(args.artifacts)
        count += checked
        findings.extend(issues)
    for finding in findings:
        print(finding)
    print(f"Release check: {count} entries inspected; {len(findings)} findings.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
