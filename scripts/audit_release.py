"""Fail a release build that is incomplete or contains private local data."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
from typing import NamedTuple


class Finding(NamedTuple):
    path: str
    kind: str


REQUIRED_ROOT_FILES = (
    "CodexBar.exe",
    "LICENSE",
    "PRIVACY.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
)
FORBIDDEN_NAMES = {
    "auth.json",
    "credentials.dat",
    "error.log",
    "error.log.1",
    "model_prices.json",
    ".codexbar_cfg.json",
    ".quota_widget_cfg.json",
}
CREDENTIAL_PATTERNS = (
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    ),
    re.compile(
        rb"(?i)(?:access_token|refresh_token|id_token)"
        rb"[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{12,}"
    ),
)
PRIVATE_PATH_PATTERNS = (
    re.compile(rb"(?i)\b[A-Z]:[\\/]Users[\\/][^\\/\s\"']+"),
    re.compile(rb"(?i)\b[A-Z]:[\\/]Projects[\\/][^\\/\s\"']+"),
)


def audit_release(root: Path) -> list[Finding]:
    """Return non-sensitive findings for one assembled release directory."""

    root = Path(root).resolve()
    findings: list[Finding] = []
    for name in REQUIRED_ROOT_FILES:
        if not (root / name).is_file():
            findings.append(Finding(name, "missing-required-file"))
    license_dir = root / "licenses"
    if not license_dir.is_dir() or not any(license_dir.iterdir()):
        findings.append(Finding("licenses", "missing-third-party-licenses"))

    if not root.is_dir():
        return findings
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if path.name.casefold() in FORBIDDEN_NAMES:
            findings.append(Finding(relative, "forbidden-file"))
        try:
            content = path.read_bytes()
        except OSError:
            findings.append(Finding(relative, "unreadable-file"))
            continue
        if any(pattern.search(content) for pattern in CREDENTIAL_PATTERNS):
            findings.append(Finding(relative, "credential-pattern"))
        if any(pattern.search(content) for pattern in PRIVATE_PATH_PATTERNS):
            findings.append(Finding(relative, "private-path"))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_dir", type=Path)
    args = parser.parse_args()
    findings = audit_release(args.release_dir)
    if findings:
        for finding in findings:
            print(f"{finding.kind}: {finding.path}")
        return 1
    print("Release audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
