"""ODRL agent: NL-to-ODRL policy engine built on the AgentCrash framework.

This is the host application. It owns the agentic loop (``adapter``),
the synthetic tools (``tools``), ODRL-subset validation (``odrl``),
the DSPy prompt probe (``dspy_opt``), the ODRL invariants
(``verifiers``), the interactive studio server (``web``), the fixture
seeder (``seed``), and the ``odrl-host`` CLI (``cli``).

The reliability framework (SDK, control plane, generic verifiers)
lives in the ``agentcrash-core`` distribution — ``import agentcrash``.
Install it first; every ``odrl_agent`` module imports from it.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
