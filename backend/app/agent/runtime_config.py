from __future__ import annotations

import os
from dataclasses import dataclass


def _float(name: str, default: float) -> float:
    try: return max(0.01, float(os.getenv(name, default)))
    except (TypeError, ValueError): return default


def _int(name: str, default: int) -> int:
    try: return max(0, int(os.getenv(name, default)))
    except (TypeError, ValueError): return default


@dataclass(frozen=True)
class InvestigationRuntimeConfig:
    planner_timeout_seconds: float = _float("PLANNER_TIMEOUT_SECONDS", 60)
    tool_timeout_seconds: float = _float("TOOL_TIMEOUT_SECONDS", 30)
    investigation_timeout_seconds: float = _float("INVESTIGATION_TIMEOUT_SECONDS", 180)
    planner_max_retries: int = _int("PLANNER_MAX_RETRIES", 0)
    poll_interval_seconds: float = _float("INVESTIGATION_POLL_INTERVAL_SECONDS", 0.25)

