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

_SECRET = "PW-SAMPLE"


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
                raise OSError(f"proxy http://u:{_SECRET}@proxy refused")
            except OSError as inner:
                raise RuntimeError(f"failed: password={_SECRET}") from inner
        except RuntimeError:
            logger.exception("job %s failed", f"token={_SECRET}")

    assert caplog.records[0].exc_info
    assert "token=**REDACTED**" in caplog.text
    assert _SECRET not in caplog.text


def test_filter_keeps_format_placeholders_and_non_string_args(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = api_module._LOGGER
    with caplog.at_level("WARNING", logger=logger.name):
        logger.warning("password=%s count=%d", "x", 3)
    assert caplog.records[0].getMessage() == "password=x count=3"


def test_scrub_exception_walks_exception_groups() -> None:
    member = ValueError(f"api_key={_SECRET}")
    group = ExceptionGroup("several", [member, KeyError("plain")])
    scrub_exception(group)
    assert str(member) == "api_key=**REDACTED**"


def test_scrub_exception_scrubs_nested_args() -> None:
    exc = RuntimeError("failed", {"detail": f"secret={_SECRET}"}, [f"token={_SECRET}"])
    scrub_exception(exc)
    assert exc.args[1] == {"detail": "secret=**REDACTED**"}
    assert exc.args[2] == ["token=**REDACTED**"]
    assert exc.args[0] == "failed"


def test_scrub_exception_handles_cause_cycles() -> None:
    first = RuntimeError(f"password={_SECRET}")
    second = RuntimeError("second")
    first.__context__ = second
    second.__context__ = first
    scrub_exception(first)
    assert _SECRET not in str(first)


def test_scrub_text_masks_quoted_value_with_escaped_quote() -> None:
    assert scrub_text(r'password="ab\"cd" end') == 'password="**REDACTED**" end'


def test_scrub_text_masks_equals_form_flags() -> None:
    assert scrub_text("rclone --token=abc x") == "rclone --token=**REDACTED** x"


def test_filter_scrubs_mapping_args(caplog: pytest.LogCaptureFixture) -> None:
    logger = api_module._LOGGER
    with caplog.at_level("WARNING", logger=logger.name):
        logger.warning("value %(a)s", {"a": f"token={_SECRET}"})
    assert caplog.records[0].getMessage() == "value token=**REDACTED**"


def test_filter_scrubs_exception_logged_as_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = api_module._LOGGER
    with caplog.at_level("ERROR", logger=logger.name):
        logger.error(ValueError(f"token={_SECRET}"))
    assert caplog.records[0].getMessage() == "token=**REDACTED**"


@pytest.mark.parametrize(
    "arg",
    [
        ValueError(f"token={_SECRET}"),
        ["plain", ValueError(f"token={_SECRET}")],
        {"err": ValueError(f"token={_SECRET}")},
    ],
)
def test_filter_scrubs_exceptions_nested_in_args(
    caplog: pytest.LogCaptureFixture, arg: object
) -> None:
    logger = api_module._LOGGER
    with caplog.at_level("WARNING", logger=logger.name):
        logger.warning("failed: %r", arg)
    message = caplog.records[0].getMessage()
    assert _SECRET not in message
    assert "token=**REDACTED**" in message


@pytest.mark.parametrize("module", ["apiparser", "event_push"])
def test_filter_scrubs_records_of_standalone_module_loggers(
    caplog: pytest.LogCaptureFixture, module: str
) -> None:
    """``apiparser``/``event_push`` get the filter by name from ``api.py``."""
    logger = logging.getLogger(f"custom_components.truenas_ce.{module}")
    with caplog.at_level("WARNING", logger=logger.name):
        logger.warning("detail %s", f"token={_SECRET}")
    assert caplog.records[0].getMessage() == "detail token=**REDACTED**"


def test_filter_fails_closed_when_scrubbing_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A self-referencing argument withholds the arguments, never drops/leaks."""
    looped: list[object] = [f"token={_SECRET}"]
    looped.append(looped)
    logger = api_module._LOGGER
    with caplog.at_level("WARNING", logger=logger.name):
        logger.warning("value %s", looped)
    assert len(caplog.records) == 1
    assert _SECRET not in caplog.text
    assert "secret scrubbing failed (RecursionError)" in caplog.text


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
