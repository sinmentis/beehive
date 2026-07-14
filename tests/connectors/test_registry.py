import pytest

from beehive.connectors.base import RawItem, SourceConnector
from beehive.connectors.registry import get, register


class _FakeConnector:
    type_key = "fake_type"

    def validate_config(self, config: dict) -> None:
        if "x" not in config:
            raise ValueError("needs x")

    def fetch(self, config: dict) -> list[RawItem]:
        return [RawItem(external_id="1", title="t", url="https://x")]


# Static assertion that the fake structurally satisfies the connector Protocol.
_typecheck: SourceConnector = _FakeConnector()


def test_register_and_get():
    register(_FakeConnector())
    connector = get("fake_type")
    assert connector.type_key == "fake_type"
    items = connector.fetch({"x": 1})
    assert items[0].external_id == "1"


def test_get_unknown_type_raises():
    with pytest.raises(ValueError, match="unknown Source type"):
        get("does_not_exist")


def test_raw_item_defaults():
    item = RawItem(external_id="1", title="t", url="https://x")
    assert item.body == ""
    assert item.created_at is None
    assert item.raw_metadata == {}
