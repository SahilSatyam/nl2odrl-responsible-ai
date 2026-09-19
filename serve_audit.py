#!/usr/bin/env python3
"""Run the auditor portal. Does not start a converter or PEP."""

from rai_card.api import serve

if __name__ == "__main__":
    serve()
