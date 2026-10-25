"""Config flow to configure TrueNAS."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import time
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from logging import getLogger
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    CONN_CLASS_LOCAL_POLL,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_NAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import SECRET_SCRUB_FILTER, TrueNASAPI, scrub_exception
from .const import (
    ALLOWED_DATA_UNITS,
    BEHAVIOR_REMOVE_INACTIVE_NIC,
    BEHAVIOR_SKIP_DISABLED_CRONJOBS,
    CONF_BEHAVIORS,
    CONF_CRONJOB_SKIP_DISABLED,
    CONF_DATA_UNIT,
    CONF_DATASET_PASSPHRASES,
    CONF_MONITORED_GROUPS,
    CONF_POLL_INTERVAL,
    CONF_SYSTEM_ID,
    DEFAULT_BEHAVIORS,
    DEFAULT_CRONJOB_SKIP_DISABLED,
    DEFAULT_DATA_UNIT,
    DEFAULT_DEVICE_NAME,
    DEFAULT_HOST,
    DEFAULT_MONITORED_GROUPS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SSL_VERIFY,
    DOMAIN,
    ERR_API_KEY_REQUIRED,
    ERR_API_NOT_FOUND,
    ERR_CERT_VERIFY_FAILED,
    ERR_CONNECTION_REFUSED,
    ERR_HANDSHAKE_TIMEOUT,
    ERR_HTTP_USED,
    ERR_INVALID_HOSTNAME,
    ERR_INVALID_KEY,
    ERR_LOST_LOGIN,
    ERR_LOST_QUERY,
    ERR_MALFORMED_RESULT,
    ERR_PERMISSION_DENIED,
    ERR_PROXY_INTERCEPTED,
    ERR_TIMEOUT,
    ERR_TLS_NOT_SUPPORTED,
    ERR_UNKNOWN_HOSTNAME,
    ERR_WS_NOT_SUPPORTED,
    KNOWN_DOMAINS,
    LEGACY_DOMAIN,
    MIGRATION_DONE,
    MONITOR_GROUP_CLOUDSYNC,
    MONITOR_GROUP_CONTAINERS,
    MONITOR_GROUP_CRONJOBS,
    MONITOR_GROUP_DATASETS,
    MONITOR_GROUP_DIRECTORY_SERVICES,
    MONITOR_GROUP_REPLICATION,
    MONITOR_GROUP_RSYNC,
    MONITOR_GROUP_SNAPSHOTS,
    MONITOR_GROUP_UPS,
    MONITOR_GROUP_VMS,
)
from .coordinator import get_truenas_coordinator
from .helper import sanitize_host

_LOGGER = getLogger(__name__)
_LOGGER.addFilter(SECRET_SCRUB_FILTER)

# Shared selector for the API key field, reused by every schema (initial setup,
# reconfigure, reauth) so all three stay visually/behaviorally in sync.
_API_KEY_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)


def _base_schema(truenas_config: Mapping[str, Any]) -> vol.Schema:
    """Generate base schema.

    The API key default is intentionally never pre-filled from
    ``truenas_config`` (e.g. a taken-over legacy entry), even though every
    other field is: a secret's value would otherwise be embedded in the
    frontend's form state and re-submitted in cleartext. Leaving it blank is
    safe because ``_async_apply_user_input`` keeps the previously known key
    when the field comes back empty.

    ``CONF_API_KEY`` is ``vol.Optional`` (not ``vol.Required``) so the
    frontend actually lets the field be submitted blank -- a ``Required``
    field blocks empty submission client-side regardless of its default,
    which made the legacy-takeover blank-to-keep flow unusable (#158).
    Unlike ``_reconfigure_schema`` below (whose ``CONF_API_KEY`` is
    ``vol.Optional`` with no default), this one keeps ``default=""``: this
    schema is also used for a brand-new setup with no prior key to fall back
    on, and ``_validate_connection`` indexes ``config[CONF_API_KEY]``
    unconditionally, so the key must always be present in the merged
    ``truenas_config`` -- ``_pop_blank_api_key`` only ever drops it from
    ``user_input``, never leaves it missing from ``truenas_config`` itself.
    """
    base_schema = {
        vol.Required(
            CONF_NAME, default=truenas_config.get(CONF_NAME, DEFAULT_DEVICE_NAME)
        ): str,
        vol.Required(
            CONF_HOST, default=truenas_config.get(CONF_HOST, DEFAULT_HOST)
        ): str,
        vol.Optional(CONF_API_KEY, default=""): _API_KEY_SELECTOR,
        vol.Required(
            CONF_VERIFY_SSL,
            default=truenas_config.get(CONF_VERIFY_SSL, DEFAULT_SSL_VERIFY),
        ): bool,
        vol.Required(
            CONF_CRONJOB_SKIP_DISABLED,
            default=truenas_config.get(
                CONF_CRONJOB_SKIP_DISABLED, DEFAULT_CRONJOB_SKIP_DISABLED
            ),
        ): bool,
        vol.Required(
            CONF_DATA_UNIT,
            default=truenas_config.get(CONF_DATA_UNIT, DEFAULT_DATA_UNIT),
        ): vol.In(ALLOWED_DATA_UNITS),
    }

    return vol.Schema(base_schema)


def _text_to_passphrases(text: str) -> dict[str, str]:
    """Parse a multi-line textarea string back into the passphrases dict.

    Each non-empty line must be of the form ``<dataset_name>#<passphrase>``
    where both parts are non-empty.  The first ``#`` is the separator;
    additional ``#`` characters in the passphrase are preserved.  Dataset
    names must not contain ``#``.
    Raises ``ValueError((error_key, line))`` on the first invalid line.
    """
    result: dict[str, str] = {}
    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "#" not in line:
            raise ValueError(("passphrase_malformed_line", line))
        name, _, pp = line.partition("#")
        name = name.strip()
        if not name:
            raise ValueError(("passphrase_empty_name", line))
        if not pp:
            raise ValueError(("passphrase_empty_value", line))
        result[name] = pp
    return result


def _reconfigure_schema(truenas_config: Mapping[str, Any]) -> vol.Schema:
    """Generate reconfigure schema (connection parameters + stored passphrases)."""
    return vol.Schema(
        {
            vol.Required(
                CONF_HOST, default=truenas_config.get(CONF_HOST, DEFAULT_HOST)
            ): str,
            vol.Optional(CONF_API_KEY): _API_KEY_SELECTOR,
            vol.Required(
                CONF_VERIFY_SSL,
                default=truenas_config.get(CONF_VERIFY_SSL, DEFAULT_SSL_VERIFY),
            ): bool,
            vol.Optional(CONF_DATASET_PASSPHRASES): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
        }
    )


def _options_schema(options: Mapping[str, Any]) -> vol.Schema:
    """Generate the options-flow schema."""
    behaviors = options.get(CONF_BEHAVIORS, DEFAULT_BEHAVIORS)
    monitored = options.get(CONF_MONITORED_GROUPS, DEFAULT_MONITORED_GROUPS)
    poll = str(options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
    data_unit = options.get(CONF_DATA_UNIT, DEFAULT_DATA_UNIT)

    behavior_options = [
        selector.SelectOptionDict(
            value=BEHAVIOR_SKIP_DISABLED_CRONJOBS,
            label="Skip disabled cronjobs",
        ),
        selector.SelectOptionDict(
            value=BEHAVIOR_REMOVE_INACTIVE_NIC,
            label="Hide RX/TX sensors for disconnected NICs",
        ),
    ]
    group_options = [
        selector.SelectOptionDict(value=MONITOR_GROUP_UPS, label="UPS"),
        selector.SelectOptionDict(value=MONITOR_GROUP_VMS, label="Virtual Machines"),
        selector.SelectOptionDict(value=MONITOR_GROUP_CONTAINERS, label="Containers"),
        selector.SelectOptionDict(value=MONITOR_GROUP_CLOUDSYNC, label="Cloudsync"),
        selector.SelectOptionDict(value=MONITOR_GROUP_REPLICATION, label="Replication"),
        selector.SelectOptionDict(value=MONITOR_GROUP_RSYNC, label="Rsync Tasks"),
        selector.SelectOptionDict(
            value=MONITOR_GROUP_SNAPSHOTS, label="Snapshot Tasks"
        ),
        selector.SelectOptionDict(value=MONITOR_GROUP_CRONJOBS, label="Cron Jobs"),
        selector.SelectOptionDict(value=MONITOR_GROUP_DATASETS, label="Datasets"),
        selector.SelectOptionDict(
            value=MONITOR_GROUP_DIRECTORY_SERVICES, label="Directory Services"
        ),
    ]

    return vol.Schema(
        {
            vol.Required(CONF_POLL_INTERVAL, default=poll): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value="5", label="5 s"),
                        selector.SelectOptionDict(value="10", label="10 s"),
                        selector.SelectOptionDict(value="30", label="30 s"),
                        selector.SelectOptionDict(value="60", label="60 s"),
                        selector.SelectOptionDict(value="120", label="120 s"),
                        selector.SelectOptionDict(value="300", label="300 s"),
                    ]
                )
            ),
            vol.Required(CONF_DATA_UNIT, default=data_unit): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value="GB", label="GB (base 1000)"),
                        selector.SelectOptionDict(value="GiB", label="GiB (base 1024)"),
                    ]
                )
            ),
            vol.Required(CONF_BEHAVIORS, default=behaviors): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=behavior_options,
                    multiple=True,
                )
            ),
            vol.Required(
                CONF_MONITORED_GROUPS, default=monitored
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=group_options,
                    multiple=True,
                )
            ),
        }
    )


# ---------------------------
#   _map_error_to_ha
# ---------------------------
def _map_error_to_ha(errorcode: str) -> str:
    """Map TrueNAS connection error codes to Home Assistant config flow errors."""
    valid_errors = {
        ERR_CERT_VERIFY_FAILED,
        ERR_HTTP_USED,
        ERR_TLS_NOT_SUPPORTED,
        ERR_WS_NOT_SUPPORTED,
        ERR_INVALID_KEY,
        ERR_PROXY_INTERCEPTED,
        ERR_INVALID_HOSTNAME,
        ERR_UNKNOWN_HOSTNAME,
        ERR_CONNECTION_REFUSED,
        ERR_HANDSHAKE_TIMEOUT,
        ERR_API_NOT_FOUND,
        ERR_MALFORMED_RESULT,
        ERR_TIMEOUT,
        ERR_LOST_LOGIN,
        ERR_LOST_QUERY,
        ERR_PERMISSION_DENIED,
    }
    return errorcode if errorcode in valid_errors else "unknown"


def _pop_blank_api_key(user_input: dict[str, Any], keep_existing: bool) -> None:
    """Drop a blank ``CONF_API_KEY`` from ``user_input`` meaning "unchanged".

    The API key field is never pre-filled (see ``_base_schema``), so a blank
    resubmission means "keep the current key" -- but only when
    ``keep_existing`` says there actually is one to fall back to; a brand
    new setup with no stored key yet still gets a validation error instead
    of silently continuing with an empty key. Shared by
    ``_async_apply_user_input`` (``keep_existing`` reflects whether the
    in-progress config already has a key, e.g. from a taken-over legacy
    entry) and ``async_step_reconfigure`` (always ``True``, since the entry
    being edited already has one).
    """
    if user_input.get(CONF_API_KEY, "") == "" and keep_existing:
        user_input.pop(CONF_API_KEY, None)


# ---------------------------
#   _async_guess_ip
# ---------------------------
# Upper bound for prefilling the host field. The guess runs before the first
# form is shown, so a DNS server that never answers must not stall the whole
# dialog (a reverse proxy turns that into a 504 -- #196).
GUESS_HOST_TIMEOUT: float = 3.0

# Own small pool for the lookups: a blocked resolver call cannot be cancelled,
# so with Home Assistant's shared executor a stalled DNS server plus repeated
# setup attempts would tie up threads that unrelated jobs need. It holds exactly
# one guess; while lookups of an earlier guess still occupy it, no new batch is
# queued (see _HostGuessState), so stalled guesses cannot pile up.
_HOST_GUESS_EXECUTOR = ThreadPoolExecutor(
    max_workers=1 + len(KNOWN_DOMAINS), thread_name_prefix="truenas_ce_host_guess"
)


@dataclass
class _HostGuessState:
    """Lookups of the latest guess, kept to tell whether they still occupy the pool."""

    lookups: list[Future[str | None]] = field(default_factory=list)


_host_guess = _HostGuessState()


def _guess_host_candidates() -> list[str]:
    """Return the hostnames to try, in priority order."""
    return ["truenas"] + [f"truenas.{domain}" for domain in KNOWN_DOMAINS]


def _resolve_host(host: str) -> str | None:
    """Resolve ``host`` to an IPv4 address, or None if it does not resolve."""
    try:
        return socket.gethostbyname(host)
    except (OSError, ValueError):
        # ValueError covers UnicodeError from IDNA encoding of a malformed label.
        return None
    except Exception:  # a prefill guess must never abort the form
        _LOGGER.debug("Unexpected error resolving %s", host, exc_info=True)
        return None


def _pick_answer(
    lookups: list[Future[str | None]], *, settle: bool
) -> tuple[bool, str | None]:
    """Return (decided, ip) for the highest-priority candidate that resolved.

    Until ``settle``, an unfinished candidate blocks the decision because it can
    still beat every lower-priority answer.
    """
    for lookup in lookups:
        if not lookup.done():
            if settle:
                continue
            return False, None
        if not lookup.cancelled() and (ip := lookup.result()):
            return True, ip
    return True, None


async def _async_wait_for_answer(lookups: list[Future[str | None]]) -> str | None:
    """Wait until the best answer is known or GUESS_HOST_TIMEOUT has passed."""
    waiters = [asyncio.wrap_future(lookup) for lookup in lookups]
    deadline = time.monotonic() + GUESS_HOST_TIMEOUT
    try:
        while (remaining := deadline - time.monotonic()) > 0:
            decided, ip = _pick_answer(lookups, settle=False)
            if decided:
                return ip
            # Never empty: a waiter only completes via a loop callback, which
            # cannot run between _pick_answer seeing an unfinished lookup and here.
            await asyncio.wait(
                [waiter for waiter in waiters if not waiter.done()],
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
        return _pick_answer(lookups, settle=True)[1]
    finally:
        # A lookup finishing later must not call back into a loop that may be
        # closed by then.
        for waiter in waiters:
            waiter.cancel()


async def _async_guess_ip() -> str:
    """Try to guess the TrueNAS IP from common local hostnames.

    All candidates are resolved in parallel and the whole guess is capped at
    GUESS_HOST_TIMEOUT. The highest-priority candidate that resolved wins, as
    soon as no better one can still answer; otherwise DEFAULT_HOST is used.
    Lookups still running at the end keep their _HOST_GUESS_EXECUTOR thread
    until the resolver gives up, but no longer delay the form.
    """
    if any(not lookup.done() for lookup in _host_guess.lookups):
        # Use the best answer that batch has produced so far -- including one
        # that arrived after its own deadline; often only slower,
        # lower-priority candidates are left running.
        fallback = _pick_answer(_host_guess.lookups, settle=True)[1] or DEFAULT_HOST
        _LOGGER.debug(
            "Lookups of an earlier host guess are still running, using %s",
            fallback,
        )
        return fallback
    candidates = _guess_host_candidates()
    start = time.monotonic()
    lookups = [_HOST_GUESS_EXECUTOR.submit(_resolve_host, host) for host in candidates]
    _host_guess.lookups = lookups
    try:
        ip = await _async_wait_for_answer(lookups)
    finally:
        # Also on caller cancellation: queued lookups must never run later.
        pending = [lookup for lookup in lookups if not lookup.done()]
        for lookup in pending:
            lookup.cancel()
    _LOGGER.debug(
        "Host guess finished in %.1fs (%d of %d lookups still pending)",
        time.monotonic() - start,
        len(pending),
        len(candidates),
    )
    if ip:
        return ip
    _LOGGER.debug(
        "No candidate hostname resolved within %.1fs, using %s",
        GUESS_HOST_TIMEOUT,
        DEFAULT_HOST,
    )
    return DEFAULT_HOST


# ---------------------------
#   _async_try_connect
# ---------------------------
async def _async_try_connect(api: TrueNASAPI, host: str, context: str) -> bool:
    """Attempt ``api.connect()``, returning False (and logging) on any failure.

    Used by the zeroconf probe, which needs to try one candidate connection
    and move on to the next on any problem rather than raising, so an
    unexpected exception here must not abort the whole discovery flow.
    ``quiet=True`` keeps ``connect()``'s own failure logging at debug too,
    since most probed candidates are expected to not be TrueNAS at all.
    """
    try:
        return await api.connect(quiet=True)
    except Exception as err:
        _LOGGER.debug("TrueNAS %s: %s: %s", host, context, err)
        return False


# ---------------------------
#   _async_safe_disconnect
# ---------------------------
async def _async_safe_disconnect(api: TrueNASAPI) -> None:
    """Disconnect ``api``, swallowing any error.

    Mirrors :func:`_async_try_connect`/:func:`_async_get_system_id`: cleanup
    in a probe/rediscovery ``finally`` block must never raise, or it would
    abort the whole discovery flow for every remaining candidate.
    """
    with contextlib.suppress(Exception):
        await api.disconnect()


# 443 is the wss default; 80 is the web UI's plain-HTTP port, which is
# worthless to retry now that only wss is probed. Either way, an mDNS
# announcement naming one of them adds nothing over probing the bare host.
_DEFAULT_WS_PORTS = frozenset({80, 443})


# ---------------------------
#   _probe_candidates
# ---------------------------
def _probe_candidates(host: str, port: int | None) -> list[str]:
    """Return the host strings to probe for ``host``, most likely first.

    TrueNAS only advertises the generic ``_http._tcp`` service, whose port is
    the web UI's plain-HTTP one -- normally 80, which is worthless for a wss
    probe (see ``_DEFAULT_WS_PORTS``). Probing the bare host therefore stays
    first so a standard box reachable over ``wss`` on its own default port
    (443) is not forced onto the advertised plain-HTTP port. A genuinely
    non-default port is appended as a fallback, so an instance behind a
    custom port or reverse proxy is no longer misread as "not TrueNAS".
    ``aiotruenas`` builds its URL from the host verbatim, so ``host:port`` is
    a valid host string here (see ``helper.sanitize_host``).
    """
    if port is None or port in _DEFAULT_WS_PORTS:
        return [host]
    return [host, f"{host}:{port}"]


# ---------------------------
#   _async_probe_candidate
# ---------------------------
async def _async_probe_candidate(host: str) -> bool:
    """Return True only if ``host`` rejects a bogus API key as invalid.

    Only a genuine TrueNAS JSON-RPC endpoint completes the WebSocket
    handshake and then answers ``ERR_INVALID_KEY``; every other outcome is
    treated as "not TrueNAS". Only ``wss`` is probed: validation and every
    later connection always default to ``wss`` too (the config flow has no
    scheme field), so a candidate that only answers on plain ``ws`` could
    never complete setup regardless.

    ``connect()`` logs in as part of connecting, so the expected rejection
    surfaces as a *failed* connect with ``api.error == ERR_INVALID_KEY``; a
    successful connect with the bogus key is not a TrueNAS answer either.
    """
    api = TrueNASAPI(host, "-", verify_ssl=False, scheme="wss")
    try:
        if await _async_try_connect(api, host, "probe is not reachable"):
            return False
        return api.error == ERR_INVALID_KEY
    finally:
        await _async_safe_disconnect(api)


# ---------------------------
#   _async_get_system_id
# ---------------------------
async def _async_get_system_id(api: TrueNASAPI, host: str) -> str | None:
    """Fetch ``system.global.id``, returning None (and logging) on failure.

    A failed identity lookup must never block configuration/rediscovery, so
    any exception here is swallowed and treated the same as "no id yet".
    """
    try:
        system_id = await api.query("system.global.id")
    except Exception as err:
        _LOGGER.debug("TrueNAS %s: failed to read system.global.id: %s", host, err)
        return None

    if isinstance(system_id, str) and system_id:
        return system_id

    if not isinstance(system_id, str):
        _LOGGER.debug(
            "TrueNAS %s: unexpected system.global.id payload (%s): %r",
            host,
            type(system_id).__name__,
            system_id,
        )

    return None


# ``interface.ip_in_use`` params: also report IPv6, including link-local --
# a box often announces itself over mDNS on its fe80:: address only.
_IP_IN_USE_PARAMS = [{"ipv4": True, "ipv6": True, "ipv6_link_local": True}]
# Upper bound for resolving a stored hostname, so a slow resolver cannot
# stall the discovery flow.
_RESOLVE_TIMEOUT = 5
# hass.data[DOMAIN] key: entry_id -> monotonic time of the last setup retry a
# discovery triggered; see TrueNASConfigFlow._async_reload_if_retrying.
_DATA_DISCOVERY_RELOAD = "discovery_reload_by_entry"
# A booting NAS announces itself repeatedly, so retry at most this often.
_DISCOVERY_RELOAD_COOLDOWN = 60


# ---------------------------
#   _host_without_port
# ---------------------------
def _host_without_port(host: str) -> str:
    """Return the bare host of a sanitized ``host[:port]`` (IPv6 unbracketed).

    URL userinfo (``user:pass@``, which ``sanitize_host`` keeps) and a
    trailing FQDN dot are dropped too, so a credential never reaches the
    logs and ``nas.local.`` still matches the discovered ``nas.local``.
    """
    host = host.rpartition("@")[2]
    if host.startswith("["):
        return host[1:].partition("]")[0]
    name, sep, port = host.rpartition(":")
    return (name if sep and port.isdigit() else host).rstrip(".")


# ---------------------------
#   _normalize_ip
# ---------------------------
def _normalize_ip(value: str) -> str | None:
    """Return ``value`` as a canonical IP string, or None if it is not an IP.

    An IPv6 scope id (``fe80::1%eth0``) is dropped: it names the local
    interface the address was seen on, not the address itself, so mDNS and
    TrueNAS would otherwise never agree on the same link-local address.
    """
    try:
        return str(ipaddress.ip_address(value.partition("%")[0]))
    except ValueError:
        return None


# ---------------------------
#   _ip_set
# ---------------------------
def _ip_set(values: list[str]) -> set[str]:
    """Return the canonical IPs among ``values``, skipping non-IP entries."""
    return {ip for value in values if (ip := _normalize_ip(value))}


# ---------------------------
#   _async_resolve_host
# ---------------------------
async def _async_resolve_host(hass: HomeAssistant, host: str) -> set[str]:
    """Resolve hostname ``host`` to its IPs; empty on any lookup failure.

    Besides ``OSError`` (incl. ``TimeoutError``), a malformed stored host
    makes ``getaddrinfo`` raise ``ValueError`` (``UnicodeError`` for e.g.
    ``nas..lan``, an embedded NUL on some platforms) -- that must not break
    discovery for every other device either.
    """
    try:
        async with asyncio.timeout(_RESOLVE_TIMEOUT):
            infos = await hass.loop.getaddrinfo(host, None)
    except (OSError, ValueError) as err:
        # The host is deliberately not logged: ``sanitize_host`` cuts at the
        # first ``/?#`` before userinfo is stripped, so a password containing
        # one of those could otherwise leak a prefix into the log.
        _LOGGER.debug(
            "TrueNAS: failed to resolve a stored host: %s", scrub_exception(err)
        )
        return set()
    return _ip_set([str(info[4][0]) for info in infos])


# ---------------------------
#   _async_entry_reported_ips
# ---------------------------
async def _async_entry_reported_ips(entry: ConfigEntry) -> set[str]:
    """Return the IPs a loaded entry's own TrueNAS reports as in use.

    Asked over the entry's *existing*, already-authenticated connection to
    the box it is configured for -- never the newly discovered host -- so no
    stored credential is ever sent anywhere new. Skipped while that
    connection is down, since ``query`` would otherwise start a reconnect to
    the box just because some device was discovered. Any failure just yields
    an empty set: this only ever suppresses a duplicate discovery.
    """
    if entry.state is not ConfigEntryState.LOADED:
        return set()
    api = getattr(getattr(entry, "runtime_data", None), "api", None)
    if api is None or not api.connected():
        return set()
    try:
        result = await api.query("interface.ip_in_use", _IP_IN_USE_PARAMS)
    except Exception as err:
        _LOGGER.debug(
            "TrueNAS %s: interface.ip_in_use failed: %s",
            entry.title,
            scrub_exception(err),
        )
        return set()
    if not isinstance(result, list):
        return set()
    return _ip_set(
        [
            item["address"]
            for item in result
            if isinstance(item, dict) and isinstance(item.get("address"), str)
        ]
    )


# ---------------------------
#   _async_entry_matches_discovery
# ---------------------------
async def _async_entry_matches_discovery(
    hass: HomeAssistant, entry: ConfigEntry, addresses: set[str], hostname: str
) -> bool:
    """Return True if ``entry`` is the box behind the discovered addresses.

    Checked cheapest first: the stored host literally being one of the
    discovered IPs or the mDNS hostname, then the stored hostname resolving
    to one of them, then the IPs the box itself reports (#195).
    """
    stored = entry.data.get(CONF_HOST)
    host = _host_without_port(sanitize_host(stored)) if isinstance(stored, str) else ""
    if host:
        host_ip = _normalize_ip(host)
        if host == hostname or (host_ip is not None and host_ip in addresses):
            return True
        if host_ip is None and await _async_resolve_host(hass, host) & addresses:
            return True
    return bool(await _async_entry_reported_ips(entry) & addresses)


# ---------------------------
#   TrueNASConfigFlow
# ---------------------------
class TrueNASConfigFlow(ConfigFlow, domain=DOMAIN):
    """TrueNASConfigFlow class."""

    VERSION = 1
    CONNECTION_CLASS = CONN_CLASS_LOCAL_POLL

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.truenas_config: dict[str, Any] = {}
        # Options of a taken-over legacy entry, applied when the entry is created.
        self._legacy_options: dict[str, Any] = {}
        # Guard so the legacy-takeover offer is made at most once per flow.
        self._migration_checked = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowWithReload:
        """Return the options flow handler."""
        return TrueNASOptionsFlow()

    async def _validate_connection(
        self, config: dict[str, Any], errors: dict[str, str]
    ) -> None:
        """Test the API connection and record a mapped error on failure."""
        try:
            api = TrueNASAPI(
                config[CONF_HOST],
                config[CONF_API_KEY],
                config[CONF_VERIFY_SSL],
            )
        except ValueError:
            # sanitize_host already removes a scheme/path up front, so this
            # only triggers for a genuinely malformed host that the API layer
            # still rejects. Surface a clear error instead of an unhandled
            # exception (which the frontend reports as a generic failure).
            errors[CONF_HOST] = ERR_INVALID_HOSTNAME
            _LOGGER.error(
                "TrueNAS host %r is not a usable hostname or IP address",
                config.get(CONF_HOST),
            )
            return

        conn = False
        errorcode = ""
        try:
            conn, errorcode = await api.connection_test()
        except Exception:
            # A bug inside aiotruenas itself must degrade to a retryable
            # "unknown" form error, not crash the flow.
            _LOGGER.exception(
                "TrueNAS %s: connection_test() raised unexpectedly",
                config.get(CONF_HOST, ""),
            )
        else:
            if conn:
                system_id = await _async_get_system_id(api, config.get(CONF_HOST, ""))
                if system_id:
                    config[CONF_SYSTEM_ID] = system_id
        finally:
            # Runs on both the success and the swallowed-exception path, so
            # an already-open WebSocket is never leaked either way.
            await _async_safe_disconnect(api)

        if not conn:
            ha_error = _map_error_to_ha(errorcode)
            errors[CONF_HOST] = ha_error
            _LOGGER.error(
                "TrueNAS connection error (%s) mapped to HA error '%s'",
                errorcode,
                ha_error,
            )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        # Offer to take over an existing legacy ``truenas`` configuration once,
        # before the (possibly prefilled) form is shown. Inert in the legacy
        # integration itself (see _find_legacy_config).
        if user_input is None and not self._migration_checked:
            self._migration_checked = True
            if self._find_legacy_config() is not None:
                return await self.async_step_migrate()

        truenas_config = self.truenas_config
        errors: dict[str, str] = {}

        if user_input is None and not truenas_config.get(CONF_HOST):
            default_host = await _async_guess_ip()
            _LOGGER.debug("Auto-discovered default host: %s", default_host)
            truenas_config[CONF_HOST] = default_host

        if user_input is not None:
            result = await self._async_apply_user_input(
                user_input, truenas_config, errors
            )
            if result is not None:
                return result

        return self.async_show_form(
            step_id="user",
            data_schema=_base_schema(truenas_config),
            errors=errors,
        )

    async def _async_apply_user_input(
        self,
        user_input: dict[str, Any],
        truenas_config: dict[str, Any],
        errors: dict[str, str],
    ) -> ConfigFlowResult | None:
        """Validate a submitted user form.

        Returns the created entry on success, or ``None`` to re-show the form
        with ``errors`` populated. Split out of ``async_step_user`` to keep its
        cognitive complexity within bounds (SonarQube S3776).
        """
        if CONF_HOST in user_input:
            user_input[CONF_HOST] = sanitize_host(user_input[CONF_HOST])
        _pop_blank_api_key(user_input, bool(truenas_config.get(CONF_API_KEY)))
        truenas_config |= user_input

        # The same device must not be configurable twice: abort when another
        # entry already points at this host.
        self._async_abort_entries_match({CONF_HOST: truenas_config[CONF_HOST]})

        # CONF_API_KEY is optional now (#158, blank means "keep the legacy
        # key" -- see _base_schema), but a brand-new setup has no prior key
        # to fall back on: still-blank here means the field was genuinely
        # left empty. Catch that before _validate_connection, which indexes
        # config[CONF_API_KEY] unconditionally and would otherwise open a
        # pointless connection with an empty credential and blame the error
        # on CONF_HOST instead of the actually-empty field. ERR_INVALID_KEY
        # ("Login failed...") would be factually wrong here since no login is
        # attempted -- use the dedicated ERR_API_KEY_REQUIRED message instead.
        if not truenas_config.get(CONF_API_KEY):
            errors[CONF_API_KEY] = ERR_API_KEY_REQUIRED
        else:
            await self._validate_connection(truenas_config, errors)

        # Once the box's stable identity is known, key the entry's unique_id
        # on it rather than on the (zeroconf-set) host, so rediscovery and
        # de-duplication survive the host/IP changing later. A match here is
        # only ever reached after *this* flow has itself authenticated to the
        # host with a real (user-typed or taken-over) API key -- never with
        # another entry's stored credential -- so it is safe to fold the new
        # host into the matched entry via ``updates`` instead of just
        # aborting: the box's identity was confirmed through this flow's own
        # authenticated connection, not by trusting the discovery source.
        system_id = truenas_config.get(CONF_SYSTEM_ID)
        if not errors and isinstance(system_id, str) and system_id:
            await self.async_set_unique_id(system_id)
            self._abort_if_unique_id_configured(
                updates={CONF_HOST: truenas_config[CONF_HOST]}
            )

        # Save instance
        if not errors:
            # The data-unit display preference isn't needed to connect, so it
            # belongs in options (mutable later via the options flow) rather
            # than the connection data. truenas_config[CONF_DATA_UNIT] already
            # reflects the right precedence -- prefilled from the legacy
            # entry's options/data by async_step_migrate_import, then
            # overridden by whatever the user actually submitted -- so it
            # always wins over the legacy entry's other, unrelated options.
            data = {k: v for k, v in truenas_config.items() if k != CONF_DATA_UNIT}
            options = dict(self._legacy_options)
            options[CONF_DATA_UNIT] = truenas_config.get(
                CONF_DATA_UNIT, DEFAULT_DATA_UNIT
            )
            return self.async_create_entry(
                title=truenas_config[CONF_NAME],
                data=data,
                options=options,
            )
        return None

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a TrueNAS instance discovered over mDNS.

        TrueNAS SCALE only advertises the generic ``_http._tcp`` service
        type shared by countless unrelated devices (printers, routers,
        media servers, ...), so the zeroconf type match alone cannot tell
        TrueNAS apart. Instead, probe the discovered host with a bogus API
        key: only a genuine TrueNAS JSON-RPC endpoint answers the WebSocket
        handshake and then rejects it specifically as an invalid key
        (``ERR_INVALID_KEY``). Any other outcome (connection refused,
        handshake timeout, ...) means some other device is behind that
        _http._tcp announcement, so the flow aborts silently without ever
        showing the user anything.

        A confirmed-TrueNAS-like host is deliberately never used to silently
        replay a stored API key from an existing entry: that endpoint is only
        known to answer the tiny probe handshake, which a spoofed device on
        the same network can mimic, so treating it as proof of identity would
        leak real credentials to whatever actually answered the discovery
        broadcast. Unless an existing entry already covers the box (matched
        without any credential, see ``_async_discovery_is_configured``; the
        flow then aborts as ``already_configured``), the flow falls through
        to the user-facing confirm step; only a connection this flow itself
        authenticates (see ``_async_apply_user_input``) can establish the
        box's real identity and fold a rediscovered host into an existing
        entry.
        """
        host = sanitize_host(discovery_info.host)
        await self._async_retry_entries_for_host(host)
        self._async_abort_entries_match({CONF_HOST: host})
        await self.async_set_unique_id(host)
        self._abort_if_unique_id_configured()

        # The probe reports back which endpoint actually answered, so a box
        # found only on its advertised non-default port is configured with
        # that port instead of silently falling back to the default one.
        probed_host = await self._probe_is_truenas(host, discovery_info.port)
        if probed_host is None:
            return self.async_abort(reason="not_truenas")

        # The checks above only catch an entry set up with this exact
        # address; one set up by hostname (or rediscovered on another of the
        # box's IPs, e.g. IPv6) needs the identity matching below (#195). It
        # runs after the probe, so its DNS lookups and RPCs are only spent on
        # actual TrueNAS hosts, not on every _http._tcp device on the LAN.
        if await self._async_discovery_is_configured(discovery_info):
            return self.async_abort(reason="already_configured")

        self.truenas_config[CONF_HOST] = probed_host
        self.context["title_placeholders"] = {CONF_NAME: probed_host}
        return await self.async_step_zeroconf_confirm()

    async def _async_discovery_is_configured(
        self, discovery_info: ZeroconfServiceInfo
    ) -> bool:
        """Return True if an existing entry already covers the discovered box.

        Entries are checked concurrently, so slow DNS lookups or RPCs of
        several entries do not add up before the confirm form appears. Every
        matching entry (not just the first) gets its pending setup retry
        triggered.
        """
        addresses = _ip_set(
            [str(ip) for ip in discovery_info.ip_addresses] + [discovery_info.host]
        )
        hostname = (discovery_info.hostname or "").rstrip(".").lower()
        entries = self._async_current_entries(include_ignore=False)
        matches = await asyncio.gather(
            *(
                _async_entry_matches_discovery(self.hass, entry, addresses, hostname)
                for entry in entries
            )
        )
        matched_entries = [
            entry for entry, matched in zip(entries, matches, strict=True) if matched
        ]
        for entry in matched_entries:
            _LOGGER.debug(
                "TrueNAS %s: discovered %s matches this configured entry",
                entry.title,
                discovery_info.host,
            )
            self._async_reload_if_retrying(entry)
        return bool(matched_entries)

    async def _async_retry_entries_for_host(self, host: str) -> None:
        """Retry the setup of entries stored with exactly ``host`` (#196).

        Only once the probe confirms the TrueNAS API answers: a booting box
        often announces itself over mDNS before its middleware is up, and an
        early retry would only fail again -- and restart Home Assistant's
        backoff from scratch. The probe targets ``host`` itself, not the
        advertised port, because that is the endpoint the entry's setup will
        connect to. Entries still in their cooldown are skipped before
        probing, so repeated announcements open no extra connections (the
        reload itself stays debounced even for concurrent flows). The
        identity matching after the probe in ``async_step_zeroconf`` covers
        entries stored under another address.
        """
        retrying = [
            entry
            for entry in self._async_current_entries(include_ignore=False)
            if entry.data.get(CONF_HOST) == host
            and entry.state is ConfigEntryState.SETUP_RETRY
            and not self._in_discovery_reload_cooldown(entry)
        ]
        if retrying and await self._probe_is_truenas(host) is not None:
            for entry in retrying:
                self._async_reload_if_retrying(entry)

    @callback
    def _discovery_reload_times(self) -> dict[str, float]:
        """Return when each entry was last reloaded from discovery."""
        times: dict[str, float] = self.hass.data.setdefault(DOMAIN, {}).setdefault(
            _DATA_DISCOVERY_RELOAD, {}
        )
        return times

    @callback
    def _in_discovery_reload_cooldown(self, entry: ConfigEntry) -> bool:
        """Return True if ``entry`` was reloaded from discovery too recently."""
        last = self._discovery_reload_times().get(entry.entry_id)
        return last is not None and time.monotonic() - last < _DISCOVERY_RELOAD_COOLDOWN

    @callback
    def _async_reload_if_retrying(self, entry: ConfigEntry) -> None:
        """Reload ``entry`` now if its setup is waiting for the next retry.

        A NAS that was offline when Home Assistant started sits in
        ``SETUP_RETRY``, whose backoff grows to 10 minutes between attempts.
        Seeing the box announce itself over mDNS means it is likely back, so
        retry right away -- the same thing Home Assistant does for discovery
        flows that match an entry's unique ID, which never happens here since
        entries are keyed on the box's system ID, not on its host. The reload
        only reconnects to the entry's own stored host, so a spoofed
        announcement can at most cause an early retry. A cooldown keeps a
        burst of announcements (or concurrent discovery flows, which all
        still see ``SETUP_RETRY`` until the scheduled reload starts) from
        reloading the entry more than once.
        """
        if (
            entry.state is not ConfigEntryState.SETUP_RETRY
            or self._in_discovery_reload_cooldown(entry)
        ):
            return
        self._discovery_reload_times()[entry.entry_id] = time.monotonic()
        _LOGGER.debug(
            "TrueNAS %s: discovered while waiting to retry setup, retrying now",
            entry.title,
        )
        self.hass.config_entries.async_schedule_reload(entry.entry_id)

    @staticmethod
    async def _probe_is_truenas(host: str, port: int | None = None) -> str | None:
        """Return the ``host[:port]`` that answers as TrueNAS, or None.

        Any unexpected connection-level error (refused, DNS, handshake, ...)
        is treated the same as "not TrueNAS" so a misbehaving non-TrueNAS
        device cannot abort zeroconf discovery for the whole flow.

        The returned value is what the entry must be configured with, so a
        box found only on its advertised non-default port keeps that port.
        """
        for candidate in _probe_candidates(host, port):
            if await _async_probe_candidate(candidate):
                return candidate
        return None

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to confirm setup of the discovered TrueNAS host."""
        if user_input is not None:
            return await self.async_step_user()
        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={CONF_HOST: self.truenas_config[CONF_HOST]},
        )

    def _find_legacy_config(self) -> ConfigEntry | None:
        """Return a legacy ``truenas`` entry to optionally take over, if any.

        Only relevant in the renamed (``truenas_ce``) integration; inert while
        ``DOMAIN == LEGACY_DOMAIN`` so the takeover never appears pre-rename.
        With more than one legacy entry (multiple boxes set up under the old
        integration), a host already known at this point (e.g. from zeroconf
        discovery) picks the matching one instead of always offering the
        first; with no host known yet and more than one legacy entry, none is
        offered here (ambiguous -- the user can still migrate manually).
        """
        if DOMAIN == LEGACY_DOMAIN:
            return None
        legacy_entries = self.hass.config_entries.async_entries(LEGACY_DOMAIN)
        if not legacy_entries:
            return None
        if host := self.truenas_config.get(CONF_HOST):
            sanitized_host = sanitize_host(host)
            for entry in legacy_entries:
                entry_host = entry.data.get(CONF_HOST)
                if entry_host and sanitize_host(entry_host) == sanitized_host:
                    return entry
        return legacy_entries[0] if len(legacy_entries) == 1 else None

    async def async_step_migrate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer to import an existing TrueNAS configuration or start fresh."""
        return self.async_show_menu(
            step_id="migrate",
            menu_options=["migrate_import", "migrate_manual"],
        )

    async def async_step_migrate_import(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prefill the form from the detected legacy entry, then show it.

        Importing the same name is required: the entity unique_ids derive from
        it, so the migration in __init__.py can re-attach the old entity_ids.
        """
        legacy = self._find_legacy_config()
        if legacy is not None:
            self.truenas_config.update(
                {
                    CONF_NAME: legacy.data.get(CONF_NAME, DEFAULT_DEVICE_NAME),
                    CONF_HOST: legacy.data.get(CONF_HOST, DEFAULT_HOST),
                    CONF_API_KEY: legacy.data.get(CONF_API_KEY, ""),
                    CONF_VERIFY_SSL: legacy.data.get(
                        CONF_VERIFY_SSL, DEFAULT_SSL_VERIFY
                    ),
                    CONF_CRONJOB_SKIP_DISABLED: legacy.data.get(
                        CONF_CRONJOB_SKIP_DISABLED, DEFAULT_CRONJOB_SKIP_DISABLED
                    ),
                    CONF_DATA_UNIT: legacy.options.get(
                        CONF_DATA_UNIT,
                        legacy.data.get(CONF_DATA_UNIT, DEFAULT_DATA_UNIT),
                    ),
                }
            )
            self._legacy_options = dict(legacy.options)
        return await self.async_step_user()

    async def async_step_migrate_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Skip the takeover and configure TrueNAS CE from scratch.

        Marks the entry as already migrated so ``async_adopt_legacy_entities``
        never auto-adopts the legacy entry later -- without this, entering the
        same host the legacy entry uses would silently override the user's
        explicit "from scratch" choice on the first coordinator setup.
        """
        self.truenas_config[MIGRATION_DONE] = True
        return await self.async_step_user()

    def _validate_passphrase_names(
        self,
        new_passphrases: dict[str, str],
        entry_id: str,
        errors: dict[str, str],
        description_placeholders: dict[str, str],
    ) -> None:
        """Set errors if any passphrase key is not a known dataset name."""
        entry = self.hass.config_entries.async_get_entry(entry_id)
        coordinator = get_truenas_coordinator(entry)
        if coordinator is None:
            return
        known = {
            v.get("name")
            for v in coordinator.ds.get("dataset", {}).values()
            if isinstance(v, dict) and v.get("name")
        }
        if not known:
            return
        if unknown := [k for k in new_passphrases if k not in known]:
            description_placeholders["datasets"] = ", ".join(sorted(unknown))
            errors[CONF_DATASET_PASSPHRASES] = "unknown_dataset"

    def _apply_passphrase_input(
        self,
        user_input: dict[str, Any],
        truenas_config: dict[str, Any],
        entry_id: str,
        errors: dict[str, str],
        description_placeholders: dict[str, str],
    ) -> None:
        """Parse/validate the passphrase textarea; mutate user_input in place."""
        new_text = user_input.get(CONF_DATASET_PASSPHRASES, "")
        if not isinstance(new_text, str) or not new_text.strip():
            user_input.pop(CONF_DATASET_PASSPHRASES, None)
            return
        try:
            new_passphrases = _text_to_passphrases(new_text)
        except ValueError as exc:
            error_key, bad_line = exc.args[0]
            errors[CONF_DATASET_PASSPHRASES] = error_key
            description_placeholders["line"] = bad_line
            return
        if new_passphrases:
            self._validate_passphrase_names(
                new_passphrases, entry_id, errors, description_placeholders
            )
        if not errors:
            existing = dict(truenas_config.get(CONF_DATASET_PASSPHRASES) or {})
            existing |= new_passphrases
            user_input[CONF_DATASET_PASSPHRASES] = existing

    async def _check_connection_if_changed(
        self,
        truenas_config: dict[str, Any],
        entry_data: Mapping[str, Any],
        errors: dict[str, str],
    ) -> None:
        """Run connection test only when transport-relevant settings changed."""
        _CONNECTION_KEYS = (CONF_HOST, CONF_API_KEY, CONF_VERIFY_SSL)
        if any(truenas_config.get(k) != entry_data.get(k) for k in _CONNECTION_KEYS):
            await self._validate_connection(truenas_config, errors)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauthentication triggered by an invalid API key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API key and validate it before updating the entry."""
        reauth_entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            config = dict(reauth_entry.data)
            config[CONF_API_KEY] = user_input[CONF_API_KEY]
            await self._validate_connection(config, errors)
            if not errors:
                data_updates = {CONF_API_KEY: user_input[CONF_API_KEY]}
                if CONF_SYSTEM_ID in config:
                    data_updates[CONF_SYSTEM_ID] = config[CONF_SYSTEM_ID]
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates=data_updates,
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): _API_KEY_SELECTOR}),
            errors=errors,
            description_placeholders={
                CONF_NAME: reauth_entry.data.get(CONF_NAME, DEFAULT_DEVICE_NAME)
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        reconfigure_entry = self._get_reconfigure_entry()
        if not self.truenas_config:
            self.truenas_config.update(reconfigure_entry.data)

        truenas_config = self.truenas_config
        reconfigure_entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}

        if user_input is not None:
            if CONF_HOST in user_input:
                user_input[CONF_HOST] = sanitize_host(user_input[CONF_HOST])
            _pop_blank_api_key(user_input, keep_existing=True)
            if CONF_DATASET_PASSPHRASES in user_input:
                self._apply_passphrase_input(
                    user_input,
                    truenas_config,
                    reconfigure_entry.entry_id,
                    errors,
                    description_placeholders,
                )

            truenas_config.update(user_input)

            if not errors:
                await self._check_connection_if_changed(
                    truenas_config, reconfigure_entry.data, errors
                )

            if not errors:
                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    title=reconfigure_entry.data[CONF_NAME],
                    data_updates=truenas_config,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_reconfigure_schema(truenas_config),
            errors=errors,
            description_placeholders=description_placeholders or None,
        )


# ---------------------------
#   TrueNASOptionsFlow
# ---------------------------
class TrueNASOptionsFlow(OptionsFlowWithReload):
    """Handle TrueNAS integration options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and process the options form."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        # A pre-existing entry may still carry CONF_DATA_UNIT only in .data
        # (from before it moved to .options); fall back to it here so the
        # form shows the entry's actual current preference instead of
        # silently reverting it to DEFAULT_DATA_UNIT on the next save.
        options = dict(self.config_entry.options)
        options.setdefault(
            CONF_DATA_UNIT,
            self.config_entry.data.get(CONF_DATA_UNIT, DEFAULT_DATA_UNIT),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(options),
        )
