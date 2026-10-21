"""``agentplatform-runtime`` entrypoint. T13 replaces the body with the runtime process."""

from __future__ import annotations

import sys


def main() -> None:
    sys.stderr.write("agentplatform-runtime is not implemented yet (task T13)\n")
    raise SystemExit(2)
