"""Unit tests for diagnostics.py."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from _fakes import make_coordinator
from aiotruenas import InterfaceTraffic, NetworkInterface, SystemInfo, SystemStats
from aiotruenas.exceptions import TrueNASCallError
from homeassistant.helpers.json import json_bytes

from custom_components.truenas_ce.coordinator import TrueNASData
from custom_components.truenas_ce.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics_redacts_sensitive_fields_and_includes_data() -> None:
    coordinator = make_coordinator(data={"system_info": {"version": "25.10.4"}})
    coordinator.config_entry.data["password"] = "s3cr3t"
    coordinator.config_entry.options = {"host": "truenas.local"}
    entry = SimpleNamespace(
        data=coordinator.config_entry.data,
        options=coordinator.config_entry.options,
        runtime_data=coordinator,
    )

    result = await async_get_config_entry_diagnostics(SimpleNamespace(), entry)

    assert result["entry"]["data"]["password"] == "**REDACTED**"
    assert result["entry"]["options"]["host"] == "**REDACTED**"
    assert result["data"]["system_info"]["version"] == "25.10.4"


async def test_diagnostics_scrubs_secrets_in_free_text_values() -> None:
    coordinator = make_coordinator(
        data={
            "cloudsync": {
                "1": {
                    "description": "offsite",
                    "job_error": "rclone failed: secret_access_key=SAK-SAMPLE",
                }
            }
        }
    )
    entry = SimpleNamespace(data={}, options={}, runtime_data=coordinator)

    result = await async_get_config_entry_diagnostics(SimpleNamespace(), entry)

    task = result["data"]["cloudsync"]["1"]
    assert task["description"] == "offsite"
    assert task["job_error"] == "rclone failed: secret_access_key=**REDACTED**"


async def test_diagnostics_no_coordinator_returns_empty_data() -> None:
    entry = SimpleNamespace(data={}, options={}, runtime_data=None)

    result = await async_get_config_entry_diagnostics(SimpleNamespace(), entry)

    assert not result["data"]


async def test_diagnostics_includes_the_typed_system_snapshot() -> None:
    """system_data is exported as plain, JSON-ready data: serials redacted,
    a failed graph's error as scrubbed text."""
    data = TrueNASData(
        info=SystemInfo(
            hostname="nas1",
            version="25.10.1",
            manufacturer=None,
            product=None,
            serial="S123",
            physical_memory=1024,
            boot_time=datetime(2026, 11, 1, 8, 30, tzinfo=UTC),
            uptime_seconds=100.0,
            load_1m=0.1,
            load_5m=0.2,
            load_15m=0.3,
            is_virtual=False,
        ),
        interfaces={
            "eth0": NetworkInterface(
                id="eth0",
                name="eth0",
                description=None,
                mtu=1500,
                link_state="LINK_STATE_UP",
                link_up=True,
                active_media_type=None,
                active_media_subtype=None,
                link_address=None,
            )
        },
        stats=SystemStats(
            cpu_usage_percent=None,
            cpu_temperature=None,
            memory_available=512,
            memory_usage_percent=50.0,
            arc_size=256,
            interfaces={"eth0": InterfaceTraffic(rx_kbps=1.0, tx_kbps=2.0)},
            failed_graphs={
                "cpu": TrueNASCallError(
                    "graph failed: password=hunter2", errname="EFAULT"
                )
            },
        ),
    )
    coordinator = make_coordinator(system_data=data)
    entry = SimpleNamespace(data={}, options={}, runtime_data=coordinator)

    result = await async_get_config_entry_diagnostics(SimpleNamespace(), entry)

    system_data = result["system_data"]
    assert system_data["info"]["serial"] == "**REDACTED**"
    assert system_data["info"]["hostname"] == "nas1"
    assert system_data["interfaces"]["eth0"]["mtu"] == 1500
    assert system_data["stats"]["interfaces"] == {
        "eth0": {"rx_kbps": 1.0, "tx_kbps": 2.0}
    }
    assert system_data["stats"]["failed_graphs"] == {
        "cpu": "graph failed: password=**REDACTED**"
    }
    json_bytes(result)


async def test_diagnostics_system_data_before_the_first_poll() -> None:
    entry = SimpleNamespace(data={}, options={}, runtime_data=make_coordinator())

    result = await async_get_config_entry_diagnostics(SimpleNamespace(), entry)

    assert result["system_data"] == {"info": None, "interfaces": {}, "stats": None}
