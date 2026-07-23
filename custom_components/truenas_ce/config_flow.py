"""Config flow to configure TrueNAS."""

from __future__ import annotations

import contextlib
import re
import socket
from collections.abc import Mapping
from logging import getLogger
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    CONN_CLASS_LOCAL_POLL,
    ConfigEntry,
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

from .api import TrueNASAPI
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
    DEFAULT_BEHAVIORS,
    DEFAULT_CRONJOB_SKIP_DISABLED,
    DEFAULT_DATA_UNIT,
    DEFAULT_DEVICE_NAME,
    DEFAULT_HOST,
    DEFAULT_MONITORED_GROUPS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SSL_VERIFY,
    DOMAIN,
    ERR_API_NOT_FOUND,
    ERR_CERT_VERIFY_FAILED,
    ERR_CONNECTION_REFUSED,
    ERR_HANDSHAKE_TIMEOUT,
    ERR_HTTP_USED,
    ERR_INVALID_HOSTNAME,
    ERR_INVALID_KEY,
    ERR_MALFORMED_RESULT,
    ERR_PROXY_INTERCEPTED,
    ERR_TLS_NOT_SUPPORTED,
    ERR_UNKNOWN_HOSTNAME,
    ERR_WS_NOT_SUPPORTED,
    KNOWN_DOMAINS,
    LEGACY_DOMAIN,
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

_LOGGER = getLogger(__name__)

# Shared selector for the API key field, reused by every schema (initial setup,
# reconfigure, reauth) so all three stay visually/behaviorally in sync.
_API_KEY_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)


def _base_schema(truenas_config: Mapping[str, Any]) -> vol.Schema:
    """Generate base schema."""
    base_schema = {
        vol.Required(
            CONF_NAME, default=truenas_config.get(CONF_NAME, DEFAULT_DEVICE_NAME)
        ): str,
        vol.Required(
            CONF_HOST, default=truenas_config.get(CONF_HOST, DEFAULT_HOST)
        ): str,
        vol.Required(
            CONF_API_KEY, default=truenas_config.get(CONF_API_KEY, "")
        ): _API_KEY_SELECTOR,
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
    for line in text.strip().splitlines():
        line = line.strip()
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
    }
    return errorcode if errorcode in valid_errors else "unknown"


# ---------------------------
#   configured_instances
# ---------------------------
@callback
def configured_instances(hass: HomeAssistant) -> set[str]:
    """Return a set of configured instances."""
    return {
        entry.data[CONF_NAME] for entry in hass.config_entries.async_entries(DOMAIN)
    }


# ---------------------------
#   _sanitize_host
# ---------------------------
# Strip a leading URL scheme (e.g. "https://" or "http://") from the host.
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
# Everything from the first path/query/fragment delimiter is not part of host.
_HOST_TAIL_RE = re.compile(r"[/?#]")


def _sanitize_host(host: str) -> str:
    """Normalize user input to the bare hostname/IP[:port] the API expects.

    Users frequently paste a full URL (for example
    ``https://nas.example.com/ui?tab=1``). The API layer requires a bare host,
    so strip any scheme as well as a trailing path, query or fragment here
    instead of erroring out, so the value just works.
    """
    host = host.strip()
    host = _SCHEME_RE.sub("", host)  # drop a leading scheme
    host = _HOST_TAIL_RE.split(host, maxsplit=1)[0]  # drop path/query/fragment
    return host.strip()


# ---------------------------
#   _guess_ip
# ---------------------------
def _guess_ip() -> str:
    """Try to guess the TrueNAS IP from common local hostnames."""
    for domain in [""] + KNOWN_DOMAINS:
        test_host = f"truenas.{domain}" if domain else "truenas"
        with contextlib.suppress(OSError):
            return socket.gethostbyname(test_host)
    return DEFAULT_HOST


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
            # _sanitize_host already removes a scheme/path up front, so this
            # only triggers for a genuinely malformed host that the API layer
            # still rejects. Surface a clear error instead of an unhandled
            # exception (which the frontend reports as a generic failure).
            errors[CONF_HOST] = ERR_INVALID_HOSTNAME
            _LOGGER.error(
                "TrueNAS host %r is not a usable hostname or IP address",
                config.get(CONF_HOST),
            )
            return

        conn, errorcode = await api.connection_test()
        await api.disconnect()

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
            default_host = await self.hass.async_add_executor_job(_guess_ip)
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
            user_input[CONF_HOST] = _sanitize_host(user_input[CONF_HOST])
        truenas_config |= user_input

        # The same device must not be configurable twice: abort when another
        # entry already points at this host (the name check below alone would
        # let the same host in again under a different name).
        self._async_abort_entries_match({CONF_HOST: truenas_config[CONF_HOST]})

        # Check if instance with this name already exists
        if truenas_config[CONF_NAME] in configured_instances(self.hass):
            errors["base"] = "name_exists"

        if not errors:
            await self._validate_connection(truenas_config, errors)

        # Save instance
        if not errors:
            return self.async_create_entry(
                title=truenas_config[CONF_NAME],
                data=truenas_config,
                options=self._legacy_options or None,
            )
        return None

    def _find_legacy_config(self) -> ConfigEntry | None:
        """Return a legacy ``truenas`` entry to optionally take over, if any.

        Only relevant in the renamed (``truenas_ce``) integration; inert while
        ``DOMAIN == LEGACY_DOMAIN`` so the takeover never appears pre-rename.
        """
        if DOMAIN == LEGACY_DOMAIN:
            return None
        legacy_entries = self.hass.config_entries.async_entries(LEGACY_DOMAIN)
        return legacy_entries[0] if legacy_entries else None

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
                    CONF_DATA_UNIT: legacy.data.get(CONF_DATA_UNIT, DEFAULT_DATA_UNIT),
                }
            )
            self._legacy_options = dict(legacy.options)
        return await self.async_step_user()

    async def async_step_migrate_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Skip the takeover and configure TrueNAS CE from scratch."""
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
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={CONF_API_KEY: user_input[CONF_API_KEY]},
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
                user_input[CONF_HOST] = _sanitize_host(user_input[CONF_HOST])
            if not user_input.get(CONF_API_KEY):
                user_input.pop(CONF_API_KEY, None)
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

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(self.config_entry.options),
        )
