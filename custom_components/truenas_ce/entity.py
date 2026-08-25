"""TrueNAS HA shared entity model."""

from __future__ import annotations

import inspect
from asyncio import Lock
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from logging import getLogger
from typing import Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ATTRIBUTION, CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_platform as ep
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .api import _summarize_payload
from .const import (
    ATTRIBUTION,
    BEHAVIOR_REMOVE_INACTIVE_NIC,
    CONF_BEHAVIORS,
    CONF_SYSTEM_ID,
    DEFAULT_BEHAVIORS,
    DOMAIN,
    SIGNAL_UPDATE_SENSORS,
)
from .coordinator import TrueNASCoordinator, get_truenas_coordinator
from .helper import format_attribute

_LOGGER = getLogger(__name__)

_UNKNOWN_KEY = "<unknown>"


# ---------------------------
#   resolve_entry_identity / format_unique_id
# ---------------------------
def resolve_entry_identity(config_entry: ConfigEntry) -> str:
    """Return a stable per-entry identity string for unique_ids/device identifiers.

    Prefers CONF_SYSTEM_ID (the TrueNAS system.global.id UUID, only populated
    when that lookup succeeded during setup); falls back to the config
    entry's own entry_id, which HA guarantees unique and stable for the
    entry's lifetime. Never use CONF_NAME (the user-editable display name)
    for this purpose -- two entries can share a display name, which would
    otherwise collide entities/devices across different TrueNAS servers.
    """
    system_id = config_entry.data.get(CONF_SYSTEM_ID)
    if isinstance(system_id, str) and system_id:
        return system_id
    return config_entry.entry_id


def format_unique_id(identity: str, key: str, reference: object = None) -> str:
    """Build an entity unique_id from the entry identity, key and reference.

    ``identity`` must be a stable per-entry identity (see
    ``resolve_entry_identity``), not the user-editable display name.
    Shared so the migration in __init__.py can resolve the same unique_id an
    entity produces.

    ``reference`` keeps its original case and is not slugified: unique_id has
    no character restrictions in HA, and both slugify() and lower() are lossy
    -- slugify's '/'/'-'/'_' collapsing and lower()'s case-folding would each
    let distinct references (e.g. ZFS datasets "tank/a-b" vs "tank/a_b", or
    "tank/Data" vs "tank/data") collide and silently drop one entity as a
    duplicate. See ``migrate_legacy_unique_ids`` for the one-time rename this
    requires on existing installations.
    """
    base = f"{identity.lower()}-{key}"
    if reference is None:
        return base
    return f"{base}-{reference!s}"


def _legacy_format_unique_id(identity: str, key: str, reference: object = None) -> str:
    """Pre-2.9 unique_id format (lossy ``slugify()`` of the lowercased reference).

    Only used by ``migrate_legacy_unique_ids`` to locate registry entries
    created under this old format; entities themselves use ``format_unique_id``.
    """
    base = f"{identity.lower()}-{key}"
    if reference is None:
        return base
    return f"{base}-{slugify(str(reference).lower())}"


def _lowercased_unique_id(identity: str, key: str, reference: object = None) -> str:
    """2.9/2.10-era unique_id format (lowercased but not slugified reference).

    Only used by ``migrate_legacy_unique_ids`` to locate registry entries
    created under this old format; entities themselves use ``format_unique_id``.
    """
    base = f"{identity.lower()}-{key}"
    if reference is None:
        return base
    return f"{base}-{str(reference).lower()}"


def format_device_identifier(identity: str) -> str:
    """Build the main TrueNAS ("System") device identifier value.

    ``identity`` must be a stable per-entry identity (see
    ``resolve_entry_identity``), not the user-editable display name. Uses
    ``identity`` alone -- an earlier format also appended the TrueNAS
    hostname, but hostname is user-editable (System Settings > General)
    while identity is already stable, so renaming the TrueNAS host silently
    orphaned this device and created a duplicate. Shared so other platforms
    (e.g. the diagnostic statistics-cleanup button) associate with the
    existing device instead of duplicating the format. See
    ``migrate_legacy_device_identifier`` for the one-time rename this
    requires on existing installations.
    """
    return identity


def _legacy_format_device_identifier(identity: str, hostname: str) -> str:
    """Pre-2.9 device identifier format (``identity`` plus the TrueNAS hostname).

    Only used by ``migrate_legacy_device_identifier`` to locate the registry
    entry created under this old format; devices themselves use
    ``format_device_identifier``.
    """
    return f"{identity}_{hostname}"


def migrate_legacy_device_identifier(
    hass: HomeAssistant, identity: str, hostname: str
) -> None:
    """Rewrite the System device's registry identifier from the earlier format.

    Must run before ``register_system_device`` so it finds the
    already-renamed record instead of creating a second device. ``hostname``
    is the *current* TrueNAS hostname: if it already changed on an existing
    installation before this migration shipped, the old device under the
    stale hostname is unrecoverable here (same tradeoff as
    ``migrate_legacy_unique_ids`` -- nothing to guess from, so it is left
    alone for the existing orphaned-device situation rather than misapplied).
    """
    legacy_identifier = _legacy_format_device_identifier(identity, hostname)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device(identifiers={(DOMAIN, legacy_identifier)})
    if device is not None:
        dev_reg.async_update_device(
            device.id, new_identifiers={(DOMAIN, format_device_identifier(identity))}
        )


def migrate_entry_identity_namespace(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """Rewrite this entry's registry records from the old name-based identity.

    Before ``resolve_entry_identity`` existed, every unique_id/device
    identifier was namespaced by ``config_entry.data[CONF_NAME]`` (see
    ``format_unique_id``/``format_device_identifier``'s history). Every
    already-registered entity/device for an existing installation therefore
    still carries that old prefix; left alone, it would never match the new
    identity-based unique_ids/identifiers the entities compute from now on,
    producing duplicate entities/devices instead of continuing the old ones.
    Must run before ``register_system_device`` and any platform setup, so the
    device/entity lookups in those find the already-renamed records.
    """
    old_name = config_entry.data.get(CONF_NAME, "")
    identity = resolve_entry_identity(config_entry)
    if not old_name or old_name == identity:
        return

    old_uid_prefix = f"{old_name.lower()}-"
    new_uid_prefix = f"{identity.lower()}-"
    ent_reg = er.async_get(hass)
    for entity_entry in er.async_entries_for_config_entry(
        ent_reg, config_entry.entry_id
    ):
        if entity_entry.unique_id.startswith(old_uid_prefix):
            ent_reg.async_update_entity(
                entity_entry.entity_id,
                new_unique_id=new_uid_prefix
                + entity_entry.unique_id[len(old_uid_prefix) :],
            )

    old_dev_prefix = f"{old_name}_"
    new_dev_prefix = f"{identity}_"
    dev_reg = dr.async_get(hass)
    for device_entry in dr.async_entries_for_config_entry(
        dev_reg, config_entry.entry_id
    ):
        if len(device_entry.config_entries) > 1:
            # Two entries that previously shared this display name (and e.g.
            # a same-named pool) collided onto the same old, name-based
            # device record. Renaming it in place for one entry would just
            # get overwritten by the other entry's own migration pass,
            # leaving entities misattributed. Leave it as-is: this entry's
            # entities still get their unique_ids migrated above, so the
            # normal device_info/get_or_create path in platform setup gives
            # them a fresh, correctly-identified device instead of reusing
            # the ambiguous shared one.
            continue
        new_identifiers = {
            (domain, new_dev_prefix + value[len(old_dev_prefix) :])
            if domain == DOMAIN and value.startswith(old_dev_prefix)
            else (domain, value)
            for domain, value in device_entry.identifiers
        }
        if new_identifiers != device_entry.identifiers:
            dev_reg.async_update_device(
                device_entry.id, new_identifiers=new_identifiers
            )


_LegacyFormatter = Callable[[str, str, object], str]


def _referenced_id_pairs(
    identity: str,
    description: TrueNASEntityDescription,
    data: Mapping[str, Any],
    legacy_formatter: _LegacyFormatter = _legacy_format_unique_id,
) -> set[tuple[str, str]]:
    """Return (legacy_unique_id, current_unique_id) pairs for one reference."""
    pairs: set[tuple[str, str]] = set()
    for uid, vals in data.items():
        if not isinstance(vals, dict):
            continue
        ref = vals.get(description.data_reference)
        reference = ref if ref is not None else uid
        pairs.add(
            (
                legacy_formatter(identity, description.key, reference),
                format_unique_id(identity, description.key, reference),
            )
        )
    return pairs


def _composite_id_pairs(
    identity: str,
    description: TrueNASEntityDescription,
    data: Mapping[str, Any],
    legacy_formatter: _LegacyFormatter = _legacy_format_unique_id,
) -> set[tuple[str, str]]:
    """Return (legacy_unique_id, current_unique_id) pairs for one composite ref."""
    pairs: set[tuple[str, str]] = set()
    if len(description.data_composite_references) != 2:
        return pairs
    container_key, leaf_key = description.data_composite_references
    for uid, vals in data.items():
        container = _get_composite_container(vals, container_key)
        if container is None:
            continue
        for item in container:
            ref = _extract_composite_ref(item, description, False, leaf_key)
            if ref is None:
                continue
            composed = f"{uid}::{ref}"
            pairs.add(
                (
                    legacy_formatter(identity, description.key, composed),
                    format_unique_id(identity, description.key, composed),
                )
            )
    return pairs


def _merge_rename_candidates(
    pairs: set[tuple[str, str]], candidates: dict[str, set[str]]
) -> None:
    """Record one description's (old, new) unique_id pairs by legacy id.

    Every candidate new id is recorded here, including ones that equal the
    old id -- an unaffected reference (unchanged by the fix) can still share
    its legacy slug with a different, affected reference, so an early
    old == new skip here would hide that collision instead of flagging it
    (Sourcery finding on an earlier version of this migration).
    """
    for old, new in pairs:
        candidates.setdefault(old, set()).add(new)


def _resolve_renames(candidates: dict[str, set[str]]) -> dict[str, str]:
    """Turn old-id -> candidate-new-ids into the final rename map.

    A legacy id with more than one distinct candidate new id (a collision
    the fix eliminates going forward) is left out entirely rather than
    arbitrarily renamed to one of them -- see ``migrate_legacy_unique_ids``
    for why.
    """
    renames: dict[str, str] = {}
    for old, news in candidates.items():
        if len(news) != 1:
            continue
        (new,) = news
        if new != old:
            renames[old] = new
    return renames


# Every unique_id format a registry entry may still carry from before the fix
# that introduced its successor -- oldest first. ``_legacy_format_unique_id``
# is the oldest format, and ``_lowercased_unique_id`` was the subsequent
# default format (both were still lowercasing the reference before
# ``format_unique_id`` stopped doing that too), so an installation that
# skipped upgrades can have entries in either format; both are checked so it
# is renamed straight to the current format.
_LEGACY_UNIQUE_ID_FORMATTERS: tuple[_LegacyFormatter, ...] = (
    _legacy_format_unique_id,
    _lowercased_unique_id,
)


def _collect_unique_id_renames(
    identity: str,
    descriptions: Sequence[TrueNASEntityDescription],
    coordinator: TrueNASCoordinator,
) -> dict[str, str]:
    """Build the old-to-new unique_id rename map across all descriptions.

    A composite-reference description (``data_composite_references`` set,
    see ``TrueNASEntityDescription.__post_init__``) is valid without a plain
    ``data_reference`` -- skipping it whenever ``data_reference`` alone was
    unset silently left its legacy entries (e.g. per-app network sensors)
    unmigrated (Sourcery finding on an earlier version of this migration).

    Checked against every format in ``_LEGACY_UNIQUE_ID_FORMATTERS``, merged
    into one candidate map so a legacy id ambiguous under one format still
    correctly blocks renaming under another (see ``_merge_rename_candidates``).
    """
    candidates: dict[str, set[str]] = {}
    for legacy_formatter in _LEGACY_UNIQUE_ID_FORMATTERS:
        for description in descriptions:
            has_composite = bool(getattr(description, "data_composite_references", ()))
            if not getattr(description, "data_reference", None) and not has_composite:
                continue
            data = coordinator.data.get(description.data_path or "")
            if not isinstance(data, dict):
                continue
            pairs = (
                _composite_id_pairs(identity, description, data, legacy_formatter)
                if has_composite
                else _referenced_id_pairs(identity, description, data, legacy_formatter)
            )
            _merge_rename_candidates(pairs, candidates)
    return _resolve_renames(candidates)


def migrate_legacy_unique_ids(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    coordinator: TrueNASCoordinator,
    descriptions: Sequence[TrueNASEntityDescription],
) -> None:
    """Rewrite registry entries from an earlier unique_id format.

    ``format_unique_id``'s reference handling has tightened twice: it first
    stopped running the reference through ``slugify()`` (which collapsed
    distinct references like ZFS datasets "tank/a-b" and "tank/a_b", or app
    names like "immich-server" combined with an interface name, onto the same
    unique_id), then stopped lowercasing it too (which just as lossily
    collapsed case-sensitive references like "tank/Data" and "tank/data").
    Each tightening silently dropped one colliding entity as a duplicate, so
    every already-registered entity for an existing installation must be
    renamed here -- otherwise HA treats it as gone and creates a fresh,
    differently-IDed entity, breaking the existing entity_id, history and any
    automations/dashboards referencing it. Must run before
    ``register_system_device``, orphaned-entity cleanup and any platform
    setup, so those find the already-renamed records.

    A single legacy id can match more than one *current* reference (that is
    exactly the collision each fix eliminates going forward). Which of those
    references the one existing legacy entry actually belonged to is
    unrecoverable, so such an old id is left unrenamed rather than guessed
    at -- silently reassigning it to the wrong dataset/interface would be
    worse than leaving a stale entity behind for the existing orphan-cleanup
    flow to catch (Sourcery finding on the initial version of this
    migration).
    """
    identity = resolve_entry_identity(config_entry)
    renames = _collect_unique_id_renames(identity, descriptions, coordinator)
    if not renames:
        return

    ent_reg = er.async_get(hass)
    for entity_entry in er.async_entries_for_config_entry(
        ent_reg, config_entry.entry_id
    ):
        new_id = renames.get(entity_entry.unique_id)
        if new_id is not None:
            ent_reg.async_update_entity(entity_entry.entity_id, new_unique_id=new_id)


@lru_cache(maxsize=1)
def _supports_via_device_id() -> bool:
    """Whether the running HA Core's device registry accepts via_device_id.

    ``via_device_id`` was only added to ``DeviceRegistry.async_get_or_create``
    in HA Core 2026.8 -- passing it as a kwarg on an older Core raises
    TypeError there, so it can only be used once detected as supported.
    ``via_device`` (the older identifiers-tuple form) keeps working
    everywhere until it is removed in 2027.8.0, so older installs fall back
    to it. Cached: this cannot change while the process is running.
    """
    return (
        "via_device_id"
        in inspect.signature(dr.DeviceRegistry.async_get_or_create).parameters
    )


def register_system_device(
    hass: HomeAssistant, config_entry: ConfigEntry, coordinator: TrueNASCoordinator
) -> str:
    """Register (or fetch) the "System" device and return its registry id.

    Called once from ``async_setup_entry`` after the coordinator's first
    refresh, before platforms create entities. Every other device links to
    it via ``coordinator.system_device_id`` (``via_device_id``) instead of
    resolving it itself -- ``via_device`` (an identifiers tuple) is
    deprecated as of HA Core 2027.8.0 because identifiers are no longer
    unique across config entries.
    """
    inst = coordinator.config_entry.data[CONF_NAME]
    identity = resolve_entry_identity(coordinator.config_entry)
    identifier = format_device_identifier(identity)
    system_info = coordinator.data["system_info"]
    http_scheme = "https" if coordinator.api.scheme == "wss" else "http"
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, identifier)},
        name=inst,
        model=f"{system_info['system_product']}",
        manufacturer=f"{system_info['system_manufacturer']}",
        sw_version=f"{system_info['version']}",
        configuration_url=f"{http_scheme}://{config_entry.data[CONF_HOST]}",
    )
    return device.id


# ---------------------------
#   TrueNASEntityDescription
# ---------------------------
# Dynamic vs static entity contract:
#   - Static descriptions (data_dynamic_keys=False) have a fixed data_path and
#     produce either one keyless entity or one entity per referenced object.
#   - Dynamic descriptions (data_dynamic_keys=True) use top-level data keys as
#     entity UIDs, allowing arbitrary objects to become entities.
#   - Composite references (data_composite_references=(container, leaf)) add a
#     second level of dynamism: the leaf value from each nested object becomes
#     part of the composite UID (``uid::leaf``), used for per-subobject entities
#     like per-NIC network sensors.
@dataclass(frozen=True, kw_only=True)
class TrueNASEntityDescription(EntityDescription):
    """Fields shared by the entity descriptions of every TrueNAS platform."""

    ha_group: str | None = None
    ha_connection: str | None = None
    ha_connection_value: str | None = None
    data_path: str | None = None
    data_name: str | None = None
    data_uid: str | None = None
    data_reference: str | None = None
    data_attributes_list: tuple[str, ...] = ()
    data_dynamic_keys: bool = False
    data_composite_references: tuple[str, ...] = ()
    func: str = ""

    def __post_init__(self) -> None:
        """Validate combinations of dynamic flags and references.

        Invalid configurations emit warnings so they fail fast in tests/CI
        without crashing the integration at import time.
        """
        composite = self.data_composite_references or ()
        has_composite = bool(composite)
        dynamic = self.data_dynamic_keys

        if has_composite and not dynamic:
            _LOGGER.warning(
                "Invalid TrueNASEntityDescription %r: "
                "data_composite_references requires data_dynamic_keys=True",
                getattr(self, "key", _UNKNOWN_KEY),
            )
            return

        if has_composite and len(composite) != 2:
            _LOGGER.warning(
                "Invalid TrueNASEntityDescription %r: "
                "data_composite_references must contain exactly two segments "
                "(container_key, leaf_key)",
                getattr(self, "key", _UNKNOWN_KEY),
            )
            return

        if dynamic and not self.data_reference and not has_composite:
            _LOGGER.warning(
                "Invalid TrueNASEntityDescription %r: "
                "data_dynamic_keys=True requires either data_reference or "
                "data_composite_references",
                getattr(self, "key", _UNKNOWN_KEY),
            )


def _composite_references(
    identity: str,
    description: TrueNASEntityDescription,
    data: dict[str, Any],
    honor_exclude: bool = True,
) -> set[str]:
    """Compute unique_ids for descriptions whose reference is nested inside a list.

    For each top-level uid in ``data``, the leaf value at
    ``data[uid][container_key][item][leaf_key]`` becomes a composite unique_id
    of the form ``identity-key-uid::ref``. This supports entities like per-NIC
    network sensors where the interface name lives inside a list of dicts.
    ``identity`` must be a stable per-entry identity (see
    ``resolve_entry_identity``), not the user-editable display name.

    When ``honor_exclude`` is True, items matching ``description.data_exclude``
    are skipped, mirroring ``_referenced_unique_ids`` behavior.
    """
    ids: set[str] = set()
    if len(description.data_composite_references) != 2:
        return ids
    container_key, leaf_key = description.data_composite_references
    for uid, vals in data.items():
        container = _get_composite_container(vals, container_key)
        if container is None:
            continue
        for item in container:
            ref = _extract_composite_ref(item, description, honor_exclude, leaf_key)
            if ref is not None:
                ids.add(format_unique_id(identity, description.key, f"{uid}::{ref}"))
    return ids


def _get_composite_container(vals: Any, container_key: str) -> list[Any] | None:
    """Return the composite container list if present and valid, else None."""
    if not isinstance(vals, dict):
        return None
    container = vals.get(container_key)
    return container if isinstance(container, list) else None


def _extract_composite_ref(
    item: Any,
    description: TrueNASEntityDescription,
    honor_exclude: bool,
    leaf_key: str,
) -> str | None:
    """Validate a composite item and return its leaf reference, or None."""
    if not isinstance(item, dict):
        return None
    if honor_exclude and _is_uid_excluded(description, item):
        return None
    ref = item.get(leaf_key)
    return ref if ref is not None else None


# ---------------------------
#   Entity discovery helpers
# ---------------------------
def _skip_keyless_description(
    entity_description: TrueNASEntityDescription, data: dict[str, Any]
) -> bool:
    """Return True if a keyless description has no value to expose."""
    attr_name = getattr(entity_description, "data_attribute", None) or getattr(
        entity_description, "data_is_on", None
    )
    return data.get(attr_name) is None if attr_name else False


def _is_uid_excluded(entity_description: TrueNASEntityDescription, vals: Any) -> bool:
    """Return True if a referenced object is excluded from entity creation.

    Honors an optional ``data_exclude`` (key, value) on the description, e.g. to
    skip traffic sensors for a network interface whose link is down.
    """
    data_exclude = getattr(entity_description, "data_exclude", None)
    if not data_exclude:
        return False

    key, value = data_exclude
    return isinstance(vals, dict) and vals.get(key) == value


def _new_referenced_entities(
    coordinator: TrueNASCoordinator,
    entity_description: TrueNASEntityDescription,
    data: Mapping[str, Any],
    dispatcher: Mapping[str, Callable[..., Any]],
    seen: set[str],
) -> list[TrueNASEntity]:
    """Collect new per-uid entities for one referenced (multi-object) description."""
    behaviors = coordinator.config_entry.options.get(CONF_BEHAVIORS, DEFAULT_BEHAVIORS)
    apply_exclude = BEHAVIOR_REMOVE_INACTIVE_NIC in behaviors
    new_entities: list[TrueNASEntity] = []
    for uid, vals in data.items():
        if apply_exclude and _is_uid_excluded(entity_description, vals):
            continue
        obj = dispatcher[entity_description.func](coordinator, entity_description, uid)
        _append_if_new(obj, seen, new_entities)
    return new_entities


def _collect_new_entities(
    coordinator: TrueNASCoordinator,
    descriptions: Sequence[TrueNASEntityDescription],
    dispatcher: Mapping[str, Callable[..., Any]],
    seen: set[str],
) -> list[TrueNASEntity]:
    """Return entity objects whose unique_id is not in ``seen`` yet.

    ``seen`` is the set of unique_ids already registered for this config entry;
    only genuinely new objects (e.g. a freshly attached disk) are returned, so
    existing entities are never re-added.
    """
    new_entities: list[TrueNASEntity] = []
    for entity_description in descriptions:
        if entity_description.func == "TrueNASAppStatsSensor":
            continue
        data = coordinator.data.get(entity_description.data_path or "")
        if data is None:
            continue
        if not isinstance(data, dict):
            # A malformed coordinator payload for this data_path would
            # otherwise raise AttributeError in _skip_keyless_description's
            # or _new_referenced_entities' unconditional .get() access.
            _LOGGER.debug(
                "Skipping non-dict coordinator payload for data_path %s"
                " (entity description key %s): %s",
                entity_description.data_path or "",
                entity_description.key,
                _summarize_payload(data),
            )
            continue

        if entity_description.data_reference:
            new_entities += _new_referenced_entities(
                coordinator, entity_description, data, dispatcher, seen
            )
        elif not _skip_keyless_description(entity_description, data):
            obj = dispatcher[entity_description.func](coordinator, entity_description)
            _append_if_new(obj, seen, new_entities)

    return new_entities


def _append_if_new(
    obj: TrueNASEntity, seen: set[str], new_entities: list[TrueNASEntity]
) -> None:
    """Append the entity to the batch when its unique_id has not been seen yet."""
    if obj.unique_id not in seen:
        seen.add(obj.unique_id)
        new_entities.append(obj)


def _cleanup_orphaned_entities(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    coordinator: TrueNASCoordinator,
) -> None:
    """Remove registry entities the integration would no longer create.

    An entity is deleted when it is not in the active set yet belongs to a data
    domain that currently holds data. This covers both true orphans (the object
    is gone) and entities filtered out by ``data_exclude`` (e.g. traffic sensors
    of a down interface). A transient empty fetch of a whole domain never wipes
    the corresponding group, and cleanup is skipped unless the last update
    succeeded.
    """
    from . import _collect_active_unique_ids

    if not coordinator.last_update_success:
        return

    identity = resolve_entry_identity(config_entry)
    active, live_bases = _collect_active_unique_ids(identity, coordinator)

    ent_reg = er.async_get(hass)
    for entity_entry in er.async_entries_for_config_entry(
        ent_reg, config_entry.entry_id
    ):
        unique_id = entity_entry.unique_id
        if unique_id in active:
            continue
        if any(
            unique_id == base or unique_id.startswith(f"{base}-") for base in live_bases
        ):
            _LOGGER.info(
                "Removing orphaned TrueNAS entity %s (unique_id=%s)",
                entity_entry.entity_id,
                unique_id,
            )
            ent_reg.async_remove(entity_entry.entity_id)

    # Remove devices that are now empty (all their entities were cleaned up above).
    dev_reg = dr.async_get(hass)
    for device_entry in dr.async_entries_for_config_entry(
        dev_reg, config_entry.entry_id
    ):
        if not er.async_entries_for_device(
            ent_reg, device_entry.id, include_disabled_entities=True
        ):
            _LOGGER.info(
                "Removing empty TrueNAS device %s",
                device_entry.name_by_user or device_entry.name,
            )
            dev_reg.async_remove_device(device_entry.id)


async def async_add_entities(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    dispatcher: Mapping[str, Callable[..., Any]],
) -> None:
    """Set up the platform and register dynamic entity discovery.

    On every coordinator refresh only entities that are not already loaded on the
    platform are created; existing entities refresh themselves through the
    coordinator and are never re-added (which previously caused "Platform truenas
    does not generate unique IDs" spam). The "already there" set is derived from
    the platform's currently-loaded entities (``platform.entities``) on each pass:

    * NOT from the entity registry — it persists across restarts, so on startup
      every entity would look "already there" and none would be (re)created,
      leaving them all stuck "unavailable".
    * NOT from a platform-lifetime set — an entity removed/disabled at runtime
      would then never be recreated until a reload.

    Deriving it from the live platform entities handles all three: startup
    (recreate everything), steady state (no re-add), and runtime removal (the
    object is recreated once it reappears). An asyncio lock serializes overlapping
    refreshes so an in-flight add is never duplicated.
    """
    platform = ep.async_get_current_platform()
    services = getattr(platform.platform, "SENSOR_SERVICES", [])
    descriptions = getattr(platform.platform, "SENSOR_TYPES", [])

    for service in services:
        platform.async_register_entity_service(
            service.name, service.schema, service.action
        )

    add_lock = Lock()

    # The coordinator for this config entry. __init__ stores it as runtime_data
    # before the platforms are forwarded, so it is always present; guard
    # explicitly so a future change to that contract fails loudly (logged)
    # instead of with an AttributeError deep inside platform setup.
    this_coordinator = get_truenas_coordinator(config_entry)
    if this_coordinator is None:
        _LOGGER.error(
            "No TrueNAS coordinator found for entry %s; skipping entity setup",
            config_entry.entry_id,
        )
        return

    async def async_update_controller(coordinator: TrueNASCoordinator) -> None:
        """Add entities for newly-appeared objects on each coordinator refresh."""

        # SIGNAL_UPDATE_SENSORS is a global dispatcher signal that __init__ always
        # fires with the *same* coordinator instance object (one per config entry),
        # so the identity check below is safe. With more than one TrueNAS config
        # entry every platform receives every entry's refresh, so ignore refreshes
        # from other entries — otherwise this platform would build the *other*
        # instance's entities and try to add them here, causing "Platform truenas
        # does not generate unique IDs … already exists" spam (#33).
        if coordinator is not this_coordinator:
            return

        _cleanup_orphaned_entities(hass, config_entry, coordinator)

        async with add_lock:
            loaded = {
                entity.unique_id
                for entity in platform.entities.values()
                if entity.unique_id is not None
            }
            new_entities = _collect_new_entities(
                coordinator, descriptions, dispatcher, loaded
            )
            if new_entities:
                _LOGGER.debug("Adding %d new TrueNAS entities", len(new_entities))
                await platform.async_add_entities(new_entities)

    await async_update_controller(this_coordinator)

    unsub = async_dispatcher_connect(
        hass, SIGNAL_UPDATE_SENSORS, async_update_controller
    )
    config_entry.async_on_unload(unsub)


# ---------------------------
#   TrueNASEntity
# ---------------------------
class TrueNASEntity(CoordinatorEntity[TrueNASCoordinator], Entity):
    """Define entity."""

    entity_description: TrueNASEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TrueNASCoordinator,
        entity_description: TrueNASEntityDescription,
        uid: str | None = None,
    ) -> None:
        """Initialize entity."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._inst = coordinator.config_entry.data[CONF_NAME]
        self._identity = resolve_entry_identity(coordinator.config_entry)
        self._config_entry = self.coordinator.config_entry
        self._attr_extra_state_attributes = {ATTR_ATTRIBUTION: ATTRIBUTION}
        self._uid = uid
        self._refresh_data()

    def _refresh_data(self) -> None:
        """Refresh cached data from the coordinator for this entity."""
        data = self.coordinator.data.get(self.entity_description.data_path or "", {})
        self._data: dict[str, Any] = data.get(self._uid, {}) if self._uid else data
        if self._uid and not self._data:
            _LOGGER.debug(
                "Data for UID %s is missing or empty in %s",
                self._uid,
                self.entity_description.data_path,
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._refresh_data()
        super()._handle_coordinator_update()

    def _core_name_translation_key(self) -> str | None:
        """Return Entity._name_translation_key, degrading gracefully.

        Isolates the one place this entity touches a private HA-core
        implementation detail (the same cached_property HA core's own
        Entity._name_internal uses to build its lookup key), so a future
        core change that renames or removes it only needs a fix here.
        """
        return getattr(self, "_name_translation_key", None)

    def _translated_description_name(self) -> str | None:
        """Resolve the description's name, preferring loaded translations.

        This entity builds its own name (below) instead of relying on HA's
        has_entity_name machinery, so the platform-translations lookup has
        to be triggered manually. Most descriptions only set translation_key
        and leave `name` at its EntityDescription default (UNDEFINED, not a
        str), so the translation lookup must run regardless of `name` --
        `desc_name` is only a fallback for the few statically-named/unnamed
        descriptions that set `name` explicitly.
        """
        platform_translations: dict[str, str] | None = getattr(
            self.platform_data, "platform_translations", None
        )
        if platform_translations:
            name_translation_key = self._core_name_translation_key()
            translated = (
                platform_translations.get(name_translation_key)
                if name_translation_key
                else None
            )
            if translated:
                return translated

        desc_name = self.entity_description.name
        return desc_name if isinstance(desc_name, str) else None

    @property
    def name(self) -> str | None:
        """Return the name for this entity."""
        desc_name = self._translated_description_name()

        if not self._uid:
            return desc_name

        data_value = None
        if self._data is not None and self.entity_description.data_name:
            data_value = self._data.get(self.entity_description.data_name)

        if data_value is None:
            data_value = str(self._uid)

        return f"{data_value} {desc_name}" if desc_name else f"{data_value}"

    @property
    def unique_id(self) -> str:
        """Return a unique id for this entity."""
        if self._uid:
            data_ref = self.entity_description.data_reference
            value = self._data.get(data_ref) if self._data and data_ref else None
            reference = value if value is not None else self._uid
            return format_unique_id(
                self._identity, self.entity_description.key, reference
            )

        return format_unique_id(self._identity, self.entity_description.key)

    @property
    def device_info(self) -> DeviceInfo:
        """Return a description for device registry."""
        ha_group = self.entity_description.ha_group or ""
        dev_connection = DOMAIN
        dev_connection_value = f"{self._identity}_{ha_group}"
        dev_group = ha_group
        if ha_group == "System":
            dev_connection_value = format_device_identifier(self._identity)

        if ha_group.startswith("data__"):
            dev_group = ha_group[6:]
            if dev_group in self._data:
                dev_group = self._data[dev_group]
                dev_connection_value = f"{self._identity}_{dev_group}"

        if self.entity_description.ha_connection:
            dev_connection = self.entity_description.ha_connection

        if self.entity_description.ha_connection_value:
            dev_connection_value = self.entity_description.ha_connection_value
            if dev_connection_value.startswith("data__"):
                data_key = dev_connection_value[6:]
                connection_val = self._data.get(data_key, "unknown")
                dev_connection_value = f"{self._identity}_{connection_val}"

        if ha_group == "System":
            http_scheme = "https" if self.coordinator.api.scheme == "wss" else "http"
            return DeviceInfo(
                identifiers={(dev_connection, f"{dev_connection_value}")},
                name=self._inst,
                model=f"{self.coordinator.data['system_info']['system_product']}",
                manufacturer=f"{self.coordinator.data['system_info']['system_manufacturer']}",
                sw_version=f"{self.coordinator.data['system_info']['version']}",
                configuration_url=f"{http_scheme}://{self.coordinator.config_entry.data[CONF_HOST]}",
            )

        # A plain dict, not DeviceInfo, so the conditional via_device/via_device_id
        # key below doesn't depend on whichever DeviceInfo TypedDict shape mypy
        # happens to resolve (via_device_id was only added to it upstream in HA
        # Core 2026.8 -- see _supports_via_device_id()).
        # HA's device-registry validation only accepts specific key
        # combinations (see DEVICE_INFO_TYPES in device_registry.py): the
        # default_name/default_model/default_manufacturer trio is only valid
        # together with "connections", not "identifiers" -- so pairing
        # identifiers with a stable identity here requires the plain
        # name/model/manufacturer keys instead.
        system_info = self.coordinator.data["system_info"]
        device_info: dict[str, Any] = {
            "identifiers": {(dev_connection, f"{dev_connection_value}")},
            "name": f"{self._inst} {dev_group}",
            "model": f"{system_info['system_product']}",
            "manufacturer": f"{system_info['system_manufacturer']}",
        }
        system_device_id = self.coordinator.system_device_id
        if _supports_via_device_id() and system_device_id is not None:
            device_info["via_device_id"] = system_device_id
        else:
            device_info["via_device"] = (
                DOMAIN,
                format_device_identifier(self._identity),
            )
        return cast(DeviceInfo, device_info)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return the state attributes."""
        attributes = dict(super().extra_state_attributes or {})
        for variable in self.entity_description.data_attributes_list:
            if variable in self._data:
                attributes[format_attribute(variable)] = self._data[variable]

        return attributes

    def _raise_unsupported(self, action: str) -> None:
        """Raise a clean, user-facing error for an unsupported action.

        Entity services are registered for a whole platform, so an action can be
        targeted at an entity type that does not implement it (e.g. service_restart
        on an app). Raising ServiceValidationError surfaces a clear message instead
        of an "Unknown error" from a bare NotImplementedError.
        """
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unsupported_action",
            translation_placeholders={
                "action": action,
                "entity_id": self.entity_id,
            },
        )

    def _raise_if_api_error(self, action: str) -> None:
        """Raise HomeAssistantError if the most recent api.query() call failed.

        query() swallows connection/middleware errors and returns None instead
        of raising, recording the failure in api.error; many middleware
        endpoints also legitimately return null on success, so only a
        non-empty api.error (reset at the start of every query) marks an
        actual failure.
        """
        if self.coordinator.api.error:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="action_failed",
                translation_placeholders={
                    "action": action,
                    "host": self.coordinator.host,
                    "error": str(self.coordinator.api.error),
                },
            )

    async def start(self) -> None:
        """Run function."""
        self._raise_unsupported("start")

    async def stop(self) -> None:
        """Stop function."""
        self._raise_unsupported("stop")

    async def restart(self) -> None:
        """Restart function."""
        self._raise_unsupported("restart")

    async def reload(self) -> None:
        """Reload function."""
        self._raise_unsupported("reload")

    async def snapshot(self) -> None:
        """Snapshot function."""
        self._raise_unsupported("snapshot")

    async def lock(self, force_umount: bool = False) -> None:
        """Lock function."""
        self._raise_unsupported("lock")

    async def unlock(
        self,
        passphrase: str | None = None,
        recursive: bool = False,
        force: bool = False,
    ) -> None:
        """Unlock function."""
        self._raise_unsupported("unlock")

    async def passphrase_set(self, passphrase: str) -> None:
        """Store passphrase function."""
        self._raise_unsupported("passphrase_set")

    async def refresh(self) -> None:
        """Refresh function."""
        self._raise_unsupported("refresh")
