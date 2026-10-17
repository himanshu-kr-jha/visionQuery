"""Fixtures shared by the runtime's tests."""

import logging
import sys
import threading
from collections.abc import Iterator

import pytest


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    pinned = {name: logging.getLogger(name).level for name in ("psycopg",)}
    factory = logging.getLogRecordFactory()
    make_record = logging.Logger.makeRecord
    hooks = sys.excepthook, threading.excepthook
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, pinned_level in pinned.items():
        logging.getLogger(name).setLevel(pinned_level)
    logging.setLogRecordFactory(factory)
    logging.Logger.makeRecord = make_record
    sys.excepthook, threading.excepthook = hooks
    logging.captureWarnings(False)
