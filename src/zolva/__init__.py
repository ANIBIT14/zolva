"""Zolva: self-hosted agent platform for banks and fintechs."""

from zolva.audit import AuditLog, Scorecard, scorecard
from zolva.bus import Bus, Step, Verdict
from zolva.channels import (
    ChannelAdapter,
    ChannelError,
    ChannelHub,
    InboundMessage,
    LogChannel,
    WebhookChannel,
)
from zolva.config import AgentConfig, ConfigError, load_agents
from zolva.evals import EvalReport, EvalRunner, load_cohorts
from zolva.feedback import Failure, FeedbackQueue
from zolva.guardrails import Guardrails
from zolva.handover import HandoverBackend, LogBackend, Ticket, WebhookBackend
from zolva.orchestrator import BLOCKED_MESSAGE, AgentApp
from zolva.redaction import Redactor
from zolva.signing import SignatureError, sign_payload, verify_zolva_signature
from zolva.synthetics import SyntheticResult, SyntheticRunner, load_synthetics
from zolva.tools import ToolRegistry, default_registry, tool

__version__ = "0.3.2"

__all__ = [
    "BLOCKED_MESSAGE",
    "AgentApp",
    "AgentConfig",
    "AuditLog",
    "Bus",
    "ChannelAdapter",
    "ChannelError",
    "ChannelHub",
    "ConfigError",
    "EvalReport",
    "EvalRunner",
    "Failure",
    "FeedbackQueue",
    "Guardrails",
    "HandoverBackend",
    "InboundMessage",
    "LogBackend",
    "LogChannel",
    "Redactor",
    "Scorecard",
    "SignatureError",
    "Step",
    "SyntheticResult",
    "SyntheticRunner",
    "Ticket",
    "ToolRegistry",
    "Verdict",
    "WebhookBackend",
    "WebhookChannel",
    "default_registry",
    "load_agents",
    "load_cohorts",
    "load_synthetics",
    "scorecard",
    "sign_payload",
    "tool",
    "verify_zolva_signature",
]
