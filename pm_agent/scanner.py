"""Forward-declaration stub for Finding. Full impl in Task 3."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal


@dataclass
class Finding:
    bug_id: str
    title: str
    severity: Literal["Critical", "High", "Medium", "Low"]
    paths: list[str]
    acceptance: list[str]
    evidence: str
    kind: Literal["bug", "tech-debt"]
