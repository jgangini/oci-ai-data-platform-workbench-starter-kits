"""Compatibility command for existing VM installations; use territorial_release_update."""
import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("territorial_release_update.py")), run_name="__main__")
else:
    from territorial_release_update import *
