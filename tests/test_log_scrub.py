"""Tests for the integration-wide secret-scrubbing log filter."""

from __future__ import annotations

import importlib
import logging
import pkgutil

import pytest

import custom_components.truenas_ce as package
from custom_components.truenas_ce import api as api_module
from custom_components.truenas_ce.api import (
    SECRET_SCRUB_FILTER,
    scrub_exception,
    scrub_text,
)

_SAMPLE = "PW-SAMPLE"


def _module_loggers() -> list[tuple[str, logging.Logger]]:
    loggers = []
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        logger = getattr(module, "_LOGGER", None)
        if isinstance(logger, logging.Logger):
            loggers.append((info.name, logger))
    loggers.append(("__init__", package._LOGGER))
    return loggers


def test_every_module_logger_has_the_scrub_filter() -> None:
    """Guard: a new module logger must attach ``SECRET_SCRUB_FILTER`` too.

    Logger filters do not apply to records propagated from child loggers, so
    every module logger needs its own reference to the shared filter.
    """
    loggers = _module_loggers()
    assert len(loggers) > 10
    missing = [
        name for name, logger in loggers if SECRET_SCRUB_FILTER not in logger.filters
    ]
    assert not missing


def test_filter_scrubs_logger_exception_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_LOGGER.exception`` in a broad handler cannot leak the message."""
    logger = api_module._LOGGER
    with caplog.at_level("ERROR", logger=logger.name):
        try:
            try:
                raise OSError(f"proxy http://u:{_SAMPLE}@proxy refused")
            except OSError as inner:
                raise RuntimeError(f"failed: password={_SAMPLE}") from inner
        except RuntimeError:
            logger.exception("job %s failed", f"token={_SAMPLE}")

    assert caplog.records[0].exc_info
    assert "token=**REDACTED**" in caplog.text
    assert _SAMPLE not in caplog.text


def test_filter_keeps_format_placeholders_and_non_string_args(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = api_module._LOGGER
    with caplog.at_level("WARNING", logger=logger.name):
        logger.warning("password=%s count=%d", "x", 3)
    assert caplog.records[0].getMessage() == "password=x count=3"


def test_scrub_exception_walks_exception_groups() -> None:
    member = ValueError(f"api_key={_SAMPLE}")
    group = ExceptionGroup("several", [member, KeyError("plain")])
    scrub_exception(group)
    assert str(member) == "api_key=**REDACTED**"


def test_scrub_exception_scrubs_nested_args() -> None:
    exc = RuntimeError("failed", {"detail": f"secret={_SAMPLE}"}, [f"token={_SAMPLE}"])
    scrub_exception(exc)
    assert exc.args[1] == {"detail": "secret=**REDACTED**"}
    assert exc.args[2] == ["token=**REDACTED**"]
    assert exc.args[0] == "failed"


def test_scrub_exception_handles_cause_cycles() -> None:
    first = RuntimeError(f"password={_SAMPLE}")
    second = RuntimeError("second")
    first.__context__ = second
    second.__context__ = first
    scrub_exception(first)
    assert _SAMPLE not in str(first)


def test_scrub_text_masks_quoted_value_with_escaped_quote() -> None:
    assert scrub_text(r'password="ab\"cd" end') == 'password="**REDACTED**" end'


def test_scrub_text_masks_equals_form_flags() -> None:
    assert scrub_text("rclone --token=abc x") == "rclone --token=**REDACTED** x"


def test_filter_scrubs_mapping_args(caplog: pytest.LogCaptureFixture) -> None:
    logger = api_module._LOGGER
    with caplog.at_level("WARNING", logger=logger.name):
        logger.warning("value %(a)s", {"a": f"token={_SAMPLE}"})
    assert caplog.records[0].getMessage() == "value token=**REDACTED**"


def test_filter_scrubs_exception_logged_as_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = api_module._LOGGER
    with caplog.at_level("ERROR", logger=logger.name):
        logger.error(ValueError(f"token={_SAMPLE}"))
    assert caplog.records[0].getMessage() == "token=**REDACTED**"


@pytest.mark.parametrize(
    "arg",
    [
        ValueError(f"token={_SAMPLE}"),
        ["plain", ValueError(f"token={_SAMPLE}")],
        {"err": ValueError(f"token={_SAMPLE}")},
    ],
)
def test_filter_scrubs_exceptions_nested_in_args(
    caplog: pytest.LogCaptureFixture, arg: object
) -> None:
    logger = api_module._LOGGER
    with caplog.at_level("WARNING", logger=logger.name):
        logger.warning("failed: %r", arg)
    message = caplog.records[0].getMessage()
    assert _SAMPLE not in message
    assert "token=**REDACTED**" in message


@pytest.mark.parametrize("module", ["apiparser", "event_push"])
def test_filter_scrubs_records_of_standalone_module_loggers(
    caplog: pytest.LogCaptureFixture, module: str
) -> None:
    """``apiparser``/``event_push`` get the filter by name from ``api.py``."""
    logger = logging.getLogger(f"custom_components.truenas_ce.{module}")
    with caplog.at_level("WARNING", logger=logger.name):
        logger.warning("detail %s", f"token={_SAMPLE}")
    assert caplog.records[0].getMessage() == "detail token=**REDACTED**"


def test_filter_fails_closed_when_scrubbing_raises(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scrubbing error withholds the arguments, never drops/leaks the record."""

    def _boom(_data: object) -> object:
        raise ValueError("scrubber broke")

    monkeypatch.setattr(api_module, "scrub_strings", _boom)
    logger = api_module._LOGGER
    with caplog.at_level("WARNING", logger=logger.name):
        logger.warning("value %s", f"token={_SAMPLE}")
    assert len(caplog.records) == 1
    assert _SAMPLE not in caplog.text
    assert "secret scrubbing failed (ValueError)" in caplog.text


def test_filter_scrubs_self_referencing_args(
    caplog: pytest.LogCaptureFixture,
) -> None:
    looped: list[object] = [f"token={_SAMPLE}"]
    looped.append(looped)
    logger = api_module._LOGGER
    with caplog.at_level("WARNING", logger=logger.name):
        logger.warning("value %s", looped)
    assert caplog.records[0].getMessage() == "value ['token=**REDACTED**', '...']"


def test_scrub_exception_survives_cyclic_args() -> None:
    """Coordinator handlers call ``scrub_exception`` directly; it must not raise."""
    looped: dict[str, object] = {"detail": f"secret={_SAMPLE}"}
    looped["self"] = looped
    exc = RuntimeError("failed", looped)
    scrub_exception(exc)
    assert exc.args[1] == {"detail": "secret=**REDACTED**", "self": "..."}


def test_scrub_exception_keeps_shared_non_cyclic_containers() -> None:
    shared = [f"token={_SAMPLE}"]
    exc = RuntimeError(shared, shared)
    scrub_exception(exc)
    assert exc.args == (["token=**REDACTED**"], ["token=**REDACTED**"])


def test_scrub_exception_scrubs_bytes_oserror_filename() -> None:
    exc = FileNotFoundError(2, "No such file", f"/mnt/token={_SAMPLE}".encode())
    scrub_exception(exc)
    assert exc.filename == "/mnt/token=**REDACTED**"
    assert _SAMPLE not in str(exc)


@pytest.mark.parametrize(
    "arg",
    [
        f"token={_SAMPLE}".encode(),
        bytearray(f"token={_SAMPLE}".encode()),
        [f"token={_SAMPLE}".encode()],
    ],
)
def test_filter_scrubs_bytes_args(
    caplog: pytest.LogCaptureFixture, arg: object
) -> None:
    logger = api_module._LOGGER
    with caplog.at_level("WARNING", logger=logger.name):
        logger.warning("value %s", arg)
    message = caplog.records[0].getMessage()
    assert _SAMPLE not in message
    assert "token=**REDACTED**" in message


def test_scrub_exception_survives_mixed_and_self_cycles() -> None:
    outer: list[object] = [f"password={_SAMPLE}"]
    outer.append({"back": outer})
    exc = RuntimeError("failed", outer)
    exc.args = (*exc.args, exc)
    scrub_exception(exc)
    assert exc.args[1] == ["password=**REDACTED**", {"back": "..."}]
    assert exc.args[2] is exc


def test_scrub_exception_fails_closed_on_deep_nesting() -> None:
    """Deep but acyclic args are cut off instead of raising RecursionError."""
    nested: list[object] = [f"token={_SAMPLE}"]
    for _ in range(5000):
        nested = [nested]
    exc = RuntimeError(nested)
    scrub_exception(exc)
    assert _SAMPLE not in repr(exc.args)


def test_scrub_exception_scrubs_bytearray_oserror_filename2() -> None:
    exc = OSError(2, "rename failed", "/a", None, bytearray(b"/b/secret=x1"))
    scrub_exception(exc)
    assert exc.filename2 == "/b/secret=**REDACTED**"


def test_scrub_exception_keeps_plain_bytes_filename() -> None:
    exc = FileNotFoundError(2, "No such file", b"/mnt/data/file")
    scrub_exception(exc)
    assert exc.filename == b"/mnt/data/file"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('password="abc def', "password=**REDACTED**"),
        ("password='abc def", "password=**REDACTED**"),
        ('password="abc\\', "password=**REDACTED**"),
        (r"token='a\'b' tail", "token='**REDACTED**' tail"),
        (r'secret="a\\" tail', 'secret="**REDACTED**" tail'),
    ],
)
def test_scrub_text_masks_unterminated_and_escaped_quotes(
    text: str, expected: str
) -> None:
    assert scrub_text(text) == expected
