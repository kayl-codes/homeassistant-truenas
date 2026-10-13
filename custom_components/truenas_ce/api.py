"""TrueNAS API."""

import asyncio
import re
from collections.abc import Mapping
from contextvars import ContextVar
from logging import DEBUG, ERROR, Filter, LogRecord, getLogger
from types import MappingProxyType
from typing import Any, NamedTuple

from aiotruenas import TrueNASClient
from aiotruenas.exceptions import (
    TrueNASAuthenticationError,
    TrueNASCallError,
    TrueNASCallTimeoutError,
    TrueNASCertificateVerificationError,
    TrueNASConnectionClosedError,
    TrueNASConnectionError,
    TrueNASConnectionRefusedError,
    TrueNASEndpointNotFoundError,
    TrueNASError,
    TrueNASHandshakeTimeoutError,
    TrueNASHostUnknownError,
    TrueNASHttpSchemeError,
    TrueNASMalformedResponseError,
    TrueNASProxyInterceptedError,
    TrueNASUnsupportedTlsVersionError,
    TrueNASWebSocketUnsupportedError,
)

from .const import (
    ERR_API_NOT_FOUND,
    ERR_CERT_VERIFY_FAILED,
    ERR_CONNECTION_REFUSED,
    ERR_HANDSHAKE_TIMEOUT,
    ERR_HTTP_USED,
    ERR_INVALID_KEY,
    ERR_LOST_LOGIN,
    ERR_LOST_QUERY,
    ERR_MALFORMED_RESULT,
    ERR_PERMISSION_DENIED,
    ERR_PROXY_INTERCEPTED,
    ERR_TIMEOUT,
    ERR_TLS_NOT_SUPPORTED,
    ERR_UNKNOWN,
    ERR_UNKNOWN_HOSTNAME,
    ERR_WS_NOT_SUPPORTED,
    ERROR_API_FORMAT,
    TO_REDACT,
)

_LOGGER = getLogger(__name__)

_REDACTED = "**REDACTED**"

# Maximum number of characters of an API payload to include in debug logs.
# Full payloads can be huge (e.g. pool.query with topology or app.query), so
# they are summarized and truncated to keep debug logs readable.
_LOG_PAYLOAD_LIMIT = 5000

# Secrets embedded in free text -- string values such as a cloud sync task's
# pre/post script or a job error, and exception messages -- are out of reach
# of the key-based ``TO_REDACT`` masking. Mask the value of anything shaped
# like ``<secret-ish name>=value`` / ``name: value`` / ``--name value`` and
# the userinfo part of a URL. Known limits: an unquoted value is masked up to
# the first whitespace/``,;&`` only, and ``credentials`` is deliberately not a
# name -- TrueNAS validation errors (``cloudsync_create.credentials: Field
# required``) would lose their reason, while the credentials themselves sit
# under ``TO_REDACT`` keys. Quantifiers are bounded to keep matching linear.
# A quoted value that is never closed (e.g. cut off by the payload length
# limit) is masked up to the end of the text.
_SENSITIVE_NAME_PATTERN = (
    r"[A-Za-z0-9_-]{0,64}"
    r"(?:passw(?:or)?d|passphrase|secret|token|api[_-]?key|access[_-]?key"
    r"|private[_-]?key|encryption[_-]?key|authorization)"
    r"[A-Za-z0-9_-]{0,64}"
)
_SENSITIVE_VALUE_PATTERN = (
    r"(?:bearer|basic)\s+[^\s\"',;&]+"
    r"|\"(?:[^\"\\]|\\.)*(?:\"|\\?$)|'(?:[^'\\]|\\.)*(?:'|\\?$)"
    r"|[^\s\"',;&]+"
)
_KEY_VALUE_SECRET_RE = re.compile(
    rf"(?P<lead>(?<![A-Za-z0-9_-]){_SENSITIVE_NAME_PATTERN}[\"']?\s*[:=]\s*)"
    rf"(?P<value>{_SENSITIVE_VALUE_PATTERN})",
    re.IGNORECASE,
)
_CLI_FLAG_SECRET_RE = re.compile(
    rf"(?P<lead>(?<![A-Za-z0-9_-])--?{_SENSITIVE_NAME_PATTERN}\s+)"
    rf"(?P<value>(?!-)(?:{_SENSITIVE_VALUE_PATTERN}))",
    re.IGNORECASE,
)
_URL_CREDENTIALS_RE = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]{0,31}://)[^\s/@]+@", re.IGNORECASE
)
# OSError renders ``str()`` from these attributes, not from ``args``.
_OSERROR_TEXT_ATTRS = ("strerror", "filename", "filename2")
_CYCLE_PLACEHOLDER = "..."
_MAX_SCRUB_DEPTH = 32

# Last error per TrueNASAPI instance (keyed by its _error_key token), stored
# per asyncio task: a caller that reads ``api.error`` right after awaiting
# ``query()`` sees only its own call's outcome, never one written meanwhile by
# a concurrent caller (a poll job, an entity action) on the shared instance.
# The mapping is replaced, never mutated, so a task's copied context stays
# isolated from its parent's: an error recorded in a child task is invisible
# to its parent, and a new task starts with its creator's last error until its
# own first call -- so read ``error`` only right after this task's own call.
_CALL_ERRORS: ContextVar[Mapping[object, str]] = ContextVar(
    "truenas_ce_call_errors", default=MappingProxyType({})
)


class _CallResult(NamedTuple):
    """Outcome of a single ``_query`` call.

    ``error`` and ``errname`` belong to this call only (both "" on
    success); ``TrueNASAPI.error`` holds the same text for the calling task.
    """

    data: Any
    error: str
    errname: str


# aiotruenas exception -> ERR_* mapping (see const.py). Order matters: more
# specific subclasses must be checked before their base classes, so this is
# consulted via isinstance() in declaration order rather than a plain dict
# keyed by exact type.
_EXCEPTION_ERR_MAP: tuple[tuple[type[TrueNASError], str], ...] = (
    (TrueNASCertificateVerificationError, ERR_CERT_VERIFY_FAILED),
    (TrueNASHttpSchemeError, ERR_HTTP_USED),
    (TrueNASUnsupportedTlsVersionError, ERR_TLS_NOT_SUPPORTED),
    (TrueNASWebSocketUnsupportedError, ERR_WS_NOT_SUPPORTED),
    (TrueNASHostUnknownError, ERR_UNKNOWN_HOSTNAME),
    (TrueNASConnectionRefusedError, ERR_CONNECTION_REFUSED),
    (TrueNASProxyInterceptedError, ERR_PROXY_INTERCEPTED),
    (TrueNASEndpointNotFoundError, ERR_API_NOT_FOUND),
    (TrueNASHandshakeTimeoutError, ERR_HANDSHAKE_TIMEOUT),
    (TrueNASCallTimeoutError, ERR_TIMEOUT),
    (TrueNASAuthenticationError, ERR_INVALID_KEY),
    (TrueNASMalformedResponseError, ERR_MALFORMED_RESULT),
)


def _mask_value(match: re.Match[str]) -> str:
    """Keep the name/separator of a secret match, mask the value (and its quotes)."""
    value = match["value"]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return f"{match['lead']}{value[0]}{_REDACTED}{value[0]}"
    return f"{match['lead']}{_REDACTED}"


def scrub_text(text: str) -> str:
    """Return ``text`` with secrets embedded in free text masked.

    Complements the key-based ``TO_REDACT`` masking for values that are free
    text, e.g. ``AWS_SECRET_ACCESS_KEY=...`` in a script, ``--password x`` in a
    command line or ``https://user:pass@host`` in an error message.
    """
    text = _KEY_VALUE_SECRET_RE.sub(_mask_value, text)
    text = _CLI_FLAG_SECRET_RE.sub(_mask_value, text)
    return _URL_CREDENTIALS_RE.sub(rf"\g<scheme>{_REDACTED}@", text)


def scrub_strings(data: Any) -> Any:
    """Return a copy of ``data`` with every secret masked.

    Walks dicts, lists and tuples recursively: values under ``TO_REDACT`` keys
    are masked whole, every other string goes through :func:`scrub_text` and
    exceptions found inside are scrubbed in place (see
    :func:`scrub_exception`). Keys are kept. Used for log arguments, exception
    args and on top of Home Assistant's ``async_redact_data`` for diagnostics.
    """
    return _scrub_value(data, set())


def _scrub_value(data: Any, seen: set[int], depth: int = 0) -> Any:
    """Recursive worker of :func:`scrub_strings`.

    ``seen`` holds the ids of exceptions already scrubbed and of the
    containers currently being walked, so reference cycles end in a ``...``
    placeholder (like ``repr``) instead of a ``RecursionError``. Containers
    nested deeper than ``_MAX_SCRUB_DEPTH`` are replaced by the placeholder
    too (fail closed), so a direct ``scrub_exception`` call never raises.
    """
    if isinstance(data, str):
        return scrub_text(data)
    if isinstance(data, bytes | bytearray):
        return _scrub_bytes(data)
    if isinstance(data, BaseException):
        return _scrub_exception_chain(data, seen)
    if not isinstance(data, list | tuple | dict):
        return data
    if id(data) in seen or depth >= _MAX_SCRUB_DEPTH:
        return _CYCLE_PLACEHOLDER
    seen.add(id(data))
    try:
        if isinstance(data, dict):
            return {
                key: _REDACTED
                if key in TO_REDACT and value not in (None, "")
                else _scrub_value(value, seen, depth + 1)
                for key, value in data.items()
            }
        items = [_scrub_value(item, seen, depth + 1) for item in data]
        return items if isinstance(data, list) else tuple(items)
    finally:
        seen.discard(id(data))


def _scrub_bytes(data: bytes | bytearray) -> bytes | bytearray | str:
    """Return ``data`` unchanged, or its scrubbed text if it held a secret."""
    text = data.decode(errors="replace")
    scrubbed = scrub_text(text)
    return data if scrubbed == text else scrubbed


def scrub_exception(exc: BaseException) -> BaseException:
    """Mask secrets in ``exc`` and every exception linked to it.

    Rewrites ``args`` (plus a call error's ``reason`` and an OSError's
    ``strerror``/``filename``) in place, following ``__cause__``,
    ``__context__``, an exception group's members and exceptions nested in
    ``args``, so both the logged message and a logged traceback -- which
    re-renders ``str(exc)`` at format time -- stay clean. Only secrets are
    removed, so mutating an exception that is also referenced elsewhere is
    harmless.
    """
    return _scrub_exception_chain(exc, set())


def _scrub_exception_chain(exc: BaseException, seen: set[int]) -> BaseException:
    """Scrub ``exc`` and its linked exceptions, skipping ids already in ``seen``."""
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        _scrub_exception_text(current, seen)
        pending.extend(_linked_exceptions(current))
    return exc


def _linked_exceptions(exc: BaseException) -> list[BaseException]:
    """Return the exceptions a traceback of ``exc`` would also render."""
    linked = [e for e in (exc.__cause__, exc.__context__) if e is not None]
    if isinstance(exc, BaseExceptionGroup):
        linked.extend(exc.exceptions)
    return linked


def _scrub_exception_text(exc: BaseException, seen: set[int]) -> None:
    """Scrub the text-bearing fields of a single exception in place."""
    exc.args = _scrub_value(exc.args, seen)
    if isinstance(exc, TrueNASCallError) and exc.reason:
        exc.reason = scrub_text(exc.reason)
    if isinstance(exc, OSError):
        for attr in _OSERROR_TEXT_ATTRS:
            value = getattr(exc, attr, None)
            if isinstance(value, str | bytes | bytearray):
                setattr(exc, attr, _scrub_value(value, seen))


class SecretScrubFilter(Filter):
    """Logger filter that masks free-text secrets in every record it sees.

    Attached to each module logger of this integration (enforced by a test),
    so a broad ``except Exception`` handler that logs an exception -- as a
    ``%s`` argument, as the message itself or via
    ``_LOGGER.exception``/``exc_info`` -- cannot leak a secret from its message
    or traceback chain. Only the arguments are scrubbed, never the format
    string, so placeholders stay intact (a pre-formatted f-string message is
    therefore not covered).
    """

    def filter(self, record: LogRecord) -> bool:
        """Scrub the record's arguments and exception in place; never drop it."""
        try:
            self._scrub_record(record)
        except Exception as err:  # logging must never fail here
            # Fail closed: withhold the arguments rather than log them unscrubbed.
            record.msg = "Log message withheld: secret scrubbing failed (%s)"
            record.args = (type(err).__name__,)
            record.exc_info = None
            record.exc_text = None
        return True

    @staticmethod
    def _scrub_record(record: LogRecord) -> None:
        if isinstance(record.msg, BaseException):
            scrub_exception(record.msg)
        if record.args:
            record.args = scrub_strings(record.args)
        if isinstance(record.exc_info, tuple) and record.exc_info[1] is not None:
            scrub_exception(record.exc_info[1])


SECRET_SCRUB_FILTER = SecretScrubFilter()
_LOGGER.addFilter(SECRET_SCRUB_FILTER)
# apiparser and event_push stay free of package-relative imports (their tests
# load them standalone), so their loggers get the shared filter from here.
for _standalone_module in ("apiparser", "event_push"):
    getLogger(f"{__package__}.{_standalone_module}").addFilter(SECRET_SCRUB_FILTER)


def _call_error_text(exc: TrueNASCallError) -> str:
    """Return the (scrubbed) error text stored in ``_error`` for a call error."""
    scrub_exception(exc)
    return exc.reason or str(exc) or ERR_UNKNOWN


def _redact(data: Any) -> Any:
    """Return a copy of ``data`` with every ``TO_REDACT`` key masked.

    Mirrors Home Assistant's ``async_redact_data`` (dicts, lists and tuples are
    walked recursively, empty values are kept) without importing Home Assistant, so
    this module stays HA-independent. Used before logging API params and
    payloads, which can carry secrets such as certificate private keys. String
    values additionally go through :func:`scrub_text` for secrets in free text.
    """
    return scrub_strings(data)


def _summarize_payload(data: Any, limit: int = _LOG_PAYLOAD_LIMIT) -> str:
    """Return a compact, length-bounded, redacted description of an API payload."""
    if isinstance(data, list):
        shape = f"list[{len(data)}]"
    elif isinstance(data, dict):
        shape = f"dict[{len(data)} keys]"
    else:
        shape = type(data).__name__

    text = repr(_redact(data))
    if len(text) > limit:
        text = f"{text[:limit]}... (truncated, {len(text)} chars total)"
    return f"{shape} {text}"


def _classify_exception(exc: TrueNASError, *, during_call: bool) -> str:
    """Map an aiotruenas exception to one of this integration's ERR_* codes."""
    if isinstance(exc, TrueNASConnectionClosedError):
        return ERR_LOST_QUERY if during_call else ERR_LOST_LOGIN

    return next(
        (
            err_code
            for exc_type, err_code in _EXCEPTION_ERR_MAP
            if isinstance(exc, exc_type)
        ),
        ERR_UNKNOWN,
    )


def _log_call_error(host: str, context: str, exc: TrueNASCallError) -> None:
    """Log a TrueNAS call error, quietly for expected permission errors.

    ``context`` is the service/event/subscription identifier the error
    occurred on, so recurring errors (e.g. a method unsupported by the
    TrueNAS version, see #81) can be traced back to their source instead of
    only showing the bare exception text.

    A read-only-scoped API key gets an ``EACCES`` response from admin-only
    methods (e.g. ``smb.status``). That is a permanent, expected condition
    for that key -- not an integration bug -- so logging it at ERROR with a
    full traceback on every call would flood the log for no benefit; log it
    at debug instead.
    """
    permission_denied = exc.errname == "EACCES"
    _LOGGER.log(
        DEBUG if permission_denied else ERROR,
        ERROR_API_FORMAT,
        host,
        context,
        scrub_exception(exc),
        exc_info=None if permission_denied else exc,
    )


# ---------------------------
#   TrueNASAPI
# ---------------------------
class TrueNASAPI:
    """Thin async adapter around aiotruenas.TrueNASClient.

    Preserves the public shape of the previous sync/thread-based
    implementation (``connect``/``connected``/``query``/``connection_test``/
    ``disconnect``/``close``/``error``/``scheme``) so callers only need to add
    ``await``; error handling still returns ``None`` on failure and records an
    ``ERR_*`` code (see const.py) instead of raising, matching the rest of the
    integration's defensive style. The one exception is
    ``get_subscription_events()``, which instead always returns a
    ``(events, error, is_connection_error)`` tuple -- see its own docstring.

    The recorded error is kept per asyncio task (see ``_CALL_ERRORS``), so
    reading ``error`` right after an awaited call is safe even while other
    tasks use the same instance. Read it only in the task that made the call:
    a call run in a child task (e.g. ``asyncio.create_task``) records its
    error there, so return it with the task's result instead.
    """

    def __init__(
        self,
        host: str,
        api_key: str,
        verify_ssl: bool = True,
        scheme: str = "wss",
    ) -> None:
        """Initialize the TrueNAS API.

        Parameters
        ----------
        host:
            Bare TrueNAS hostname or IP address without scheme or path
            (for example, ``"truenas.local"`` or ``"192.168.1.10"``). This
            class does not itself validate or strip a scheme/path -- callers
            (``helper.sanitize_host``) are expected to normalize user
            input before constructing this class, to avoid malformed
            WebSocket URLs.
        api_key:
            API key used to authenticate with the TrueNAS API.
        verify_ssl:
            Whether to verify the SSL certificate when using ``wss``.
        scheme:
            WebSocket scheme, either ``"ws"`` or ``"wss"`` (default).
        """
        scheme = scheme.lower()
        if scheme not in ("ws", "wss"):
            raise ValueError(
                f"Invalid WebSocket scheme '{scheme}'. Expected 'ws' or 'wss'."
            )

        self._host = host
        self._scheme = scheme
        self._error_key = object()
        self._closed = False
        self._client = TrueNASClient(
            host,
            api_key,
            verify_ssl=verify_ssl,
            use_tls=(scheme == "wss"),
        )

    # ---------------------------
    #   connect
    # ---------------------------
    async def connect(self, *, quiet: bool = False) -> bool:
        """Connect and log in. Return connected boolean.

        Parameters
        ----------
        quiet:
            Log a connection failure at debug instead of error, and pass
            ``quiet=True`` through to aiotruenas so it drops its own
            "verify_ssl=False" warning too. Used by zeroconf discovery, which
            probes many non-TrueNAS devices on the network and expects most
            connection attempts to fail -- logging those at error with a full
            traceback (or the insecure-TLS warning, which fires before the
            failure is even known) would flood the log for no benefit.
        """
        if self._closed:
            self._error = ERR_UNKNOWN
            _LOGGER.error(
                "TrueNAS %s: cannot connect, API was permanently closed", self._host
            )
            return False

        if self._client.connected:
            self._error = ""
            return True

        try:
            await self._client.connect(quiet=quiet)
        except TrueNASError as exc:
            self._error = _classify_exception(exc, during_call=False)
            _LOGGER.log(
                DEBUG if quiet else ERROR,
                "Error while communicating with host %s: %s",
                self._host,
                scrub_exception(exc),
                exc_info=None if quiet else exc,
            )
            return False

        self._error = ""
        return True

    # ---------------------------
    #   disconnect / close
    # ---------------------------
    async def disconnect(self) -> None:
        """Close the WebSocket connection (reconnectable)."""
        await self._client.close()

    async def close(self) -> None:
        """Permanently close the API."""
        self._closed = True
        await self._client.close()

    # ---------------------------
    #   connected
    # ---------------------------
    def connected(self) -> bool:
        """Return connected boolean."""
        return self._client is not None and self._client.connected

    # ---------------------------
    #   connection_test
    # ---------------------------
    async def connection_test(self) -> tuple[bool, str]:
        """Test connection."""
        if not await self.connect():
            return self.connected(), self._error

        result, error, errname = await self._query("system.info")
        if not isinstance(result, dict) or not result.get("hostname"):
            if errname == "EACCES":
                # Login worked, but the key's user lacks a role that may read
                # system.info -- say so instead of the raw TrueNAS reason,
                # which the config flow could only show as "unknown".
                error = ERR_PERMISSION_DENIED
            self._error = error or ERR_MALFORMED_RESULT
            return False, self._error

        return True, ""

    # ---------------------------
    #   query
    # ---------------------------
    async def query(
        self,
        service: str,
        params: dict[str, Any] | list[Any] | None = None,
        *,
        job: bool = False,
    ) -> Any:
        """Retrieve data from TrueNAS.

        With ``job=True`` the call is treated as a job: the client waits for
        the job to finish and returns its result (or ``None`` on failure)
        instead of the bare job id.
        """
        return (await self._query(service, params, job=job)).data

    async def _query(
        self,
        service: str,
        params: dict[str, Any] | list[Any] | None = None,
        *,
        job: bool = False,
    ) -> _CallResult:
        """Run ``query`` and also return this call's error and errname.

        The error matches what ``self.error`` then reports for the calling
        task; the errname is only returned here (e.g. "EACCES" for a failed
        call, "" on success or non-call errors), for connection_test.
        """
        if not self.connected() and not await self.connect():
            # connect() just recorded this task's connect error.
            return _CallResult(None, self._error, "")

        self._error = ""
        if _LOGGER.isEnabledFor(DEBUG):
            _LOGGER.debug(
                "TrueNAS %s query: %s, %s", self._host, service, _redact(params)
            )

        try:
            if job:
                data = await self._client.call(service, params, job=True)
            else:
                data = await self._client.call(service, params)
        except TrueNASCallError as exc:
            self._error = _call_error_text(exc)
            _log_call_error(self._host, service, exc)
            return _CallResult(None, self._error, exc.errname or "")
        except TrueNASError as exc:
            self._error = _classify_exception(exc, during_call=True)
            _LOGGER.warning(
                'TrueNAS %s unable to fetch data "%s" (%s)',
                self._host,
                service,
                scrub_exception(exc),
            )
            return _CallResult(None, self._error, "")

        if _LOGGER.isEnabledFor(DEBUG):
            _LOGGER.debug(
                "TrueNAS %s query (%s) response: %s",
                self._host,
                service,
                _summarize_payload(data),
            )

        return _CallResult(data, "", "")

    # ---------------------------
    #   subscribe_events
    # ---------------------------
    async def subscribe_events(
        self, event: str
    ) -> tuple[str, asyncio.Queue[Any]] | tuple[None, None]:
        """Subscribe to an event and return (subscription_id, queue).

        The queue yields either raw event payload dicts or aiotruenas's
        internal subscription-terminator sentinel (an unstable, underscored
        symbol we deliberately avoid importing); callers should just treat
        any non-dict item as end-of-subscription. ``asyncio.Queue`` is
        invariant, so the item type must stay structurally compatible with
        aiotruenas's own (private) return type -- ``Any`` achieves that
        without importing the private symbol; a plain ``object`` would not,
        since it is a supertype rather than a structural match.
        """
        if not self.connected() and not await self.connect():
            self._error = self._error or ERR_CONNECTION_REFUSED
            _LOGGER.warning(
                "TrueNAS %s subscribe_events: connection failed for %s",
                self._host,
                event,
            )
            return None, None

        self._error = ""
        _LOGGER.debug("TrueNAS %s subscribe_events: %s", self._host, event)

        try:
            return await self._client.subscribe(event)
        except TrueNASCallError as exc:
            self._error = _call_error_text(exc)
            _log_call_error(self._host, event, exc)
            return None, None
        except TrueNASError as exc:
            self._error = _classify_exception(exc, during_call=True)
            _LOGGER.warning(
                'TrueNAS %s unable to subscribe to events "%s" (%s)',
                self._host,
                event,
                scrub_exception(exc),
            )
            return None, None

    # ---------------------------
    #   unsubscribe_events
    # ---------------------------
    async def unsubscribe_events(self, subscription_id: str) -> None:
        """Unsubscribe from a TrueNAS event."""
        if not self.connected():
            _LOGGER.debug(
                "TrueNAS %s unsubscribe_events: client not connected,"
                " skipping unsubscribe for %s",
                self._host,
                subscription_id,
            )
            return

        _LOGGER.debug("TrueNAS %s unsubscribe_events: %s", self._host, subscription_id)

        try:
            await self._client.unsubscribe(subscription_id)
        except TrueNASCallError as exc:
            _LOGGER.debug(
                "TrueNAS %s failed to unsubscribe %s: %s",
                self._host,
                subscription_id,
                scrub_exception(exc),
            )
        except TrueNASError as exc:
            self._error = _classify_exception(exc, during_call=True)
            _LOGGER.warning(
                "TrueNAS %s unable to unsubscribe_events %s (%s)",
                self._host,
                subscription_id,
                scrub_exception(exc),
            )

    async def get_subscription_events(
        self, subscription_id: str, event_timeout: float | None = None
    ) -> tuple[list[dict[str, Any]], str, bool]:
        """Read events from a subscription queue.

        Also returns the call's own error (if any) and whether that error is
        specifically attributable to this call's own connection attempt/loss.
        A caller that can overlap with other callers on this shared
        TrueNASAPI instance (e.g. entity actions running alongside the poll)
        cannot rely on reading back self.connected() after this returns:
        another caller can change it during this call's own internal await,
        so it does not reflect whether THIS call's own failure was actually
        connection-related. is_connection_error is True for
        this call's own initial connect() check failing, for any
        TrueNASConnectionError raised mid-call (isinstance, so this also
        covers e.g. TrueNASConnectionClosedError/TrueNASUnknownError), and
        for an empty read whose subscription has gone missing mid-call (the
        client signals a disconnect by draining an internal queue sentinel
        rather than raising, so an empty result alone is ambiguous between
        "nothing new yet" and "the connection just dropped" -- checking
        is_subscribed() afterward disambiguates the two). is_connection_error
        is never True for a TrueNASCallError (a completed exchange whose
        response was an application-level error) or any other TrueNASError
        subtype, which are call-specific failures regardless of the shared
        connection state.
        """
        if not self.connected() and not await self.connect():
            # Captured into a local so the return below does not depend on
            # re-reading self._error.
            connect_error = self._error or ERR_CONNECTION_REFUSED
            self._error = connect_error
            _LOGGER.warning(
                "TrueNAS %s get_subscription_events: connection failed for %s",
                self._host,
                subscription_id,
            )
            return [], connect_error, True

        self._error = ""
        _LOGGER.debug(
            "TrueNAS %s get_subscription_events: %s", self._host, subscription_id
        )

        try:
            events = await self._client.get_subscription_events(
                subscription_id, event_timeout=event_timeout
            )
            if events:
                if _LOGGER.isEnabledFor(DEBUG):
                    _LOGGER.debug(
                        "TrueNAS %s get_subscription_events drained %d events: %s",
                        self._host,
                        len(events),
                        _summarize_payload(events),
                    )
                return events, "", False
            if not await self._client.is_subscribed(subscription_id):
                # No exception was raised -- the client drains a disconnect's
                # queue-terminator sentinel internally and returns [] the
                # same way it would for a routine "nothing new yet" timeout.
                # is_subscribed() only turns False once _teardown_connection()
                # has cleared this subscription, so an empty read plus a now-
                # missing subscription means the connection dropped during
                # this read rather than that nothing happened.
                self._error = ERR_LOST_QUERY
                _LOGGER.warning(
                    "TrueNAS %s subscription %s gone after an empty read"
                    " (connection dropped mid-read)",
                    self._host,
                    subscription_id,
                )
                return [], self._error, True
            return [], "", False
        except TrueNASCallError as exc:
            self._error = _call_error_text(exc)
            _log_call_error(self._host, subscription_id, exc)
            return [], self._error, False
        except TrueNASError as exc:
            self._error = _classify_exception(exc, during_call=True)
            _LOGGER.warning(
                "TrueNAS %s unable to read subscription events %s (%s)",
                self._host,
                subscription_id,
                scrub_exception(exc),
            )
            return [], self._error, isinstance(exc, TrueNASConnectionError)

    async def is_subscribed(self, subscription_id: str) -> bool:
        """Check if a subscription is currently active in the client."""
        if not self.connected():
            return False
        try:
            return await self._client.is_subscribed(subscription_id)
        except TrueNASError as exc:
            self._error = _classify_exception(exc, during_call=True)
            _LOGGER.warning(
                "TrueNAS %s unable to check subscription status %s (%s)",
                self._host,
                subscription_id,
                scrub_exception(exc),
            )
            return False

    @property
    def client(self) -> TrueNASClient:
        """Return the underlying TrueNASClient, e.g. for aiotruenas.TrueNASState."""
        return self._client

    @property
    def _error(self) -> str:
        """Return the calling task's last recorded error for this instance."""
        return _CALL_ERRORS.get().get(self._error_key, "")

    @_error.setter
    def _error(self, value: str) -> None:
        errors = _CALL_ERRORS.get()
        if errors.get(self._error_key, "") != value:
            _CALL_ERRORS.set(MappingProxyType({**errors, self._error_key: value}))

    @property
    def error(self) -> str:
        """Return the error of the calling task's last call ("" on success).

        Set by connect(), connection_test(), query(), subscribe_events() and
        get_subscription_events(). The best-effort is_subscribed() and
        unsubscribe_events() only record a failure and never clear it.
        """
        return self._error

    @property
    def scheme(self) -> str:
        """Return the scheme used for the WebSocket."""
        return self._scheme
