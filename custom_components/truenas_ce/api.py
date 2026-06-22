"""TrueNAS API."""

import errno
import json
import re
import socket
import ssl
import time
from logging import DEBUG, getLogger
from threading import RLock
from typing import Any

from websockets.exceptions import (
    NegotiationError,
    WebSocketException,
)
from websockets.sync.client import ClientConnection, connect

# Current websockets (manifest pins >= 15.0.1) raise ``InvalidStatus`` on an
# HTTP reject, carrying the code on ``exc.response.status_code`` (read
# generically below). ``InvalidStatusCode`` is the legacy exception from
# websockets < 14; import it defensively so status-code extraction keeps
# working on older installs too.
try:  # pragma: no cover - depends on installed websockets version
    from websockets.exceptions import InvalidStatusCode
except ImportError:  # pragma: no cover - removed in newer websockets
    InvalidStatusCode = None

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
    ERR_PROXY_INTERCEPTED,
    ERR_TIMEOUT,
    ERR_TLS_NOT_SUPPORTED,
    ERR_UNKNOWN,
    ERR_UNKNOWN_HOSTNAME,
    ERR_WS_NOT_SUPPORTED,
    ERROR_API_FORMAT,
    QUERY_TIMEOUT,
)

_LOGGER = getLogger(__name__)

# Maximum number of characters of an API payload to include in debug logs.
# Full payloads can be huge (e.g. pool.query with topology or app.query), so
# they are summarized and truncated to keep debug logs readable.
_LOG_PAYLOAD_LIMIT = 500


def _summarize_payload(data: Any, limit: int = _LOG_PAYLOAD_LIMIT) -> str:
    """Return a compact, length-bounded description of an API payload."""
    if isinstance(data, list):
        shape = f"list[{len(data)}]"
    elif isinstance(data, dict):
        shape = f"dict[{len(data)} keys]"
    else:
        shape = type(data).__name__

    text = repr(data)
    if len(text) > limit:
        text = f"{text[:limit]}... (truncated, {len(text)} chars total)"
    return f"{shape} {text}"


# ---------------------------
#   TrueNASAPI
# ---------------------------
class TrueNASAPI:
    """Handle all communication with TrueNAS."""

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
            (for example, ``"truenas.local"`` or ``"192.168.1.10"``).
            Values containing a URL scheme (``"://"``) or path (``"/"``)
            are rejected to prevent malformed WebSocket URLs.
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

        # Require bare host (no scheme, no path) to avoid malformed URLs
        if "://" in host or "/" in host:
            raise ValueError(
                "Invalid host value. Expected a bare hostname or IP address "
                'without scheme or path (for example, "truenas.local" or "192.168.1.1")'
            )

        self._host = host
        self._api_key = api_key
        self._scheme = scheme
        self._url = f"{self._scheme}://{self._host}/websocket"

        # The SSL context is built lazily on first connect (which runs in an
        # executor thread), not here: ssl.create_default_context() performs
        # blocking certificate loading and __init__ runs inside the event loop
        # during async_setup_entry, which Home Assistant flags as a blocking call.
        self._verify_ssl = verify_ssl
        self._ssl_context: ssl.SSLContext | None = None

        self._lock = RLock()
        # Lock ordering: If both locks are needed, _io_lock MUST be acquired
        # BEFORE _lock to prevent deadlocks. _lock is for fast state updates only.
        self._io_lock = RLock()

        # Websocket / connection state.
        # All direct websocket I/O must hold _io_lock.
        # Fast state attributes may be read under either lock, but writes must
        # follow lock ordering (_io_lock first, then _lock if both are needed).
        self._connected = False
        self._error = ""
        self._closed = False
        self._error_logged = False
        self._binary_warning_logged = False
        self._next_rpc_id = 1
        self._ws: ClientConnection | None = None

    # ------------------------------------------------------------------
    # Internal helpers for lock-disciplined access to connection state.
    # These helpers both document and enforce the expected lock ownership.
    # ------------------------------------------------------------------
    def _assert_holds_lock(self) -> None:
        """Best-effort assertion that the current thread holds _lock.

        NOTE: RLock does not expose ownership, so this is intentionally
        a no-op for now. It exists to document intent and allows us to
        later add stronger checking (e.g. via a debug-only wrapper)
        without touching all call sites.
        """
        # Intentionally left as a no-op; see docstring.

    def _assert_holds_io_lock(self) -> None:
        """Best-effort assertion that the current thread holds _io_lock.

        As with `_assert_holds_lock`, this is currently a no-op because
        `RLock` does not expose ownership. It documents the intended
        locking discipline and allows us to tighten checks later without
        modifying all call sites.
        """
        # Intentionally left as a no-op; see docstring.

    # ------------------------------------------------------------------
    # WebSocket I/O helpers.
    # All direct websocket recv/send should go through these helpers to
    # ensure `_io_lock` is consistently held and the discipline cannot
    # be accidentally violated by new call sites.
    # ------------------------------------------------------------------
    def _recv_locked(self, timeout: float | None = None) -> Any:
        """Receive a message from the websocket under `_io_lock`.

        This helper centralizes `ws.recv()` so that:
        * `_io_lock` is always held during the call.
        * Call sites don't need to reason about low-level I/O locking.
        """
        with self._io_lock:
            self._assert_holds_io_lock()
            ws = self._get_ws()
            if ws is None:
                raise ConnectionError("Websocket connection is not established")

            return ws.recv(timeout=timeout)

    def _send_locked(self, payload: str) -> None:
        """Send a message to the websocket under `_io_lock`."""
        with self._io_lock:
            self._assert_holds_io_lock()
            ws = self._get_ws()
            if ws is None:
                raise ConnectionError("Websocket connection is not established")

            ws.send(payload)

    # State helpers (require _lock, and if used together with I/O, _io_lock first).
    def _set_connected(self, value: bool) -> None:
        """Set connection flag.

        Caller must hold _lock (and _io_lock first if both are needed).
        """
        self._assert_holds_lock()
        self._connected = value

    def _is_connected(self) -> bool:
        """Read connection flag. Caller should hold _lock or _io_lock."""
        return self._connected

    def _set_error(self, message: str) -> None:
        """Set error message.

        Caller must hold _lock (and _io_lock first if both are needed).
        """
        self._assert_holds_lock()
        self._error = message

    def _get_error(self) -> str:
        """Read error message. Caller should hold _lock or _io_lock."""
        return self._error

    def _set_closed(self, value: bool) -> None:
        """Set closed flag.

        Caller must hold _lock (and _io_lock first if both are needed).
        """
        self._assert_holds_lock()
        self._closed = value

    def _is_closed(self) -> bool:
        """Read closed flag. Caller should hold _lock or _io_lock."""
        return self._closed

    def _next_rpc_id_locked(self) -> int:
        """Increment and return the next RPC id.

        Caller must hold _lock (and _io_lock first if both are needed).
        """
        self._assert_holds_lock()
        rpc_id = self._next_rpc_id
        self._next_rpc_id += 1
        return rpc_id

    # Websocket helpers (require _io_lock, and _io_lock must be taken before _lock).
    def _set_ws(self, ws: ClientConnection | None) -> None:
        """Set websocket connection. Caller must hold _io_lock."""
        self._assert_holds_io_lock()
        self._ws = ws

    def _get_ws(self) -> ClientConnection | None:
        """Get websocket connection. Caller should hold _io_lock."""
        return self._ws

    def _extract_status_code(self, exc: BaseException) -> int | None:
        """Try to extract a numeric HTTP status code from a known exception shape.

        Modern ``websockets`` (>= 14) raises ``InvalidStatus`` carrying the code
        on ``exc.response.status_code``; older releases raised
        ``InvalidStatusCode`` with ``exc.status_code``. Only attributes known to
        represent HTTP status codes are used, to avoid misinterpreting unrelated
        numeric values (e.g. errno).
        """
        # websockets >= 14: InvalidStatus carries a Response object.
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            return status_code

        # websockets < 14 legacy: InvalidStatusCode exposes status_code directly.
        if InvalidStatusCode is not None and isinstance(exc, InvalidStatusCode):
            return exc.status_code

        if isinstance(exc, WebSocketException):
            status = getattr(exc, "status", None)
            if isinstance(status, int):
                return status
            status_code = getattr(exc, "status_code", None)
            if isinstance(status_code, int):
                return status_code
        return None

    def _get_connection_error(self, exception: Exception) -> str | None:
        # 1. Evaluate by exception type
        match exception:
            case ssl.SSLCertVerificationError():
                return ERR_CERT_VERIFY_FAILED

            case socket.gaierror():
                err_no = getattr(exception, "errno", None)
                eai_noname = getattr(socket, "EAI_NONAME", None)
                eai_nodata = getattr(socket, "EAI_NODATA", None)
                if err_no is not None and err_no in {eai_noname, eai_nodata}:
                    return ERR_UNKNOWN_HOSTNAME

            case ConnectionRefusedError():
                return ERR_CONNECTION_REFUSED

            case TimeoutError():
                return ERR_HANDSHAKE_TIMEOUT

            case OSError() if getattr(exception, "errno", None) is not None:
                match exception.errno:
                    case errno.ECONNREFUSED:
                        return ERR_CONNECTION_REFUSED
                    case errno.ETIMEDOUT:
                        return ERR_HANDSHAKE_TIMEOUT
                    case err if err == getattr(errno, "EHOSTUNREACH", None):
                        return ERR_CONNECTION_REFUSED

        # 2. Evaluate by HTTP status code
        match self._extract_status_code(exception):
            case 301 | 302 | 303 | 307 | 308:
                # A redirect on the WebSocket handshake means something other
                # than TrueNAS answered: typically a reverse proxy / SSO portal
                # (e.g. Cloudflare Access) sending the request to a login page
                # before it ever reaches TrueNAS.
                return ERR_PROXY_INTERCEPTED
            case 401 | 403:
                # Auth rejected at the HTTP handshake, before the TrueNAS login
                # RPC. TrueNAS upgrades the WebSocket regardless of key validity
                # and only checks the key afterwards, so a 401/403 here is the
                # proxy/gateway refusing the connection, not a bad API key.
                return ERR_PROXY_INTERCEPTED
            case 404:
                return ERR_API_NOT_FOUND

        # 3. Evaluate by text contents (fallback string matching)
        #
        # WARNING:
        # This fallback logic is deliberately bound to specific text fragments
        # of the exception messages (e.g. from websockets, SSL, or socket stack).
        # Changes in library versions or localization of these messages
        # can cause this matching to break.
        # If structured information (error codes, specific exception types, etc.)
        # becomes available in the future, prefer that over string matching.
        #
        # For unknown or non-matching cases, there is a safe default fallback below.
        normalized = str(exception).strip().lower()

        if "certificate_verify_failed" in normalized:
            return ERR_CERT_VERIFY_FAILED
        if "plain http request was sent to https port" in normalized:
            return ERR_HTTP_USED
        if "tlsv1_unrecognized_name" in normalized:
            return ERR_TLS_NOT_SUPPORTED
        if "no websocket upgrade" in normalized:
            return ERR_WS_NOT_SUPPORTED
        if "connection refused" in normalized or "no route to host" in normalized:
            return ERR_CONNECTION_REFUSED
        if "timed out while waiting for handshake response" in normalized:
            return ERR_HANDSHAKE_TIMEOUT

        # Safe fallback for unrecognized error texts.
        return ERR_UNKNOWN

    def _log_error(
        self,
        message: str,
        exception: Exception,
        *,
        logged_flag: dict | None = None,
    ) -> None:
        """Log an error only once per scope.

        Uses a fixed format to avoid formatting issues.
        """
        if logged_flag is not None:
            if logged_flag.get("logged"):
                return
            _LOGGER.error(
                "Error while communicating with host %s: %s",
                self._host,
                message,
                exc_info=exception,
            )
            logged_flag["logged"] = True
            return

        if not self._error_logged:
            _LOGGER.error(
                "Error while communicating with host %s: %s",
                self._host,
                message,
                exc_info=exception,
            )

        self._error_logged = True

    def _fail_connection(
        self, message: str, error: Exception, error_state: dict
    ) -> bool:
        """Log fatal error and disconnect."""
        self._log_error(message, error, logged_flag=error_state)
        self.disconnect()
        return False

    def _handle_query_error(self, res: dict) -> bool:
        """Handle JSON-RPC query error response."""
        error = res.get("error")
        if not error:
            return False

        if not isinstance(error, dict):
            with self._lock:
                self._set_error(str(error))
            _LOGGER.error(ERROR_API_FORMAT, self._host, error)
            return True

        err_message = error.get("message")
        data = error.get("data")
        reason = data.get("reason") if isinstance(data, dict) else None

        with self._lock:
            self._set_error(reason or err_message or ERR_UNKNOWN)

        if err_message and reason:
            _LOGGER.error(
                ERROR_API_FORMAT,
                self._host,
                err_message,
                reason,
            )
        elif err_message:
            _LOGGER.error(
                ERROR_API_FORMAT,
                self._host,
                err_message,
            )
        else:
            _LOGGER.error(
                ERROR_API_FORMAT,
                self._host,
                error,
            )

        return True

    # ---------------------------
    #   _establish_websocket
    # ---------------------------
    def _establish_websocket(self, kwargs: dict[str, Any]) -> ClientConnection:
        """Establish the WebSocket connection, handling subprotocol negotiation."""
        try:
            return connect(self._url, **kwargs)
        except NegotiationError as e:
            if "no subprotocols supported" not in str(e):
                raise e

            kwargs["subprotocols"] = ["__extract__"]
            try:
                return connect(self._url, **kwargs)
            except NegotiationError as e2:
                return self._handle_negotiation_error(e2, kwargs)

    def _handle_negotiation_error(
        self, e2: NegotiationError, kwargs: dict[str, Any]
    ) -> ClientConnection:
        """Extract the expected subprotocol and retry the connection."""
        msg2 = str(e2)

        # We currently have to rely on the error message format from the
        # websockets library. Parse this defensively and bail out if we cannot
        # safely extract a reasonable subprotocol token.
        marker = "unsupported subprotocol: "
        parts = msg2.split(marker, 1)
        if len(parts) < 2:
            return self._fallback_negotiation_retry(
                "Failed to parse NegotiationError subprotocol (missing '%s'): %s",
                marker,
                msg2,
                kwargs,
            )
        tail = parts[1].strip()
        # Strip at common delimiters to avoid feeding arbitrary or composite
        # values back into connect. This keeps only the first token-like value.
        for sep in (",", ";", " "):
            if sep in tail:
                tail = tail.split(sep, 1)[0].strip()

        subp = tail
        if not subp or not re.match(r"^[a-zA-Z0-9_\-\.]+$", subp):
            return self._fallback_negotiation_retry(
                "Extracted NegotiationError subprotocol is invalid: %r (msg: %s)",
                subp,
                msg2,
                kwargs,
            )
        kwargs["subprotocols"] = [subp]
        return connect(self._url, **kwargs)

    def _fallback_negotiation_retry(
        self, log_msg: str, arg: str, error_msg: str, kwargs: dict[str, Any]
    ) -> Any:
        """Handle fallback for negotiation protocol errors."""
        _LOGGER.warning(log_msg, arg, error_msg)
        kwargs.pop("subprotocols", None)
        return connect(self._url, **kwargs)

    def _wait_for_message(
        self,
        match_key: str,
        match_val: str,
        timeout_msg: str,
        error_state: dict,
        timeout: float = 10.0,
    ) -> dict[str, Any] | None:
        """Wait for a specific message from WebSocket.

        All websocket I/O goes through _recv_locked().
        Note: The caller holds _io_lock, ensuring no concurrent RPCs are in flight.
        Any unmatched messages are unsolicited server events and can be safely ignored.
        """
        deadline = time.time() + timeout
        while True:
            timeout = deadline - time.time()
            try:
                if timeout <= 0:
                    raise TimeoutError
                message = self._recv_locked(timeout=timeout)
            except TimeoutError as e:
                self._fail_connection(timeout_msg, e, error_state)
                return None

            # Handle binary / non-text messages: emit a one-time warning and skip
            if not isinstance(message, str):
                if not self._binary_warning_logged:
                    _LOGGER.warning(
                        "Received non-text WebSocket message from TrueNAS; "
                        "binary frames are ignored. This may indicate a protocol "
                        "or configuration change."
                    )
                    self._binary_warning_logged = True
                _LOGGER.debug(
                    "TrueNAS %s: ignoring non-string WebSocket message: %r",
                    self._host,
                    message,
                )
                continue

            try:
                candidate = json.loads(message)
            except json.JSONDecodeError:
                continue

            if str(candidate.get(match_key)) == match_val:
                return candidate

            _LOGGER.debug(
                "TrueNAS %s: received unrelated WebSocket message "
                "while waiting for %s=%r: %s",
                self._host,
                match_key,
                match_val,
                candidate,
            )

    def _log_login_failure(self, result: dict) -> None:
        """Extract and log login failure details efficiently."""
        error_reason = result.get("reason")
        error_message = result.get("message")

        if not error_reason or not error_message:
            match result.get("error") or result.get("errors"):
                case dict() as err_obj:
                    error_reason = (
                        error_reason or err_obj.get("reason") or err_obj.get("code")
                    )
                    error_message = (
                        error_message or err_obj.get("message") or err_obj.get("detail")
                    )
                case [dict() as first_err, *_] if first_err:
                    error_reason = (
                        error_reason or first_err.get("reason") or first_err.get("code")
                    )
                    error_message = (
                        error_message
                        or first_err.get("message")
                        or first_err.get("detail")
                    )

        response_type = (
            result.get("response_type") if isinstance(result, dict) else None
        )

        if not (error_reason or error_message or response_type):
            _LOGGER.warning(
                "TrueNAS login failed for %s with non-success response.", self._host
            )
            res_str = str(result)
            _LOGGER.debug(
                "TrueNAS login failure payload for %s: %s",
                self._host,
                f"{res_str[:250]}..." if len(res_str) > 250 else res_str,
            )
            return

        # Complexity fix: Build the log string flat in advance
        reason_prefix = f"{error_reason}: " if error_reason else ""
        details_msg = f"{reason_prefix}{error_message or ''}"

        _LOGGER.warning(
            "TrueNAS login failed for %s. Check API key and permissions.", self._host
        )
        _LOGGER.debug(
            "TrueNAS login failure details for %s: response_type=%s, %s",
            self._host,
            response_type,
            details_msg,
        )

    # ---------------------------
    #   _perform_login
    # ---------------------------
    def _perform_login(self, error_state: dict) -> bool:
        """Perform login and return boolean success.

        This method is responsible for acquiring ``_io_lock`` for all
        login-related I/O. Callers MUST NOT hold ``_io_lock`` when invoking
        this method.
        """
        with self._lock:
            rpc_id = self._next_rpc_id_locked()

        with self._lock:
            ws = self._get_ws()

        if not ws:
            # Set error instead of silently aborting
            with self._lock:
                self._set_error(ERR_UNKNOWN)
            return False

        with self._io_lock:
            # Acquire _lock inside _io_lock (following lock ordering)
            with self._lock:
                if self._get_ws() is not ws:
                    self._set_error(ERR_LOST_LOGIN)
                    _LOGGER.warning(
                        "TrueNAS %s WebSocket changed mid-login", self._host
                    )
                    return False

            try:
                self._send_locked(
                    json.dumps({"msg": "connect", "version": "1", "support": ["1"]})
                )
            except (OSError, WebSocketException) as e:
                return self._fail_connection(
                    "failed to send connect message", e, error_state
                )

            if not self._wait_for_message(
                "msg",
                "connected",
                "timeout while waiting for connect response",
                error_state,
                timeout=30.0,
            ):
                return False

            payload = {
                "msg": "method",
                "method": "auth.login_with_api_key",
                "id": str(rpc_id),
                "params": [self._api_key],
            }

            try:
                self._send_locked(json.dumps(payload))
            except (OSError, WebSocketException) as e:
                return self._fail_connection(
                    "failed to send login message", e, error_state
                )

            res = self._wait_for_message(
                "id",
                str(rpc_id),
                "timeout while waiting for login response",
                error_state,
                timeout=30.0,
            )
            if res is None:
                return False

        result = res.get("result")
        connected = False

        if isinstance(result, dict):
            if result.get("response_type") == "SUCCESS":
                connected = True
            else:
                self._log_login_failure(result)
        elif isinstance(result, bool):
            connected = result
        else:
            _LOGGER.warning(
                "Unexpected TrueNAS login result type for %s: %r",
                self._host,
                result,
            )

        if not connected:
            with self._lock:
                self._set_error(ERR_INVALID_KEY)
            self.disconnect()

        return connected

    def _handle_connection_exception(
        self, exception: Exception, error_state: dict
    ) -> bool:
        """Handle connection setup exceptions and log the failure."""
        with self._lock:
            self._set_error(self._get_connection_error(exception) or ERR_UNKNOWN)
        self._log_error("failed to connect", exception, logged_flag=error_state)
        return False

    def _connect_websocket_with_retries(
        self, kwargs: dict[str, Any], error_state: dict
    ) -> ClientConnection | None:
        """Try to connect websocket, with one retry on timeout.

        TrueNAS may briefly hold a connection slot open after a clean close
        (e.g. during an integration reload). 5 s matches the delay that has
        been observed to reliably let TrueNAS finish its internal cleanup
        before accepting a new connection.
        """
        _TIMEOUT_RETRY_DELAY = 5.0
        for attempt in range(2):
            try:
                return self._establish_websocket(kwargs)
            except TimeoutError as e:
                if attempt != 0:
                    self._handle_connection_exception(e, error_state)
                    return None
                _LOGGER.debug(
                    "TrueNAS %s: WebSocket handshake timed out on first attempt; "
                    "retrying in %.0fs (server may be briefly at connection limit)",
                    self._host,
                    _TIMEOUT_RETRY_DELAY,
                )
                time.sleep(_TIMEOUT_RETRY_DELAY)
            except (OSError, WebSocketException) as e:
                self._handle_connection_exception(e, error_state)
                return None
        return None

    # ---------------------------
    #   connect
    # ---------------------------
    def _create_ssl_context(self) -> ssl.SSLContext:
        """Build the WebSocket SSL context.

        Blocking (loads system certificates) so it must run off the event loop;
        ``connect`` is always invoked via ``async_add_executor_job``.
        """
        context = ssl.create_default_context()  # noqa: S4423 # NOSONAR
        if self._verify_ssl:
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
        else:
            # Insecure configuration: certificate validation and hostname
            # checking are disabled. This must only be used in trusted
            # environments (e.g. local networks) and never exposed to untrusted
            # networks, as it is vulnerable to MITM attacks.
            _LOGGER.warning(
                "TrueNAS WebSocket configured with verify_ssl=False for '%s'. "
                "This disables TLS certificate verification and hostname "
                "checking and should only be used in trusted environments.",
                self._host,
            )
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def connect(self) -> bool:
        """Return connected boolean."""
        with self._lock:
            if self._is_connected():
                return True
            if self._is_closed():
                return False
            self._set_error("")
            self._error_logged = False

        error_state = {"logged": False}

        kwargs = {
            "max_size": 16777216,
            "ping_interval": 20,
            "open_timeout": 10,
        }
        if self._scheme == "wss":
            if self._ssl_context is None:
                self._ssl_context = self._create_ssl_context()
            kwargs["ssl"] = self._ssl_context

        ws = self._connect_websocket_with_retries(kwargs, error_state)
        if ws is None:
            return False

        # Prepare state for the new websocket connection.
        # We do not hold _io_lock here; _perform_login manages it.
        with self._io_lock:
            with self._lock:
                self._set_ws(ws)

        try:
            connected = self._perform_login(error_state)
        except (OSError, WebSocketException) as e:
            return self._fail_connection("failed to login", e, error_state)

        if not connected:
            self.disconnect()
            return False

        with self._lock:
            self._set_connected(True)
            return True

    # ---------------------------
    #   disconnect
    # ---------------------------
    def disconnect(self) -> None:
        """Close the WebSocket connection."""
        with self._io_lock:
            with self._lock:
                ws = self._get_ws()
                self._set_ws(None)
                self._set_connected(False)

        if ws:
            try:
                ws.close()
            except Exception as exc:
                _LOGGER.debug("Error while closing WebSocket connection: %s", exc)

    # ---------------------------
    #   close
    # ---------------------------
    def close(self) -> None:
        """Permanently close the API and prevent reconnection."""
        with self._lock:
            self._set_closed(True)
        self.disconnect()

    # ---------------------------
    #   reconnect
    # ---------------------------
    def reconnect(self) -> bool:
        """Return connected boolean."""
        self.disconnect()
        return self.connect()

    # ---------------------------
    #   connected
    # ---------------------------
    def connected(self) -> bool:
        """Return connected boolean."""
        with self._lock:
            return self._is_connected()

    # ---------------------------
    #   connection_test
    # ---------------------------
    def connection_test(self) -> tuple[bool, str]:
        """Test connection."""
        if not self.connect():
            with self._lock:
                return self._is_connected(), self._get_error()

        try:
            result = self.query("system.info")
        except (OSError, WebSocketException) as exc:
            with self._lock:
                self._set_connected(False)
                self._set_error(self._get_connection_error(exc) or ERR_UNKNOWN)
                return self._is_connected(), self._get_error()

        if result is None:
            self.disconnect()
            with self._lock:
                self._set_connected(False)
                if not self._get_error():
                    self._set_error(ERR_MALFORMED_RESULT)

        with self._lock:
            return self._is_connected(), self._get_error()

    # ---------------------------
    #   query
    # ---------------------------
    def _parse_websocket_message(
        self, message: str, rpc_id: int
    ) -> dict[str, Any] | None:
        """Helper: Validate and filter a single WebSocket message."""
        if not isinstance(message, str):
            if not self._binary_warning_logged:
                _LOGGER.warning(
                    "Received non-text WebSocket message; binary frames are ignored."
                )
                self._binary_warning_logged = True
            _LOGGER.debug("Ignoring non-text WebSocket message: %r", message)
            return None

        try:
            candidate = json.loads(message)
        except json.JSONDecodeError as exc:
            _LOGGER.debug(
                "TrueNAS %s: ignoring malformed JSON: %r (%s)", self._host, message, exc
            )
            return None

        # Spam protection: Filter frequent event messages directly
        if candidate.get("msg") == "event":
            return None

        # Success: RPC ID matches
        if str(candidate.get("id")) == str(rpc_id):
            return candidate

        return {"_unrelated": True, "id": candidate.get("id")}

    def _read_query_response(self, rpc_id: int) -> dict[str, Any] | None:
        """Wait for and filter WebSocket messages matching the RPC ID."""
        deadline = time.time() + QUERY_TIMEOUT

        while True:
            timeout = deadline - time.time()
            try:
                if timeout <= 0:
                    raise TimeoutError
                message = self._recv_locked(timeout=timeout)
            except TimeoutError:
                _LOGGER.error(
                    "TrueNAS %s timeout while waiting for query response",
                    self._host,
                )
                with self._lock:
                    self._set_error(ERR_TIMEOUT)
                self.disconnect()
                return None

            res = self._parse_websocket_message(message, rpc_id)
            if not res:
                continue

            # If it was an unrelated ID, skip it silently
            if res.get("_unrelated"):
                continue

            return res

    def query(
        self,
        service: str,
        params: dict[str, Any] | list[Any] | None = None,
    ) -> list | dict | str | None:
        """Retrieve data from TrueNAS."""
        if not self.connected() and not self.connect():
            return None

        with self._io_lock, self._lock:
            self._set_error("")
            ws = self._get_ws()
            if not self._is_connected() or not ws:
                return None
            rpc_id = self._next_rpc_id_locked()

        try:
            _LOGGER.debug("TrueNAS %s query: %s, %s", self._host, service, params)

            # Flat, unnested statement instead of a one-liner (Sonar S3358).
            if params is None:
                clean_params = []
            elif isinstance(params, list):
                clean_params = params
            else:
                clean_params = [params]

            payload = {
                "msg": "method",
                "method": service,
                "id": str(rpc_id),
                "params": clean_params,
            }

            with self._io_lock:
                with self._lock:
                    if self._get_ws() is not ws:
                        self._set_error(ERR_LOST_QUERY)
                        _LOGGER.warning(
                            "TrueNAS %s WebSocket changed mid-query for %s",
                            self._host,
                            service,
                        )
                        return None

                self._send_locked(json.dumps(payload))
                res = self._read_query_response(rpc_id)

            if res is not None and self._handle_query_error(res):
                return None

            # Precise check: allows empty containers such as [] or {}.
            if res is None or "result" not in res:
                return None

            data = res.get("result")
            if _LOGGER.isEnabledFor(DEBUG):
                _LOGGER.debug(
                    "TrueNAS %s query (%s) response: %s",
                    self._host,
                    service,
                    _summarize_payload(data),
                )
        except (OSError, WebSocketException) as e:
            _LOGGER.warning(
                'TrueNAS %s unable to fetch data "%s" (%s)',
                self._host,
                service,
                e,
                # noqa: G004
            )
            self.disconnect()
            with self._lock:
                self._set_error(self._get_connection_error(e) or ERR_UNKNOWN)
            return None

        return data

    @property
    def error(self):
        """Return error."""
        with self._lock:
            return self._get_error()

    @property
    def scheme(self) -> str:
        """Return the scheme used for the WebSocket."""
        return self._scheme
