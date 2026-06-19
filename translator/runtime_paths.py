from __future__ import annotations

import sys
from pathlib import Path

APP_NAME = "DFJP"
DATA_DIR_NAME = "dfjp-data"
OFFSETS_FILE_NAME = "offsets-dfjp-auto.toml"
DF_EXE_NAME = "Dwarf Fortress.exe"
HOOK_DLL_NAME = "dfhooks.dll"

DEFAULT_CONFIG_TOML = """[translator]
# 使用する翻訳エンジン: "google" または "deepl"
engine = "google"
# 翻訳先言語コード
# 例: "ja" / "en" / "ko" / "zh-CN"
target_language = "ja"

[deepl]
# DeepL API キー
# 環境変数 DEEPL_API_KEY でも設定可能（設定されていればそちらを優先）
api_key = ""

[overlay]
# 翻訳ツールチップの透過率（0.05 ～ 1.0）
# 1.0 に近いほど濃く、低いほど元テキストが見えやすい
tooltip_opacity = 0.78
# all text モードで重なったツールチップを縦にどれくらいずらすか
# 0.5 = 半分ずらす / 1.0 = 完全にずらす
all_text_vertical_shift_ratio = 1.0

[debug]
# true にすると受信したテキストを debug.log に記録する
log = true
"""


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def translator_dir() -> Path:
    return Path(__file__).resolve().parent


def app_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return translator_dir()


def repo_root() -> Path:
    return translator_dir().parent


def data_dir() -> Path:
    if is_frozen():
        return app_dir() / DATA_DIR_NAME
    return translator_dir()


def ensure_data_dir() -> Path:
    path = data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    if is_frozen():
        return data_dir() / "config.toml"
    return translator_dir() / "config.toml"


def ensure_default_config() -> Path:
    path = config_path()
    if not path.exists():
        ensure_data_dir()
        path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return path


def cache_path() -> Path:
    ensure_data_dir()
    return data_dir() / "translation_cache.json"


def debug_log_path() -> Path:
    ensure_data_dir()
    return data_dir() / "debug.log"


def offsets_dir(game_dir: Path) -> Path:
    return game_dir / "dfint-data"


def offsets_path(game_dir: Path) -> Path:
    return offsets_dir(game_dir) / OFFSETS_FILE_NAME
