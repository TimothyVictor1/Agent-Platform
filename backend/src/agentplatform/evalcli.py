"""Launch the repository's evaluation runner via ``agentplatform-eval``."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> None:
    # evals is a repository package outside the installed backend source tree.
    root = str(Path(__file__).resolve().parents[3])
    if root not in sys.path:
        sys.path.insert(0, root)
    runpy.run_module("evals.runner", run_name="__main__")
