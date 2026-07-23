"""Integrity of the declarative ChannelDefinition registry: exactly the three stored kinds are
defined, each with coherent behavior fields, and kind validation stays tied to the registry
rather than a private tuple of literals."""
import pytest

from beehive.channels import (
    all_definitions,
    get_definition,
    require_channel_kind,
)
from beehive.channels.definitions import ChannelDefinition
from beehive.domain.channels import (
    ChannelKind,
    EmailEventType,
    LifecycleMode,
    PersistenceMode,
    RankingMode,
    ReadModel,
)


def test_exactly_the_three_stored_kinds_are_defined():
    kinds = [definition.kind for definition in all_definitions()]
    assert kinds == [ChannelKind.EDITORIAL, ChannelKind.MONITOR, ChannelKind.TRACKER]
    assert {kind.value for kind in ChannelKind} == {"editorial", "monitor", "tracker"}


def test_every_kind_has_exactly_one_definition():
    for kind in ChannelKind:
        definition = get_definition(kind)
        assert isinstance(definition, ChannelDefinition)
        assert definition.kind is kind
    assert len(all_definitions()) == len(list(ChannelKind))


def test_editorial_definition_fields():
    definition = get_definition(ChannelKind.EDITORIAL)
    assert definition.ranking_mode is RankingMode.EDITORIAL
    assert definition.persistence_mode is PersistenceMode.APPEND
    assert definition.lifecycle_mode is LifecycleMode.FEED
    assert definition.read_model is ReadModel.TRACKED
    assert definition.home_eligible is True
    assert definition.archive_eligible is True
    assert definition.manual_watch is False
    assert definition.email_event_types == frozenset({EmailEventType.DISCOVERED})


def test_monitor_definition_fields():
    definition = get_definition(ChannelKind.MONITOR)
    assert definition.ranking_mode is RankingMode.LISTING
    assert definition.persistence_mode is PersistenceMode.MUTABLE_SNAPSHOT
    assert definition.lifecycle_mode is LifecycleMode.CATALOGUE
    assert definition.read_model is ReadModel.SNAPSHOT
    assert definition.home_eligible is False
    assert definition.archive_eligible is False
    assert definition.manual_watch is False
    assert definition.email_event_types == frozenset(
        {
            EmailEventType.DISCOVERED,
            EmailEventType.PRICE_DROP,
            EmailEventType.BACK_IN_STOCK,
        }
    )


def test_tracker_definition_fields():
    definition = get_definition(ChannelKind.TRACKER)
    assert definition.ranking_mode is RankingMode.LISTING
    assert definition.persistence_mode is PersistenceMode.MUTABLE_SNAPSHOT
    assert definition.lifecycle_mode is LifecycleMode.ACTIVE_HISTORY
    assert definition.read_model is ReadModel.SNAPSHOT
    assert definition.home_eligible is False
    assert definition.archive_eligible is False
    assert definition.manual_watch is True
    assert definition.email_event_types == frozenset({EmailEventType.DISCOVERED})


def test_only_the_tracker_kind_supports_a_manual_watch():
    watch_kinds = {d.kind for d in all_definitions() if d.manual_watch}
    assert watch_kinds == {ChannelKind.TRACKER}


def test_only_the_editorial_kind_is_home_eligible():
    home_kinds = {d.kind for d in all_definitions() if d.home_eligible}
    assert home_kinds == {ChannelKind.EDITORIAL}


def test_only_the_editorial_kind_is_archive_eligible():
    archive_kinds = {d.kind for d in all_definitions() if d.archive_eligible}
    assert archive_kinds == {ChannelKind.EDITORIAL}


def test_definitions_are_frozen():
    definition = get_definition(ChannelKind.EDITORIAL)
    with pytest.raises(Exception):
        definition.manual_watch = True  # type: ignore[misc]


def test_require_channel_kind_accepts_every_stored_value():
    for kind in ChannelKind:
        assert require_channel_kind(kind.value) is kind


@pytest.mark.parametrize("value", ["subscription", "editorial ", "", "EDITORIAL", None, 1])
def test_require_channel_kind_rejects_unknown_values(value):
    with pytest.raises(ValueError, match="unknown Channel kind"):
        require_channel_kind(value)


def test_get_definition_rejects_an_unknown_value():
    with pytest.raises(ValueError, match="unknown Channel kind"):
        get_definition("bogus_kind")
    with pytest.raises(ValueError, match="unknown Channel kind"):
        get_definition(1)
