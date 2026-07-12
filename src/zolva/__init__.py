"""Zolva: self-hosted agent platform for banks and fintechs."""

from zolva.bus import Bus, Step, Verdict
from zolva.config import AgentConfig, ConfigError, load_agents
from zolva.handover import HandoverBackend, LogBackend, Ticket, WebhookBackend
from zolva.orchestrator import BLOCKED_MESSAGE, AgentApp
from zolva.tools import ToolRegistry, default_registry, tool

__version__ = "0.1.0"

__all__ = [
    "BLOCKED_MESSAGE",
    "AgentApp",
    "AgentConfig",
    "Bus",
    "ConfigError",
    "HandoverBackend",
    "LogBackend",
    "Step",
    "Ticket",
    "ToolRegistry",
    "Verdict",
    "WebhookBackend",
    "default_registry",
    "load_agents",
    "tool",
]
