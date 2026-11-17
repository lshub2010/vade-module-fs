"""Adapter tests: manifest identity, schema generation, error mapping, CLI.

Skipped entirely when the SDK is not installed, so `pytest tests` still runs on
a bare Python (that is the module's own gate). The SDK is looked up in the
usual three places: `VADE_SDK_PATH`, a vendored `sdk/python`, and the sibling
Vade checkout used during development.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_ROOT / "src"))


def _sdk_candidates() -> list[Path]:
    candidates: list[Path] = []
    if os.environ.get("VADE_SDK_PATH"):
        candidates.append(Path(os.environ["VADE_SDK_PATH"]))
    # vendored (repo layout `modules/fs`), then the local dev checkout
    candidates.append(MODULE_ROOT / "sdk" / "python")
    candidates.append(MODULE_ROOT.parent / "Vade" / "sdk" / "python")
    candidates.append(MODULE_ROOT.parents[1] / "sdk" / "python")
    return candidates


SDK_DIR = next((path for path in _sdk_candidates() if (path / "vade_sdk").is_dir()), None)
if SDK_DIR is not None:
    sys.path.insert(0, str(SDK_DIR))

pytest.importorskip("vade_sdk", reason="vade-sdk is required for the adapter tests")

from fs_module import server, tools  # noqa: E402


@pytest.fixture()
def app():
    return server.build_app()


def test_module_identity_is_stable():
    assert server.MODULE_ID == "com.example.fs"
    assert server.MODULE_VERSION == "1.0.0"


def test_committed_manifest_matches_print_manifest(app):
    committed = json.loads((MODULE_ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert app.manifest() == committed


def test_check_is_clean(app):
    assert app.check() == []


def test_schema_comes_from_the_tool_signature(app):
    listing = app.tool("fs_list").input_schema
    assert listing["required"] == ["path"]
    assert listing["properties"]["path"] == {"type": "string"}
    assert listing["additionalProperties"] is False

    reading = app.tool("fs_read").input_schema
    assert reading["required"] == ["path"]
    assert reading["properties"]["max_bytes"] == {"type": "integer", "default": tools.DEFAULT_MAX_BYTES}

    writing = app.tool("fs_write").input_schema
    assert writing["required"] == ["path", "content"]
    assert writing["properties"]["create_parents"] == {"type": "boolean", "default": False}

    deleting = app.tool("fs_delete").input_schema
    assert deleting["properties"]["recursive"] == {"type": "boolean", "default": False}


def test_tool_metadata_comes_from_the_decorator(app):
    for fn in tools.all_tools():
        meta = fn.tool_meta  # type: ignore[attr-defined]
        spec = app.tool(meta["name"])
        assert spec.description == meta["description"]
        assert spec.risk == meta["risk"]
        assert spec.timeout_ms == meta["timeout_ms"]
        assert spec.idempotent is meta["idempotent"]


def test_high_risk_tools_are_declared_high(app):
    assert app.tool("fs_write").risk == "high"
    assert app.tool("fs_write").idempotent is False
    assert app.tool("fs_delete").risk == "high"
    assert app.tool("fs_delete").idempotent is True
    assert [name for name in app.tool_names if app.tool(name).risk == "high"] == ["fs_write", "fs_delete"]


async def test_structured_tool_error_keeps_its_code(app, tmp_path, monkeypatch):
    monkeypatch.setenv("FS_ROOTS", str(tmp_path))
    monkeypatch.delenv("FS_DATA_DIR", raising=False)
    envelope = await app.call_tool("fs_read", {"path": "../../etc/passwd"})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "PERMISSION_DENIED"
    assert envelope["error"]["retriable"] is False
    assert "/etc" not in envelope["error"]["message"]


async def test_write_then_read_through_the_adapter(app, tmp_path, monkeypatch):
    monkeypatch.setenv("FS_ROOTS", str(tmp_path))
    monkeypatch.delenv("FS_DATA_DIR", raising=False)
    written = await app.call_tool("fs_write", {"path": "notes/a.txt", "content": "hi", "create_parents": True})
    assert written["ok"] is True, written
    assert written["result"] == {"bytes": 2, "created": True}
    read = await app.call_tool("fs_read", {"path": "notes/a.txt"})
    assert read["result"] == {"text": "hi", "bytes": 2, "truncated": False}


async def test_unknown_tool_is_not_found(app):
    assert (await app.call_tool("shell_exec", {"cmd": "id"}))["error"]["code"] == "NOT_FOUND"


async def test_wrong_type_is_rejected_by_the_sdk(app, tmp_path, monkeypatch):
    monkeypatch.setenv("FS_ROOTS", str(tmp_path))
    envelope = await app.call_tool("fs_read", {"path": "a.txt", "max_bytes": "10"})
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"


async def test_unknown_argument_is_rejected(app, tmp_path, monkeypatch):
    monkeypatch.setenv("FS_ROOTS", str(tmp_path))
    envelope = await app.call_tool("fs_list", {"path": ".", "recursive": True})
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"


async def test_missing_argument_is_rejected(app):
    assert (await app.call_tool("fs_write", {"path": "a.txt"}))["error"]["code"] == "INVALID_ARGUMENT"


def _cli_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    parts = [str(MODULE_ROOT / "src")]
    if SDK_DIR is not None:
        parts.append(str(SDK_DIR))
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env.pop("FS_ROOTS", None)
    env.pop("FS_DATA_DIR", None)
    env.update(extra)
    return env


def test_standalone_entrypoint_output(tmp_path):
    """The real CLI, in a fresh interpreter (no event loop from pytest)."""
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "fs_module", "--standalone", "fs_list", "--args", '{"path":"."}'],
        cwd=MODULE_ROOT,
        env=_cli_env(FS_ROOTS=str(tmp_path)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert [entry["name"] for entry in payload["result"]["entries"]] == ["a.txt"]
    assert "tool ok" in completed.stderr


def test_standalone_denies_an_escape(tmp_path):
    completed = subprocess.run(
        [sys.executable, "-m", "fs_module", "--standalone", "fs_read", "--args", '{"path":"../../etc/passwd"}'],
        cwd=MODULE_ROOT,
        env=_cli_env(FS_ROOTS=str(tmp_path)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode != 0
    assert "PERMISSION_DENIED" in completed.stderr
    assert "/etc/passwd" not in completed.stderr


def test_print_manifest_is_byte_identical_to_the_committed_file():
    completed = subprocess.run(
        [sys.executable, "-m", "fs_module", "--print-manifest"],
        cwd=MODULE_ROOT,
        env=_cli_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == (MODULE_ROOT / "manifest.json").read_text(encoding="utf-8")
