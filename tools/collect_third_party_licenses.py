"""Collect third-party license files for DFJP release bundles."""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import re
import shutil
import sys
import textwrap
import urllib.request


PROJECT_PACKAGE_NAME = "dfjp-translator"
LICENSE_HINTS = ("license", "copying", "notice", "authors")
MANUAL_TEXT_LICENSES: dict[str, str] = {
    "GNU-GPL-2.0.txt": "https://www.gnu.org/licenses/old-licenses/gpl-2.0.txt",
    "GNU-GPL-3.0.txt": "https://www.gnu.org/licenses/gpl-3.0.txt",
    "GNU-LGPL-3.0.txt": "https://www.gnu.org/licenses/lgpl-3.0.txt",
}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Directory where THIRD_PARTY_LICENSES contents will be written.",
    )
    return parser.parse_args(argv)


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "package"


def _iter_license_files(dist: metadata.Distribution) -> list[metadata.PackagePath]:
    matches: list[metadata.PackagePath] = []
    for file in dist.files or []:
        parts = [part.lower() for part in Path(file).parts]
        file_name = Path(file).name.lower()
        if file_name == "metadata":
            matches.append(file)
            continue
        if any(hint in file_name for hint in LICENSE_HINTS):
            matches.append(file)
            continue
        if any(any(hint in part for hint in LICENSE_HINTS) for part in parts):
            matches.append(file)
            continue
    unique: dict[str, metadata.PackagePath] = {}
    for file in matches:
        unique[str(file)] = file
    return [unique[key] for key in sorted(unique.keys())]


def _copy_distribution_files(dist: metadata.Distribution, destination: Path) -> list[str]:
    copied: list[str] = []
    destination.mkdir(parents=True, exist_ok=True)
    for file in _iter_license_files(dist):
        source = Path(dist.locate_file(file))
        if not source.is_file():
            continue
        relative = Path(*Path(file).parts[1:]) if len(Path(file).parts) > 1 else Path(source.name)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(relative).replace("\\", "/"))
    return copied


def _project_urls(meta: metadata.PackageMetadata) -> list[str]:
    urls: list[str] = []
    homepage = meta.get("Home-page")
    if homepage:
        urls.append(homepage)
    for entry in meta.get_all("Project-URL") or []:
        if "," in entry:
            _label, url = entry.split(",", 1)
            urls.append(url.strip())
        else:
            urls.append(entry.strip())
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def _download_text(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "DFJP license collector"})
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read()
    destination.write_bytes(content)


def _write_python_notice(output_dir: Path) -> tuple[str, list[str]]:
    python_dir = output_dir / "python"
    python_dir.mkdir(parents=True, exist_ok=True)

    license_src = Path(sys.base_prefix) / "LICENSE.txt"
    copied: list[str] = []
    if license_src.is_file():
        license_dst = python_dir / "LICENSE.txt"
        shutil.copy2(license_src, license_dst)
        copied.append("python/LICENSE.txt")

    version = ".".join(str(part) for part in sys.version_info[:3])
    notice = textwrap.dedent(
        f"""\
        Python runtime
        ==============

        Version: {version}
        License: PSF
        Source: https://www.python.org/
        License page: https://docs.python.org/3/license.html
        """
    )
    notice_path = python_dir / "NOTICE.txt"
    notice_path.write_text(notice, encoding="utf-8")
    copied.append("python/NOTICE.txt")
    return f"Python {version}", copied


def _write_qt_notice(output_dir: Path) -> list[str]:
    qt_dir = output_dir / "qt"
    qt_dir.mkdir(parents=True, exist_ok=True)

    notice = textwrap.dedent(
        """\
        Qt / PySide6 notice
        ===================

        DFJP uses PySide6, the official Qt for Python bindings.

        PySide6 package metadata declares:
          LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only

        Official project pages:
          https://pyside.org/
          https://doc.qt.io/qtforpython/
          https://code.qt.io/cgit/pyside/pyside-setup.git/

        Qt open-source licensing information:
          https://www.qt.io/licensing/open-source-lgpl-obligations

        Note:
          The installed PySide6 wheel may also contain a
          LicenseRef-Qt-Commercial.txt file. That file is kept as-is from the
          installed distribution, but the package metadata for this build
          declares the open-source alternatives above.

        The GNU license texts referenced by the PySide6 metadata are included
        in this folder for convenience.
        """
    )
    written = ["qt/PySide6-NOTICE.txt"]
    (qt_dir / "PySide6-NOTICE.txt").write_text(notice, encoding="utf-8")

    for filename, url in MANUAL_TEXT_LICENSES.items():
        target = qt_dir / filename
        try:
            _download_text(url, target)
        except Exception as exc:
            target.write_text(
                f"Failed to download {url}\nReason: {exc}\n",
                encoding="utf-8",
            )
        written.append(f"qt/{filename}")
    return written


def _write_microsoft_notice(output_dir: Path) -> list[str]:
    ms_dir = output_dir / "microsoft"
    ms_dir.mkdir(parents=True, exist_ok=True)
    notice = textwrap.dedent(
        """\
        Microsoft runtime notice
        ========================

        This release bundle may contain Microsoft runtime DLLs copied by the
        build toolchain (for example VCRUNTIME / UCRT components).

        Redistribution terms are governed by Microsoft's Visual C++
        Redistributable licensing terms:
          https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files
        """
    )
    (ms_dir / "NOTICE.txt").write_text(notice, encoding="utf-8")
    return ["microsoft/NOTICE.txt"]


def _distribution_sort_key(dist: metadata.Distribution) -> str:
    return dist.metadata["Name"].lower()


def collect(output_dir: Path) -> None:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    package_root = output_dir / "packages"
    package_root.mkdir(parents=True, exist_ok=True)

    summary_lines = [
        "# DFJP third-party licenses",
        "",
        "This folder contains license notices for third-party components bundled",
        "or used to build the DFJP release artifact.",
        "",
        "## Included notices",
        "",
    ]

    python_label, python_files = _write_python_notice(output_dir)
    qt_files = _write_qt_notice(output_dir)
    ms_files = _write_microsoft_notice(output_dir)

    summary_lines.extend(
        [
            f"- {python_label}",
            *(f"  - `{path}`" for path in python_files),
            "- Qt / PySide6 supplemental notices",
            *(f"  - `{path}`" for path in qt_files),
            "- Microsoft runtime supplemental notice",
            *(f"  - `{path}`" for path in ms_files),
            "",
            "## Python package licenses",
            "",
        ]
    )

    for dist in sorted(metadata.distributions(), key=_distribution_sort_key):
        name = dist.metadata["Name"]
        if name.lower() == PROJECT_PACKAGE_NAME:
            continue

        package_dir = package_root / _safe_name(name)
        copied = _copy_distribution_files(dist, package_dir)
        version = dist.version
        license_name = dist.metadata.get("License") or "Unknown"
        urls = _project_urls(dist.metadata)

        summary_lines.append(f"### {name} {version}")
        summary_lines.append("")
        summary_lines.append(f"- License metadata: `{license_name}`")
        if urls:
            summary_lines.append("- Project URLs:")
            summary_lines.extend(f"  - {url}" for url in urls)
        if copied:
            summary_lines.append("- Included files:")
            summary_lines.extend(f"  - `packages/{_safe_name(name)}/{path}`" for path in copied)
        else:
            summary_lines.append("- Included files: none found in installed distribution")
        summary_lines.append("")

    (output_dir / "THIRD_PARTY_LICENSES.md").write_text(
        "\n".join(summary_lines) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    collect(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
