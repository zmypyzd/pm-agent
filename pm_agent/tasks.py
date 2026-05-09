"""Shared CoderTask schema, used by both planner and TUI.

allowed_paths and acceptance default to empty so day-3/4 single-mode and
day-5 mock decomposition still construct CoderTask without changes; the
real planner (day 6) populates them.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CoderTask:
    id: str
    title: str
    prompt: str
    allowed_paths: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
