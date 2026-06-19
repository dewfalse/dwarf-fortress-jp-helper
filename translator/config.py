"""config.toml を読み込んで設定を返す。"""

import os
import tomllib
from dataclasses import dataclass, field

from runtime_paths import ensure_default_config


@dataclass
class Config:
    engine: str = "google"
    target_language: str = "ja"
    deepl_api_key: str = field(default="")
    tooltip_opacity: float = 0.78
    all_text_vertical_shift_ratio: float = 1.0
    toggle_hotkey: str = "ctrl"
    debug_log: bool = False


def load_config() -> Config:
    cfg = Config()
    path = ensure_default_config()

    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)

        t = data.get("translator", {})
        cfg.engine = t.get("engine", cfg.engine)
        cfg.target_language = t.get("target_language", cfg.target_language)

        d = data.get("deepl", {})
        cfg.deepl_api_key = d.get("api_key", "")

        overlay = data.get("overlay", {})
        try:
            cfg.tooltip_opacity = float(overlay.get("tooltip_opacity", cfg.tooltip_opacity))
        except (TypeError, ValueError):
            pass
        cfg.tooltip_opacity = max(0.05, min(1.0, cfg.tooltip_opacity))
        try:
            cfg.all_text_vertical_shift_ratio = float(
                overlay.get(
                    "all_text_vertical_shift_ratio",
                    cfg.all_text_vertical_shift_ratio,
                )
            )
        except (TypeError, ValueError):
            pass
        cfg.all_text_vertical_shift_ratio = max(0.1, min(2.0, cfg.all_text_vertical_shift_ratio))
        cfg.toggle_hotkey = str(overlay.get("toggle_hotkey", cfg.toggle_hotkey)).strip().lower()
        if cfg.toggle_hotkey not in {"ctrl", "shift", "alt"}:
            cfg.toggle_hotkey = "ctrl"

        dbg = data.get("debug", {})
        cfg.debug_log = dbg.get("log", False)

    # 環境変数は config.toml より優先
    env_key = os.environ.get("DEEPL_API_KEY", "").strip()
    if env_key:
        cfg.deepl_api_key = env_key

    return cfg
