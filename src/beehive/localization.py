from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TypeAlias

from beehive.db import app_state
from beehive.translations import background, common, web

DEFAULT_LANGUAGE_CODE = "en"
PLATFORM_LANGUAGE_KEY = "platform_language"

Message: TypeAlias = str | dict[str, str]


class UnsupportedLanguageError(ValueError):
    pass


class MissingTranslationError(KeyError):
    pass


@dataclass(frozen=True)
class Language:
    code: str
    native_name: str
    llm_name: str


SUPPORTED_LANGUAGES = (
    Language(code="en", native_name="English", llm_name="English"),
    Language(code="zh-CN", native_name="简体中文", llm_name="Simplified Chinese"),
    Language(code="ja", native_name="日本語", llm_name="Japanese"),
    Language(code="ko", native_name="한국어", llm_name="Korean"),
    Language(code="es", native_name="Español", llm_name="Spanish"),
    Language(code="fr", native_name="Français", llm_name="French"),
    Language(code="de", native_name="Deutsch", llm_name="German"),
)
_LANGUAGES_BY_CODE = {language.code: language for language in SUPPORTED_LANGUAGES}


def _build_catalogs() -> dict[str, dict[str, Message]]:
    modules = (common.CATALOGS, web.CATALOGS, background.CATALOGS)
    expected_codes = set(_LANGUAGES_BY_CODE)
    catalogs = {code: {} for code in expected_codes}
    for module_catalogs in modules:
        if set(module_catalogs) != expected_codes:
            raise RuntimeError("translation module language codes do not match supported languages")
        for code, messages in module_catalogs.items():
            duplicate_keys = catalogs[code].keys() & messages.keys()
            if duplicate_keys:
                raise RuntimeError(
                    f"duplicate translation keys for {code}: {sorted(duplicate_keys)}")
            catalogs[code].update(messages)
    return catalogs


_CATALOGS = _build_catalogs()


def _plural_category(language_code: str, count: int | float) -> str:
    if language_code == "fr":
        return "one" if count in (0, 1) else "other"
    return "one" if count == 1 else "other"


@dataclass(frozen=True)
class Localizer:
    language: Language

    @property
    def code(self) -> str:
        return self.language.code

    @property
    def html_lang(self) -> str:
        return self.language.code

    @property
    def llm_name(self) -> str:
        return self.language.llm_name

    def text(self, key: str, *, count: int | float | None = None, **values: object) -> str:
        try:
            message = _CATALOGS[self.code][key]
        except KeyError as exc:
            raise MissingTranslationError(
                f"missing translation key {key!r} for language {self.code!r}") from exc

        if isinstance(message, dict):
            if count is None:
                raise ValueError(f"translation key {key!r} requires a count")
            category = _plural_category(self.code, count)
            try:
                template = message[category]
            except KeyError as exc:
                raise MissingTranslationError(
                    f"translation key {key!r} has no {category!r} plural for {self.code!r}"
                ) from exc
        else:
            template = message

        format_values = dict(values)
        if count is not None:
            format_values["count"] = count
        return template.format(**format_values)


def localizer_for(language_code: str) -> Localizer:
    try:
        language = _LANGUAGES_BY_CODE[language_code]
    except KeyError as exc:
        raise UnsupportedLanguageError(
            f"Unsupported platform language: {language_code!r}") from exc
    return Localizer(language)


def load_localizer(conn: sqlite3.Connection) -> Localizer:
    language_code = app_state.get(
        conn,
        PLATFORM_LANGUAGE_KEY,
        default=DEFAULT_LANGUAGE_CODE,
    )
    return localizer_for(language_code)


def save_language(conn: sqlite3.Connection, language_code: str) -> None:
    localizer_for(language_code)
    app_state.set(conn, PLATFORM_LANGUAGE_KEY, language_code)
