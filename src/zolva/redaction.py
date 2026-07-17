"""Redaction plugin: mask configured PII in everything sent to an LLM provider.

Session history, audit, and handover keep the real content by design (humans
and regulators need the true transcript); only the adapter-bound copy is
masked. Patterns are config, not code: enable builtins and add your own via
a YAML file, then pass it to `AgentApp.from_config(..., redaction=...)`.

# ponytail: regex-based detection; an NER/LLM detector can plug in later by
# subclassing Redactor, the orchestrator seam stays the same.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from zolva.bridge import LLMAdapter, LLMResponse, Message
from zolva.config import ConfigError
from zolva.tools import ToolSpec

BUILTIN_PATTERNS: dict[str, str] = {
    # 13-19 digit card numbers, allowing space/dash group separators
    "card": r"\b(?:\d[ -]?){12,18}\d\b",
    "email": r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b",
    # phone numbers: optional +country, 10-13 digits with separators
    "phone": r"(?<!\d)\+?\d[\d -]{8,12}\d(?!\d)",
    "aadhaar": r"\b\d{4} \d{4} \d{4}\b",
    "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
}


class Redactor:
    def __init__(self, patterns: dict[str, str]) -> None:
        if not patterns:
            raise ConfigError("redaction: no patterns enabled")
        self._compiled: list[tuple[str, re.Pattern[str]]] = []
        for name, pattern in patterns.items():
            try:
                self._compiled.append((name, re.compile(pattern)))
            except re.error as e:
                raise ConfigError(f"redaction: invalid regex for {name!r}: {e}") from e

    @classmethod
    def from_file(cls, path: str | Path) -> Redactor:
        """YAML mapping: `builtin: [card, email, ...]` and/or `custom: {name: regex}`."""
        raw = yaml.safe_load(Path(path).read_text())
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: redaction file must be a mapping")
        patterns: dict[str, str] = {}
        for name in raw.get("builtin") or []:
            if name not in BUILTIN_PATTERNS:
                raise ConfigError(
                    f"{path}: unknown builtin pattern {name!r} "
                    f"(known: {', '.join(sorted(BUILTIN_PATTERNS))})"
                )
            patterns[str(name)] = BUILTIN_PATTERNS[name]
        custom = raw.get("custom") or {}
        if not isinstance(custom, dict):
            raise ConfigError(f"{path}: 'custom' must be a mapping of name -> regex")
        for name, pattern in custom.items():
            patterns[str(name)] = str(pattern)
        if not patterns:
            raise ConfigError(f"{path}: redaction file enables nothing")
        return cls(patterns)

    def redact(self, text: str) -> str:
        for name, compiled in self._compiled:
            text = compiled.sub(f"[REDACTED:{name}]", text)
        return text

    def redact_messages(self, messages: list[Message]) -> list[Message]:
        """New Message objects; never mutates inputs, they live in the session store."""
        return [m.model_copy(update={"content": self.redact(m.content)}) for m in messages]


class RedactingAdapter:
    """Wraps any LLMAdapter; masks system + messages on the way out.

    Tool results (role `tool`) are redacted too, they may carry account data.
    Tool specs pass through untouched, schemas contain no customer content.
    """

    def __init__(self, inner: LLMAdapter, redactor: Redactor) -> None:
        self._inner = inner
        self._redactor = redactor

    async def complete(
        self, *, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse:
        return await self._inner.complete(
            model=model,
            system=self._redactor.redact(system),
            messages=self._redactor.redact_messages(messages),
            tools=tools,
        )


def load_redactor(config_dir: str | Path, redaction: str) -> Redactor:
    """Resolve a redaction file declared relative to the config dir."""
    path = Path(config_dir) / redaction
    if not path.is_file():
        raise ConfigError(f"redaction file not found: {path}")
    return Redactor.from_file(path)


__all__: list[str] = ["BUILTIN_PATTERNS", "RedactingAdapter", "Redactor", "load_redactor"]
