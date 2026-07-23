"""The fail-closed Source/Channel compatibility policy: the recurring connectors declare the
right kinds, the matrix is exactly the approved one, and every unknown kind / unknown connector /
missing declaration / mismatch fails clearly instead of silently allowing or dropping a Source."""
import pytest

from beehive.channels.source_policy import (
    assert_source_allowed,
    connector_supports_kind,
    source_types_for_kind,
)
from beehive.connectors.registry import get as get_connector
from beehive.connectors.registry import register
from beehive.domain.channels import ChannelKind

# The approved Source/Channel compatibility matrix for this foundation. Kept explicit here (not
# derived from the connectors) so a connector silently changing its declared kinds fails this
# test instead of quietly redefining the policy.
_EXPECTED = {
    ChannelKind.EDITORIAL: {
        "reddit_subreddit",
        "google_news_query",
        "hackernews_stories",
        "hackernews_query",
        "rbnz_news",
        "nz_government_news",
        "federal_reserve_news",
    },
    ChannelKind.MONITOR: {"shopify_collection", "land_sea_collection"},
    ChannelKind.TRACKER: {"all_about_auctions"},
}
_ALL_REAL_TYPES = set().union(*_EXPECTED.values())


@pytest.mark.parametrize("kind", list(ChannelKind))
def test_source_types_for_kind_matches_the_approved_matrix(kind):
    # Intersect with the known real connectors so the assertion is robust to test-only stub
    # connectors other test modules register into the shared registry.
    offered = {t for t in source_types_for_kind(kind) if t in _ALL_REAL_TYPES}
    assert offered == _EXPECTED[kind]


def test_no_real_source_type_is_offered_for_more_than_one_kind():
    seen: set[str] = set()
    for kind in ChannelKind:
        offered = {t for t in source_types_for_kind(kind) if t in _ALL_REAL_TYPES}
        assert not (offered & seen), "a recurring connector claims more than one kind"
        seen |= offered
    assert seen == _ALL_REAL_TYPES


def test_every_recurring_connector_declares_a_valid_non_empty_kind_set():
    for type_key in _ALL_REAL_TYPES:
        declared = get_connector(type_key).supported_channel_kinds
        assert isinstance(declared, frozenset)
        assert declared, f"{type_key} declares an empty supported_channel_kinds"
        assert declared <= set(ChannelKind)


@pytest.mark.parametrize("kind", list(ChannelKind))
def test_connector_supports_kind_agrees_with_the_matrix(kind):
    for type_key in _ALL_REAL_TYPES:
        expected = type_key in _EXPECTED[kind]
        assert connector_supports_kind(type_key, kind) is expected


def test_assert_source_allowed_passes_for_a_compatible_pair():
    assert_source_allowed("all_about_auctions", ChannelKind.TRACKER) is None


def test_assert_source_allowed_rejects_a_mismatch():
    with pytest.raises(ValueError, match="not compatible with a 'monitor' Channel"):
        assert_source_allowed("reddit_subreddit", ChannelKind.MONITOR)


def test_unknown_source_type_fails_clearly():
    with pytest.raises(ValueError, match="unknown Source type"):
        connector_supports_kind("does_not_exist", ChannelKind.EDITORIAL)


def test_unknown_channel_kind_fails_clearly():
    with pytest.raises(ValueError, match="unknown Channel kind"):
        source_types_for_kind("editorial")  # a raw string is not a ChannelKind
    with pytest.raises(ValueError, match="unknown Channel kind"):
        connector_supports_kind("reddit_subreddit", "editorial")


class _NoDeclarationConnector:
    type_key = "policy_test_no_declaration"

    def validate_config(self, config):
        pass

    def fetch(self, config):
        return []


class _EmptyDeclarationConnector:
    type_key = "policy_test_empty_declaration"
    supported_channel_kinds = frozenset()

    def validate_config(self, config):
        pass

    def fetch(self, config):
        return []


def test_missing_declaration_fails_closed_when_queried_directly():
    register(_NoDeclarationConnector())
    with pytest.raises(ValueError, match="declares no supported_channel_kinds"):
        connector_supports_kind("policy_test_no_declaration", ChannelKind.EDITORIAL)


def test_empty_declaration_fails_closed_when_queried_directly():
    register(_EmptyDeclarationConnector())
    with pytest.raises(ValueError, match="supported_channel_kinds is empty"):
        assert_source_allowed("policy_test_empty_declaration", ChannelKind.EDITORIAL)


def test_undeclared_connector_is_excluded_from_enumeration_not_crashing():
    # A connector with a missing/empty declaration must never appear as compatible with any kind,
    # and must not make the whole enumeration raise.
    register(_NoDeclarationConnector())
    register(_EmptyDeclarationConnector())
    for kind in ChannelKind:
        offered = set(source_types_for_kind(kind))
        assert "policy_test_no_declaration" not in offered
        assert "policy_test_empty_declaration" not in offered
