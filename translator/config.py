"""Load DFJP runtime settings from config.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from typing import Any

from runtime_paths import ensure_default_config


def _read_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


@dataclass
class Config:
    engine: str = "google"
    target_language: str = "ja"
    deepl_api_key: str = field(default="")
    tooltip_opacity: float = 0.78
    all_text_vertical_shift_ratio: float = 1.0
    translation_font_size: float = 12.0
    toggle_hotkey: str = "ctrl"
    collect_detected_text: bool = False
    debug_log: bool = False


def load_config() -> Config:
    cfg = Config()
    path = ensure_default_config()

    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)

        translator = data.get("translator", {})
        cfg.engine = str(translator.get("engine", cfg.engine)).strip().lower() or cfg.engine
        cfg.target_language = str(translator.get("target_language", cfg.target_language)).strip() or cfg.target_language

        deepl = data.get("deepl", {})
        cfg.deepl_api_key = str(deepl.get("api_key", "")).strip()

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

        try:
            cfg.translation_font_size = float(
                overlay.get(
                    "translation_font_size",
                    cfg.translation_font_size,
                )
            )
        except (TypeError, ValueError):
            pass
        cfg.translation_font_size = max(8.0, min(24.0, cfg.translation_font_size))

        cfg.toggle_hotkey = str(overlay.get("toggle_hotkey", cfg.toggle_hotkey)).strip().lower()
        if cfg.toggle_hotkey not in {"ctrl", "shift", "alt"}:
            cfg.toggle_hotkey = "ctrl"

        manual_rules = data.get("manual_rules", {})
        cfg.collect_detected_text = _read_bool(
            manual_rules.get("collect_detected_text", cfg.collect_detected_text),
            cfg.collect_detected_text,
        )

        debug = data.get("debug", {})
        cfg.debug_log = _read_bool(debug.get("log", cfg.debug_log), cfg.debug_log)

    env_key = os.environ.get("DEEPL_API_KEY", "").strip()
    if env_key:
        cfg.deepl_api_key = env_key

    return cfg
