"""Collect upstream license files for components shipped in CodexBar."""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import shutil
import sys


DISTRIBUTIONS = (
    "comtypes",
    "pywebview",
    "bottle",
    "pythonnet",
    "clr-loader",
    "cffi",
    "pycparser",
    "typing-extensions",
    "pyinstaller",
    "altgraph",
    "packaging",
    "pefile",
    "pyinstaller-hooks-contrib",
    "pywin32-ctypes",
    "setuptools",
)


def _license_files(distribution: metadata.Distribution) -> list[Path]:
    result: list[Path] = []
    for relative in distribution.files or ():
        filename = Path(str(relative)).name.casefold()
        if not filename.startswith(("license", "copying", "notice", "copyright")):
            continue
        source = Path(distribution.locate_file(relative))
        if source.is_file():
            result.append(source)
    return result


def _copy_python_licenses(destination: Path) -> None:
    base = Path(sys.base_prefix)
    python_candidates = (base / "LICENSE.txt", base / "LICENSE_PYTHON.txt")
    python_license = next((path for path in python_candidates if path.is_file()), None)
    if python_license is None:
        raise RuntimeError("CPython license file was not found in the build runtime")
    shutil.copy2(python_license, destination / "CPython-LICENSE.txt")

    tcl_candidates = (
        base / "tcl" / "tk8.6" / "license.terms",
        base / "Library" / "lib" / "tk8.6" / "license.terms",
    )
    tcl_license = next((path for path in tcl_candidates if path.is_file()), None)
    if tcl_license is None:
        raise RuntimeError("Tcl/Tk license file was not found in the build runtime")
    shutil.copy2(tcl_license, destination / "Tcl-Tk-LICENSE.txt")


def collect_licenses(destination: Path, project_root: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    _copy_python_licenses(destination)
    for name in DISTRIBUTIONS:
        distribution = metadata.distribution(name)
        sources = _license_files(distribution)
        if not sources:
            raise RuntimeError(f"No license file found for {name}")
        for index, source in enumerate(sources, start=1):
            safe_name = distribution.metadata["Name"].replace("_", "-")
            target = destination / (
                f"{safe_name}-{distribution.version}-{index}-{source.name}"
            )
            shutil.copy2(source, target)

    proxy_license = project_root / "third_party_licenses" / "proxy_tools-LICENSE.txt"
    if not proxy_license.is_file():
        raise RuntimeError("Vendored proxy_tools license is missing")
    shutil.copy2(proxy_license, destination / proxy_license.name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    collect_licenses(args.destination.resolve(), root)
    print(f"Collected licenses: {args.destination.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
