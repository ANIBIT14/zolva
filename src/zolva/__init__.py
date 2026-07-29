"""Zolva: self-hosted agent platform for banks and fintechs."""

from zolva.audit import AuditLog, AuditStore, InMemoryAuditStore, Scorecard, scorecard
from zolva.audit_postgres import PostgresAuditStore
from zolva.bus import Bus, Step, Verdict
from zolva.channels import (
    ChannelAdapter,
    ChannelError,
    ChannelHub,
    InboundMessage,
    LogChannel,
    WebhookChannel,
)
from zolva.compliance import ComplianceReport, Control, build_report, config_hash
from zolva.config import AgentConfig, ConfigError, load_agents
from zolva.evals import EvalReport, EvalRunner, load_cohorts
from zolva.feedback import Failure, FeedbackQueue
from zolva.guardrails import Guardrails
from zolva.handover import HandoverBackend, LogBackend, Ticket, WebhookBackend
from zolva.orchestrator import BLOCKED_MESSAGE, AgentApp
from zolva.otel import OTelExporter
from zolva.redaction import Redactor
from zolva.signing import SignatureError, sign_payload, verify_zolva_signature
from zolva.synthetics import SyntheticResult, SyntheticRunner, load_synthetics
from zolva.tools import ToolRegistry, default_registry, tool

__version__ = "0.5.1"

__all__ = [
    "BLOCKED_MESSAGE",
    "AgentApp",
    "AgentConfig",
    "AuditLog",
    "AuditStore",
    "Bus",
    "ChannelAdapter",
    "ChannelError",
    "ChannelHub",
    "ComplianceReport",
    "ConfigError",
    "Control",
    "EvalReport",
    "EvalRunner",
    "Failure",
    "FeedbackQueue",
    "Guardrails",
    "InMemoryAuditStore",
    "HandoverBackend",
    "InboundMessage",
    "LogBackend",
    "LogChannel",
    "OTelExporter",
    "PostgresAuditStore",
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
    "build_report",
    "config_hash",
    "default_registry",
    "load_agents",
    "load_cohorts",
    "load_synthetics",
    "scorecard",
    "sign_payload",
    "tool",
    "verify_zolva_signature",
]
