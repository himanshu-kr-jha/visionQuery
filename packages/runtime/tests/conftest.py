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
    factory = logging.getLogRecordFactory()
    hooks = sys.excepthook, threading.excepthook
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.setLogRecordFactory(factory)
    sys.excepthook, threading.excepthook = hooks
    logging.captureWarnings(False)
