"""DFJP entry point."""

from __future__ import annotations

import argparse
import ctypes
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PyQt6.QtWidgets import QApplication

from gui import OverlayController
from runtime_paths import (
    APP_NAME,
    DF_EXE_NAME,
    HOOK_DLL_NAME,
    app_dir,
    ensure_default_config,
    offsets_dir,
    offsets_path,
)


class StartupError(RuntimeError):
    """Raised when startup prerequisites are not met."""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--game-dir",
        type=Path,
        help="Folder containing Dwarf Fortress.exe",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Only run offset detection, then exit",
    )
    return parser.parse_args(argv)


def normalize_game_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_file():
        if resolved.name.lower() != DF_EXE_NAME.lower():
            raise StartupError(
                f"When --game-dir points to a file, it must be {DF_EXE_NAME}: {resolved}"
            )
        return resolved.parent
    return resolved


def resolve_game_dir(explicit: Path | None) -> Path:
    candidates: list[Path] = []

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved not in candidates:
            candidates.append(resolved)

    if explicit is not None:
        add(normalize_game_dir(explicit))

    add(Path.cwd())
    add(app_dir())
    add(REPO_ROOT)
    add(SCRIPT_DIR)

    for candidate in candidates:
        if (candidate / DF_EXE_NAME).is_file():
            return candidate

    searched = "\n".join(f"  - {candidate}" for candidate in candidates)
    raise StartupError(
        f"{DF_EXE_NAME} was not found.\n"
        "Extract the ZIP into your Dwarf Fortress folder, or pass --game-dir.\n\n"
        f"Searched:\n{searched}"
    )


def prepare_runtime(game_dir: Path, prepare_only: bool) -> Path:
    game_exe = game_dir / DF_EXE_NAME
    hook_dll = game_dir / HOOK_DLL_NAME
    output = offsets_path(game_dir)

    if not game_exe.is_file():
        raise StartupError(f"{DF_EXE_NAME} was not found: {game_exe}")
    if not prepare_only and not hook_dll.is_file():
        raise StartupError(
            f"{HOOK_DLL_NAME} was not found: {hook_dll}\n"
            "Place DFJP in the same folder as Dwarf Fortress.exe."
        )

    offsets_dir(game_dir).mkdir(parents=True, exist_ok=True)

    try:
        from tools.detect_offsets import ensure_offsets_file

        changed = ensure_offsets_file(game_exe, output)
    except Exception as exc:
        raise StartupError(
            "Automatic RVA detection failed.\n"
            "This Dwarf Fortress version may need updated detection logic.\n\n"
            f"Details: {exc}"
        ) from exc

    if changed:
        logging.info("Updated offsets file: %s", output)
    else:
        logging.info("Offsets file is already current: %s", output)
    return output


def show_startup_error(message: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, message, APP_NAME, 0x10)
    except Exception:
        print(message, file=sys.stderr)


def run_app(argv: list[str]) -> int:
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    controller = OverlayController(app)
    app.aboutToQuit.connect(controller.shutdown)
    app._dfjp_controller = controller  # type: ignore[attr-defined]

    return app.exec()


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    try:
        ensure_default_config()
        game_dir = resolve_game_dir(args.game_dir)
        prepare_runtime(game_dir, prepare_only=args.prepare_only)
    except StartupError as exc:
        show_startup_error(str(exc))
        return 1

    if args.prepare_only:
        return 0

    return run_app(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
