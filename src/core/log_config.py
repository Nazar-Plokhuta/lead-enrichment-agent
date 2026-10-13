"""Logging bootstrap for the API process."""

from __future__ import annotations

import logging

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(level: str) -> None:
    """Route application logs to stderr at *level*.

    uvicorn configures only its own loggers, so without this every
    ``logger.info`` in the pipeline is dropped and ``LOG_LEVEL`` has no effect.
    ``basicConfig`` does nothing when the root logger already has handlers (a
    host that configured logging first keeps its setup), so the level is also
    set explicitly.  The CLI installs its own Rich handler in ``src.main``.

    Raises:
        ValueError: *level* is not a standard logging level name.
    """
    logging.basicConfig(level=level.upper(), format=_FORMAT)
    logging.getLogger().setLevel(level.upper())
