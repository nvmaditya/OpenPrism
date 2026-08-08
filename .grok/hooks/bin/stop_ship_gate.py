#!/usr/bin/env python3
"""Stop hook entry: always run auto_ship for OpenPrism."""
from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("auto_ship.py")), run_name="__main__")
