import pytest

from beehive.db import app_state
from beehive.db.connection import connect, init_schema
from beehive.localization import (
    DEFAULT_LANGUAGE_CODE,
    PLATFORM_LANGUAGE_KEY,
    MissingTranslationError,
    SUPPORTED_LANGUAGES,
    UnsupportedLanguageError,
    load_localizer,
    localizer_for,
    save_language,
)
from beehive.translations import background, common, web


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "test.db"))
    init_schema(connection)
    return connection


def test_supported_languages_match_the_confirmed_first_release():
    assert [language.code for language in SUPPORTED_LANGUAGES] == [
        "en",
        "zh-CN",
        "ja",
        "ko",
        "es",
        "fr",
        "de",
    ]


def test_missing_setting_defaults_to_english(conn):
    localizer = load_localizer(conn)
    assert localizer.code == DEFAULT_LANGUAGE_CODE == "en"
    assert localizer.llm_name == "English"


def test_save_language_roundtrips_through_app_state(conn):
    save_language(conn, "ja")
    assert app_state.get(conn, PLATFORM_LANGUAGE_KEY) == "ja"
    assert load_localizer(conn).language.native_name == "日本語"


def test_unsupported_language_is_rejected_without_writing(conn):
    with pytest.raises(UnsupportedLanguageError, match="Unsupported platform language"):
        save_language(conn, "pt-BR")
    assert app_state.get(conn, PLATFORM_LANGUAGE_KEY) is None


def test_invalid_stored_language_fails_loudly(conn):
    app_state.set(conn, PLATFORM_LANGUAGE_KEY, "invalid")
    with pytest.raises(UnsupportedLanguageError, match="invalid"):
        load_localizer(conn)


def test_translation_interpolates_values_and_selects_plurals():
    english = localizer_for("en")
    french = localizer_for("fr")
    chinese = localizer_for("zh-CN")

    assert english.text("common.item_count", count=1) == "1 item"
    assert english.text("common.item_count", count=2) == "2 items"
    assert french.text("common.item_count", count=0) == "0 élément"
    assert french.text("common.item_count", count=2) == "2 éléments"
    assert chinese.text("common.item_count", count=2) == "2 条"


def test_missing_translation_key_fails_loudly():
    with pytest.raises(MissingTranslationError, match="does.not.exist"):
        localizer_for("de").text("does.not.exist")


def test_every_translation_module_has_the_supported_language_set():
    expected = {language.code for language in SUPPORTED_LANGUAGES}
    assert set(common.CATALOGS) == expected
    assert set(web.CATALOGS) == expected
    assert set(background.CATALOGS) == expected


def test_combined_catalog_keys_and_plural_shapes_match_english():
    modules = (common.CATALOGS, web.CATALOGS, background.CATALOGS)
    for module_catalogs in modules:
        english = module_catalogs["en"]
        for language in SUPPORTED_LANGUAGES:
            catalog = module_catalogs[language.code]
            assert set(catalog) == set(english)
            for key, english_message in english.items():
                translated_message = catalog[key]
                assert isinstance(translated_message, dict) == isinstance(english_message, dict)
                if isinstance(english_message, dict):
                    assert set(translated_message) == set(english_message)
