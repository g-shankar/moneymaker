"""Daily pipeline package: scan, council handoff, and execution.

Entry point: ``python -m pipeline.daily (--scan-only | --execute FILE | --full)``.
"""

from pipeline.common import REPO_ROOT, load_config, resolve

__all__ = ["REPO_ROOT", "load_config", "resolve"]
