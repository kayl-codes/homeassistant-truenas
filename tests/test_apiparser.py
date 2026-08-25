"""Unit tests for ``custom_components.truenas_ce.apiparser``.

These are pure-function tests (no Home Assistant import needed) mirroring how
HA Core itself tests standalone helper modules, in preparation for the
HA Core integration submission.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import ModuleType

import pytest


# ---------------------------
#   utc_from_timestamp / human_date_to_utc
# ---------------------------
def test_utc_from_timestamp(ap: ModuleType) -> None:
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
def test_human_date_to_utc(ap: ModuleType, date_str, expected) -> None:
    assert ap.human_date_to_utc(date_str) == expected


# ---------------------------
#   from_entry
# ---------------------------
def test_from_entry_returns_value(ap: ModuleType) -> None:
    assert ap.from_entry({"a": "b"}, "a") == "b"


def test_from_entry_missing_returns_default(ap: ModuleType) -> None:
    assert ap.from_entry({"a": "b"}, "missing", default="fallback") == "fallback"


def test_from_entry_none_entry_returns_default(ap: ModuleType) -> None:
    assert ap.from_entry(None, "a", default="fallback") == "fallback"


def test_from_entry_nested_path(ap: ModuleType) -> None:
    entry = {"scan": {"start_time": {"$date": 1700000000000}}}
    assert ap.from_entry(entry, "scan/start_time/$date") == 1700000000000


def test_from_entry_nested_path_missing_segment(ap: ModuleType) -> None:
    entry = {"scan": {"start_time": {}}}
    assert ap.from_entry(entry, "scan/start_time/$date", default="none") == "none"


def test_from_entry_truncates_long_strings(ap: ModuleType) -> None:
    entry = {"a": "x" * 300}
    assert ap.from_entry(entry, "a", max_len=10) == "x" * 10


def test_from_entry_rounds_floats(ap: ModuleType) -> None:
    entry = {"a": 1.23456}
    assert ap.from_entry(entry, "a", round_digits=2) == pytest.approx(1.23)


def test_from_entry_max_len_none_disables_truncation(ap: ModuleType) -> None:
    entry = {"a": "x" * 300}
    assert ap.from_entry(entry, "a", max_len=None) == "x" * 300


def test_from_entry_round_digits_ignored_for_non_float(ap: ModuleType) -> None:
    entry = {"a": "not-a-number"}
    assert ap.from_entry(entry, "a", round_digits=2) == "not-a-number"


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
def test_from_entry_bool_coercion(ap: ModuleType, value, expected) -> None:
    assert ap.from_entry_bool({"a": value}, "a") is expected


def test_from_entry_bool_missing_returns_default(ap: ModuleType) -> None:
    assert ap.from_entry_bool({}, "a", default=True) is True


def test_from_entry_bool_none_value_uses_default(ap: ModuleType) -> None:
    assert ap.from_entry_bool({"a": None}, "a") is False
    assert ap.from_entry_bool({"a": None}, "a", default=True) is True


def test_from_entry_bool_reverse(ap: ModuleType) -> None:
    assert ap.from_entry_bool({"a": True}, "a", reverse=True) is False
    assert ap.from_entry_bool({"a": "off"}, "a", reverse=True) is True


def test_from_entry_bool_missing_key_reverse_returns_default_unchanged(
    ap: ModuleType,
) -> None:
    """A missing key short-circuits to ``default``; ``reverse`` never applies to it."""
    assert ap.from_entry_bool({}, "a", default=True, reverse=True) is True
    assert ap.from_entry_bool({}, "a", default=False, reverse=True) is False


# ---------------------------
#   get_uid / generate_keymap
# ---------------------------
def test_get_uid_by_key(ap: ModuleType) -> None:
    assert ap.get_uid({"id": "abc"}, "id", None, None, None) == "abc"


def test_get_uid_by_key_secondary(ap: ModuleType) -> None:
    assert ap.get_uid({"other": "abc"}, "id", "other", None, None) == "abc"


def test_get_uid_non_dict_entry_returns_none(ap: ModuleType) -> None:
    assert ap.get_uid("not-a-dict", "id", None, None, None) is None


def test_get_uid_via_key_search(ap: ModuleType) -> None:
    keymap = {"guid-1": "uid-1"}
    assert ap.get_uid({"guid": "guid-1"}, None, None, "guid", keymap) == "uid-1"


def test_generate_keymap_none_when_no_key_search(ap: ModuleType) -> None:
    assert ap.generate_keymap({"uid-1": {"guid": "guid-1"}}, None) is None


def test_generate_keymap_builds_reverse_map(ap: ModuleType) -> None:
    data = {"uid-1": {"guid": "guid-1"}, "uid-2": {}}
    assert ap.generate_keymap(data, "guid") == {"guid-1": "uid-1"}


def test_generate_keymap_ignores_non_dict_values(ap: ModuleType) -> None:
    data = {"uid-1": "unexpected", "uid-2": {"guid": "guid-2"}}
    assert ap.generate_keymap(data, "guid") == {"guid-2": "uid-2"}


def test_generate_keymap_ignores_non_dict_value_containing_key_search_substring(
    ap: ModuleType,
) -> None:
    data = {"uid-1": "has a guid in it", "uid-2": {"guid": "guid-2"}}
    assert ap.generate_keymap(data, "guid") == {"guid-2": "uid-2"}


def test_generate_keymap_skips_entries_missing_key_search(ap: ModuleType) -> None:
    data = {
        "uid-1": {"guid": "guid-1"},
        "uid-2": {},
        "uid-3": {"other": "value"},
    }
    assert ap.generate_keymap(data, "guid") == {"guid-1": "uid-1"}


def test_generate_keymap_skips_non_hashable_key_search_value(ap: ModuleType) -> None:
    data = {"uid-1": {"guid": ["not", "hashable"]}, "uid-2": {"guid": "guid-2"}}
    assert ap.generate_keymap(data, "guid") == {"guid-2": "uid-2"}


# ---------------------------
#   matches_only / can_skip
# ---------------------------
def test_matches_only_all_match(ap: ModuleType) -> None:
    only = [{"key": "type", "value": "DISK"}]
    assert ap.matches_only({"type": "DISK"}, only) is True
    assert ap.matches_only({"type": "SSD"}, only) is False


def test_matches_only_empty_list_returns_true(ap: ModuleType) -> None:
    assert ap.matches_only({"type": "DISK"}, []) is True


def test_can_skip_matches_value(ap: ModuleType) -> None:
    skip = [{"name": "enabled", "value": False}]
    assert ap.can_skip({"enabled": False}, skip) is True
    assert ap.can_skip({"enabled": True}, skip) is False


def test_can_skip_missing_key_with_empty_value(ap: ModuleType) -> None:
    skip = [{"name": "enabled", "value": ""}]
    assert ap.can_skip({}, skip) is True


# ---------------------------
#   fill_defaults
# ---------------------------
def test_fill_defaults_str_and_bool(ap: ModuleType) -> None:
    vals: list[ap.ApiValueSpec] = [
        {"name": "label", "default": "n/a"},
        {"name": "enabled", "type": "bool", "default": True},
    ]
    assert ap.fill_defaults({}, vals) == {"label": "n/a", "enabled": True}


def test_fill_defaults_bool_reverse(ap: ModuleType) -> None:
    vals: list[ap.ApiValueSpec] = [
        {"name": "disabled", "type": "bool", "default": True, "reverse": True}
    ]
    assert ap.fill_defaults({}, vals) == {"disabled": False}


def test_fill_defaults_does_not_overwrite_existing(ap: ModuleType) -> None:
    vals: list[ap.ApiValueSpec] = [{"name": "label", "default": "n/a"}]
    assert ap.fill_defaults({"label": "kept"}, vals) == {"label": "kept"}


def test_fill_defaults_none_data(ap: ModuleType) -> None:
    assert ap.fill_defaults(None, [{"name": "label", "default": "n/a"}]) == {
        "label": "n/a"
    }


def test_fill_defaults_no_vals_returns_data_unchanged(ap: ModuleType) -> None:
    assert ap.fill_defaults({"a": 1}, None) == {"a": 1}
    assert ap.fill_defaults({"a": 1}, []) == {"a": 1}


def test_fill_defaults_default_val_reference(ap: ModuleType) -> None:
    """``default_val`` names a sibling key in the same spec to use as default."""
    vals: list[ap.ApiValueSpec] = [
        {"name": "label", "default_val": "fallback_field", "fallback_field": "computed"}
    ]
    assert ap.fill_defaults({}, vals) == {"label": "computed"}


def test_fill_defaults_default_val_missing_falls_back_to_default(
    ap: ModuleType,
) -> None:
    vals: list[ap.ApiValueSpec] = [
        {"name": "label", "default_val": "missing_field", "default": "n/a"}
    ]
    assert ap.fill_defaults({}, vals) == {"label": "n/a"}


# ---------------------------
#   parse_api
# ---------------------------
def test_parse_api_empty_source_fills_defaults(ap: ModuleType) -> None:
    vals: list[ap.ApiValueSpec] = [{"name": "label", "default": "n/a"}]
    assert ap.parse_api(source=None, vals=vals) == {"label": "n/a"}


def test_parse_api_none_source_with_key_returns_data_unchanged(
    ap: ModuleType,
) -> None:
    assert ap.parse_api(data={"existing": {}}, source=None, key="id") == {
        "existing": {}
    }


def test_parse_api_empty_list_source_with_key_prunes_data(ap: ModuleType) -> None:
    assert ap.parse_api(data={"existing": {}}, source=[], key="id") == {}


def test_parse_api_prunes_uid_missing_from_nonempty_source(ap: ModuleType) -> None:
    """A previously-seen object (e.g. a physically removed disk) absent from an
    otherwise successful, non-empty response must be dropped, not left behind
    forever alongside the objects that are still present."""
    data = {"1": {"name": "pool0"}, "2": {"name": "pool1"}}
    source = [{"id": "2", "name": "pool1"}]
    result = ap.parse_api(data=data, source=source, key="id", vals=[{"name": "name"}])
    assert result == {"2": {"name": "pool1"}}


def test_parse_api_prunes_uid_missing_from_key_search_source(ap: ModuleType) -> None:
    data = {"uid-1": {"guid": "guid-1", "name": "old"}, "uid-2": {"guid": "guid-2"}}
    source = [{"guid": "guid-1", "name": "new"}]
    result = ap.parse_api(
        data=data, source=source, key_search="guid", vals=[{"name": "name"}]
    )
    assert result == {"uid-1": {"guid": "guid-1", "name": "new"}}


def test_parse_api_prune_false_keeps_uids_absent_from_partial_source(
    ap: ModuleType,
) -> None:
    """A caller merging one extra record into an already-populated map (e.g.
    adding the boot-pool to the regular pools) must not have that unrelated
    subset misread as "everything else was removed"."""
    data = {"1": {"name": "pool0"}, "2": {"name": "pool1"}}
    source = [{"id": "boot-pool", "name": "boot-pool"}]
    result = ap.parse_api(
        data=data, source=source, key="id", vals=[{"name": "name"}], prune=False
    )
    assert result == {
        "1": {"name": "pool0"},
        "2": {"name": "pool1"},
        "boot-pool": {"name": "boot-pool"},
    }


def test_parse_api_keyless_source_does_not_prune(ap: ModuleType) -> None:
    """Keyless (single-object) data has no per-uid identity to prune by."""
    result = ap.parse_api(
        data={"total": 42, "stale": "kept"},
        source=[{"total": 43}],
        vals=[{"name": "total"}],
    )
    assert result == {"total": 43, "stale": "kept"}


def test_parse_api_single_dict_source_is_wrapped(ap: ModuleType) -> None:
    result = ap.parse_api(
        source={"id": "1", "name": "pool0"},
        key="id",
        vals=[{"name": "name"}],
    )
    assert result == {"1": {"name": "pool0"}}


def test_parse_api_multiple_entries_by_key(ap: ModuleType) -> None:
    source = [
        {"id": "1", "name": "pool0"},
        {"id": "2", "name": "pool1"},
    ]
    result = ap.parse_api(source=source, key="id", vals=[{"name": "name"}])
    assert result == {"1": {"name": "pool0"}, "2": {"name": "pool1"}}


def test_parse_api_only_filter_skips_non_matching(ap: ModuleType) -> None:
    source = [{"id": "1", "type": "DISK"}, {"id": "2", "type": "SSD"}]
    result = ap.parse_api(
        source=source,
        key="id",
        vals=[{"name": "type"}],
        only=[{"key": "type", "value": "DISK"}],
    )
    assert result == {"1": {"type": "DISK"}}


def test_parse_api_skip_filter_excludes_matching(ap: ModuleType) -> None:
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


def test_parse_api_only_and_skip_combined_skip_wins_on_overlap(ap: ModuleType) -> None:
    """An entry matching both ``only`` and ``skip`` is excluded: ``skip`` wins."""
    source = [
        {"id": "1", "type": "DISK", "enabled": False},
        {"id": "2", "type": "DISK", "enabled": True},
        {"id": "3", "type": "SSD", "enabled": True},
    ]
    result = ap.parse_api(
        source=source,
        key="id",
        vals=[{"name": "type"}],
        only=[{"key": "type", "value": "DISK"}],
        skip=[{"name": "enabled", "value": False}],
    )
    assert result == {"2": {"type": "DISK"}}


def test_parse_api_ensure_vals_adds_missing_keys(ap: ModuleType) -> None:
    result = ap.parse_api(
        source=[{"id": "1"}],
        key="id",
        ensure_vals=[{"name": "extra", "default": "fallback"}],
    )
    assert result == {"1": {"extra": "fallback"}}


def test_parse_api_key_search_maps_to_existing_uid(ap: ModuleType) -> None:
    data = {"uid-1": {"guid": "guid-1", "name": "old"}}
    source = [{"guid": "guid-1", "name": "new"}]
    result = ap.parse_api(
        data=data, source=source, key_search="guid", vals=[{"name": "name"}]
    )
    assert result == {"uid-1": {"guid": "guid-1", "name": "new"}}


def test_parse_api_convert_timestamp(ap: ModuleType) -> None:
    result = ap.parse_api(
        source=[{"id": "1", "started": 1700000000}],
        key="id",
        vals=[{"name": "started", "convert": "utc_from_timestamp"}],
    )
    assert result["1"]["started"] == ap.utc_from_timestamp(1700000000)


def test_parse_api_convert_timestamp_millis(ap: ModuleType) -> None:
    result = ap.parse_api(
        source=[{"id": "1", "started": 1700000000000}],
        key="id",
        vals=[{"name": "started", "convert": "utc_from_timestamp"}],
    )
    assert result["1"]["started"] == ap.utc_from_timestamp(1700000000)


def test_parse_api_no_key_no_search_targets_root(ap: ModuleType) -> None:
    result = ap.parse_api(source=[{"total": 42}], vals=[{"name": "total"}])
    assert result == {"total": 42}


def test_parse_api_no_key_no_search_single_dict_targets_root(ap: ModuleType) -> None:
    """A single-dict ``source`` behaves identically to a one-item list."""
    result = ap.parse_api(source={"total": 42}, vals=[{"name": "total"}])
    assert result == {"total": 42}


def test_parse_api_entry_without_matching_uid_is_skipped(ap: ModuleType) -> None:
    """An entry lacking ``key``/``key_secondary``/``key_search`` is dropped."""
    source = [{"id": "1", "name": "pool0"}, {"name": "orphan"}]
    result = ap.parse_api(source=source, key="id", vals=[{"name": "name"}])
    assert result == {"1": {"name": "pool0"}}


def test_parse_api_all_entries_without_matching_uid_returns_empty(
    ap: ModuleType,
) -> None:
    """When every entry lacks the key fields, an empty mapping is returned."""
    source = [{"name": "orphan1"}, {"name": "orphan2"}]
    result = ap.parse_api(source=source, key="id", vals=[{"name": "name"}])
    assert result == {}


def test_parse_api_key_secondary_used_when_key_missing(ap: ModuleType) -> None:
    source = [{"other": "abc", "name": "pool0"}]
    result = ap.parse_api(
        source=source, key="id", key_secondary="other", vals=[{"name": "name"}]
    )
    assert result == {"abc": {"name": "pool0"}}


def test_parse_api_convert_timestamp_zero_normalized_to_none(ap: ModuleType) -> None:
    result = ap.parse_api(
        source=[{"id": "1", "started": 0}],
        key="id",
        vals=[{"name": "started", "convert": "utc_from_timestamp"}],
    )
    assert result["1"]["started"] is None


def test_parse_api_convert_timestamp_non_int_normalized_to_none(
    ap: ModuleType,
) -> None:
    result = ap.parse_api(
        source=[{"id": "1", "started": "unknown"}],
        key="id",
        vals=[{"name": "started", "convert": "utc_from_timestamp"}],
    )
    assert result["1"]["started"] is None


def test_parse_api_convert_timestamp_negative_normalized_to_none(
    ap: ModuleType,
) -> None:
    result = ap.parse_api(
        source=[{"id": "1", "started": -1}],
        key="id",
        vals=[{"name": "started", "convert": "utc_from_timestamp"}],
    )
    assert result["1"]["started"] is None


def test_parse_api_convert_timestamp_bool_normalized_to_none(ap: ModuleType) -> None:
    """bool is an int subclass but never a valid timestamp."""
    result = ap.parse_api(
        source=[{"id": "1", "started": True}],
        key="id",
        vals=[{"name": "started", "convert": "utc_from_timestamp"}],
    )
    assert result["1"]["started"] is None


def test_parse_api_convert_timestamp_bool_false_normalized_to_none(
    ap: ModuleType,
) -> None:
    """False is also a bool and must not be treated as a timestamp."""
    result = ap.parse_api(
        source=[{"id": "1", "started": False}],
        key="id",
        vals=[{"name": "started", "convert": "utc_from_timestamp"}],
    )
    assert result["1"]["started"] is None


def test_parse_api_convert_timestamp_running_scrub_end_time_none(
    ap: ModuleType,
) -> None:
    """Regression: while a scrub runs, TrueNAS sends scan.end_time = null.

    The spec default (0) must become None so the timestamp sensor reports
    "unknown" instead of crashing on an int state.
    """
    result = ap.parse_api(
        source=[{"id": "1", "scan": {"state": "SCANNING", "end_time": None}}],
        key="id",
        vals=[
            {
                "name": "scrub_end",
                "source": "scan/end_time/$date",
                "default": 0,
                "convert": "utc_from_timestamp",
            }
        ],
    )
    assert result["1"]["scrub_end"] is None


def test_parse_api_convert_timestamp_completed_scrub_end_time_converted(
    ap: ModuleType,
) -> None:
    """Companion to the running-scrub regression: a finished scrub reports
    end_time as {"$date": <millis>}, which must convert to a UTC datetime."""
    result = ap.parse_api(
        source=[
            {
                "id": "1",
                "scan": {"state": "FINISHED", "end_time": {"$date": 1700000000000}},
            }
        ],
        key="id",
        vals=[
            {
                "name": "scrub_end",
                "source": "scan/end_time/$date",
                "default": 0,
                "convert": "utc_from_timestamp",
            }
        ],
    )
    assert result["1"]["scrub_end"] == ap.utc_from_timestamp(1700000000)


def test_parse_api_convert_human_date(ap: ModuleType) -> None:
    result = ap.parse_api(
        source=[{"id": "1", "until": "Fri Mar 26 00:59:59 2100"}],
        key="id",
        vals=[{"name": "until", "convert": "human_date_to_utc"}],
    )
    assert result["1"]["until"] == datetime(2100, 3, 26, 0, 59, 59, tzinfo=UTC)


def test_parse_api_convert_human_date_unparsable_becomes_none(ap: ModuleType) -> None:
    result = ap.parse_api(
        source=[{"id": "1", "until": "not-a-date"}],
        key="id",
        vals=[{"name": "until", "convert": "human_date_to_utc"}],
    )
    assert result["1"]["until"] is None


def test_parse_api_val_proc_runs_alongside_vals(ap: ModuleType) -> None:
    source = [{"id": "1", "host": "truenas", "port": "443"}]
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
    result = ap.parse_api(
        source=source,
        key="id",
        vals=[{"name": "host"}, {"name": "port"}],
        val_proc=val_proc,
    )
    assert result == {
        "1": {"host": "truenas", "port": "443", "url": "https://truenas:443"}
    }


# ---------------------------
#   fill_ensure_vals
# ---------------------------
def test_fill_ensure_vals_creates_missing_uid(ap: ModuleType) -> None:
    result = ap.fill_ensure_vals(
        {}, "uid-1", [{"name": "extra", "default": "fallback"}]
    )
    assert result == {"uid-1": {"extra": "fallback"}}


# ---------------------------
#   fill_vals_proc (combine action)
# ---------------------------
def test_fill_vals_proc_combine(ap: ModuleType) -> None:
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


def test_fill_vals_proc_unsupported_action_raises(ap: ModuleType) -> None:
    val_proc = [[{"name": "url"}, {"action": "unsupported"}]]
    with pytest.raises(ValueError, match="Unsupported action"):
        ap.fill_vals_proc({"uid-1": {}}, "uid-1", val_proc)


def test_fill_vals_proc_ignores_group_starting_before_name_or_action(
    ap: ModuleType,
) -> None:
    """A value item before ``name``/``action`` are set aborts that whole group."""
    val_proc = [
        [{"text": "stray"}, {"name": "url"}, {"action": "combine"}, {"text": "x"}]
    ]
    result = ap.fill_vals_proc({"uid-1": {}}, "uid-1", val_proc)
    assert result == {"uid-1": {}}


def test_fill_vals_proc_aborts_group_when_only_name_is_set(ap: ModuleType) -> None:
    """A value item between ``name`` and ``action`` also aborts the group.

    ``action`` is required to know how to process a value item, so a value
    item seen while only ``name`` is set must abort just like the
    both-missing case, not be silently skipped.
    """
    val_proc = [[{"name": "url"}, {"text": "x"}, {"action": "combine"}, {"text": "y"}]]
    result = ap.fill_vals_proc({"uid-1": {}}, "uid-1", val_proc)
    assert result == {"uid-1": {}}
