"""Translation engines, cache, and manual TSV rules for DFJP."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import threading
from typing import Protocol

from config import Config, load_config
from runtime_paths import DEFAULT_MANUAL_RULES_TSV, cache_path, manual_rules_path

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
    except Exception as exc:
        logger.warning("Failed to load translation cache: %s", exc)
        return {}


def _save_cache(cache: dict[str, str]) -> None:
    cache_file = cache_path()
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("Failed to save translation cache: %s", exc)


def _escape_rule_field(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def _unescape_rule_field(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            if nxt == "n":
                out.append("\n")
                index += 2
                continue
            if nxt == "t":
                out.append("\t")
                index += 2
                continue
            if nxt == "r":
                out.append("\r")
                index += 2
                continue
            if nxt == "\\":
                out.append("\\")
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _append_lines(path: Path, lines: list[str]) -> None:
    needs_separator = path.exists() and path.stat().st_size > 0
    if needs_separator:
        with open(path, "rb") as existing:
            existing.seek(-1, 2)
            needs_separator = existing.read(1) not in {b"\n", b"\r"}

    with open(path, "a", encoding="utf-8", newline="\n") as f:
        if needs_separator:
            f.write("\n")
        for line in lines:
            f.write(line)
            f.write("\n")


@dataclass(frozen=True)
class RegexManualRule:
    source_pattern: str
    replacement: str
    pattern: re.Pattern[str]

    def try_translate(self, text: str) -> str | None:
        match = self.pattern.fullmatch(text)
        if match is None:
            return None
        try:
            return match.expand(self.replacement)
        except re.error as exc:
            logger.warning(
                "Invalid regex replacement in manual rule %r -> %r: %s",
                self.source_pattern,
                self.replacement,
                exc,
            )
            return None

    def matches(self, text: str) -> bool:
        return self.pattern.fullmatch(text) is not None


class ManualRuleStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or manual_rules_path()
        self._lock = threading.RLock()
        self._exact_rules: dict[str, str] = {}
        self._pending_exact_sources: set[str] = set()
        self._regex_rules: list[RegexManualRule] = []
        self.reload()

    @property
    def path(self) -> Path:
        return self._path

    def _ensure_file_exists(self) -> Path:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text(DEFAULT_MANUAL_RULES_TSV, encoding="utf-8")
        return self._path

    def reload(self) -> None:
        exact_rules: dict[str, str] = {}
        pending_exact_sources: set[str] = set()
        regex_rules: list[RegexManualRule] = []

        if self._path.exists():
            try:
                with open(self._path, encoding="utf-8") as f:
                    for line_no, raw_line in enumerate(f, start=1):
                        line = raw_line.rstrip("\r\n")
                        stripped = line.strip()
                        if not stripped or stripped.startswith("#"):
                            continue

                        columns = line.split("\t", 2)
                        if len(columns) < 2:
                            logger.warning(
                                "Ignoring malformed manual rule line %d in %s",
                                line_no,
                                self._path,
                            )
                            continue
                        while len(columns) < 3:
                            columns.append("")

                        rule_type = columns[0].strip().lower()
                        source = _unescape_rule_field(columns[1])
                        target = _unescape_rule_field(columns[2])

                        if rule_type == "exact":
                            if not source:
                                continue
                            if target:
                                exact_rules[source] = target
                                pending_exact_sources.discard(source)
                            elif source not in exact_rules:
                                pending_exact_sources.add(source)
                            continue

                        if rule_type == "regex":
                            if not source:
                                continue
                            if not target:
                                logger.debug(
                                    "Ignoring regex manual rule with blank replacement at %s:%d",
                                    self._path,
                                    line_no,
                                )
                                continue
                            try:
                                compiled = re.compile(source)
                            except re.error as exc:
                                logger.warning(
                                    "Ignoring invalid regex manual rule at %s:%d: %s",
                                    self._path,
                                    line_no,
                                    exc,
                                )
                                continue
                            regex_rules.append(
                                RegexManualRule(
                                    source_pattern=source,
                                    replacement=target,
                                    pattern=compiled,
                                )
                            )
                            continue

                        logger.warning(
                            "Ignoring unknown manual rule type %r at %s:%d",
                            rule_type,
                            self._path,
                            line_no,
                        )
            except Exception as exc:
                logger.warning("Failed to load manual translation rules: %s", exc)

        with self._lock:
            self._exact_rules = exact_rules
            self._pending_exact_sources = pending_exact_sources
            self._regex_rules = regex_rules

        logger.info(
            "Loaded manual rules: %d exact, %d regex, %d pending from %s",
            len(exact_rules),
            len(regex_rules),
            len(pending_exact_sources),
            self._path,
        )

    def lookup(self, text: str) -> str | None:
        if not text.strip():
            return text

        with self._lock:
            exact = self._exact_rules.get(text)
            regex_rules = tuple(self._regex_rules)

        if exact is not None:
            return exact

        for rule in regex_rules:
            translated = rule.try_translate(text)
            if translated is not None:
                return translated
        return None

    def has_entry(self, text: str) -> bool:
        if not text.strip():
            return True

        with self._lock:
            if text in self._exact_rules or text in self._pending_exact_sources:
                return True
            regex_rules = tuple(self._regex_rules)

        return any(rule.matches(text) for rule in regex_rules)

    def collect_exact_placeholders(self, texts: list[str]) -> int:
        candidates = list(dict.fromkeys(text for text in texts if text.strip()))
        if not candidates:
            return 0

        with self._lock:
            regex_rules = tuple(self._regex_rules)
            new_sources: list[str] = []
            for text in candidates:
                if text in self._exact_rules or text in self._pending_exact_sources:
                    continue
                if any(rule.matches(text) for rule in regex_rules):
                    continue
                new_sources.append(text)

            if not new_sources:
                return 0

            output_path = self._ensure_file_exists()
            lines = [f"exact\t{_escape_rule_field(text)}\t" for text in new_sources]
            _append_lines(output_path, lines)
            self._pending_exact_sources.update(new_sources)

        logger.info(
            "Appended %d detected texts to manual rules file: %s",
            len(new_sources),
            output_path,
        )
        return len(new_sources)


class TranslationEngine(Protocol):
    def translate_batch(self, texts: list[str]) -> list[str]: ...

    @property
    def name(self) -> str: ...


class GoogleEngine:
    name = "Google Translate"

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
                GoogleTranslator(source="auto", target=self._target).translate(text) or text
                for text in texts
            ]
        except Exception as exc:
            logger.debug("Google Translate failed: %s", exc)
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
        except Exception as exc:
            logger.debug("DeepL failed: %s", exc)
            return texts


class Translator:
    """Translate texts with manual rules, cache, and an optional machine engine."""

    def __init__(self) -> None:
        self._config = load_config()
        self._cache_lock = threading.RLock()
        self._cache: dict[str, str] = _load_cache()
        self._manual_rules = ManualRuleStore()
        self._collect_detected_text = self._config.collect_detected_text
        self._engine: TranslationEngine | None = self._init_engine(self._config)
        logger.info("Loaded translation cache entries: %d", len(self._cache))
        if self._collect_detected_text:
            logger.info("Detected-text collection mode is enabled")

    def _init_engine(self, cfg: Config) -> TranslationEngine | None:
        if cfg.engine == "deepl":
            return self._try_deepl(cfg)
        return self._try_google(cfg)

    def _try_google(self, cfg: Config) -> TranslationEngine | None:
        try:
            engine = GoogleEngine(cfg.target_language)
            logger.info("Translation engine: Google Translate")
            return engine
        except ImportError:
            logger.error("deep-translator is not installed")
            return None

    def _try_deepl(self, cfg: Config) -> TranslationEngine | None:
        if not cfg.deepl_api_key:
            logger.warning("DeepL API key is not configured; falling back to Google Translate")
            return self._try_google(cfg)
        try:
            engine = DeepLEngine(cfg.deepl_api_key, cfg.target_language)
            logger.info("Translation engine: DeepL")
            return engine
        except ImportError:
            logger.warning("deepl is not installed; falling back to Google Translate")
            return self._try_google(cfg)
        except Exception as exc:
            logger.error("DeepL initialization failed: %s; falling back to Google Translate", exc)
            return self._try_google(cfg)

    @property
    def is_active(self) -> bool:
        return self._engine is not None

    @property
    def engine_name(self) -> str:
        return self._engine.name if self._engine else "Disabled"

    def collect_detected_texts(self, texts: list[str]) -> int:
        if not self._collect_detected_text:
            return 0
        return self._manual_rules.collect_exact_placeholders(texts)

    def get_cached_translation(self, text: str) -> str | None:
        if not text.strip():
            return text

        manual = self._manual_rules.lookup(text)
        if manual is not None:
            return manual

        with self._cache_lock:
            return self._cache.get(text)

    def translate(self, text: str) -> str:
        if not text.strip():
            return text
        return self.translate_batch([text])[0]

    def _cached_value(self, text: str) -> str | None:
        with self._cache_lock:
            return self._cache.get(text)

    def _update_cache(self, updates: dict[str, str]) -> None:
        with self._cache_lock:
            self._cache.update(updates)
            cache_snapshot = dict(self._cache)
        _save_cache(cache_snapshot)

    def translate_batch(self, texts: list[str]) -> list[str]:
        if not texts:
            return []

        manual_results: dict[str, str] = {}
        uncached: list[str] = []
        unique_texts = list(dict.fromkeys(texts))

        for text in unique_texts:
            if not text.strip():
                continue

            manual = self._manual_rules.lookup(text)
            if manual is not None:
                manual_results[text] = manual
                continue

            if self._cached_value(text) is not None:
                continue

            uncached.append(text)

        if uncached and self._engine is not None:
            translated = self._engine.translate_batch(uncached)
            updates = {
                source: (dest or source)
                for source, dest in zip(uncached, translated)
            }
            if updates:
                self._update_cache(updates)

        with self._cache_lock:
            cache_snapshot = dict(self._cache)

        results: list[str] = []
        for text in texts:
            if not text.strip():
                results.append(text)
            elif text in manual_results:
                results.append(manual_results[text])
            else:
                results.append(cache_snapshot.get(text, text))
        return results
