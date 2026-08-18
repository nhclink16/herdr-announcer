#!/usr/bin/env python3
"""Compatibility entry point for the announcer dashboard."""

import sys

from announcer import dashboard as _implementation


if __name__ == "__main__":
    sys.exit(_implementation.main())

# Preserve imports such as ``import dashboard`` and every name callers reached
# through that module before the implementation moved into the package.
sys.modules[__name__] = _implementation
