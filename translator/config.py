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

        dbg = data.get("debug", {})
        cfg.debug_log = dbg.get("log", False)

    # 環境変数は config.toml より優先
    env_key = os.environ.get("DEEPL_API_KEY", "").strip()
    if env_key:
        cfg.deepl_api_key = env_key

    return cfg
