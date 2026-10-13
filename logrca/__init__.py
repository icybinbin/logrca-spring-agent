"""A small, interview-oriented log root-cause-analysis Agent demo.

The default implementation is deliberately offline and deterministic so the
whole project can be run, tested, and explained without an API key.  The Agent
runtime and tool contracts are real; only the reasoning policy is replaceable.
"""

from .agents import IncidentManager
from .models import DiagnosisReport, Incident, LogEvent
from .synthetic import SCENARIOS, build_scenario

__all__ = [
    "DiagnosisReport",
    "Incident",
    "IncidentManager",
    "LogEvent",
    "SCENARIOS",
    "build_scenario",
]

__version__ = "1.0.0"
