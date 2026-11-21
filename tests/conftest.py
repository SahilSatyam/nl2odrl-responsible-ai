"""Shared fixtures."""
import pytest

from odrl_agent.grounder import TRUSTED_PARTIES_ENV
from odrl_agent.verifiers import register_odrl_verifiers
from odrl_agent.web import SETTINGS


@pytest.fixture(autouse=True)
def _odrl_verifiers():
    """agentcrash.cli.main re-registers core built-ins; restore the ODRL binding."""
    register_odrl_verifiers()
    SETTINGS.reset()
    yield
    SETTINGS.reset()


@pytest.fixture(autouse=True)
def _trusted_issuer(monkeypatch):
    """The synthetic fixtures' issuing party comes from deployment context."""
    monkeypatch.setenv(TRUSTED_PARTIES_ENV, "data-steward")
