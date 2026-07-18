# src/beehive/research/connector_policy.py
"""The application-side allowlist and strict schema for every Research Source a Research Plan
may propose (ADR-0007: the AI proposes plans, but the application validates every connector
type and configuration before anything executes). Research AI, planner.py, and every other
caller in this package must go through `normalize_and_validate_source(s)` -- there is no other
sanctioned way to turn AI-proposed (connector_type, config) data into something the collector
may actually run, and this module never calls a connector's `fetch()` itself.

The seven connector types below are exactly the currently registered, credentialless
connectors: Reddit's and Google News's public RSS endpoints, Hacker News's public
Firebase/Algolia endpoints, and three fixed official-institution RSS feeds. There is
deliberately no path here for a Research Plan to configure an API key, token, or any other
secret -- every config key is checked against an explicit allowlist per connector type, and any
key (allowed or not) that is itself shaped like a credential name is rejected outright, so a
plan can never smuggle a secret through a field that happens to look legitimate.

`normalize_and_validate_source` always finishes by calling the real connector's own
`validate_config` as the final authority, so this module's schema and the connector's actual
runtime requirements can never silently diverge -- if a connector's requirements change, its
own validate_config catches what this module's schema might miss, rather than the two silently
disagreeing forever."""
from __future__ import annotations

from dataclasses import dataclass

from beehive.connectors import (  # noqa: F401 (import side effect: registers the connectors)
    google_news,
    hackernews,
    official_feeds,
    reddit,
)
from beehive.connectors.registry import get as get_connector
from beehive.research.limits import MAX_CONFIG_STRING_LENGTH, MAX_SOURCES_PER_PLAN

# Any config key whose name itself smells like a credential is rejected, independent of
# whether it also happens to be an otherwise-allowed key for that connector type -- defense in
# depth against a plan (or an injected model response) trying to smuggle a secret through a
# field that looks legitimate.
_CREDENTIAL_KEY_MARKERS = (
    "key", "token", "secret", "password", "passwd", "auth", "credential", "cookie", "session",
)


class ConnectorPolicyError(ValueError):
    """Raised for any Research Source that fails connector-policy validation: an unknown or
    disallowed connector type, a config with an unknown/wrong-typed/out-of-range/
    credential-shaped key, an invalid enum value, or a Research Plan that is too large or
    contains a duplicate source. Never silently drops or repairs a bad source -- the caller's
    whole plan fails validation."""


def _is_credential_shaped(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _CREDENTIAL_KEY_MARKERS)


@dataclass(frozen=True)
class _StringField:
    key: str
    # None = any non-empty string bounded by MAX_CONFIG_STRING_LENGTH; otherwise an exact enum.
    allowed_values: frozenset[str] | None = None


@dataclass(frozen=True)
class ConnectorTypeSpec:
    connector_type: str
    fields: tuple[_StringField, ...]
    prompt_hint: str  # short, human-readable config shape rendered into the planner prompt


# Kept in exact sync with the real connectors' own validate_config (reddit.py, google_news.py,
# hackernews.py, official_feeds.py) -- the enum values below mirror those modules' internal
# _FEED_ENDPOINTS / _QUERY_ENDPOINTS keys, and get_connector(...).validate_config(...) is still
# called as the final authority in normalize_and_validate_source, so this list drifting out of
# sync with a connector would fail closed (reject a config the connector would have accepted)
# rather than fail open.
_SPECS: tuple[ConnectorTypeSpec, ...] = (
    ConnectorTypeSpec(
        connector_type="reddit_subreddit",
        fields=(_StringField(key="subreddit"),),
        prompt_hint='{"subreddit": "<subreddit name, no r/ prefix>"}',
    ),
    ConnectorTypeSpec(
        connector_type="google_news_query",
        fields=(_StringField(key="query"),),
        prompt_hint='{"query": "<search query>"}',
    ),
    ConnectorTypeSpec(
        connector_type="hackernews_stories",
        fields=(
            _StringField(
                key="feed",
                allowed_values=frozenset({"top", "best", "new", "ask", "show", "job"}),
            ),
        ),
        prompt_hint='{"feed": "top|best|new|ask|show|job"}',
    ),
    ConnectorTypeSpec(
        connector_type="hackernews_query",
        fields=(
            _StringField(key="query"),
            _StringField(key="sort", allowed_values=frozenset({"relevance", "recent"})),
        ),
        prompt_hint='{"query": "<search query>", "sort": "relevance|recent"}',
    ),
    ConnectorTypeSpec(connector_type="rbnz_news", fields=(), prompt_hint="{}"),
    ConnectorTypeSpec(connector_type="nz_government_news", fields=(), prompt_hint="{}"),
    ConnectorTypeSpec(connector_type="federal_reserve_news", fields=(), prompt_hint="{}"),
)

ALLOWED_CONNECTOR_TYPES: dict[str, ConnectorTypeSpec] = {
    spec.connector_type: spec for spec in _SPECS
}
if len(ALLOWED_CONNECTOR_TYPES) != len(_SPECS):
    raise AssertionError("duplicate connector_type in research connector_policy _SPECS")


def _validate_field_value(spec: _StringField, value: object, *, connector_type: str) -> str:
    if not isinstance(value, str):
        raise ConnectorPolicyError(
            f"{connector_type} config field '{spec.key}' must be a string, "
            f"got {type(value).__name__}")
    normalized = value.strip()
    if not normalized:
        raise ConnectorPolicyError(
            f"{connector_type} config field '{spec.key}' must not be empty")
    if len(normalized) > MAX_CONFIG_STRING_LENGTH:
        raise ConnectorPolicyError(
            f"{connector_type} config field '{spec.key}' exceeds "
            f"{MAX_CONFIG_STRING_LENGTH} characters")
    if spec.allowed_values is not None and normalized not in spec.allowed_values:
        allowed = ", ".join(sorted(spec.allowed_values))
        raise ConnectorPolicyError(
            f"{connector_type} config field '{spec.key}' must be one of: {allowed}, "
            f"got {normalized!r}")
    return normalized


def normalize_and_validate_source(
    connector_type: object, config: object,
) -> tuple[str, dict[str, str]]:
    """Strictly validates one proposed (connector_type, config) pair and returns the normalized
    pair ready to persist/execute. Raises ConnectorPolicyError for any unknown connector type,
    non-object config, unknown/credential-shaped/wrong-typed/out-of-range config key, or invalid
    enum value; as the LAST step, delegates to the real connector's own validate_config."""
    if not isinstance(connector_type, str) or connector_type not in ALLOWED_CONNECTOR_TYPES:
        raise ConnectorPolicyError(
            f"unknown or disallowed Research connector type: {connector_type!r}")
    spec = ALLOWED_CONNECTOR_TYPES[connector_type]

    if not isinstance(config, dict):
        raise ConnectorPolicyError(f"{connector_type} config must be a JSON object")

    allowed_keys = {field.key for field in spec.fields}
    for key in config:
        if not isinstance(key, str):
            raise ConnectorPolicyError(f"{connector_type} config has a non-string key: {key!r}")
        if key in allowed_keys:
            continue
        if _is_credential_shaped(key):
            raise ConnectorPolicyError(
                f"{connector_type} config key {key!r} looks credential-shaped and is never "
                "permitted in a Research Source config")
        raise ConnectorPolicyError(f"{connector_type} config has an unexpected key: {key!r}")

    normalized_config: dict[str, str] = {}
    for field in spec.fields:
        if field.key not in config:
            raise ConnectorPolicyError(
                f"{connector_type} config is missing required key '{field.key}'")
        normalized_config[field.key] = _validate_field_value(
            field, config[field.key], connector_type=connector_type)

    try:
        connector = get_connector(connector_type)
    except ValueError as exc:
        # Distinct from a config-rejection failure below: this means connector_type is in
        # ALLOWED_CONNECTOR_TYPES but registry.get() could not resolve it -- an allowlist/
        # registration drift bug in this application, not anything about the proposed config.
        raise ConnectorPolicyError(
            f"{connector_type} is allowlisted but not registered with the connector "
            f"registry: {exc}") from exc

    try:
        connector.validate_config(normalized_config)
    except ValueError as exc:
        raise ConnectorPolicyError(f"{connector_type} config rejected by connector: {exc}") from exc

    return connector_type, normalized_config


def normalize_and_validate_sources(
    sources: list[tuple[object, object]],
) -> list[tuple[str, dict[str, str]]]:
    """Validates a whole proposed source list: per-entry validation (see above), a hard cap on
    how many sources one plan may add, and rejection of duplicate normalized sources -- the
    same connector type with the same normalized config proposed twice is never silently
    deduplicated, it fails the whole batch so the caller can see the plan repeated itself."""
    if len(sources) > MAX_SOURCES_PER_PLAN:
        raise ConnectorPolicyError(
            f"proposed {len(sources)} Research Sources, exceeding the max of "
            f"{MAX_SOURCES_PER_PLAN}")

    normalized: list[tuple[str, dict[str, str]]] = []
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for connector_type, config in sources:
        norm_type, norm_config = normalize_and_validate_source(connector_type, config)
        dedupe_key = (norm_type, tuple(sorted(norm_config.items())))
        if dedupe_key in seen:
            raise ConnectorPolicyError(
                f"duplicate Research Source proposed: {norm_type} {norm_config}")
        seen.add(dedupe_key)
        normalized.append((norm_type, norm_config))
    return normalized
