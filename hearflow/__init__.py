"""HearFlow Studio application package."""

from __future__ import annotations

import logging

__all__ = ["__version__"]

__version__ = "0.1.0"

logging.getLogger("hearflow").addHandler(logging.NullHandler())
