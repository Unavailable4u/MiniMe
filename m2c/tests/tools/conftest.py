"""
m2c/tests/tools/conftest.py -- Patch C05: shared fixtures for the tool tests.

* ``proj``, ``ctx`` and ``outside`` come from tools_fixtures.py (the helpers
  themselves are imported by name in each module).
* ``caplog`` is wrapped so it still sees the ``m2c`` logger after another test
  module has called ``configure_logging()`` (which turns propagation off, so
  pytest's root-logger handler would otherwise capture nothing).  The handler
  is attached to the ``m2c`` logger directly and propagation is held off for the
  duration, so each record is captured exactly once either way.
"""
import logging

import pytest
from tools_fixtures import ctx, outside, proj  # noqa: F401  (fixtures)


@pytest.fixture
def caplog(caplog):
    logger = logging.getLogger("m2c")
    saved_propagate, saved_level = logger.propagate, logger.level
    logger.addHandler(caplog.handler)
    logger.propagate = False
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)
        logger.propagate = saved_propagate
        logger.setLevel(saved_level)
