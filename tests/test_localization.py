import string

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


def _placeholder_names(template: str) -> set[str]:
    """Field names a str.format() template consumes, e.g. {"count", "title"} for
    "{count} results for {title}". Ignores literal text and positional/unnamed fields."""
    return {
        field_name for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name
    }


def test_combined_catalog_placeholders_match_english_for_every_locale():
    """Every locale's translation of a key must interpolate exactly the same {placeholder}
    names as the English original -- a missing or renamed placeholder would raise KeyError at
    render time, or silently drop data, only for that one language."""
    modules = (common.CATALOGS, web.CATALOGS, background.CATALOGS)
    for module_catalogs in modules:
        english = module_catalogs["en"]
        for key, english_message in english.items():
            if isinstance(english_message, dict):
                english_placeholders = {
                    category: _placeholder_names(template)
                    for category, template in english_message.items()
                }
            else:
                english_placeholders = _placeholder_names(english_message)

            for language in SUPPORTED_LANGUAGES:
                translated_message = module_catalogs[language.code][key]
                if isinstance(english_message, dict):
                    for category, expected in english_placeholders.items():
                        actual = _placeholder_names(translated_message[category])
                        assert actual == expected, (
                            f"{key!r} ({category}) placeholder mismatch for "
                            f"{language.code!r}: {actual} != {expected}")
                else:
                    actual = _placeholder_names(translated_message)
                    assert actual == english_placeholders, (
                        f"{key!r} placeholder mismatch for {language.code!r}: "
                        f"{actual} != {english_placeholders}")


def test_deep_read_namespace_renders_in_every_supported_language():
    """Narrow, targeted coverage for the web.deep_read.* namespace added for the deep-read
    feature: proves every key -- button states, dedicated-page metadata, section labels,
    failure copy, owner-only controls, and live-region/announcement text -- renders a non-empty
    string with plausible interpolation values in every supported language, and that the
    English catalog contains every state the feature plan calls for."""
    deep_read_keys = {key for key in web.CATALOGS["en"] if key.startswith("web.deep_read.")}
    expected_keys = {
        "web.deep_read.button_start", "web.deep_read.button_start_aria",
        "web.deep_read.button_pending", "web.deep_read.button_pending_aria",
        "web.deep_read.button_open", "web.deep_read.button_open_aria",
        "web.deep_read.button_retry", "web.deep_read.button_retry_aria",
        "web.deep_read.button_regenerate", "web.deep_read.button_regenerate_aria",
        "web.deep_read.eyebrow",
        "web.deep_read.back_default", "web.deep_read.back_to_dashboard",
        "web.deep_read.back_to_channel", "web.deep_read.back_to_archive",
        "web.deep_read.source_label", "web.deep_read.generated_at_label",
        "web.deep_read.generated_language_label",
        "web.deep_read.source_link", "web.deep_read.source_link_aria",
        "web.deep_read.section_bottom_line", "web.deep_read.section_key_findings",
        "web.deep_read.section_important_figures", "web.deep_read.section_why_it_matters",
        "web.deep_read.section_limitations",
        "web.deep_read.no_important_figures", "web.deep_read.incomplete_warning",
        "web.deep_read.stored_source_warning",
        "web.deep_read.failure_fetch", "web.deep_read.failure_fetch_not_found",
        "web.deep_read.failure_fetch_http_error", "web.deep_read.failure_fetch_timeout",
        "web.deep_read.failure_extraction", "web.deep_read.failure_extraction_google_news",
        "web.deep_read.failure_llm", "web.deep_read.failure_unavailable",
        "web.deep_read.failure_heading", "web.deep_read.failure_next_open_source",
        "web.deep_read.failure_next_retry",
        "web.deep_read.owner_controls_aria",
        "web.deep_read.pending_heading", "web.deep_read.pending_body",
        "web.deep_read.pending_live_region",
        "web.deep_read.ready_announcement", "web.deep_read.failed_announcement",
    }
    assert deep_read_keys == expected_keys
    assert "web.title.deep_read_brief" in web.CATALOGS["en"]

    sample_values = {
        "product": "Beehive", "title": "Rates fall again", "channel": "NZ Finance",
        "source": "Reuters", "time": "2 hours ago", "language": "Deutsch",
    }
    for language in SUPPORTED_LANGUAGES:
        localizer = localizer_for(language.code)
        rendered = localizer.text("web.title.deep_read_brief", **sample_values)
        assert rendered.strip()
        for key in deep_read_keys:
            placeholders = _placeholder_names(web.CATALOGS["en"][key])
            values = {name: sample_values[name] for name in placeholders}
            rendered = localizer.text(key, **values)
            assert rendered.strip(), f"{key!r} rendered blank for {language.code!r}"


def test_admin_safety_catalog_is_localized_not_english_fallback():
    """Guards the admin/owner-safety strings (web.py `_ADMIN_SAFETY_CATALOGS`) against
    silently reverting to the old behavior of applying the English catalog to every locale.
    Full key and placeholder parity is already enforced by the combined-catalog tests, so this
    stays deliberately narrow: a handful of stable keys whose wording must differ from English
    in every non-English locale (common actions, a weekday, a health status, admin labels). If
    any of these equals the English source for any locale, the block has fallen back to English."""
    english = web.CATALOGS["en"]
    always_translated_keys = [
        "common.save",
        "common.cancel",
        "web.admin.activity.undo",
        "web.weekday.monday",
        "web.admin.health.status_ok",
        "web.admin.source_test.eyebrow",
        "web.admin.email_group.schedule_heading",
        "web.research.history.duration_pending",
    ]
    for key in always_translated_keys:
        english_message = english[key]
        for language in SUPPORTED_LANGUAGES:
            if language.code == "en":
                continue
            translated = web.CATALOGS[language.code][key]
            assert translated != english_message, (
                f"{key!r} is still the English fallback ({english_message!r}) for "
                f"{language.code!r}; the admin-safety catalog is not localized")
