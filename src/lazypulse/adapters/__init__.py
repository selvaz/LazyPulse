"""Inbound adapters for LazyPulse.

The :class:`Adapter` protocol and the dependency-light adapters live here.
``WebhookAdapter`` (needs ``starlette``) and the Gmail adapter (needs the
Google client libraries) are imported lazily from the top-level package so
``import lazypulse`` never pulls those extras.
"""

from __future__ import annotations

from lazypulse.adapters.base import Adapter

__all__ = ["Adapter"]
