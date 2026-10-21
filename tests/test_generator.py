"""Generator guarantees: deterministic output, and the multi-provider extension leaves
the original Claude Code output byte-identical for the same seed."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "data" / "generate_fake_data.py"
ARGS = ["--num-users", "12", "--num-sessions", "40", "--days", "7", "--seed", "7"]

# Last commit with the original, Claude-Code-only generator.
ORIGINAL_COMMIT = "1f1a5df"
# Fallback when that commit isn't available: digests of the original generator's output
# for ARGS (uuid4 pinned), *normalised* — the original writes str(random.gauss(...)) and
# platform libm can differ in the last float digit (macOS vs Linux), so floats inside
# string-typed attributes are rounded to 12 significant digits and JSON is canonicalised;
# employees.csv is compared with normalised newlines.
ORIGINAL_NORMALISED_SHA256 = {
    "telemetry_logs.jsonl": "0787238d51a80488488eaa6c1a9dcb43b35837b64804824ecac89ee4df83f1de",
    "employees.csv": "801a53e708208a5be20c64bd726a8710e81e2d8752ee296c3d56f524266d2bdb",
}
_FLOAT_STR = re.compile(r"^-?\d+\.\d*(?:[eE][-+]?\d+)?$|^-?\d+[eE][-+]?\d+$")


def _round(value):
    if isinstance(value, float):
        return format(value, ".12g")
    if isinstance(value, str) and _FLOAT_STR.match(value):
        return format(float(value), ".12g")
    return value


def normalised_digest(path: Path) -> str:
    """Platform-independent sha256 of a generator output file (see above)."""
    h = hashlib.sha256()
    if path.name == "employees.csv":
        h.update(path.read_bytes().replace(b"\r\n", b"\n"))
        return h.hexdigest()
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            batch = json.loads(line)
            for event in batch.get("logEvents", []):
                msg = json.loads(event["message"])
                msg["attributes"] = {k: _round(v) for k, v in (msg.get("attributes") or {}).items()}
                event["message"] = msg
            h.update(json.dumps(batch, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            h.update(b"\n")
    return h.hexdigest()


# The original generator draws session/account/org ids from uuid.uuid4() (os.urandom),
# so even it is not byte-reproducible run to run. Pin uuid4 to a seeded stream in the
# child process; everything else comes from the seeded `random` module.
_PINNED = """
import random, runpy, sys, uuid
_rng = random.Random(12345)
uuid.uuid4 = lambda: uuid.UUID(int=_rng.getrandbits(128), version=4)
script = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(script, run_name="__main__")
"""


def _run(script: Path, out: Path, *extra: str, pinned: bool = True) -> dict[str, str]:
    cmd = [sys.executable, "-c", _PINNED, str(script)] if pinned else [sys.executable, str(script)]
    subprocess.run(cmd + ARGS + ["--output-dir", str(out), *extra], check=True,
                   capture_output=True, cwd=out.parent)
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir())}


def test_same_seed_gives_identical_files(tmp_path):
    a = _run(GENERATOR, tmp_path / "a")
    b = _run(GENERATOR, tmp_path / "b")
    assert a == b
    assert set(a) == {"telemetry_logs.jsonl", "employees.csv", "openai_usage.jsonl",
                      "gemini_usage.jsonl", "deepseek_usage.csv"}
    # provider files don't use uuid4 at all -> reproducible even without pinning
    c = _run(GENERATOR, tmp_path / "c", pinned=False)
    for name in ("openai_usage.jsonl", "gemini_usage.jsonl", "deepseek_usage.csv", "employees.csv"):
        assert c[name] == a[name]


def _original_generator_source() -> str | None:
    if shutil.which("git") is None:
        return None
    show = subprocess.run(["git", "show", f"{ORIGINAL_COMMIT}:data/generate_fake_data.py"],
                          cwd=ROOT, capture_output=True, text=True)
    return show.stdout if show.returncode == 0 else None


def test_claude_code_output_matches_original_generator(tmp_path):
    """The extended generator must reproduce the original Claude-Code-only output.

    Primary: re-run the original generator from its commit on this machine and compare
    bytes (CI checks out full history for this). Fallback (no git / shallow clone):
    compare platform-normalised digests against frozen values.
    """
    _run(GENERATOR, tmp_path / "ext")
    ext = tmp_path / "ext"
    source = _original_generator_source()
    if source is not None:
        original = tmp_path / "original_generator.py"
        original.write_text(source)
        _run(original, tmp_path / "orig")
        for name in ORIGINAL_NORMALISED_SHA256:
            assert (ext / name).read_bytes() == (tmp_path / "orig" / name).read_bytes(), \
                f"{name} changed vs. the generator at {ORIGINAL_COMMIT}"
    for name, digest in ORIGINAL_NORMALISED_SHA256.items():
        assert normalised_digest(ext / name) == digest, f"{name} changed vs. the original generator"


def test_normalised_digest_ignores_last_digit_and_newlines(tmp_path):
    def batch(cost):
        event = {"body": "claude_code.api_request", "attributes": {"cost_usd": cost, "model": "m",
                                                                    "input_tokens": "12"}}
        return json.dumps({"logEvents": [{"id": "1", "message": json.dumps(event)}]}) + "\n"
    a, b, c = (tmp_path / n / "telemetry_logs.jsonl" for n in "abc")
    for path, cost in ((a, "0.00450132158805959"), (b, "0.004501321588059591"), (c, "0.0045014")):
        path.parent.mkdir()
        path.write_text(batch(cost))
    assert normalised_digest(a) == normalised_digest(b) != normalised_digest(c)
    unix, dos = tmp_path / "a" / "employees.csv", tmp_path / "b" / "employees.csv"
    unix.write_bytes(b"email,full_name\nx@example.com,X\n")
    dos.write_bytes(b"email,full_name\r\nx@example.com,X\r\n")
    assert normalised_digest(unix) == normalised_digest(dos)


def test_providers_flag_limits_output(tmp_path):
    files = _run(GENERATOR, tmp_path / "x", "--providers", "claude_code,deepseek")
    assert set(files) == {"telemetry_logs.jsonl", "employees.csv", "deepseek_usage.csv"}
    full = _run(GENERATOR, tmp_path / "y")
    assert files["deepseek_usage.csv"] == full["deepseek_usage.csv"]    # subset-stable
    bad = subprocess.run([sys.executable, str(GENERATOR), "--providers", "bard",
                          "--output-dir", str(tmp_path / "z")], capture_output=True)
    assert bad.returncode != 0
