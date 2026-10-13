"""API logging bootstrap."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from src.core.log_config import configure_logging


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    yield
    root.setLevel(level)
    root.handlers[:] = handlers


@pytest.mark.usefixtures("restore_root_logger")
@pytest.mark.parametrize(
    ("name", "expected"), [("debug", logging.DEBUG), ("INFO", logging.INFO)]
)
def test_level_is_applied_even_when_handlers_already_exist(
    name: str, expected: int
) -> None:
    assert logging.getLogger().handlers  # pytest's capture handlers
    configure_logging(name)
    assert logging.getLogger().level == expected


@pytest.mark.usefixtures("restore_root_logger")
def test_application_info_logs_are_emitted_after_configuration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging("INFO")
    with caplog.at_level(logging.INFO):
        logging.getLogger("src.services.enrichment").info("Pipeline start")
    assert "Pipeline start" in caplog.text


@pytest.mark.usefixtures("restore_root_logger")
def test_unknown_level_fails_fast() -> None:
    with pytest.raises(ValueError):
        configure_logging("LOUD")
