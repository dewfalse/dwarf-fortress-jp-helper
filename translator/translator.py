"""翻訳エンジン管理。config.toml で Google 翻訳 / DeepL を切り替える。"""

from __future__ import annotations

import json
import logging
from typing import Protocol

from config import Config, load_config
from runtime_paths import cache_path

try:
    from deep_translator import GoogleTranslator
except ImportError:
    GoogleTranslator = None

try:
    import deepl
except ImportError:
    deepl = None

logger = logging.getLogger(__name__)


def _load_cache() -> dict[str, str]:
    cache_file = cache_path()
    try:
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("キャッシュ読み込みエラー: %s", e)
        return {}


def _save_cache(cache: dict[str, str]) -> None:
    cache_file = cache_path()
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("キャッシュ保存エラー: %s", e)


class TranslationEngine(Protocol):
    def translate_batch(self, texts: list[str]) -> list[str]: ...

    @property
    def name(self) -> str: ...


class GoogleEngine:
    name = "Google 翻訳"

    def __init__(self, target_language: str) -> None:
        if GoogleTranslator is None:
            raise ImportError("deep-translator")
        self._target = target_language

    def translate_batch(self, texts: list[str]) -> list[str]:
        if not texts:
            return []
        if GoogleTranslator is None:
            return texts
        try:
            return [
                GoogleTranslator(source="auto", target=self._target).translate(text)
                or text
                for text in texts
            ]
        except Exception as e:
            logger.debug("Google 翻訳エラー: %s", e)
            return texts


class DeepLEngine:
    name = "DeepL"

    def __init__(self, api_key: str, target_language: str) -> None:
        if deepl is None:
            raise ImportError("deepl")
        self._client = deepl.Translator(api_key)
        self._target = target_language.upper()

    def translate_batch(self, texts: list[str]) -> list[str]:
        if not texts:
            return []
        try:
            results = self._client.translate_text(texts, target_lang=self._target)
            return [result.text for result in results]
        except Exception as e:
            logger.debug("DeepL 翻訳エラー: %s", e)
            return texts


class Translator:
    """設定に従って翻訳エンジンを選択し、結果をキャッシュする。"""

    def __init__(self) -> None:
        self._cache: dict[str, str] = _load_cache()
        self._engine: TranslationEngine | None = self._init_engine(load_config())
        logger.info("翻訳キャッシュ: %d 件を読み込みました", len(self._cache))

    def _init_engine(self, cfg: Config) -> TranslationEngine | None:
        if cfg.engine == "deepl":
            return self._try_deepl(cfg)
        return self._try_google(cfg)

    def _try_google(self, cfg: Config) -> TranslationEngine | None:
        try:
            engine = GoogleEngine(cfg.target_language)
            logger.info("翻訳エンジン: Google 翻訳")
            return engine
        except ImportError:
            logger.error(
                "deep-translator がインストールされていません。依存関係を確認してください"
            )
            return None

    def _try_deepl(self, cfg: Config) -> TranslationEngine | None:
        if not cfg.deepl_api_key:
            logger.warning("DeepL API キーが未設定のため Google 翻訳にフォールバックします")
            return self._try_google(cfg)
        try:
            engine = DeepLEngine(cfg.deepl_api_key, cfg.target_language)
            logger.info("翻訳エンジン: DeepL")
            return engine
        except ImportError:
            logger.warning("deepl パッケージがないため Google 翻訳にフォールバックします")
            return self._try_google(cfg)
        except Exception as e:
            logger.error("DeepL 初期化エラー: %s。Google 翻訳にフォールバックします", e)
            return self._try_google(cfg)

    @property
    def is_active(self) -> bool:
        return self._engine is not None

    @property
    def engine_name(self) -> str:
        return self._engine.name if self._engine else "利用不可"

    def translate(self, text: str) -> str:
        if not text.strip():
            return text
        if text in self._cache:
            return self._cache[text]
        return self.translate_batch([text])[0]

    def translate_batch(self, texts: list[str]) -> list[str]:
        if self._engine is None:
            return texts

        uncached = [text for text in texts if text.strip() and text not in self._cache]
        if uncached:
            translated = self._engine.translate_batch(uncached)
            for source, dest in zip(uncached, translated):
                self._cache[source] = dest
            _save_cache(self._cache)

        return [self._cache.get(text, text) for text in texts]
