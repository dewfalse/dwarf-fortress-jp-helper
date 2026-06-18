"""DFJP 起動エントリポイント。"""

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

from gui import TranslationWindow
from pipe_reader import PipeReader
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
    """ユーザーに表示すべき起動前エラー。"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--game-dir",
        type=Path,
        help="Dwarf Fortress.exe があるフォルダを明示する",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="自動オフセット検出だけを行って終了する",
    )
    return parser.parse_args(argv)


def normalize_game_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_file():
        if resolved.name.lower() != DF_EXE_NAME.lower():
            raise StartupError(
                f"--game-dir にファイルを渡す場合は {DF_EXE_NAME} を指定してください: {resolved}"
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
        f"{DF_EXE_NAME} が見つかりません。\n"
        "ZIP の中身を Dwarf Fortress 本体フォルダへ展開するか、"
        "--game-dir でフォルダを指定してください。\n\n"
        f"検索先:\n{searched}"
    )


def prepare_runtime(game_dir: Path, prepare_only: bool) -> Path:
    game_exe = game_dir / DF_EXE_NAME
    hook_dll = game_dir / HOOK_DLL_NAME
    output = offsets_path(game_dir)

    if not game_exe.is_file():
        raise StartupError(f"{DF_EXE_NAME} が見つかりません: {game_exe}")
    if not prepare_only and not hook_dll.is_file():
        raise StartupError(
            f"{HOOK_DLL_NAME} が見つかりません: {hook_dll}\n"
            "ZIP の中身を Dwarf Fortress 本体フォルダに展開してください。"
        )

    offsets_dir(game_dir).mkdir(parents=True, exist_ok=True)
    try:
        from tools.detect_offsets import ensure_offsets_file

        changed = ensure_offsets_file(game_exe, output)
    except Exception as exc:
        raise StartupError(
            "RVA 自動検出に失敗しました。\n"
            "この Dwarf Fortress バージョンで検出条件が変わった可能性があります。\n\n"
            f"詳細: {exc}"
        ) from exc

    if changed:
        logging.info("オフセットファイルを更新しました: %s", output)
    else:
        logging.info("オフセットファイルは最新です: %s", output)
    return output


def show_startup_error(message: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, message, APP_NAME, 0x10)
    except Exception:
        print(message, file=sys.stderr)


def run_gui(argv: list[str]) -> int:
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)

    window = TranslationWindow()
    window.show()

    reader = PipeReader(on_frame=window.on_frame)
    reader.start()
    app.aboutToQuit.connect(reader.stop)

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

    return run_gui(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
