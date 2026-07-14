"""Unit tests for ``custom_components.truenas_ce.apiparser``.

These are pure-function tests (no Home Assistant import needed) mirroring how
HA Core itself tests standalone helper modules, in preparation for the
HA Core integration submission.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPO / "custom_components" / "truenas_ce" / "apiparser.py"

_spec = importlib.util.spec_from_file_location("apiparser", _MODULE_PATH)
assert _spec and _spec.loader
ap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ap)


# ---------------------------
#   utc_from_timestamp / human_date_to_utc
# ---------------------------
def test_utc_from_timestamp() -> None:
    assert ap.utc_from_timestamp(0) == datetime(1970, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("date_str", "expected"),
    [
        ("Fri Mar 26 00:59:59 2100", datetime(2100, 3, 26, 0, 59, 59, tzinfo=UTC)),
        ("not a date", None),
        (None, None),
        (12345, None),
    ],
)
def test_human_date_to_utc(date_str, expected) -> None:
    assert ap.human_date_to_utc(date_str) == expected


# ---------------------------
#   from_entry
# ---------------------------
def test_from_entry_returns_value() -> None:
    assert ap.from_entry({"a": "b"}, "a") == "b"


def test_from_entry_missing_returns_default() -> None:
    assert ap.from_entry({"a": "b"}, "missing", default="fallback") == "fallback"


def test_from_entry_none_entry_returns_default() -> None:
    assert ap.from_entry(None, "a", default="fallback") == "fallback"


def test_from_entry_nested_path() -> None:
    entry = {"scan": {"start_time": {"$date": 1700000000000}}}
    assert ap.from_entry(entry, "scan/start_time/$date") == 1700000000000


def test_from_entry_nested_path_missing_segment() -> None:
    entry = {"scan": {"start_time": {}}}
    assert ap.from_entry(entry, "scan/start_time/$date", default="none") == "none"


def test_from_entry_truncates_long_strings() -> None:
    entry = {"a": "x" * 300}
    assert ap.from_entry(entry, "a", max_len=10) == "x" * 10


def test_from_entry_rounds_floats() -> None:
    entry = {"a": 1.23456}
    assert ap.from_entry(entry, "a", round_digits=2) == 1.23


# ---------------------------
#   from_entry_bool
# ---------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("on", True),
        ("YES", True),
        ("up", True),
        ("true", True),
        ("1", True),
        ("off", False),
        ("no", False),
        ("down", False),
        ("false", False),
        ("0", False),
        ("unrecognized", False),
    ],
)
def test_from_entry_bool_coercion(value, expected) -> None:
    assert ap.from_entry_bool({"a": value}, "a") is expected


def test_from_entry_bool_missing_returns_default() -> None:
    assert ap.from_entry_bool({}, "a", default=True) is True


def test_from_entry_bool_reverse() -> None:
    assert ap.from_entry_bool({"a": True}, "a", reverse=True) is False
    assert ap.from_entry_bool({"a": "off"}, "a", reverse=True) is True


# ---------------------------
#   get_uid / generate_keymap
# ---------------------------
def test_get_uid_by_key() -> None:
    assert ap.get_uid({"id": "abc"}, "id", None, None, None) == "abc"


def test_get_uid_by_key_secondary() -> None:
    assert ap.get_uid({"other": "abc"}, "id", "other", None, None) == "abc"


def test_get_uid_non_dict_entry_returns_none() -> None:
    assert ap.get_uid("not-a-dict", "id", None, None, None) is None


def test_get_uid_via_key_search() -> None:
    keymap = {"guid-1": "uid-1"}
    assert ap.get_uid({"guid": "guid-1"}, None, None, "guid", keymap) == "uid-1"


def test_generate_keymap_none_when_no_key_search() -> None:
    assert ap.generate_keymap({"uid-1": {"guid": "guid-1"}}, None) is None


def test_generate_keymap_builds_reverse_map() -> None:
    data = {"uid-1": {"guid": "guid-1"}, "uid-2": {}}
    assert ap.generate_keymap(data, "guid") == {"guid-1": "uid-1"}


# ---------------------------
#   matches_only / can_skip
# ---------------------------
def test_matches_only_all_match() -> None:
    only = [{"key": "type", "value": "DISK"}]
    assert ap.matches_only({"type": "DISK"}, only) is True
    assert ap.matches_only({"type": "SSD"}, only) is False


def test_can_skip_matches_value() -> None:
    skip = [{"name": "enabled", "value": False}]
    assert ap.can_skip({"enabled": False}, skip) is True
    assert ap.can_skip({"enabled": True}, skip) is False


def test_can_skip_missing_key_with_empty_value() -> None:
    skip = [{"name": "enabled", "value": ""}]
    assert ap.can_skip({}, skip) is True


# ---------------------------
#   fill_defaults
# ---------------------------
def test_fill_defaults_str_and_bool() -> None:
    vals: list[ap.ApiValueSpec] = [
        {"name": "label", "default": "n/a"},
        {"name": "enabled", "type": "bool", "default": True},
    ]
    assert ap.fill_defaults({}, vals) == {"label": "n/a", "enabled": True}


def test_fill_defaults_bool_reverse() -> None:
    vals: list[ap.ApiValueSpec] = [
        {"name": "disabled", "type": "bool", "default": True, "reverse": True}
    ]
    assert ap.fill_defaults({}, vals) == {"disabled": False}


def test_fill_defaults_does_not_overwrite_existing() -> None:
    vals: list[ap.ApiValueSpec] = [{"name": "label", "default": "n/a"}]
    assert ap.fill_defaults({"label": "kept"}, vals) == {"label": "kept"}


def test_fill_defaults_none_data() -> None:
    assert ap.fill_defaults(None, [{"name": "label", "default": "n/a"}]) == {
        "label": "n/a"
    }


# ---------------------------
#   parse_api
# ---------------------------
def test_parse_api_empty_source_fills_defaults() -> None:
    vals: list[ap.ApiValueSpec] = [{"name": "label", "default": "n/a"}]
    assert ap.parse_api(source=None, vals=vals) == {"label": "n/a"}


def test_parse_api_empty_source_with_key_returns_data_unchanged() -> None:
    assert ap.parse_api(data={"existing": {}}, source=[], key="id") == {"existing": {}}


def test_parse_api_single_dict_source_is_wrapped() -> None:
    result = ap.parse_api(
        source={"id": "1", "name": "pool0"},
        key="id",
        vals=[{"name": "name"}],
    )
    assert result == {"1": {"name": "pool0"}}


def test_parse_api_multiple_entries_by_key() -> None:
    source = [
        {"id": "1", "name": "pool0"},
        {"id": "2", "name": "pool1"},
    ]
    result = ap.parse_api(source=source, key="id", vals=[{"name": "name"}])
    assert result == {"1": {"name": "pool0"}, "2": {"name": "pool1"}}


def test_parse_api_only_filter_skips_non_matching() -> None:
    source = [{"id": "1", "type": "DISK"}, {"id": "2", "type": "SSD"}]
    result = ap.parse_api(
        source=source,
        key="id",
        vals=[{"name": "type"}],
        only=[{"key": "type", "value": "DISK"}],
    )
    assert result == {"1": {"type": "DISK"}}


def test_parse_api_skip_filter_excludes_matching() -> None:
    source = [
        {"id": "1", "enabled": False},
        {"id": "2", "enabled": True},
    ]
    result = ap.parse_api(
        source=source,
        key="id",
        vals=[{"name": "enabled", "type": "bool"}],
        skip=[{"name": "enabled", "value": False}],
    )
    assert result == {"2": {"enabled": True}}


def test_parse_api_ensure_vals_adds_missing_keys() -> None:
    result = ap.parse_api(
        source=[{"id": "1"}],
        key="id",
        ensure_vals=[{"name": "extra", "default": "fallback"}],
    )
    assert result == {"1": {"extra": "fallback"}}


def test_parse_api_key_search_maps_to_existing_uid() -> None:
    data = {"uid-1": {"guid": "guid-1", "name": "old"}}
    source = [{"guid": "guid-1", "name": "new"}]
    result = ap.parse_api(
        data=data, source=source, key_search="guid", vals=[{"name": "name"}]
    )
    assert result == {"uid-1": {"guid": "guid-1", "name": "new"}}


def test_parse_api_convert_timestamp() -> None:
    result = ap.parse_api(
        source=[{"id": "1", "started": 1700000000}],
        key="id",
        vals=[{"name": "started", "convert": "utc_from_timestamp"}],
    )
    assert result["1"]["started"] == ap.utc_from_timestamp(1700000000)


def test_parse_api_convert_timestamp_millis() -> None:
    result = ap.parse_api(
        source=[{"id": "1", "started": 1700000000000}],
        key="id",
        vals=[{"name": "started", "convert": "utc_from_timestamp"}],
    )
    assert result["1"]["started"] == ap.utc_from_timestamp(1700000000)


def test_parse_api_no_key_no_search_targets_root() -> None:
    result = ap.parse_api(source=[{"total": 42}], vals=[{"name": "total"}])
    assert result == {"total": 42}


# ---------------------------
#   fill_vals_proc (combine action)
# ---------------------------
def test_fill_vals_proc_combine() -> None:
    data = {"uid-1": {"host": "truenas", "port": "443"}}
    val_proc = [
        [
            {"name": "url"},
            {"action": "combine"},
            {"text": "https://"},
            {"key": "host"},
            {"text": ":"},
            {"key": "port"},
        ]
    ]
    result = ap.fill_vals_proc(data, "uid-1", val_proc)
    assert result["uid-1"]["url"] == "https://truenas:443"


def test_fill_vals_proc_unsupported_action_raises() -> None:
    val_proc = [[{"name": "url"}, {"action": "unsupported"}]]
    with pytest.raises(ValueError, match="Unsupported action"):
        ap.fill_vals_proc({"uid-1": {}}, "uid-1", val_proc)
