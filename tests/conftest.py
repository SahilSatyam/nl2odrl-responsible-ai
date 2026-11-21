"""Shared fixtures."""

import pytest

from odrl_agent.verifiers import register_odrl_verifiers
from odrl_agent.web import SETTINGS


@pytest.fixture(autouse=True)
def _odrl_verifiers():
    """agentcrash.cli.main re-registers core built-ins; restore the ODRL binding."""
    register_odrl_verifiers()
    SETTINGS.reset()
    yield
    SETTINGS.reset()
