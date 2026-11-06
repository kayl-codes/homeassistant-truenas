"""Diagnostics support for TrueNAS."""

from __future__ import annotations

from dataclasses import asdict, fields
from typing import Any

from aiotruenas import SystemStats
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .api import scrub_exception, scrub_strings
from .const import TO_REDACT
from .coordinator import TrueNASConfigEntry, TrueNASData, get_truenas_coordinator


def _system_stats(stats: SystemStats) -> dict[str, Any]:
    """Return ``stats`` as plain data, a failed graph's error as scrubbed text.

    Built field by field: ``asdict`` cannot copy the read-only mappings
    aiotruenas uses for ``interfaces`` and ``failed_graphs``.
    """
    result: dict[str, Any] = {
        item.name: getattr(stats, item.name)
        for item in fields(stats)
        if item.name not in ("interfaces", "failed_graphs")
    }
    result["interfaces"] = {
        interface_id: asdict(traffic)
        for interface_id, traffic in stats.interfaces.items()
    }
    result["failed_graphs"] = {
        graph: str(scrub_exception(err)) for graph, err in stats.failed_graphs.items()
    }
    return result


def _system_data(data: TrueNASData) -> dict[str, Any]:
    """Return the typed system snapshot as plain data."""
    return {
        "info": asdict(data.info) if data.info else None,
        "interfaces": {
            interface_id: asdict(interface)
            for interface_id, interface in data.interfaces.items()
        },
        "stats": _system_stats(data.stats) if data.stats else None,
    }


async def async_get_config_entry_diagnostics(
    _hass: HomeAssistant, config_entry: TrueNASConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = get_truenas_coordinator(config_entry)
    return {
        "entry": {
            "data": async_redact_data(config_entry.data, TO_REDACT),
            "options": async_redact_data(config_entry.options, TO_REDACT),
        },
        "data": scrub_strings(async_redact_data(coordinator.ds, TO_REDACT))
        if coordinator
        else {},
        "system_data": scrub_strings(
            async_redact_data(_system_data(coordinator.system_data), TO_REDACT)
        )
        if coordinator
        else {},
    }
