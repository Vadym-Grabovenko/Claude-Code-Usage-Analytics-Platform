"""Normalize raw provider model ids to a stable ``model_family``.

Providers version models with date / revision suffixes (``gpt-4o-2024-08-06``,
``claude-sonnet-4-5-20250929``, ``gemini-2.0-flash-001``, ``models/gemini-2.5-pro``).
Pricing, leaderboards and filters key on the family so a dated snapshot rolls up to
its model. Rules are an ordered regex table: add a row, not code, for a new pattern.
"""
from __future__ import annotations

import re

# (provider or None for "any", compiled pattern, replacement) — applied in order.
_RULES: list[tuple[str | None, re.Pattern[str], str]] = [
    (None, re.compile(r"^models/"), ""),                              # Gemini API resource prefix
    (None, re.compile(r"^(?:openai|google|anthropic|deepseek)/"), ""),  # gateway vendor prefixes
    (None, re.compile(r"-\d{4}-\d{2}-\d{2}$"), ""),                   # OpenAI: -2024-08-06
    (None, re.compile(r"-\d{8}$"), ""),                               # Anthropic: -20250929
    (None, re.compile(r"-latest$"), ""),                              # *-latest aliases
    ("gemini", re.compile(r"-preview(?:-\d{2}-\d{2,4})?$"), ""),      # -preview-05-06
    ("gemini", re.compile(r"-\d{3}$"), ""),                           # -001 revisions
]


def normalize_model(provider: str | None, raw: str | None) -> str | None:
    """``normalize_model('openai', 'gpt-4o-2024-08-06') -> 'gpt-4o'``.

    Unknown patterns pass through lower-cased and trimmed; ``None``/blank -> ``None``.
    """
    if raw is None:
        return None
    model = str(raw).strip().lower()
    if not model:
        return None
    for rule_provider, pattern, repl in _RULES:
        if rule_provider is None or rule_provider == provider:
            model = pattern.sub(repl, model)
    return model or None
