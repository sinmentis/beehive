# tests/research/test_connector_policy.py
import pytest

from beehive.research.connector_policy import (
    ALLOWED_CONNECTOR_TYPES,
    ConnectorPolicyError,
    normalize_and_validate_source,
    normalize_and_validate_sources,
)
from beehive.research.limits import MAX_CONFIG_STRING_LENGTH, MAX_SOURCES_PER_PLAN

# ============================================================================
# 1. Allowlist covers exactly the currently registered, credentialless connectors
# ============================================================================

_EXPECTED_CONNECTOR_TYPES = {
    "reddit_subreddit",
    "google_news_query",
    "hackernews_stories",
    "hackernews_query",
    "rbnz_news",
    "nz_government_news",
    "federal_reserve_news",
}


def test_allowlist_contains_exactly_the_seven_registered_connector_types():
    assert set(ALLOWED_CONNECTOR_TYPES) == _EXPECTED_CONNECTOR_TYPES


# ============================================================================
# 2. Each connector type: happy path
# ============================================================================

def test_reddit_subreddit_valid_config_normalizes():
    connector_type, config = normalize_and_validate_source(
        "reddit_subreddit", {"subreddit": " newzealand "})
    assert connector_type == "reddit_subreddit"
    assert config == {"subreddit": "newzealand"}


def test_google_news_query_valid_config_normalizes():
    connector_type, config = normalize_and_validate_source(
        "google_news_query", {"query": "RBNZ interest rate"})
    assert connector_type == "google_news_query"
    assert config == {"query": "RBNZ interest rate"}


@pytest.mark.parametrize("feed", ["top", "best", "new", "ask", "show", "job"])
def test_hackernews_stories_accepts_each_valid_feed(feed):
    connector_type, config = normalize_and_validate_source(
        "hackernews_stories", {"feed": feed})
    assert config == {"feed": feed}


@pytest.mark.parametrize("sort", ["relevance", "recent"])
def test_hackernews_query_accepts_each_valid_sort(sort):
    connector_type, config = normalize_and_validate_source(
        "hackernews_query", {"query": "layoffs", "sort": sort})
    assert config == {"query": "layoffs", "sort": sort}


@pytest.mark.parametrize(
    "connector_type", ["rbnz_news", "nz_government_news", "federal_reserve_news"])
def test_official_feed_connectors_require_empty_config(connector_type):
    result_type, config = normalize_and_validate_source(connector_type, {})
    assert result_type == connector_type
    assert config == {}


# ============================================================================
# 3. Unknown connector type rejection
# ============================================================================

def test_unknown_connector_type_is_rejected():
    with pytest.raises(ConnectorPolicyError, match="unknown or disallowed"):
        normalize_and_validate_source("twitter_account", {"handle": "someone"})


def test_non_string_connector_type_is_rejected():
    with pytest.raises(ConnectorPolicyError, match="unknown or disallowed"):
        normalize_and_validate_source(123, {})


@pytest.mark.parametrize("bad_type", [
    "REDDIT_SUBREDDIT",  # case must match exactly, no case-insensitive fallback
    "reddit-subreddit",
    "reddit_subreddit ",
    "",
])
def test_connector_type_must_match_exactly(bad_type):
    with pytest.raises(ConnectorPolicyError, match="unknown or disallowed"):
        normalize_and_validate_source(bad_type, {"subreddit": "newzealand"})


# ============================================================================
# 4. Config: wrong types
# ============================================================================

def test_config_must_be_a_dict():
    with pytest.raises(ConnectorPolicyError, match="must be a JSON object"):
        normalize_and_validate_source("reddit_subreddit", ["newzealand"])
    with pytest.raises(ConnectorPolicyError, match="must be a JSON object"):
        normalize_and_validate_source("reddit_subreddit", "newzealand")
    with pytest.raises(ConnectorPolicyError, match="must be a JSON object"):
        normalize_and_validate_source("reddit_subreddit", None)


def test_config_field_must_be_a_string():
    with pytest.raises(ConnectorPolicyError, match="must be a string"):
        normalize_and_validate_source("reddit_subreddit", {"subreddit": 123})
    with pytest.raises(ConnectorPolicyError, match="must be a string"):
        normalize_and_validate_source("reddit_subreddit", {"subreddit": ["newzealand"]})
    with pytest.raises(ConnectorPolicyError, match="must be a string"):
        normalize_and_validate_source("reddit_subreddit", {"subreddit": None})


# ============================================================================
# 5. Config: empty / overlong strings
# ============================================================================

def test_empty_string_field_is_rejected():
    with pytest.raises(ConnectorPolicyError, match="must not be empty"):
        normalize_and_validate_source("reddit_subreddit", {"subreddit": ""})


def test_whitespace_only_field_is_rejected():
    with pytest.raises(ConnectorPolicyError, match="must not be empty"):
        normalize_and_validate_source("reddit_subreddit", {"subreddit": "   "})


def test_overlong_field_is_rejected():
    overlong = "a" * (MAX_CONFIG_STRING_LENGTH + 1)
    with pytest.raises(ConnectorPolicyError, match="exceeds"):
        normalize_and_validate_source("google_news_query", {"query": overlong})


def test_field_at_exactly_the_length_cap_is_accepted():
    exact = "a" * MAX_CONFIG_STRING_LENGTH
    _, config = normalize_and_validate_source("google_news_query", {"query": exact})
    assert config["query"] == exact


# ============================================================================
# 6. Config: unknown keys, missing required keys
# ============================================================================

def test_unknown_config_key_is_rejected():
    with pytest.raises(ConnectorPolicyError, match="unexpected key"):
        normalize_and_validate_source(
            "reddit_subreddit", {"subreddit": "newzealand", "extra_flag": "x"})


def test_missing_required_key_is_rejected():
    with pytest.raises(ConnectorPolicyError, match="missing required key"):
        normalize_and_validate_source("reddit_subreddit", {})


def test_missing_one_of_two_required_keys_is_rejected():
    with pytest.raises(ConnectorPolicyError, match="missing required key"):
        normalize_and_validate_source("hackernews_query", {"query": "layoffs"})


def test_official_feed_rejects_any_non_empty_config():
    with pytest.raises(ConnectorPolicyError, match="unexpected key"):
        normalize_and_validate_source("rbnz_news", {"url": "https://evil.example/feed"})


# ============================================================================
# 7. Config: credential-shaped keys
# ============================================================================

@pytest.mark.parametrize("credential_key", [
    "api_key", "apikey", "token", "access_token", "secret", "client_secret",
    "password", "passwd", "auth", "authorization", "credential", "cookie", "session_id",
])
def test_credential_shaped_unknown_key_gets_a_specific_error(credential_key):
    with pytest.raises(ConnectorPolicyError, match="credential-shaped"):
        normalize_and_validate_source(
            "reddit_subreddit", {"subreddit": "newzealand", credential_key: "sekrit"})


# ============================================================================
# 8. Config: invalid enum values
# ============================================================================

def test_hackernews_stories_rejects_invalid_feed_value():
    with pytest.raises(ConnectorPolicyError, match="must be one of"):
        normalize_and_validate_source("hackernews_stories", {"feed": "trending"})


def test_hackernews_query_rejects_invalid_sort_value():
    with pytest.raises(ConnectorPolicyError, match="must be one of"):
        normalize_and_validate_source(
            "hackernews_query", {"query": "layoffs", "sort": "popularity"})


def test_hackernews_stories_rejects_case_mismatched_feed_value():
    # no case-insensitive fallback -- the enum match must be exact.
    with pytest.raises(ConnectorPolicyError, match="must be one of"):
        normalize_and_validate_source("hackernews_stories", {"feed": "Top"})


# ============================================================================
# 9. normalize_and_validate_source calls the real connector's validate_config
# ============================================================================

def test_normalize_and_validate_source_calls_the_registered_connectors_validate_config(monkeypatch):
    """Defense-in-depth guard: even if this module's own schema were wrong/out of date, the
    real connector's validate_config must still run and can still reject a config."""
    import beehive.connectors.reddit as reddit_module

    original_validate = reddit_module.RedditSubredditConnector.validate_config
    calls = []

    def spy_validate(self, config):
        calls.append(config)
        return original_validate(self, config)

    monkeypatch.setattr(
        reddit_module.RedditSubredditConnector, "validate_config", spy_validate)

    normalize_and_validate_source("reddit_subreddit", {"subreddit": "newzealand"})
    assert calls == [{"subreddit": "newzealand"}]


def test_connector_validate_config_rejection_is_wrapped_as_connector_policy_error(monkeypatch):
    import beehive.connectors.reddit as reddit_module

    def always_reject(self, config):
        raise ValueError("simulated connector-level rejection")

    monkeypatch.setattr(reddit_module.RedditSubredditConnector, "validate_config", always_reject)

    with pytest.raises(ConnectorPolicyError, match="rejected by connector"):
        normalize_and_validate_source("reddit_subreddit", {"subreddit": "newzealand"})


# ============================================================================
# 9b. Allowlist and connector registry must never silently drift apart
# ============================================================================

def test_every_allowlisted_connector_type_resolves_via_the_connector_registry():
    """Every ALLOWED_CONNECTOR_TYPES key must be resolvable by the real connector registry --
    if this ever fails, the allowlist and the registered connectors have drifted apart and
    normalize_and_validate_source would (before this fix) misreport it as a config rejection."""
    from beehive.connectors.registry import get as registry_get

    for connector_type in ALLOWED_CONNECTOR_TYPES:
        connector = registry_get(connector_type)
        assert connector is not None


def test_registry_lookup_failure_is_reported_distinctly_from_a_config_rejection(monkeypatch):
    """If a connector type is allowlisted here but the registry cannot resolve it (an allowlist/
    registration drift bug), normalize_and_validate_source must say so explicitly instead of
    reporting a misleading 'config rejected by connector' message."""
    import beehive.research.connector_policy as connector_policy_module

    def always_unregistered(connector_type):
        raise ValueError(f"unknown Source type: {connector_type!r}")

    monkeypatch.setattr(connector_policy_module, "get_connector", always_unregistered)

    with pytest.raises(ConnectorPolicyError, match="allowlisted but not registered") as exc_info:
        normalize_and_validate_source("rbnz_news", {})
    assert "rejected by connector" not in str(exc_info.value)


# ============================================================================
# 10. normalize_and_validate_sources: batch rules (max count, duplicates)
# ============================================================================

def test_normalize_and_validate_sources_happy_path_preserves_order():
    sources = [
        ("reddit_subreddit", {"subreddit": "newzealand"}),
        ("rbnz_news", {}),
    ]
    result = normalize_and_validate_sources(sources)
    assert result == [
        ("reddit_subreddit", {"subreddit": "newzealand"}),
        ("rbnz_news", {}),
    ]


def test_too_many_sources_is_rejected():
    sources = [("rbnz_news", {})] * (MAX_SOURCES_PER_PLAN + 1)
    with pytest.raises(ConnectorPolicyError, match="exceeding the max"):
        normalize_and_validate_sources(sources)


def test_exactly_max_sources_is_accepted():
    sources = [("google_news_query", {"query": f"topic {i}"}) for i in range(MAX_SOURCES_PER_PLAN)]
    result = normalize_and_validate_sources(sources)
    assert len(result) == MAX_SOURCES_PER_PLAN


def test_duplicate_normalized_source_is_rejected():
    sources = [
        ("google_news_query", {"query": "RBNZ rate cut"}),
        ("google_news_query", {"query": "RBNZ rate cut"}),
    ]
    with pytest.raises(ConnectorPolicyError, match="duplicate"):
        normalize_and_validate_sources(sources)


def test_duplicate_detection_normalizes_whitespace_before_comparing():
    # "  RBNZ rate cut  " strips down to the same normalized config as "RBNZ rate cut" --
    # duplicate detection must compare normalized values, not raw input.
    sources = [
        ("google_news_query", {"query": "RBNZ rate cut"}),
        ("google_news_query", {"query": "  RBNZ rate cut  "}),
    ]
    with pytest.raises(ConnectorPolicyError, match="duplicate"):
        normalize_and_validate_sources(sources)


def test_same_connector_type_different_config_is_not_a_duplicate():
    sources = [
        ("google_news_query", {"query": "RBNZ rate cut"}),
        ("google_news_query", {"query": "RBNZ inflation report"}),
    ]
    result = normalize_and_validate_sources(sources)
    assert len(result) == 2


def test_one_invalid_source_fails_the_whole_batch():
    sources = [
        ("reddit_subreddit", {"subreddit": "newzealand"}),
        ("unknown_connector", {}),
    ]
    with pytest.raises(ConnectorPolicyError, match="unknown or disallowed"):
        normalize_and_validate_sources(sources)
