"""Compatibility namespace for existing local integrations; use app.territorial."""
from pathlib import Path

__path__ = [str(Path(__file__).resolve().parent.parent / "territorial")]
