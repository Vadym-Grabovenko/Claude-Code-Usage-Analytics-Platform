"""Provider adapters: each maps one provider's raw usage log to canonical event rows.

``REGISTRY`` is the single list of supported providers (order = load order; the
Claude Code adapter comes first). Adding a provider = add a module with a
``BaseAdapter`` subclass and register it here — the ETL/queries need no changes.
"""
from __future__ import annotations

from pathlib import Path

from .base import (
    CANONICAL_COLUMNS,
    CANONICAL_SCHEMA,
    BaseAdapter,
    ProviderAdapter,
    SessionAssigner,
    empty_row,
)
from .claude_code import ClaudeCodeAdapter
from .deepseek import DeepSeekAdapter
from .gemini import GeminiAdapter
from .models import normalize_model
from .openai import OpenAIAdapter

REGISTRY: dict[str, type[BaseAdapter]] = {
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    OpenAIAdapter.name: OpenAIAdapter,
    GeminiAdapter.name: GeminiAdapter,
    DeepSeekAdapter.name: DeepSeekAdapter,
}

PROVIDER_NAMES: list[str] = list(REGISTRY)
PROVIDER_LABELS: dict[str, str] = {name: cls.label for name, cls in REGISTRY.items()}


def validate_providers(names: list[str] | None) -> list[str]:
    """Return ``names`` (deduped, registry order) or raise ``ValueError`` listing valid ones."""
    if not names:
        return list(PROVIDER_NAMES)
    unknown = sorted(set(names) - set(REGISTRY))
    if unknown:
        raise ValueError(f"unknown provider(s) {unknown}; valid: {PROVIDER_NAMES}")
    return [n for n in PROVIDER_NAMES if n in set(names)]


def get_adapters(
    enabled: list[str] | None = None,
    paths: dict[str, dict[str, Path | str]] | None = None,
) -> list[BaseAdapter]:
    """Instantiate the enabled adapters. ``paths`` overrides per provider, e.g.
    ``{"openai": {"events": "x.jsonl"}}``."""
    paths = paths or {}
    return [REGISTRY[name](paths.get(name)) for name in validate_providers(enabled)]


__all__ = [
    "CANONICAL_COLUMNS", "CANONICAL_SCHEMA",
    "BaseAdapter", "ProviderAdapter", "SessionAssigner", "empty_row", "normalize_model",
    "ClaudeCodeAdapter", "OpenAIAdapter", "GeminiAdapter", "DeepSeekAdapter",
    "REGISTRY", "PROVIDER_NAMES", "PROVIDER_LABELS", "validate_providers", "get_adapters",
]
