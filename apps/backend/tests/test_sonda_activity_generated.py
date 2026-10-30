"""The unused backend helper must match the canonical source bundle exactly."""
import importlib.util
from pathlib import Path


def test_generated_helper_matches_its_canonical_sources():
    root=Path(__file__).resolve().parents[3]
    spec=importlib.util.spec_from_file_location('render_sonda_activity_test',root/'scripts/render_sonda_activity.py')
    renderer=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    assert renderer.TARGET.read_bytes()==renderer.rendered()
