"""Shared types for the ELRO Connects Real-time integration."""

from __future__ import annotations

from .hub import ElroConnectsHub
from .k2_hub import ElroK2Hub

# Both hub implementations expose the same interface to the coordinator, the
# entity platforms and the services: a dict of ElroDevice plus update callbacks.
ElroHub = ElroConnectsHub | ElroK2Hub

__all__ = ["ElroConnectsHub", "ElroHub", "ElroK2Hub"]
