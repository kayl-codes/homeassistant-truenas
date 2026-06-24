"""The TrueNAS integration."""

from __future__ import annotations

from logging import getLogger

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .binary_sensor_types import SENSOR_TYPES as BINARY_SENSOR_TYPES
from .const import (
    BEHAVIOR_REMOVE_INACTIVE_NIC,
    CONF_BEHAVIORS,
    CONF_DATA_UNIT,
    CONF_MONITORED_GROUPS,
    DEFAULT_BEHAVIORS,
    DEFAULT_DATA_UNIT,
    DEFAULT_MONITORED_GROUPS,
    DOMAIN,
    GROUP_DATA_PATHS,
    PLATFORMS,
    SCHEMA_SERVICE_ALERT_DISMISS,
    SCHEMA_SERVICE_ALERT_LIST,
    SCHEMA_SERVICE_ALERT_RESTORE,
    SERVICE_ALERT_CONFIG_ENTRY,
    SERVICE_ALERT_DISMISS,
    SERVICE_ALERT_LIST,
    SERVICE_ALERT_PROPERTIES,
    SERVICE_ALERT_RESTORE,
    SERVICE_ALERT_UUID,
    SIGNAL_UPDATE_SENSORS,
)
from .coordinator import TrueNASCoordinator
from .entity import _is_uid_excluded, format_unique_id
from .helper import scaled_data_unit
from .migration import (
    async_adopt_legacy_entities,
    async_notify_migration_result,
    finalize_legacy_adoption,
)
from .sensor_types import SENSOR_TYPES
from .switch_types import SENSOR_TYPES as SWITCH_SENSOR_TYPES
from .update_types import SENSOR_TYPES as UPDATE_SENSOR_TYPES

_LOGGER = getLogger(__name__)

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


def _migrate_description(ent_reg, coordinator, inst, description, binary) -> None:
    """Force units for all entities produced by a single DATA_SIZE description."""
    data = coordinator.ds.get(description.data_path)
    if not isinstance(data, dict):
        return

    if not description.data_reference:
        value = data.get(description.data_attribute)
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


def _force_entity_unit(ent_reg, inst, description, reference, value, binary) -> None:
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
    inst: str, description, data: dict, honor_exclude: bool = True
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

        data = coordinator.data.get(description.data_path)

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
def _get_coordinator(
    hass: HomeAssistant, entry_id: str | None
) -> tuple[str | None, dict]:
    """Resolve coordinator; return (entry_id, error_dict) or (entry_id, {})."""
    coords = hass.data.get(DOMAIN, {})
    if not coords:
        return None, {"error": "No TrueNAS instances configured"}

    if not entry_id:
        entries = list(coords.keys())
        if len(entries) != 1:
            return (
                None,
                {
                    "error": (
                        f"Multiple TrueNAS instances ({len(entries)}); "
                        "please specify config_entry"
                    )
                },
            )
        entry_id = entries[0]

    if entry_id not in coords:
        return None, {"error": f"TrueNAS instance {entry_id} not found"}

    return entry_id, {}


async def _alert_action(
    hass: HomeAssistant, coordinator: TrueNASCoordinator, uuid: str, action: str
) -> None:
    """Execute alert dismiss/restore action (shared helper)."""
    await hass.async_add_executor_job(coordinator.api.query, f"alert.{action}", [uuid])
    await coordinator.async_refresh()


async def _handle_alert_list(hass: HomeAssistant, call) -> dict:
    """List all TrueNAS alerts with selectable properties."""
    entry_id, error = _get_coordinator(hass, call.data.get(SERVICE_ALERT_CONFIG_ENTRY))
    if error:
        return {"alerts": [], **error}

    coordinator = hass.data[DOMAIN][entry_id]
    alerts = await hass.async_add_executor_job(coordinator.api.query, "alert.list")
    if not isinstance(alerts, list):
        return {"alerts": []}

    # Filter properties if specified
    props = call.data.get(SERVICE_ALERT_PROPERTIES, "uuid,formatted")
    if props == "*":
        return {"alerts": alerts}

    prop_list = [p.strip() for p in props.split(",")]
    filtered = [{k: a[k] for k in prop_list if k in a} for a in alerts]
    return {"alerts": filtered}


async def _handle_alert_dismiss(hass: HomeAssistant, call) -> None:
    """Dismiss a TrueNAS alert by UUID."""
    entry_id, error = _get_coordinator(hass, call.data.get(SERVICE_ALERT_CONFIG_ENTRY))
    if error:
        raise ServiceValidationError(error.get("error", "Unknown error"))

    coordinator = hass.data[DOMAIN][entry_id]
    uuid = call.data.get(SERVICE_ALERT_UUID)
    await _alert_action(hass, coordinator, uuid, "dismiss")


async def _handle_alert_restore(hass: HomeAssistant, call) -> None:
    """Restore a TrueNAS alert by UUID."""
    entry_id, error = _get_coordinator(hass, call.data.get(SERVICE_ALERT_CONFIG_ENTRY))
    if error:
        raise ServiceValidationError(error.get("error", "Unknown error"))

    coordinator = hass.data[DOMAIN][entry_id]
    uuid = call.data.get(SERVICE_ALERT_UUID)
    await _alert_action(hass, coordinator, uuid, "restore")


# ---------------------------
#   async_setup_entry
# ---------------------------
async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up TrueNAS config entry."""
    coordinator = TrueNASCoordinator(hass, config_entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = coordinator

    # Community-Edition rename: free the legacy "truenas" entity_ids before the
    # platforms create the new entities (no-op until the domain is renamed).
    adopted = await async_adopt_legacy_entities(hass, config_entry)

    _migrate_data_size_units(hass, config_entry, coordinator)
    _cleanup_orphaned_entities(hass, config_entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    # Register alert services (domain-level, instance-specific) once.
    services = hass.services.async_services().get(DOMAIN, {})
    if SERVICE_ALERT_DISMISS not in services:

        async def _alert_dismiss_handler(call) -> None:
            await _handle_alert_dismiss(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_ALERT_DISMISS,
            _alert_dismiss_handler,
            schema=SCHEMA_SERVICE_ALERT_DISMISS,
        )
    if SERVICE_ALERT_RESTORE not in services:

        async def _alert_restore_handler(call) -> None:
            await _handle_alert_restore(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_ALERT_RESTORE,
            _alert_restore_handler,
            schema=SCHEMA_SERVICE_ALERT_RESTORE,
        )
    if SERVICE_ALERT_LIST not in services:

        async def _alert_list_handler(call) -> dict:
            return await _handle_alert_list(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_ALERT_LIST,
            _alert_list_handler,
            schema=SCHEMA_SERVICE_ALERT_LIST,
            supports_response=True,
        )

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

    # Reload the entry when the user saves new options so the coordinator
    # picks up the changed poll interval / group toggles immediately.
    config_entry.async_on_unload(
        config_entry.add_update_listener(_async_options_updated)
    )
    return True


async def _async_options_updated(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """Reload the integration when options change."""
    await hass.config_entries.async_reload(config_entry.entry_id)


# ---------------------------
#   async_unload_entry
# ---------------------------
async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Unload TrueNAS config entry."""

    if unload_ok := await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    ):
        coordinator = hass.data[DOMAIN].pop(config_entry.entry_id)
        await hass.async_add_executor_job(coordinator.api.close)

    return unload_ok
