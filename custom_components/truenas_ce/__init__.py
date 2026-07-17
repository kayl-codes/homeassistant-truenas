"""The TrueNAS integration."""

from __future__ import annotations

from logging import getLogger
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_NAME
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .binary_sensor_types import SENSOR_TYPES as BINARY_SENSOR_TYPES
from .const import (
    BEHAVIOR_REMOVE_INACTIVE_NIC,
    CONF_BEHAVIORS,
    CONF_DATA_UNIT,
    CONF_DATASET_PASSPHRASES,
    CONF_MONITORED_GROUPS,
    DEFAULT_ALERT_PROPERTIES,
    DEFAULT_BEHAVIORS,
    DEFAULT_DATA_UNIT,
    DEFAULT_MONITORED_GROUPS,
    DOMAIN,
    GROUP_DATA_PATHS,
    PLATFORMS,
    SCHEMA_SERVICE_ALERT_DISMISS,
    SCHEMA_SERVICE_ALERT_LIST,
    SCHEMA_SERVICE_ALERT_RESTORE,
    SCHEMA_SERVICE_PASSPHRASE_LIST,
    SCHEMA_SERVICE_PASSPHRASE_REMOVE,
    SERVICE_ALERT_CONFIG_ENTRY,
    SERVICE_ALERT_DISMISS,
    SERVICE_ALERT_LIST,
    SERVICE_ALERT_PROPERTIES,
    SERVICE_ALERT_RESTORE,
    SERVICE_ALERT_UUID,
    SERVICE_CONFIG_ENTRY,
    SERVICE_PASSPHRASE_DATASET_PATH,
    SERVICE_PASSPHRASE_LIST,
    SERVICE_PASSPHRASE_REMOVE,
    SIGNAL_UPDATE_SENSORS,
)
from .coordinator import TrueNASConfigEntry, TrueNASCoordinator
from .entity import TrueNASEntityDescription, _is_uid_excluded, format_unique_id
from .helper import alert_action, scaled_data_unit
from .migration import (
    async_adopt_legacy_entities,
    async_notify_migration_result,
    finalize_legacy_adoption,
)
from .sensor_types import SENSOR_TYPES, TrueNASSensorEntityDescription
from .switch_types import SENSOR_TYPES as SWITCH_SENSOR_TYPES
from .update_types import SENSOR_TYPES as UPDATE_SENSOR_TYPES

_LOGGER = getLogger(__name__)
_UNKNOWN_ERROR = "Unknown error"

# All entity descriptions across platforms, used to compute the set of unique_ids
# that legitimately exist for the current TrueNAS objects (orphan cleanup).
_ALL_DESCRIPTIONS = (
    *SENSOR_TYPES,
    *BINARY_SENSOR_TYPES,
    *SWITCH_SENSOR_TYPES,
    *UPDATE_SENSOR_TYPES,
)


# ---------------------------
#   _migrate_data_size_units
# ---------------------------
def _migrate_data_size_units(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    coordinator: TrueNASCoordinator,
) -> None:
    """Force each DATA_SIZE sensor's display unit from the base and magnitude.

    The unit is derived from the configured base (GB/GiB) and the entity's
    current value, then written directly to the entity registry on every startup
    so the GB/GiB preference takes effect and the unit tracks the value (e.g. a
    pool is shown in TiB once it exceeds 1 TiB).
    """
    data_unit = config_entry.options.get(
        CONF_DATA_UNIT, config_entry.data.get(CONF_DATA_UNIT, DEFAULT_DATA_UNIT)
    )
    binary = data_unit == "GiB"
    inst = config_entry.data[CONF_NAME]
    ent_reg = er.async_get(hass)

    for description in SENSOR_TYPES:
        if getattr(description, "device_class", None) == SensorDeviceClass.DATA_SIZE:
            _migrate_description(ent_reg, coordinator, inst, description, binary)


def _migrate_description(
    ent_reg: er.EntityRegistry,
    coordinator: TrueNASCoordinator,
    inst: str,
    description: TrueNASSensorEntityDescription,
    binary: bool,
) -> None:
    """Force units for all entities produced by a single DATA_SIZE description."""
    data = coordinator.ds.get(description.data_path or "")
    if not isinstance(data, dict):
        return

    if not description.data_reference:
        value = data.get(description.data_attribute or "")
        _force_entity_unit(ent_reg, inst, description, None, value, binary)
        return

    for uid, vals in data.items():
        if not isinstance(vals, dict):
            continue
        ref = vals.get(description.data_reference)
        _force_entity_unit(
            ent_reg,
            inst,
            description,
            ref if ref is not None else uid,
            vals.get(description.data_attribute),
            binary,
        )


def _force_entity_unit(
    ent_reg: er.EntityRegistry,
    inst: str,
    description: TrueNASSensorEntityDescription,
    reference: Any,
    value: Any,
    binary: bool,
) -> None:
    """Write the magnitude-appropriate display unit of one entity to the registry."""
    entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, format_unique_id(inst, description.key, reference)
    )
    if entity_id is None:
        return

    unit, _ = scaled_data_unit(value, binary)
    entry = ent_reg.async_get(entity_id)
    options = dict(entry.options.get("sensor", {})) if entry else {}
    if options.get("unit_of_measurement") != unit:
        options["unit_of_measurement"] = unit
        ent_reg.async_update_entity_options(entity_id, "sensor", options)


# ---------------------------
#   _collect_active_unique_ids / _cleanup_orphaned_entities
# ---------------------------
def _handle_keyless(
    base: str, is_disabled: bool, active: set[str], live_bases: set[str]
) -> None:
    """Route a keyless entity base to the correct set for the cleanup decision."""
    if is_disabled:
        live_bases.add(base)
    else:
        active.add(base)


def _referenced_unique_ids(
    inst: str,
    description: TrueNASEntityDescription,
    data: dict[str, Any],
    honor_exclude: bool = True,
) -> set[str]:
    """Unique_ids the integration would create for one referenced description.

    Mirrors entity creation; the ``data_exclude`` filter (e.g. traffic sensors
    of a down interface) is only applied when ``honor_exclude`` is True.
    """
    ids: set[str] = set()
    for uid, vals in data.items():
        if honor_exclude and _is_uid_excluded(description, vals):
            continue
        ref = vals.get(description.data_reference)
        reference = ref if ref is not None else uid
        ids.add(format_unique_id(inst, description.key, reference))

    return ids


def _collect_active_unique_ids(
    inst: str, coordinator: TrueNASCoordinator
) -> tuple[set[str], set[str]]:
    """Return (active unique_ids, live bases) for the current TrueNAS objects.

    ``active`` is every unique_id the integration would create right now (see
    ``_referenced_unique_ids``). The ``data_exclude`` filter (e.g. traffic
    sensors of down interfaces) is only honoured when the
    BEHAVIOR_REMOVE_INACTIVE_NIC option is active; otherwise excluded entities
    are kept. ``live bases`` are the per-description id prefixes whose data
    domain currently holds data, so cleanup never wipes a whole group on a
    transient empty fetch.
    """
    behaviors = coordinator.config_entry.options.get(CONF_BEHAVIORS, DEFAULT_BEHAVIORS)
    honor_exclude = BEHAVIOR_REMOVE_INACTIVE_NIC in behaviors

    monitored = coordinator.config_entry.options.get(
        CONF_MONITORED_GROUPS, DEFAULT_MONITORED_GROUPS
    )
    disabled_data_paths: set[str] = set()
    for group, paths in GROUP_DATA_PATHS.items():
        if group not in monitored:
            disabled_data_paths.update(paths)

    active: set[str] = set()
    live_bases: set[str] = set()

    for description in _ALL_DESCRIPTIONS:
        base = format_unique_id(inst, description.key)
        is_disabled_group = description.data_path in disabled_data_paths

        if not getattr(description, "data_reference", None):
            _handle_keyless(base, is_disabled_group, active, live_bases)
            continue

        data = coordinator.data.get(description.data_path or "")

        if not data and not is_disabled_group:
            # Transient empty fetch for an enabled group → protect entities.
            continue

        # Mark base as live so the cleanup loop considers it.
        live_bases.add(base)
        if data and not is_disabled_group:
            # Normal enabled group: build the active set (respecting NIC exclusion).
            active |= _referenced_unique_ids(inst, description, data, honor_exclude)
        # Disabled group: base in live_bases, nothing in active → entities removed.

    return active, live_bases


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
    if not coordinator.last_update_success:
        return

    inst = config_entry.data[CONF_NAME]
    active, live_bases = _collect_active_unique_ids(inst, coordinator)

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


# ---------------------------
#   Alert Service Handlers
# ---------------------------
def _resolve_config_entry(
    hass: HomeAssistant, entry_id: str | None
) -> tuple[TrueNASConfigEntry | None, dict[str, str]]:
    """Resolve the target config entry; return (entry, {}) or (None, error_dict).

    Services are registered in ``async_setup`` (before any entry exists), so the
    target entry is validated here at call time: it must exist and be loaded.
    """
    loaded: list[TrueNASConfigEntry] = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if not loaded:
        return None, {"error": "No TrueNAS instances configured"}

    if not entry_id:
        if len(loaded) != 1:
            return (
                None,
                {
                    "error": (
                        f"Multiple TrueNAS instances ({len(loaded)}); "
                        "please specify config_entry"
                    )
                },
            )
        return loaded[0], {}

    for entry in loaded:
        if entry.entry_id == entry_id:
            return entry, {}

    return None, {"error": f"TrueNAS instance {entry_id} not found"}


async def _handle_alert_list(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    """List all TrueNAS alerts with selectable properties."""
    entry, error = _resolve_config_entry(
        hass, call.data.get(SERVICE_ALERT_CONFIG_ENTRY)
    )
    if error or entry is None:
        return {"alerts": [], **error}

    alerts = await entry.runtime_data.api.query("alert.list")
    if not isinstance(alerts, list):
        return {"alerts": [], "error": "Unexpected alert.list response"}

    # Filter properties if specified
    props = call.data.get(SERVICE_ALERT_PROPERTIES, DEFAULT_ALERT_PROPERTIES)
    if props == "*":
        return {"alerts": alerts}

    prop_list = [p.strip() for p in props.split(",")]
    filtered = [{k: a[k] for k in prop_list if k in a} for a in alerts]
    return {"alerts": filtered}


async def _handle_alert_dismiss(hass: HomeAssistant, call: ServiceCall) -> None:
    """Dismiss a TrueNAS alert by UUID."""
    entry, error = _resolve_config_entry(
        hass, call.data.get(SERVICE_ALERT_CONFIG_ENTRY)
    )
    if error or entry is None:
        raise ServiceValidationError(error.get("error", _UNKNOWN_ERROR))

    uuid = call.data.get(SERVICE_ALERT_UUID)
    if not uuid:
        raise ServiceValidationError("Alert UUID is required for dismiss action")

    await alert_action(entry.runtime_data, uuid, "dismiss")


async def _handle_alert_restore(hass: HomeAssistant, call: ServiceCall) -> None:
    """Restore a TrueNAS alert by UUID."""
    entry, error = _resolve_config_entry(
        hass, call.data.get(SERVICE_ALERT_CONFIG_ENTRY)
    )
    if error or entry is None:
        raise ServiceValidationError(error.get("error", _UNKNOWN_ERROR))

    uuid = call.data.get(SERVICE_ALERT_UUID)
    if not uuid:
        raise ServiceValidationError("Alert UUID is required for restore action")

    await alert_action(entry.runtime_data, uuid, "restore")


async def _handle_passphrase_remove(hass: HomeAssistant, call: ServiceCall) -> None:
    """Remove a stored dataset passphrase by dataset path."""
    entry, error = _resolve_config_entry(hass, call.data.get(SERVICE_CONFIG_ENTRY))
    if error or entry is None:
        raise ServiceValidationError(error.get("error", _UNKNOWN_ERROR))

    dataset_path = call.data.get(SERVICE_PASSPHRASE_DATASET_PATH, "").strip()
    if not dataset_path:
        raise ServiceValidationError("dataset_path is required")

    existing = entry.data.get(CONF_DATASET_PASSPHRASES, {})
    if not isinstance(existing, dict) or dataset_path not in existing:
        raise ServiceValidationError(
            f"No stored passphrase found for dataset '{dataset_path}'"
        )
    updated = {k: v for k, v in existing.items() if k != dataset_path}
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_DATASET_PASSPHRASES: updated},
    )


async def _handle_passphrase_list(
    hass: HomeAssistant, call: ServiceCall
) -> dict[str, Any]:
    """Return the dataset names that have a stored passphrase (no values)."""
    entry, error = _resolve_config_entry(hass, call.data.get(SERVICE_CONFIG_ENTRY))
    if error or entry is None:
        return {"datasets": [], **error}

    stored = entry.data.get(CONF_DATASET_PASSPHRASES, {})
    datasets = sorted(stored.keys()) if isinstance(stored, dict) else []
    return {"datasets": datasets}


# ---------------------------
#   async_setup
# ---------------------------
async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the domain-level services.

    Registration happens at integration setup (not per config entry) so the
    services exist independently of any entry; the handlers resolve and
    validate the target entry at call time (see ``_resolve_config_entry``).
    """

    async def _alert_dismiss_handler(call: ServiceCall) -> None:
        await _handle_alert_dismiss(hass, call)

    async def _alert_restore_handler(call: ServiceCall) -> None:
        await _handle_alert_restore(hass, call)

    async def _alert_list_handler(call: ServiceCall) -> ServiceResponse:
        return await _handle_alert_list(hass, call)

    async def _passphrase_remove_handler(call: ServiceCall) -> None:
        await _handle_passphrase_remove(hass, call)

    async def _passphrase_list_handler(call: ServiceCall) -> ServiceResponse:
        return await _handle_passphrase_list(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_ALERT_DISMISS,
        _alert_dismiss_handler,
        schema=SCHEMA_SERVICE_ALERT_DISMISS,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ALERT_RESTORE,
        _alert_restore_handler,
        schema=SCHEMA_SERVICE_ALERT_RESTORE,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ALERT_LIST,
        _alert_list_handler,
        schema=SCHEMA_SERVICE_ALERT_LIST,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PASSPHRASE_REMOVE,
        _passphrase_remove_handler,
        schema=SCHEMA_SERVICE_PASSPHRASE_REMOVE,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PASSPHRASE_LIST,
        _passphrase_list_handler,
        schema=SCHEMA_SERVICE_PASSPHRASE_LIST,
        supports_response=SupportsResponse.OPTIONAL,
    )

    return True


# ---------------------------
#   async_setup_entry
# ---------------------------
async def async_setup_entry(
    hass: HomeAssistant, config_entry: TrueNASConfigEntry
) -> bool:
    """Set up TrueNAS config entry."""
    coordinator = TrueNASCoordinator(hass, config_entry)
    await coordinator.async_config_entry_first_refresh()
    config_entry.runtime_data = coordinator

    # Community-Edition rename: free the legacy "truenas" entity_ids before the
    # platforms create the new entities (no-op until the domain is renamed).
    adopted = await async_adopt_legacy_entities(hass, config_entry)

    _migrate_data_size_units(hass, config_entry, coordinator)
    _cleanup_orphaned_entities(hass, config_entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    # Re-attach the freed legacy entity_ids now that the new entities exist, then
    # report the outcome once (validation checks + guide link + rollback hint).
    finalize_legacy_adoption(hass, adopted)
    async_notify_migration_result(hass, config_entry, adopted)

    # Re-run entity discovery on every coordinator refresh so entities for newly
    # appearing objects (e.g. a network interface coming up, a new pool/dataset)
    # are created without requiring an integration reload. The discovery handler
    # (entity.async_add_entities) expects the coordinator as its argument and does
    # not request another refresh, so this does not create a refresh loop.
    @callback
    def _handle_coordinator_refresh() -> None:
        async_dispatcher_send(hass, SIGNAL_UPDATE_SENSORS, coordinator)

    config_entry.async_on_unload(
        coordinator.async_add_listener(_handle_coordinator_refresh)
    )

    return True


# ---------------------------
#   async_unload_entry
# ---------------------------
async def async_unload_entry(
    hass: HomeAssistant, config_entry: TrueNASConfigEntry
) -> bool:
    """Unload TrueNAS config entry."""

    if unload_ok := await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    ):
        await config_entry.runtime_data.api.close()

    return unload_ok
