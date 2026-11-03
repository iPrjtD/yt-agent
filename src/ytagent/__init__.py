"""Autonomous YouTube content agent."""

from ytagent import tls

__version__ = "0.1.0"

tls.install()  # before any HTTP client is created (see tls.py)
