#!/usr/bin/env python3
"""Compatibility entry point; the implementation lives in gentoo_agent.prepare."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gentoo_agent.prepare import main  # noqa: E402

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit("Interrupted")
