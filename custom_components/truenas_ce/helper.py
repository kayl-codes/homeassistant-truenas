"""Helper functions."""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from homeassistant.const import UnitOfInformation
from homeassistant.exceptions import HomeAssistantError
from homeassistant.loader import IntegrationNotFound, async_get_integration

from .api import SECRET_SCRUB_FILTER
from .const import (
    DOMAIN,
    LEGACY_DOMAIN,
    SCRUB_STATE_CANCELED,
    SCRUB_STATE_FINISHED,
    SCRUB_STATE_PAUSED,
    SCRUB_STATE_SCANNING,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import TrueNASCoordinator

_LOGGER = logging.getLogger(__name__)
_LOGGER.addFilter(SECRET_SCRUB_FILTER)

# Strip a leading URL scheme (e.g. "https://" or "http://") from the host.
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
# Everything from the first path/query/fragment delimiter is not part of host.
_HOST_TAIL_RE = re.compile(r"[/?#]")


# ---------------------------
#   async_legacy_code_installed
# ---------------------------
async def async_legacy_code_installed(hass: HomeAssistant) -> bool:
    """Whether the old ``truenas`` integration's files are still installed.

    Uninstalling it in HACS deletes only ``custom_components/truenas/``; the
    disabled legacy config entry stays behind, and Home Assistant then logs
    "Unable to get manifest for integration truenas" (#201). Without the files a
    rollback cannot work either: the re-enabled entry would never load.

    Lives here so both ``coordinator`` and ``migration`` can use it without an
    import cycle (``migration`` -> ``entity`` -> ``coordinator``).
    """
    try:
        integration = await async_get_integration(hass, LEGACY_DOMAIN)
    except IntegrationNotFound:
        _LOGGER.debug("The old '%s' integration is not installed", LEGACY_DOMAIN)
        return False
    # The loader caches what it resolved, so an uninstall in HACS still
    # resolves until the next restart: confirm the files are really there.
    manifest = integration.file_path / "manifest.json"
    if not await hass.async_add_executor_job(manifest.is_file):
        _LOGGER.debug(
            "The old '%s' integration was removed since startup", LEGACY_DOMAIN
        )
        return False
    return True


# ---------------------------
#   async_legacy_integration_resolvable
# ---------------------------
async def async_legacy_integration_resolvable(hass: HomeAssistant) -> bool:
    """Whether Home Assistant's loader still resolves the old integration.

    The loader caches custom integrations from startup, so one uninstalled in
    HACS since then still resolves until a restart. Removing its config entry
    before that fails inside Home Assistant: it imports the deleted module.
    """
    try:
        await async_get_integration(hass, LEGACY_DOMAIN)
    except IntegrationNotFound:
        return False
    return True


# ---------------------------
#   sanitize_host
# ---------------------------
def sanitize_host(host: str) -> str:
    """Normalize user input to the bare hostname/IP[:port] the API expects.

    Users frequently paste a full URL (for example
    ``https://nas.example.com/ui?tab=1``). The API layer requires a bare host,
    so strip any scheme as well as a trailing path, query or fragment here
    instead of erroring out, so the value just works. Hostnames (and DNS in
    general) are case-insensitive, so the result is also lowercased -- this
    keeps ``NAS.local`` and ``nas.local`` from being treated as two different
    hosts by the exact-string duplicate/rediscovery matching elsewhere.

    Lives here (rather than in ``config_flow``) so both ``config_flow`` and
    ``migration`` can depend on it without either importing the other.

    A bare IPv6 literal is bracketed (e.g. "::1" -> "[::1]") so it combines
    unambiguously with a scheme/port into a valid WebSocket URL.
    """
    host = host.strip()
    host = _SCHEME_RE.sub("", host)  # drop a leading scheme
    host = _HOST_TAIL_RE.split(host, maxsplit=1)[0]  # drop path/query/fragment
    host = host.strip().lower()
    return _bracket_ipv6(host)


def _bracket_ipv6(host: str) -> str:
    """Bracket ``host`` if it is a bare (unbracketed) IPv6 literal."""
    try:
        ipaddress.IPv6Address(host)
    except ValueError:
        return host
    return f"[{host}]"


# Data-size display tiers as (threshold_in_bytes, unit, precision). The first
# tier whose threshold the value reaches is used; a precision of ``None`` keeps
# the entity description's own precision. Index _BASE_TIER_INDEX (GiB/GB) is the
# base tier used for zero/unknown values. Binary tiers scale by 1024, decimal
# tiers by 1000.
_BINARY_TIERS = (
    (1024**5, UnitOfInformation.PEBIBYTES, 2),
    (1024**4, UnitOfInformation.TEBIBYTES, 2),
    (1024**3, UnitOfInformation.GIBIBYTES, None),
    (0, UnitOfInformation.MEBIBYTES, None),
)
_DECIMAL_TIERS = (
    (1000**5, UnitOfInformation.PETABYTES, 2),
    (1000**4, UnitOfInformation.TERABYTES, 2),
    (1000**3, UnitOfInformation.GIGABYTES, None),
    (0, UnitOfInformation.MEGABYTES, None),
)
_BASE_TIER_INDEX = 2  # GiB / GB

# Suggested units that opt a DATA_SIZE description into the GB/GiB scaling
# behavior of scaled_data_unit(), shared by sensor.py (runtime entity setup)
# and __init__.py (entity-registry unit migration) so both stay in sync.
GB_SCALED_UNITS = (UnitOfInformation.GIGABYTES, UnitOfInformation.GIBIBYTES)


# ---------------------------
#   scaled_data_unit
# ---------------------------
def scaled_data_unit(value: object, binary: bool) -> tuple[str, int | None]:
    """Pick a data-size display unit (and precision) by magnitude and base.

    Scales the displayed unit to the value relative to the configured base
    (binary GiB or decimal GB): >= 1 TiB/TB -> TiB/TB, >= 1 PiB/PB -> PiB/PB,
    and < 1 GiB/GB -> MiB/MB. A precision of ``None`` keeps the description's
    own precision.
    """
    tiers = _BINARY_TIERS if binary else _DECIMAL_TIERS
    if not isinstance(value, (int, float)) or value <= 0:
        return tiers[_BASE_TIER_INDEX][1], None

    for threshold, unit, precision in tiers:
        if value >= threshold:
            return unit, precision

    return tiers[_BASE_TIER_INDEX][1], None


# ---------------------------
#   pool_scrub_state
# ---------------------------
def pool_scrub_state(pool: Mapping[str, Any]) -> str | None:
    """Return a pool's scrub state, reporting a paused scrub as PAUSED.

    TrueNAS keeps ``scan.state`` at SCANNING while a scrub is paused and only
    sets ``scan.pause`` (aiotruenas: ``scrub_pause``), so running and paused
    are told apart here.
    """
    state = pool.get("scrub_state")
    if not isinstance(state, str):
        return None
    if state == SCRUB_STATE_SCANNING and pool.get("scrub_pause"):
        return SCRUB_STATE_PAUSED
    return state


# Scrub states whose scan.percentage is meaningful as shown.
_SCRUB_PROGRESS_STATES = (
    SCRUB_STATE_SCANNING,
    SCRUB_STATE_PAUSED,
    SCRUB_STATE_CANCELED,
)


# ---------------------------
#   pool_scrub_progress
# ---------------------------
def pool_scrub_progress(pool: Mapping[str, Any]) -> float | None:
    """Return a pool's scrub progress in percent, rounded to one decimal.

    A finished scrub reads 100: TrueNAS leaves ``scan.percentage`` just below
    it (99.91-99.99). A canceled scrub keeps the value it was stopped at. With
    no scrub (or an unknown state) there is no progress to report.
    """
    state = pool_scrub_state(pool)
    if state == SCRUB_STATE_FINISHED:
        return 100.0
    if state not in _SCRUB_PROGRESS_STATES:
        return None
    percentage = pool.get("scrub_percentage")
    if isinstance(percentage, bool) or not isinstance(percentage, (int, float)):
        return None
    return round(float(percentage), 1)


# ---------------------------
#   alert_action
# ---------------------------
async def alert_action(coordinator: TrueNASCoordinator, uuid: str, action: str) -> None:
    """Execute alert dismiss/restore action (shared helper)."""
    await coordinator.api.query(f"alert.{action}", [uuid])
    if coordinator.api.error:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="alert_action_failed",
            translation_placeholders={
                "action": action,
                "uuid": uuid,
                "host": coordinator.host,
                "error": str(coordinator.api.error),
            },
        )
    await coordinator.async_refresh()
