#!/usr/bin/env python3
"""
Generate synthetic AI-assistant usage data: Claude Code telemetry plus
ChatGPT/OpenAI, Gemini and DeepSeek usage logs for the same employees.

Produces realistic telemetry events matching the structure and distributions
of real Claude Code usage. All user identifiers are synthetic.

Outputs (in --output-dir): telemetry_logs.jsonl + employees.csv (Claude Code, unchanged
format and byte-identical for a given seed), openai_usage.jsonl, gemini_usage.jsonl,
deepseek_usage.csv. Model parameters live in MODELS / MODEL_CATALOG.

Usage:
    python3 generate_fake_data.py [--num-users 100] [--num-sessions 5000] [--days 60]
                                  [--providers claude_code,openai,gemini,deepseek]
"""

import argparse
import csv
import hashlib
import json
import math
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Configuration: distributions derived from real data exploration
# ---------------------------------------------------------------------------

# Models with weights (proportional to real counts) and their stats.
# Claude Code reports cost itself, so its cost_usd keeps the original sampled
# distribution (avg_cost/cost_std) -- this keeps telemetry_logs.jsonl byte-identical
# for a given seed. The *_per_mtok prices (USD per 1M tokens, illustrative) are listed
# so the catalog is uniform across providers (see MODEL_CATALOG below).
MODELS = {
    "claude-haiku-4-5-20251001": {
        "weight": 362,
        "input_per_mtok": 1.00, "cached_input_per_mtok": 0.10, "output_per_mtok": 5.00,
        "avg_cost": 0.0033, "cost_std": 0.005,
        "avg_duration_ms": 5330, "duration_std": 4000,
        "avg_input_tokens": 860, "input_std": 1200,
        "avg_output_tokens": 105, "output_std": 150,
        "avg_cache_read": 7431, "cache_read_std": 15000,
        "avg_cache_create": 941, "cache_create_std": 2000,
    },
    "claude-opus-4-6": {
        "weight": 203,
        "input_per_mtok": 5.00, "cached_input_per_mtok": 0.50, "output_per_mtok": 25.00,
        "avg_cost": 0.071, "cost_std": 0.08,
        "avg_duration_ms": 10230, "duration_std": 8000,
        "avg_input_tokens": 263, "input_std": 500,
        "avg_output_tokens": 454, "output_std": 400,
        "avg_cache_read": 73099, "cache_read_std": 50000,
        "avg_cache_create": 3149, "cache_create_std": 5000,
    },
    "claude-opus-4-5-20251101": {
        "weight": 185,
        "input_per_mtok": 5.00, "cached_input_per_mtok": 0.50, "output_per_mtok": 25.00,
        "avg_cost": 0.084, "cost_std": 0.09,
        "avg_duration_ms": 10793, "duration_std": 8000,
        "avg_input_tokens": 61, "input_std": 200,
        "avg_output_tokens": 418, "output_std": 400,
        "avg_cache_read": 76688, "cache_read_std": 50000,
        "avg_cache_create": 5485, "cache_create_std": 8000,
    },
    "claude-sonnet-4-5-20250929": {
        "weight": 155,
        "input_per_mtok": 3.00, "cached_input_per_mtok": 0.30, "output_per_mtok": 15.00,
        "avg_cost": 0.062, "cost_std": 0.07,
        "avg_duration_ms": 11886, "duration_std": 9000,
        "avg_input_tokens": 83, "input_std": 200,
        "avg_output_tokens": 516, "output_std": 500,
        "avg_cache_read": 68556, "cache_read_std": 50000,
        "avg_cache_create": 6483, "cache_create_std": 8000,
    },
    "claude-sonnet-4-6": {
        "weight": 21,
        "input_per_mtok": 3.00, "cached_input_per_mtok": 0.30, "output_per_mtok": 15.00,
        "avg_cost": 0.066, "cost_std": 0.07,
        "avg_duration_ms": 9914, "duration_std": 8000,
        "avg_input_tokens": 142, "input_std": 300,
        "avg_output_tokens": 460, "output_std": 400,
        "avg_cache_read": 70715, "cache_read_std": 50000,
        "avg_cache_create": 2905, "cache_create_std": 5000,
    },
}

# Tool weights from real tool_decision data
TOOLS = {
    "Read": 190,
    "Bash": 176,
    "Edit": 79,
    "Grep": 47,
    "Glob": 29,
    "mcp_tool": 26,
    "Write": 18,
    "TodoWrite": 16,
    "TaskUpdate": 12,
    "Task": 11,
    "TaskCreate": 6,
    "AskUserQuestion": 4,
    "WebFetch": 2,
    "ToolSearch": 2,
    "WebSearch": 2,
    "NotebookEdit": 1,
    "ExitPlanMode": 1,
}

# Decision sources with weights
DECISION_SOURCES = {
    "config": 80,
    "user_temporary": 15,
    "user_permanent": 3,
    "user_reject": 2,
}

# Tool success rates (approximate from real data)
TOOL_SUCCESS_RATES = {
    "Read": 0.986,
    "Bash": 0.933,
    "Edit": 0.99,
    "Grep": 0.99,
    "Glob": 0.99,
    "mcp_tool": 0.968,
    "Write": 0.99,
    "TodoWrite": 0.99,
    "TaskUpdate": 0.99,
    "Task": 0.99,
    "TaskCreate": 0.99,
    "AskUserQuestion": 0.99,
    "WebFetch": 0.95,
    "ToolSearch": 0.99,
    "WebSearch": 0.95,
    "NotebookEdit": 0.99,
    "ExitPlanMode": 0.99,
}

# Tool average durations (ms)
TOOL_AVG_DURATIONS = {
    "Read": 34,
    "Bash": 5169,
    "Edit": 1817,
    "Grep": 474,
    "Glob": 750,
    "mcp_tool": 2531,
    "Write": 349,
    "TodoWrite": 17,
    "TaskUpdate": 1,
    "Task": 476282,
    "TaskCreate": 2,
    "AskUserQuestion": 0,
    "WebFetch": 4968,
    "ToolSearch": 1,
    "WebSearch": 193087,
    "NotebookEdit": 11,
    "ExitPlanMode": 1,
}

# API error messages with weights
API_ERRORS = [
    ("Request was aborted.", "undefined", 44),
    ("This request would exceed your account's rate limit. Please try again later.", "429", 19),
    ("output_config: Extra inputs are not permitted", "400", 6),
    ("Internal server error", "500", 4),
    ("tools: Tool names must be unique.", "400", 4),
    ("Could not load credentials from any providers", "undefined", 3),
    ("OAuth token has expired. Please obtain a new token or refresh your existing token.", "401", 2),
    ("400 The provided request is not valid", "400", 2),
]

# Scope versions with weights
SCOPE_VERSIONS = [
    ("2.1.39", 159), ("2.1.50", 151), ("2.0.76", 121), ("2.1.37", 111),
    ("2.1.45", 108), ("2.1.49", 79), ("2.1.42", 73), ("2.1.29", 69),
    ("2.1.34", 68), ("2.1.47", 67), ("2.1.56", 64), ("2.1.31", 64),
    ("2.1.12", 59), ("2.1.44", 59), ("2.1.2", 58), ("2.1.38", 54),
    ("2.1.59", 54), ("2.1.19", 48), ("2.1.20", 47), ("2.1.41", 45),
]

# Terminal types
TERMINAL_TYPES = [
    ("vscode", 40), ("pycharm", 20), ("WarpTerminal", 10),
    ("webstorm", 8), ("iTerm2", 7), ("Terminal", 5),
    ("intellij", 5), ("cursor", 3), ("goland", 2),
]

# OS types
OS_CONFIGS = [
    ({"arch": "arm64", "os_type": "darwin", "os_version": "24.6.0"}, 60),
    ({"arch": "arm64", "os_type": "darwin", "os_version": "24.5.0"}, 15),
    ({"arch": "x86_64", "os_type": "linux", "os_version": "6.1.0"}, 10),
    ({"arch": "x86_64", "os_type": "linux", "os_version": "5.15.0"}, 5),
    ({"arch": "arm64", "os_type": "darwin", "os_version": "25.0.0"}, 5),
    ({"arch": "x86_64", "os_type": "darwin", "os_version": "24.6.0"}, 3),
    ({"arch": "x86_64", "os_type": "windows", "os_version": "10.0.22631"}, 2),
]

# Engineering practices
PRACTICES = [
    "Platform Engineering",
    "Data Engineering",
    "ML Engineering",
    "Backend Engineering",
    "Frontend Engineering",
]

# Locations
LOCATIONS = [
    "United States",
    "Germany",
    "United Kingdom",
    "Poland",
    "Canada",
]

# Levels L1-L10 with weights (bell curve around L4-L6)
LEVELS = [
    ("L1", 2), ("L2", 5), ("L3", 10), ("L4", 18), ("L5", 25),
    ("L6", 20), ("L7", 10), ("L8", 5), ("L9", 3), ("L10", 2),
]

FAKE_FIRST_NAMES = [
    "alex", "jordan", "casey", "taylor", "morgan", "riley", "quinn",
    "avery", "blake", "cameron", "drew", "emery", "frankie", "harper",
    "jamie", "kai", "lennox", "max", "nico", "parker", "reese", "sage",
    "sam", "skyler", "val", "west", "eden", "phoenix", "river", "rowan",
]

FAKE_LAST_NAMES = [
    "chen", "patel", "kim", "garcia", "smith", "johnson", "lee",
    "williams", "brown", "jones", "davis", "wilson", "taylor", "martinez",
    "anderson", "thomas", "jackson", "white", "harris", "clark",
]

FAKE_HOSTNAMES = [
    "{name}s-MacBook-Pro.local", "{name}s-MacBook-Air.local",
    "MacBookPro", "{name}-dev-machine", "{name}-workstation.local",
    "dev-{name}.internal", "{name}-laptop",
]

FAKE_PROFILES = [
    "{first}", "{first}{last}", "{first}.{last}", "{first}_{last}",
    "{first[0]}{last}", "{last}", "dev-{first}",
]


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def weighted_choice(items_with_weights):
    """Choose from list of (item, weight) tuples."""
    items, weights = zip(*items_with_weights)
    return random.choices(items, weights=weights, k=1)[0]


def positive_normal(mean, std, min_val=0):
    """Generate a positive value from a normal distribution."""
    return max(min_val, random.gauss(mean, std))


def make_deterministic_hash(seed_str):
    """Create a deterministic hash string from a seed."""
    return hashlib.sha256(seed_str.encode()).hexdigest()


def generate_fake_user(existing_emails):
    """Generate a fake user identity with employee metadata."""
    # Ensure unique email
    while True:
        first = random.choice(FAKE_FIRST_NAMES)
        last = random.choice(FAKE_LAST_NAMES)
        email = f"{first}.{last}@example.com"
        if email not in existing_emails:
            break

    account_uuid = str(uuid.uuid4())
    user_id = make_deterministic_hash(email)
    org_id = str(uuid.uuid4())

    hostname_template = random.choice(FAKE_HOSTNAMES)
    name = first.capitalize()
    hostname = hostname_template.format(name=name)

    profile_template = random.choice(FAKE_PROFILES)
    profile = profile_template.format(
        first=first, last=last,
        **{"first[0]": first[0]}
    )

    serial = "".join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=10))

    os_config = weighted_choice([(c, w) for c, w in OS_CONFIGS])
    terminal = weighted_choice(TERMINAL_TYPES)
    version = weighted_choice(SCOPE_VERSIONS)

    # Employee metadata
    practice = random.choice(PRACTICES)
    level = weighted_choice(LEVELS)
    location = random.choice(LOCATIONS)
    full_name = f"{first.capitalize()} {last.capitalize()}"

    return {
        "email": email,
        "full_name": full_name,
        "account_uuid": account_uuid,
        "user_id": user_id,
        "org_id": org_id,
        "hostname": hostname,
        "profile": profile,
        "serial": serial,
        "os_config": os_config,
        "terminal": terminal,
        "version": version,
        # Employee fields
        "practice": practice,
        "level": level,
        "location": location,
    }


def make_scope(version):
    """Build scope object."""
    return {
        "name": "com.anthropic.claude_code.events",
        "version": version,
    }


def make_resource(user):
    """Build resource object."""
    return {
        "host.arch": user["os_config"]["arch"],
        "host.name": user["hostname"],
        "os.type": user["os_config"]["os_type"],
        "os.version": user["os_config"]["os_version"],
        "service.name": "claude-code-None",
        "service.version": user["version"],
        "user.email": "",
        "user.practice": user["practice"],
        "user.profile": user["profile"],
        "user.serial": user["serial"],
    }


def make_common_attributes(user, session_id, timestamp):
    """Build common attributes present in all events."""
    return {
        "event.timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{timestamp.microsecond // 1000:03d}Z",
        "organization.id": user["org_id"],
        "session.id": session_id,
        "terminal.type": user["terminal"],
        "user.account_uuid": user["account_uuid"],
        "user.email": user["email"],
        "user.id": user["user_id"],
    }


def generate_api_request_event(user, session_id, timestamp):
    """Generate a claude_code.api_request event."""
    model_name = weighted_choice([(m, d["weight"]) for m, d in MODELS.items()])
    model = MODELS[model_name]

    input_tokens = max(0, int(positive_normal(model["avg_input_tokens"], model["input_std"])))
    output_tokens = max(1, int(positive_normal(model["avg_output_tokens"], model["output_std"])))
    cache_read = max(0, int(positive_normal(model["avg_cache_read"], model["cache_read_std"])))
    cache_create = max(0, int(positive_normal(model["avg_cache_create"], model["cache_create_std"])))
    cost = max(0, positive_normal(model["avg_cost"], model["cost_std"]))
    duration = max(100, int(positive_normal(model["avg_duration_ms"], model["duration_std"])))

    attrs = make_common_attributes(user, session_id, timestamp)
    attrs.update({
        "event.name": "api_request",
        "cache_creation_tokens": str(cache_create),
        "cache_read_tokens": str(cache_read),
        "cost_usd": str(cost),
        "duration_ms": str(duration),
        "input_tokens": str(input_tokens),
        "model": model_name,
        "output_tokens": str(output_tokens),
    })

    return {
        "body": "claude_code.api_request",
        "attributes": attrs,
        "scope": make_scope(user["version"]),
        "resource": make_resource(user),
    }


def generate_tool_decision_event(user, session_id, timestamp, tool_name=None):
    """Generate a claude_code.tool_decision event."""
    if tool_name is None:
        tool_name = weighted_choice([(t, w) for t, w in TOOLS.items()])

    source = weighted_choice([(s, w) for s, w in DECISION_SOURCES.items()])
    decision = "reject" if source == "user_reject" else "accept"

    attrs = make_common_attributes(user, session_id, timestamp)
    attrs.update({
        "event.name": "tool_decision",
        "decision": decision,
        "source": source,
        "tool_name": tool_name,
    })

    return {
        "body": "claude_code.tool_decision",
        "attributes": attrs,
        "scope": make_scope(user["version"]),
        "resource": make_resource(user),
    }


def generate_tool_result_event(user, session_id, timestamp, tool_name=None, decision_accepted=True):
    """Generate a claude_code.tool_result event."""
    if tool_name is None:
        tool_name = weighted_choice([(t, w) for t, w in TOOLS.items()])

    success_rate = TOOL_SUCCESS_RATES.get(tool_name, 0.95)
    success = random.random() < success_rate

    avg_dur = TOOL_AVG_DURATIONS.get(tool_name, 1000)
    duration = max(0, int(positive_normal(avg_dur, avg_dur * 0.8)))

    attrs = make_common_attributes(user, session_id, timestamp)
    attrs.update({
        "event.name": "tool_result",
        "decision_source": "config" if decision_accepted else "user_reject",
        "decision_type": "accept" if decision_accepted else "reject",
        "duration_ms": str(duration),
        "success": str(success).lower(),
        "tool_name": tool_name,
    })

    # Optionally add tool_result_size_bytes
    if random.random() < 0.3:
        attrs["tool_result_size_bytes"] = str(random.randint(10, 50000))

    return {
        "body": "claude_code.tool_result",
        "attributes": attrs,
        "scope": make_scope(user["version"]),
        "resource": make_resource(user),
    }


def generate_user_prompt_event(user, session_id, timestamp):
    """Generate a claude_code.user_prompt event."""
    # Prompt length: lognormal-ish distribution matching p50=128, p90=2969
    prompt_length = max(1, int(random.lognormvariate(4.85, 1.8)))

    attrs = make_common_attributes(user, session_id, timestamp)
    attrs.update({
        "event.name": "user_prompt",
        "prompt": "<REDACTED>",
        "prompt_length": str(prompt_length),
    })

    return {
        "body": "claude_code.user_prompt",
        "attributes": attrs,
        "scope": make_scope(user["version"]),
        "resource": make_resource(user),
    }


def generate_api_error_event(user, session_id, timestamp):
    """Generate a claude_code.api_error event."""
    error_msg, status_code = weighted_choice(
        [((msg, code), w) for msg, code, w in API_ERRORS]
    )
    model_name = weighted_choice([(m, d["weight"]) for m, d in MODELS.items()])
    duration = max(50, int(positive_normal(500, 600)))
    attempt = random.choices([1, 2, 3], weights=[70, 20, 10], k=1)[0]

    attrs = make_common_attributes(user, session_id, timestamp)
    attrs.update({
        "event.name": "api_error",
        "attempt": str(attempt),
        "duration_ms": str(duration),
        "error": error_msg,
        "model": model_name,
        "status_code": status_code,
    })

    return {
        "body": "claude_code.api_error",
        "attributes": attrs,
        "scope": make_scope(user["version"]),
        "resource": make_resource(user),
    }


def generate_session_events(user, session_id, session_start):
    """Generate a realistic sequence of events for one coding session.

    A session follows a pattern:
    1. User sends a prompt
    2. Claude makes API requests and uses tools (interleaved)
    3. Occasionally errors occur
    4. Repeat for multiple turns
    """
    events = []
    current_time = session_start

    # Number of turns in this session (user prompts)
    num_turns = max(1, int(random.lognormvariate(1.5, 1.0)))  # ~4-5 avg, long tail
    num_turns = min(num_turns, 100)  # cap at 100

    for turn in range(num_turns):
        # User prompt
        events.append(generate_user_prompt_event(user, session_id, current_time))
        current_time += timedelta(milliseconds=random.randint(100, 2000))

        # Number of API request + tool cycles per turn
        num_cycles = max(1, int(random.lognormvariate(1.0, 0.8)))
        num_cycles = min(num_cycles, 30)

        for cycle in range(num_cycles):
            # API request
            api_event = generate_api_request_event(user, session_id, current_time)
            duration_ms = int(api_event["attributes"]["duration_ms"])
            events.append(api_event)
            current_time += timedelta(milliseconds=duration_ms)

            # Possible API error (rare)
            if random.random() < 0.012:
                events.append(generate_api_error_event(user, session_id, current_time))
                current_time += timedelta(milliseconds=random.randint(500, 3000))
                # Retry
                api_event = generate_api_request_event(user, session_id, current_time)
                duration_ms = int(api_event["attributes"]["duration_ms"])
                events.append(api_event)
                current_time += timedelta(milliseconds=duration_ms)

            # Tool usage (0-3 tools per API response)
            num_tools = random.choices([0, 1, 2, 3], weights=[15, 50, 25, 10], k=1)[0]
            for _ in range(num_tools):
                tool_name = weighted_choice([(t, w) for t, w in TOOLS.items()])

                # Tool decision
                decision_event = generate_tool_decision_event(
                    user, session_id, current_time, tool_name
                )
                accepted = decision_event["attributes"]["decision"] == "accept"
                events.append(decision_event)
                current_time += timedelta(milliseconds=random.randint(1, 50))

                if accepted:
                    # Tool result
                    result_event = generate_tool_result_event(
                        user, session_id, current_time, tool_name, True
                    )
                    tool_duration = int(result_event["attributes"]["duration_ms"])
                    events.append(result_event)
                    current_time += timedelta(milliseconds=max(1, tool_duration))

            # Small gap between cycles
            current_time += timedelta(milliseconds=random.randint(50, 500))

        # Gap between turns (user thinking time)
        current_time += timedelta(seconds=random.randint(5, 120))

    return events


def events_to_log_batches(events, batch_size_range=(1, 10)):
    """Group events into CloudWatch-style log batches."""
    batches = []
    i = 0
    while i < len(events):
        batch_size = random.randint(*batch_size_range)
        batch_events = events[i:i + batch_size]
        i += batch_size

        logevents = []
        for event in batch_events:
            ts = event["attributes"]["event.timestamp"]
            # Parse the timestamp back to epoch ms
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            epoch_ms = int(dt.timestamp() * 1000)

            logevents.append({
                "id": str(random.randint(10**55, 10**57)),
                "timestamp": epoch_ms,
                "message": json.dumps(event),
            })

        if logevents:
            # Use the first event's timestamp for partitioning
            first_dt = datetime.fromtimestamp(
                logevents[0]["timestamp"] / 1000, tz=timezone.utc
            )
            batch = {
                "messageType": "DATA_MESSAGE",
                "owner": "123456789012",
                "logGroup": "/claude-code/telemetry",
                "logStream": "otel-collector",
                "subscriptionFilters": ["logs-to-s3"],
                "logEvents": logevents,
                "year": first_dt.year,
                "month": first_dt.month,
                "day": first_dt.day,
            }
            batches.append(batch)

    return batches


# ---------------------------------------------------------------------------
# Other AI providers (ChatGPT/OpenAI, Gemini, DeepSeek)
#
# Generated strictly AFTER Claude Code, each with its own random.Random(seed + offset),
# so the Claude Code stream (the global `random`) and its output files are untouched.
# Each provider is written in its own realistic raw format; cost_usd is computed from
# tokens x the model's catalog price and written into every record.
# ---------------------------------------------------------------------------

ALL_PROVIDERS = ["claude_code", "openai", "gemini", "deepseek"]
OTHER_PROVIDERS = ["openai", "gemini", "deepseek"]
PROVIDER_SEED_OFFSETS = {"population": 1_000, "openai": 2_000, "gemini": 3_000, "deepseek": 4_000}

# Model catalog grouped by provider. Per model: weight (share within the provider),
# illustrative USD prices per 1M tokens (input / cached input / output), token profiles
# (mean, std) for prompt + completion, cache-hit probability and share, reasoning share
# of completion tokens, latency (mean, std ms), error rate and weighted error codes.
# Prices are illustrative for a synthetic demo -- verify against vendor pricing pages
# before using them for anything real.
MODEL_CATALOG = {
    "claude_code": MODELS,   # see MODELS above (cost sampled, not price-derived)
    "openai": {
        "gpt-4o-2024-08-06": {
            "weight": 25, "input_per_mtok": 2.50, "cached_input_per_mtok": 1.25, "output_per_mtok": 10.00,
            "prompt_tokens": (1800, 1400), "completion_tokens": (420, 350),
            "cache_hit_p": 0.45, "cache_share": (0.3, 0.8), "reasoning_share": 0.0,
            "latency_ms": (1900, 900), "error_rate": 0.015,
        },
        "gpt-4o-2024-11-20": {
            "weight": 20, "input_per_mtok": 2.50, "cached_input_per_mtok": 1.25, "output_per_mtok": 10.00,
            "prompt_tokens": (1800, 1400), "completion_tokens": (420, 350),
            "cache_hit_p": 0.45, "cache_share": (0.3, 0.8), "reasoning_share": 0.0,
            "latency_ms": (1800, 850), "error_rate": 0.015,
        },
        "gpt-4o-mini-2024-07-18": {
            "weight": 30, "input_per_mtok": 0.15, "cached_input_per_mtok": 0.075, "output_per_mtok": 0.60,
            "prompt_tokens": (1200, 900), "completion_tokens": (300, 250),
            "cache_hit_p": 0.40, "cache_share": (0.3, 0.8), "reasoning_share": 0.0,
            "latency_ms": (900, 400), "error_rate": 0.010,
        },
        "o3-2025-04-16": {
            "weight": 10, "input_per_mtok": 2.00, "cached_input_per_mtok": 0.50, "output_per_mtok": 8.00,
            "prompt_tokens": (2500, 1800), "completion_tokens": (1600, 1200),
            "cache_hit_p": 0.35, "cache_share": (0.3, 0.7), "reasoning_share": 0.55,
            "latency_ms": (9000, 5000), "error_rate": 0.025,
        },
        "o4-mini-2025-04-16": {
            "weight": 15, "input_per_mtok": 1.10, "cached_input_per_mtok": 0.275, "output_per_mtok": 4.40,
            "prompt_tokens": (2000, 1500), "completion_tokens": (1100, 900),
            "cache_hit_p": 0.35, "cache_share": (0.3, 0.7), "reasoning_share": 0.45,
            "latency_ms": (4200, 2200), "error_rate": 0.020,
        },
    },
    "gemini": {
        "gemini-2.5-pro": {
            "weight": 35, "input_per_mtok": 1.25, "cached_input_per_mtok": 0.31, "output_per_mtok": 10.00,
            "prompt_tokens": (2600, 2000), "completion_tokens": (650, 500),
            "cache_hit_p": 0.30, "cache_share": (0.25, 0.75), "reasoning_share": 0.6,
            "latency_ms": (3200, 1500), "error_rate": 0.025,
        },
        "gemini-2.5-flash": {
            "weight": 42, "input_per_mtok": 0.30, "cached_input_per_mtok": 0.075, "output_per_mtok": 2.50,
            "prompt_tokens": (1900, 1500), "completion_tokens": (450, 350),
            "cache_hit_p": 0.30, "cache_share": (0.25, 0.75), "reasoning_share": 0.35,
            "latency_ms": (1300, 600), "error_rate": 0.018,
        },
        "gemini-2.0-flash-001": {
            "weight": 15, "input_per_mtok": 0.10, "cached_input_per_mtok": 0.025, "output_per_mtok": 0.40,
            "prompt_tokens": (1400, 1100), "completion_tokens": (350, 280),
            "cache_hit_p": 0.25, "cache_share": (0.25, 0.75), "reasoning_share": 0.0,
            "latency_ms": (800, 350), "error_rate": 0.015,
        },
        "gemini-2.5-flash-lite": {
            "weight": 8, "input_per_mtok": 0.10, "cached_input_per_mtok": 0.025, "output_per_mtok": 0.40,
            "prompt_tokens": (1000, 800), "completion_tokens": (250, 200),
            "cache_hit_p": 0.25, "cache_share": (0.25, 0.75), "reasoning_share": 0.0,
            "latency_ms": (600, 250), "error_rate": 0.012,
        },
    },
    "deepseek": {
        "deepseek-chat": {
            "weight": 70, "input_per_mtok": 0.28, "cached_input_per_mtok": 0.028, "output_per_mtok": 0.42,
            "prompt_tokens": (2200, 1700), "completion_tokens": (500, 400),
            "cache_hit_p": 0.60, "cache_share": (0.2, 0.9), "reasoning_share": 0.0,
            "latency_ms": (2100, 1100), "error_rate": 0.028,
        },
        "deepseek-reasoner": {
            "weight": 30, "input_per_mtok": 0.28, "cached_input_per_mtok": 0.028, "output_per_mtok": 0.42,
            "prompt_tokens": (2400, 1800), "completion_tokens": (1500, 1100),
            "cache_hit_p": 0.55, "cache_share": (0.2, 0.9), "reasoning_share": 0.6,
            "latency_ms": (7800, 4000), "error_rate": 0.035,
        },
    },
}

# Weighted error codes per provider, as each one reports them.
PROVIDER_ERRORS = {
    # (type, code, http status, weight)
    "openai": [
        ("rate_limit_error", "rate_limit_exceeded", 429, 55),
        ("server_error", "server_error", 500, 25),
        ("invalid_request_error", "context_length_exceeded", 400, 10),
        ("service_unavailable", "overloaded", 503, 10),
    ],
    # (status code, message, weight)
    "gemini": [(429, "RESOURCE_EXHAUSTED", 55), (500, "INTERNAL", 20),
               (503, "UNAVAILABLE", 15), (400, "INVALID_ARGUMENT", 10)],
    "deepseek": [(429, "Rate limit reached", 50),
                 (503, "Server overloaded, please try again later", 30),
                 (500, "Internal server error", 20)],
}

# Probability that an employee of a practice uses a provider at all (Claude Code: everyone).
PROVIDER_ADOPTION = {
    "Backend Engineering":  {"openai": 0.75, "gemini": 0.25, "deepseek": 0.25},
    "Frontend Engineering": {"openai": 0.80, "gemini": 0.30, "deepseek": 0.15},
    "Data Engineering":     {"openai": 0.55, "gemini": 0.60, "deepseek": 0.30},
    "ML Engineering":       {"openai": 0.60, "gemini": 0.70, "deepseek": 0.45},
    "Platform Engineering": {"openai": 0.50, "gemini": 0.30, "deepseek": 0.35},
}
DEFAULT_PROVIDER_ADOPTION = {"openai": 0.55, "gemini": 0.40, "deepseek": 0.25}

# (mean conversations per active day, mean requests per conversation) at the baseline
# density of 5000 sessions / 100 users / 60 days; scaled with --num-sessions.
PROVIDER_INTENSITY = {"openai": (2.2, 4.5), "gemini": (1.8, 4.0), "deepseek": (1.4, 5.0)}
BASELINE_SESSION_DENSITY = 5000 / (100 * 60)
OPENAI_TOOLS = ["code_interpreter", "web_search", "file_search", "image_generation"]


def _iso_ms(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def _hex(rng, nbytes):
    return f"{rng.getrandbits(nbytes * 8):0{nbytes * 2}x}"


def _tokens(rng, mean, std):
    """Right-skewed positive count with the given mean/std (lognormal)."""
    sigma2 = math.log(1 + (std / mean) ** 2)
    return max(1, int(rng.lognormvariate(math.log(mean) - sigma2 / 2, math.sqrt(sigma2))))


def _poisson(rng, lam):
    if lam <= 0:
        return 0
    threshold, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= threshold:
            return k
        k += 1


def _pick(rng, weighted):
    """weighted: list of (item, weight)."""
    items, weights = zip(*weighted)
    return rng.choices(items, weights=weights, k=1)[0]


def _price(model, uncached, cached, output):
    """USD for one request from the catalog prices (output includes reasoning)."""
    return round((uncached * model["input_per_mtok"]
                  + cached * model["cached_input_per_mtok"]
                  + output * model["output_per_mtok"]) / 1e6, 8)


def _sample_usage(rng, model):
    """-> (prompt tokens, cached part of prompt, completion tokens, reasoning part)."""
    prompt = _tokens(rng, *model["prompt_tokens"])
    cached = 0
    if rng.random() < model["cache_hit_p"]:
        cached = min(prompt, int(prompt * rng.uniform(*model["cache_share"])))
    completion = _tokens(rng, *model["completion_tokens"])
    reasoning = int(completion * model["reasoning_share"] * rng.uniform(0.7, 1.3)) if model["reasoning_share"] else 0
    return prompt, cached, max(completion, reasoning), min(reasoning, max(completion, reasoning))


def _latency(rng, model):
    mean, std = model["latency_ms"]
    return max(50, int(rng.gauss(mean, std)))


def build_provider_population(users, providers, seed):
    """Who uses which provider and how intensely (deterministic, practice-weighted).

    Story: ~6% "power users" use every provider at ~2.5x volume.
    """
    rng = random.Random(seed + PROVIDER_SEED_OFFSETS["population"])
    emails = sorted(u["email"] for u in users)
    practice = {u["email"]: u["practice"] for u in users}
    power = set(rng.sample(emails, min(len(emails), max(3, round(len(emails) * 0.06)))))
    population = {p: [] for p in OTHER_PROVIDERS}
    for email in emails:
        weights = PROVIDER_ADOPTION.get(practice[email], DEFAULT_PROVIDER_ADOPTION)
        for provider in OTHER_PROVIDERS:          # always draw -> stable for any --providers subset
            adopt = rng.random() < weights[provider]
            intensity = rng.lognormvariate(0, 0.5)
            start_frac = rng.random() * (0.5 if provider == "gemini" else 0.15)
            if email in power or adopt:
                population[provider].append({
                    "email": email,
                    "intensity": intensity * (2.5 if email in power else 1.0),
                    "start_frac": 0.0 if email in power else start_frac,
                })
    return {p: population[p] for p in providers if p in population}, sorted(power)


def _iter_provider_requests(rng, provider, people, start_date, days, scale):
    """Yield (email, conversation key, timestamp). Business-hours bias like Claude Code.

    Story: Gemini's usage ramps from 0.45x to 1.55x over the period (growing share).
    """
    conv_mean, req_mean = PROVIDER_INTENSITY[provider]
    for p_idx, person in enumerate(people):
        for d in range(days):
            if d < int(person["start_frac"] * days):
                continue
            day = start_date + timedelta(days=d)
            ramp = 0.45 + 1.1 * (d / max(1, days - 1)) if provider == "gemini" else 1.0
            active_p = 0.15 if day.weekday() >= 5 else 0.72
            if rng.random() >= min(0.98, active_p * ramp):
                continue
            for c in range(_poisson(rng, conv_mean * scale * person["intensity"] * ramp)):
                hour = rng.randint(9, 17) if rng.random() < 0.7 else rng.randint(0, 23)
                ts = day.replace(hour=hour, minute=rng.randint(0, 59), second=rng.randint(0, 59),
                                 microsecond=rng.randint(0, 999) * 1000)
                for _ in range(max(1, _poisson(rng, req_mean))):
                    yield person["email"], (p_idx, d, c), ts
                    ts += timedelta(seconds=rng.randint(15, 300), milliseconds=rng.randint(0, 999))


def generate_openai(people, start_date, days, scale, seed):
    """openai_usage.jsonl lines. Stories: a one-day 429 storm (days//3 before the end);
    ~0.3% of lines replayed by the gateway (exact duplicates -> de-duplication)."""
    rng = random.Random(seed + PROVIDER_SEED_OFFSETS["openai"])
    catalog = MODEL_CATALOG["openai"]
    spike_day = (start_date + timedelta(days=days - max(1, days // 3))).date()
    convs, records = {}, []
    for email, conv, ts in _iter_provider_requests(rng, "openai", people, start_date, days, scale):
        if conv not in convs:
            client = "chatgpt_enterprise" if rng.random() < 0.7 else "api"
            convs[conv] = (client, f"conv_{_hex(rng, 12)}" if client == "chatgpt_enterprise" else None)
        client, conv_id = convs[conv]
        name = _pick(rng, [(m, d["weight"]) for m, d in catalog.items()])
        model = catalog[name]
        rec = {"id": f"chatcmpl-{_hex(rng, 12)}", "object": "chat.completion",
               "created": int(ts.timestamp()), "model": name, "user": email,
               "client": client, "conversation_id": conv_id}
        storm = ts.date() == spike_day
        if rng.random() < (0.25 if storm else model["error_rate"]):
            if storm and rng.random() < 0.9:
                etype, code, status = "rate_limit_error", "rate_limit_exceeded", 429
            else:
                etype, code, status = _pick(rng, [(e[:3], e[3]) for e in PROVIDER_ERRORS["openai"]])
            rec.update(latency_ms=rng.randint(80, 900), usage=None, tool_calls=[], cost_usd=0.0,
                       error={"type": etype, "code": code, "status": status})
        else:
            prompt, cached, completion, reasoning = _sample_usage(rng, model)
            calls = []
            if rng.random() < (0.18 if client == "chatgpt_enterprise" else 0.10):
                calls = [{"type": "function", "name": rng.choice(OPENAI_TOOLS)}
                         for _ in range(1 if rng.random() < 0.8 else 2)]
            rec.update(
                latency_ms=_latency(rng, model),
                usage={"prompt_tokens": prompt, "completion_tokens": completion,
                       "total_tokens": prompt + completion,
                       "prompt_tokens_details": {"cached_tokens": cached},
                       "completion_tokens_details": {"reasoning_tokens": reasoning}},
                tool_calls=calls, cost_usd=_price(model, prompt - cached, cached, completion),
                error=None)
        records.append(rec)
    records.sort(key=lambda r: (r["created"], r["id"]))
    lines = []
    for rec in records:
        line = json.dumps(rec, separators=(",", ":"))
        lines.append(line)
        if rng.random() < 0.003:
            lines.append(line)
    return lines


def generate_gemini(people, start_date, days, scale, seed):
    """gemini_usage.jsonl lines. Story: ~0.05% truncated lines (data-quality: skipped rows).
    Thoughts are reported outside candidatesTokenCount but billed as output."""
    rng = random.Random(seed + PROVIDER_SEED_OFFSETS["gemini"])
    catalog = MODEL_CATALOG["gemini"]
    convs, records = {}, []
    for email, conv, ts in _iter_provider_requests(rng, "gemini", people, start_date, days, scale):
        if conv not in convs:
            r = rng.random()
            surface = "gemini_code_assist" if r < 0.45 else "gemini_app" if r < 0.80 else "api"
            convs[conv] = (surface, f"g-{_hex(rng, 8)}")
        surface, sid = convs[conv]
        name = _pick(rng, [(m, d["weight"]) for m, d in catalog.items()])
        model = catalog[name]
        rec = {"timestamp": _iso_ms(ts), "responseId": _hex(rng, 10), "principalEmail": email,
               "modelVersion": name, "surface": surface}
        if surface != "api":                       # raw API calls carry no session id
            rec["sessionId"] = sid
        if rng.random() < model["error_rate"]:
            code, message = _pick(rng, [(e[:2], e[2]) for e in PROVIDER_ERRORS["gemini"]])
            rec.update(latencyMs=rng.randint(60, 700), cost_usd=0.0,
                       status={"code": code, "message": message})
        else:
            prompt, cached, completion, thoughts = _sample_usage(rng, model)
            candidates = max(1, completion - thoughts)
            usage = {"promptTokenCount": prompt, "candidatesTokenCount": candidates}
            if cached:
                usage["cachedContentTokenCount"] = cached
            if thoughts:
                usage["thoughtsTokenCount"] = thoughts
            usage["totalTokenCount"] = prompt + candidates + thoughts
            rec.update(latencyMs=_latency(rng, model), usageMetadata=usage,
                       cost_usd=_price(model, prompt - cached, cached, candidates + thoughts),
                       status={"code": 200})
        records.append(rec)
    records.sort(key=lambda r: (r["timestamp"], r["responseId"]))
    lines = []
    for rec in records:
        line = json.dumps(rec, separators=(",", ":"))
        lines.append(line[: len(line) // 2] if rng.random() < 0.0005 else line)
    return lines


DEEPSEEK_HEADER = [
    "request_id", "timestamp", "user_email", "model", "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens", "completion_tokens", "reasoning_tokens", "cost_usd",
    "latency_ms", "status_code", "error",
]


def generate_deepseek(people, start_date, days, scale, seed):
    """deepseek_usage.csv rows (gateway log; no session id -> sessions are synthesized)."""
    rng = random.Random(seed + PROVIDER_SEED_OFFSETS["deepseek"])
    catalog = MODEL_CATALOG["deepseek"]
    rows = []
    for email, _conv, ts in _iter_provider_requests(rng, "deepseek", people, start_date, days, scale):
        name = _pick(rng, [(m, d["weight"]) for m, d in catalog.items()])
        model = catalog[name]
        row = {"request_id": f"ds-{_hex(rng, 12)}", "timestamp": _iso_ms(ts),
               "user_email": email, "model": name}
        if rng.random() < model["error_rate"]:
            status, message = _pick(rng, [(e[:2], e[2]) for e in PROVIDER_ERRORS["deepseek"]])
            row.update(prompt_cache_hit_tokens="", prompt_cache_miss_tokens="", completion_tokens="",
                       reasoning_tokens="", cost_usd="0.0", latency_ms=rng.randint(50, 600),
                       status_code=status, error=message)
        else:
            prompt, cached, completion, reasoning = _sample_usage(rng, model)
            row.update(prompt_cache_hit_tokens=cached, prompt_cache_miss_tokens=prompt - cached,
                       completion_tokens=completion, reasoning_tokens=reasoning,
                       cost_usd=repr(_price(model, prompt - cached, cached, completion)),
                       latency_ms=_latency(rng, model), status_code=200, error="")
        rows.append(row)
    rows.sort(key=lambda r: (r["timestamp"], r["request_id"]))
    return rows


def generate_other_providers(users, providers, start_date, days, num_sessions, seed, output_dir):
    """Write the non-Claude-Code provider files; returns a printable summary."""
    others = [p for p in providers if p in OTHER_PROVIDERS]
    if not others:
        return []
    density = num_sessions / max(1, len(users) * days)
    scale = density / BASELINE_SESSION_DENSITY
    population, power = build_provider_population(users, others, seed)
    summary = [f"  power users (all providers): {len(power)}"]
    if "openai" in population:
        lines = generate_openai(population["openai"], start_date, days, scale, seed)
        path = os.path.join(output_dir, "openai_usage.jsonl")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(line + "\n" for line in lines)
        summary.append(f"  {path}: {len(lines):,} records, {len(population['openai'])} users")
    if "gemini" in population:
        lines = generate_gemini(population["gemini"], start_date, days, scale, seed)
        path = os.path.join(output_dir, "gemini_usage.jsonl")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(line + "\n" for line in lines)
        summary.append(f"  {path}: {len(lines):,} records, {len(population['gemini'])} users")
    if "deepseek" in population:
        rows = generate_deepseek(population["deepseek"], start_date, days, scale, seed)
        path = os.path.join(output_dir, "deepseek_usage.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=DEEPSEEK_HEADER, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        summary.append(f"  {path}: {len(rows):,} records, {len(population['deepseek'])} users")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Generate fake Claude Code telemetry data")
    parser.add_argument("--num-users", type=int, default=30, help="Number of fake users")
    parser.add_argument("--num-sessions", type=int, default=500, help="Total number of sessions")
    parser.add_argument("--days", type=int, default=30, help="Number of days to span")
    parser.add_argument("--output-dir", default="output", help="Output directory")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--providers", default=",".join(ALL_PROVIDERS),
                        help="Comma-separated providers to generate (default: all of "
                             + ",".join(ALL_PROVIDERS) + ")")
    args = parser.parse_args()
    providers = [p.strip() for p in args.providers.split(",") if p.strip()] or list(ALL_PROVIDERS)
    unknown = [p for p in providers if p not in ALL_PROVIDERS]
    if unknown:
        parser.error(f"unknown provider(s) {unknown}; choose from {ALL_PROVIDERS}")
    with_claude_code = "claude_code" in providers

    random.seed(args.seed)

    print(f"Generating fake telemetry data...")
    print(f"  Users: {args.num_users}")
    print(f"  Sessions: {args.num_sessions}")
    print(f"  Days: {args.days}")

    # Generate users with unique emails
    existing_emails = set()
    users = []
    for _ in range(args.num_users):
        user = generate_fake_user(existing_emails)
        existing_emails.add(user["email"])
        users.append(user)
    print(f"  Generated {len(users)} fake users")

    # Generate sessions
    end_date = datetime(2026, 2, 1, tzinfo=timezone.utc)
    start_date = end_date - timedelta(days=args.days)

    all_events = []
    for session_num in range(args.num_sessions if with_claude_code else 0):
        user = random.choice(users)
        session_id = str(uuid.uuid4())

        # Random start time within the date range, weighted toward business hours
        day_offset = random.random() * args.days
        session_day = start_date + timedelta(days=day_offset)

        # Business hours bias: 70% during 9-18, 30% other times
        if random.random() < 0.7:
            hour = random.randint(9, 17)
        else:
            hour = random.randint(0, 23)
        minute = random.randint(0, 59)
        session_start = session_day.replace(hour=hour, minute=minute, second=0, microsecond=0)

        events = generate_session_events(user, session_id, session_start)
        all_events.extend(events)

        if (session_num + 1) % 100 == 0:
            print(f"  Generated session {session_num + 1}/{args.num_sessions} ({len(all_events)} events so far)")

    print(f"  Total events generated: {len(all_events)}")

    # Sort all events by timestamp
    all_events.sort(key=lambda e: e["attributes"]["event.timestamp"])

    # Group into log batches
    batches = events_to_log_batches(all_events)
    print(f"  Total log batches: {len(batches)}")

    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)

    # Output as JSONL (one batch per line, same format as Athena table)
    output_path = os.path.join(args.output_dir, "telemetry_logs.jsonl")
    if with_claude_code:
        with open(output_path, "w") as f:
            for batch in batches:
                f.write(json.dumps(batch) + "\n")
        print(f"  Written to {output_path}")

    # Output employee table as CSV
    emp_path = os.path.join(args.output_dir, "employees.csv")
    with open(emp_path, "w") as f:
        f.write("email,full_name,practice,level,location\n")
        for user in users:
            f.write(f"{user['email']},{user['full_name']},{user['practice']},{user['level']},{user['location']}\n")
    print(f"  Written to {emp_path}")

    # Other providers: own seeded RNGs, run after Claude Code (its output is unaffected).
    for line in generate_other_providers(users, providers, start_date, args.days,
                                         args.num_sessions, args.seed, args.output_dir):
        print(line)

    # Print summary stats
    print(f"\n=== Summary ===")
    event_counts = {}
    for e in all_events:
        body = e["body"]
        event_counts[body] = event_counts.get(body, 0) + 1
    for body, count in sorted(event_counts.items(), key=lambda x: -x[1]):
        print(f"  {body}: {count}")

    total_cost = sum(
        float(e["attributes"].get("cost_usd", 0))
        for e in all_events
        if e["body"] == "claude_code.api_request"
    )
    print(f"\n  Total simulated cost: ${total_cost:.2f}")
    print(f"  Date range: {start_date.date()} to {end_date.date()}")

    if with_claude_code:
        file_size = os.path.getsize(output_path) / (1024 * 1024)
        print(f"  File size: {output_path} = {file_size:.1f} MB")


if __name__ == "__main__":
    main()
