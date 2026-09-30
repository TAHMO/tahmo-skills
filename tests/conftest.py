"""Shared helpers for per-skill correctness tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"


def load_skill(skill_dir_name, script_stem):
    """Import a skill script module by directory name and script stem."""
    path = SKILLS_ROOT / skill_dir_name / "scripts" / f"{script_stem}.py"
    if not path.is_file():
        raise FileNotFoundError(path)
    mod_name = f"skill_{skill_dir_name}_{script_stem}".replace("-", "_")
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load skill script: {path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses can resolve the module's namespace.
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def run_skill(fn, *argv):
    """Invoke a decorated skill with an argv list (in-process)."""
    return fn(list(argv))
