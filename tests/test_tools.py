"""fs tool tests — stdlib + pytest only, NO SDK import (module self-sufficiency).

Two things are tested here on purpose and they are not the same thing:

* the tool contract (`list`/`stat`/`read`/`write`/`delete` shapes and codes);
* the boundary — `..`, absolute paths, symlinks, prefix siblings, missing
  roots. Those cases are the module's reason to exist, so they live next to the
  happy paths and are asserted with the exact code, not just "raises".
"""

from __future__ import annotations

import ast
import os
import re
import stat as stat_module
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fs_module import tools  # noqa: E402


class Sandbox:
    def __init__(self, root: Path, outside: Path) -> None:
        self.root = root
        self.outside = outside


@pytest.fixture()
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    monkeypatch.setenv("FS_ROOTS", str(root))
    monkeypatch.delenv("FS_DATA_DIR", raising=False)
    return Sandbox(root, outside)


def _failure(code: str, fn, *args, **kwargs) -> tools.ToolFailure:
    with pytest.raises(tools.ToolFailure) as excinfo:
        fn(*args, **kwargs)
    assert excinfo.value.code == code, f"expected {code}, got {excinfo.value.code}: {excinfo.value.message}"
    return excinfo.value


# ------------------------------------------------------------------ self-sufficiency


def test_no_sdk_import_in_tools():
    """AST check, not a substring scan: a docstring may *mention* the SDK."""
    tree = ast.parse(Path(tools.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [name for name in imported if name == "vade_sdk" or name.startswith("vade_sdk.")]
    assert not [name for name in imported if name == "vade" or name.startswith("vade.")]


def test_documented_limits():
    assert tools.MAX_IO_BYTES == 1024 * 1024
    assert tools.DEFAULT_MAX_BYTES == 65_536
    assert tools.MAX_LIST_ENTRIES == 10_000


def test_every_tool_is_decorated_and_unique():
    names = [fn.tool_meta["name"] for fn in tools.all_tools()]
    assert names == ["fs_list", "fs_stat", "fs_read", "fs_write", "fs_delete"]
    assert len(names) == len(set(names))
    for fn in tools.all_tools():
        assert fn.__name__ == fn.tool_meta["name"]


def test_tool_names_are_legal_for_the_sdk():
    """Same pattern as `vade_sdk.spec.TOOL_NAME_RE`; dots are NOT allowed."""
    pattern = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
    for fn in tools.all_tools():
        assert pattern.match(fn.tool_meta["name"]), fn.tool_meta["name"]


def test_risk_and_idempotency_are_declared_per_tool():
    meta = {fn.tool_meta["name"]: fn.tool_meta for fn in tools.all_tools()}
    assert meta["fs_write"]["risk"] == "high"
    assert meta["fs_write"]["idempotent"] is False
    assert meta["fs_delete"]["risk"] == "high"
    assert meta["fs_delete"]["idempotent"] is True
    for name in ("fs_list", "fs_stat", "fs_read"):
        assert meta[name]["risk"] == "low"
        assert meta[name]["idempotent"] is True


# --------------------------------------------------------------------- configuration


def test_no_roots_means_every_call_is_denied(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FS_ROOTS", raising=False)
    monkeypatch.delenv("FS_DATA_DIR", raising=False)
    for call in (
        lambda: tools.fs_list("."),
        lambda: tools.fs_stat("."),
        lambda: tools.fs_read("a.txt"),
        lambda: tools.fs_write("a.txt", "x"),
        lambda: tools.fs_delete("a.txt"),
    ):
        _failure("PERMISSION_DENIED", call)


def test_root_defaults_to_fs_data_dir(box: Sandbox, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FS_ROOTS")
    monkeypatch.setenv("FS_DATA_DIR", str(box.root))
    (box.root / "a.txt").write_text("hello", encoding="utf-8")
    assert tools.fs_read("a.txt")["text"] == "hello"


def test_roots_are_read_on_every_call(box: Sandbox, monkeypatch: pytest.MonkeyPatch):
    (box.outside / "secret.txt").write_text("nope", encoding="utf-8")
    monkeypatch.setenv("FS_ROOTS", str(box.outside))
    assert tools.fs_read("secret.txt")["text"] == "nope"

    monkeypatch.setenv("FS_ROOTS", str(box.root))
    _failure("PERMISSION_DENIED", tools.fs_read, str(box.outside / "secret.txt"))


def test_multiple_roots_are_colon_separated(box: Sandbox, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FS_ROOTS", os.pathsep.join([str(box.root), str(box.outside)]))
    (box.outside / "a.txt").write_text("out", encoding="utf-8")
    assert tools.fs_read(str(box.outside / "a.txt"))["text"] == "out"
    _failure("PERMISSION_DENIED", tools.fs_read, "/etc/passwd")


def test_empty_path_and_relative_paths_use_the_first_root(box: Sandbox):
    (box.root / "a.txt").write_text("hi", encoding="utf-8")
    assert tools.fs_read("a.txt")["text"] == "hi"
    listing = tools.fs_list("")
    assert [entry["name"] for entry in listing["entries"]] == ["a.txt"]
    assert tools.fs_stat("")["kind"] == "dir"


def test_root_may_itself_be_a_symlink(box: Sandbox, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    alias = tmp_path / "alias"
    alias.symlink_to(box.root, target_is_directory=True)
    monkeypatch.setenv("FS_ROOTS", str(alias))
    (box.root / "a.txt").write_text("through the alias", encoding="utf-8")
    assert tools.fs_read("a.txt")["text"] == "through the alias"


# --------------------------------------------------------------------------- list


def test_list_reports_kinds_sorts_and_does_not_recurse(box: Sandbox):
    (box.root / "b.txt").write_text("bb", encoding="utf-8")
    (box.root / "a_dir").mkdir()
    (box.root / "a_dir" / "nested.txt").write_text("x", encoding="utf-8")

    result = tools.fs_list(".")
    assert result["truncated"] is False
    assert [entry["name"] for entry in result["entries"]] == ["a_dir", "b.txt"]
    by_name = {entry["name"]: entry for entry in result["entries"]}
    assert by_name["a_dir"]["kind"] == "dir"
    assert by_name["b.txt"]["kind"] == "file"
    assert by_name["b.txt"]["size"] == 2
    assert isinstance(by_name["b.txt"]["mtime"], int)
    # Non-recursive: the nested file is not in the listing.
    assert "nested.txt" not in by_name


def test_list_reports_symlinks_without_following_them(box: Sandbox):
    (box.outside / "secret.txt").write_text("secret", encoding="utf-8")
    (box.root / "link").symlink_to(box.outside / "secret.txt")
    (box.root / "dir_link").symlink_to(box.outside, target_is_directory=True)

    by_name = {entry["name"]: entry for entry in tools.fs_list(".")["entries"]}
    assert by_name["link"]["kind"] == "symlink"
    assert by_name["dir_link"]["kind"] == "symlink"
    # lstat semantics: the link's size is the length of its target string.
    assert by_name["link"]["size"] == len(str(box.outside / "secret.txt"))


def test_list_truncates_at_the_entry_cap(box: Sandbox, monkeypatch: pytest.MonkeyPatch):
    names = [f"f{index}.txt" for index in range(5)]
    for name in names:
        (box.root / name).write_text("x", encoding="utf-8")
    monkeypatch.setattr(tools, "MAX_LIST_ENTRIES", 3)
    result = tools.fs_list(".")
    # The cap bounds the scan, so which 3 of the 5 arrive is directory order —
    # but the answer is always sorted and always exactly the cap.
    assert len(result["entries"]) == 3
    assert result["truncated"] is True
    returned = [entry["name"] for entry in result["entries"]]
    assert returned == sorted(returned)
    assert set(returned) <= set(names)


def test_list_not_truncated_at_exactly_the_cap(box: Sandbox, monkeypatch: pytest.MonkeyPatch):
    for index in range(3):
        (box.root / f"f{index}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(tools, "MAX_LIST_ENTRIES", 3)
    result = tools.fs_list(".")
    assert len(result["entries"]) == 3
    assert result["truncated"] is False


def test_list_rejects_a_file_and_a_missing_path(box: Sandbox):
    (box.root / "a.txt").write_text("hi", encoding="utf-8")
    _failure("INVALID_ARGUMENT", tools.fs_list, "a.txt")
    _failure("NOT_FOUND", tools.fs_list, "missing")
    _failure("INVALID_ARGUMENT", tools.fs_list, 123)


# --------------------------------------------------------------------------- stat


def test_stat_reports_kind_size_mtime_mode(box: Sandbox):
    target = box.root / "a.txt"
    target.write_text("hello", encoding="utf-8")
    os.chmod(target, 0o640)

    result = tools.fs_stat("a.txt")
    assert result["kind"] == "file"
    assert result["size"] == 5
    assert result["mode"] == 0o640
    assert result["mtime"] == int(os.stat(target).st_mtime)
    assert tools.fs_stat(".")["kind"] == "dir"


def test_stat_on_a_symlink_reports_the_resolved_target(box: Sandbox):
    (box.root / "a.txt").write_text("hello", encoding="utf-8")
    (box.root / "link").symlink_to(box.root / "a.txt")
    # `list` shows the entry, `stat` shows what the path resolves to.
    assert tools.fs_stat("link")["kind"] == "file"
    assert tools.fs_stat("a.txt")["size"] == 5


def test_stat_missing_path(box: Sandbox):
    _failure("NOT_FOUND", tools.fs_stat, "nope.txt")


# --------------------------------------------------------------------------- read


def test_read_returns_text_bytes_and_truncated(box: Sandbox):
    (box.root / "a.txt").write_text("hello 世界", encoding="utf-8")
    result = tools.fs_read("a.txt")
    assert result["text"] == "hello 世界"
    assert result["bytes"] == len("hello 世界".encode("utf-8"))
    assert result["truncated"] is False


def test_read_truncates_at_max_bytes_without_error(box: Sandbox):
    (box.root / "a.txt").write_text("x" * 200, encoding="utf-8")
    result = tools.fs_read("a.txt", max_bytes=10)
    assert result == {"text": "x" * 10, "bytes": 10, "truncated": True}


def test_read_default_window_is_65536(box: Sandbox):
    (box.root / "big.txt").write_text("a" * (tools.DEFAULT_MAX_BYTES + 10), encoding="utf-8")
    result = tools.fs_read("big.txt")
    assert result["bytes"] == tools.DEFAULT_MAX_BYTES
    assert result["truncated"] is True


def test_read_trims_a_multibyte_character_cut_by_the_window(box: Sandbox):
    # "你" is 3 bytes; a 4-byte window holds one full character plus one byte.
    (box.root / "u.txt").write_bytes("你".encode("utf-8") * 10)
    result = tools.fs_read("u.txt", max_bytes=4)
    assert result["text"] == "你"
    assert result["bytes"] == 3
    assert result["truncated"] is True


def test_read_window_entirely_inside_one_character(box: Sandbox):
    """max_bytes=1 over `你` is an empty window, not a binary file."""
    (box.root / "u.txt").write_bytes("你".encode("utf-8") * 10)
    result = tools.fs_read("u.txt", max_bytes=1)
    assert result == {"text": "", "bytes": 0, "truncated": True}


def test_read_rejects_binary_and_invalid_utf8(box: Sandbox):
    (box.root / "bin").write_bytes(b"abc\x00def")
    _failure("INVALID_ARGUMENT", tools.fs_read, "bin")

    (box.root / "latin1").write_bytes(b"abc\xff\xfe")
    _failure("INVALID_ARGUMENT", tools.fs_read, "latin1")


def test_read_window_must_be_inside_the_hard_limit(box: Sandbox):
    (box.root / "a.txt").write_text("x", encoding="utf-8")
    for bad in (0, -1, tools.MAX_IO_BYTES + 1, True, "10", 1.5):
        _failure("INVALID_ARGUMENT", tools.fs_read, "a.txt", max_bytes=bad)


def test_read_rejects_directories_and_missing_files(box: Sandbox):
    (box.root / "sub").mkdir()
    _failure("INVALID_ARGUMENT", tools.fs_read, "sub")
    _failure("NOT_FOUND", tools.fs_read, "missing.txt")


# -------------------------------------------------------------------------- write


def test_write_creates_then_overwrites(box: Sandbox):
    first = tools.fs_write("a.txt", "hello 世界")
    assert first == {"bytes": len("hello 世界".encode("utf-8")), "created": True}
    assert (box.root / "a.txt").read_text(encoding="utf-8") == "hello 世界"

    second = tools.fs_write("a.txt", "again")
    assert second == {"bytes": 5, "created": False}
    assert (box.root / "a.txt").read_text(encoding="utf-8") == "again"


def test_write_requires_an_existing_parent_by_default(box: Sandbox):
    _failure("NOT_FOUND", tools.fs_write, "missing/a.txt", "x")
    assert not (box.root / "missing").exists()


def test_write_can_create_parents(box: Sandbox):
    result = tools.fs_write("a/b/c.txt", "x", create_parents=True)
    assert result["created"] is True
    assert (box.root / "a" / "b" / "c.txt").read_text(encoding="utf-8") == "x"


def test_write_rejects_oversized_content(box: Sandbox):
    _failure("PAYLOAD_TOO_LARGE", tools.fs_write, "big.txt", "x" * (tools.MAX_IO_BYTES + 1))
    assert not (box.root / "big.txt").exists()


def test_write_rejects_a_directory_and_a_fifo(box: Sandbox):
    (box.root / "sub").mkdir()
    _failure("INVALID_ARGUMENT", tools.fs_write, "sub", "x")

    os.mkfifo(box.root / "pipe")
    _failure("INVALID_ARGUMENT", tools.fs_write, "pipe", "x")


def test_write_rejects_the_root_itself(box: Sandbox):
    _failure("PERMISSION_DENIED", tools.fs_write, "", "x")
    _failure("PERMISSION_DENIED", tools.fs_write, ".", "x")


def test_write_rejects_wrong_types(box: Sandbox):
    _failure("INVALID_ARGUMENT", tools.fs_write, "a.txt", 123)
    _failure("INVALID_ARGUMENT", tools.fs_write, "a.txt", "x", create_parents="yes")


# ------------------------------------------------------------------------- delete


def test_delete_removes_a_file(box: Sandbox):
    (box.root / "a.txt").write_text("x", encoding="utf-8")
    assert tools.fs_delete("a.txt") == {"deleted": True}
    assert not (box.root / "a.txt").exists()


def test_delete_missing_path_is_not_found(box: Sandbox):
    _failure("NOT_FOUND", tools.fs_delete, "nope.txt")


def test_delete_non_empty_directory_requires_recursive(box: Sandbox):
    (box.root / "sub").mkdir()
    (box.root / "sub" / "a.txt").write_text("x", encoding="utf-8")
    _failure("INVALID_ARGUMENT", tools.fs_delete, "sub")
    assert (box.root / "sub" / "a.txt").exists()

    assert tools.fs_delete("sub", recursive=True) == {"deleted": True}
    assert not (box.root / "sub").exists()


def test_delete_removes_an_empty_directory(box: Sandbox):
    (box.root / "empty").mkdir()
    assert tools.fs_delete("empty") == {"deleted": True}


def test_delete_refuses_a_configured_root(box: Sandbox):
    _failure("PERMISSION_DENIED", tools.fs_delete, "")
    _failure("PERMISSION_DENIED", tools.fs_delete, ".", recursive=True)
    assert box.root.exists()


def test_delete_rejects_wrong_recursive_type(box: Sandbox):
    (box.root / "a.txt").write_text("x", encoding="utf-8")
    _failure("INVALID_ARGUMENT", tools.fs_delete, "a.txt", recursive="yes")


# ----------------------------------------------------------------------- boundary


def test_parent_traversal_is_denied(box: Sandbox):
    (box.outside / "secret.txt").write_text("secret", encoding="utf-8")
    _failure("PERMISSION_DENIED", tools.fs_read, "../outside/secret.txt")
    _failure("PERMISSION_DENIED", tools.fs_read, "a/../../outside/secret.txt")
    _failure("PERMISSION_DENIED", tools.fs_list, "..")
    _failure("PERMISSION_DENIED", tools.fs_write, "../outside/secret.txt", "pwned")
    _failure("PERMISSION_DENIED", tools.fs_delete, "../outside/secret.txt")
    assert (box.outside / "secret.txt").read_text(encoding="utf-8") == "secret"


def test_absolute_path_outside_the_roots_is_denied(box: Sandbox):
    (box.outside / "secret.txt").write_text("secret", encoding="utf-8")
    _failure("PERMISSION_DENIED", tools.fs_read, str(box.outside / "secret.txt"))
    _failure("PERMISSION_DENIED", tools.fs_stat, "/etc/passwd")
    _failure("PERMISSION_DENIED", tools.fs_list, "/etc")


def test_sibling_with_the_same_prefix_is_denied(box: Sandbox, tmp_path: Path):
    """`/data/root-evil` must not pass for `/data/root` (string prefix bug)."""
    evil = tmp_path / "root-evil"
    evil.mkdir()
    (evil / "a.txt").write_text("evil", encoding="utf-8")
    _failure("PERMISSION_DENIED", tools.fs_read, str(evil / "a.txt"))
    _failure("PERMISSION_DENIED", tools.fs_stat, str(evil))


def test_symlink_to_outside_is_denied_for_every_tool(box: Sandbox):
    (box.outside / "secret.txt").write_text("secret", encoding="utf-8")
    (box.root / "link").symlink_to(box.outside / "secret.txt")
    (box.root / "dir_link").symlink_to(box.outside, target_is_directory=True)

    _failure("PERMISSION_DENIED", tools.fs_read, "link")
    _failure("PERMISSION_DENIED", tools.fs_stat, "link")
    _failure("PERMISSION_DENIED", tools.fs_list, "dir_link")
    _failure("PERMISSION_DENIED", tools.fs_read, "dir_link/secret.txt")
    _failure("PERMISSION_DENIED", tools.fs_write, "link", "pwned")
    _failure("PERMISSION_DENIED", tools.fs_delete, "link")
    assert (box.outside / "secret.txt").read_text(encoding="utf-8") == "secret"


def test_symlink_inside_the_root_is_allowed(box: Sandbox):
    (box.root / "a.txt").write_text("hello", encoding="utf-8")
    (box.root / "link").symlink_to(box.root / "a.txt")
    assert tools.fs_read("link")["text"] == "hello"
    tools.fs_write("link", "through the link")
    assert (box.root / "a.txt").read_text(encoding="utf-8") == "through the link"


def test_dangling_symlink_to_outside_is_still_denied(box: Sandbox):
    """A broken link is exactly the case a naive `exists()` check lets through."""
    (box.root / "dangling").symlink_to(box.outside / "never-created.txt")
    _failure("PERMISSION_DENIED", tools.fs_read, "dangling")
    _failure("PERMISSION_DENIED", tools.fs_stat, "dangling")
    _failure("PERMISSION_DENIED", tools.fs_write, "dangling", "x")
    _failure("PERMISSION_DENIED", tools.fs_delete, "dangling")
    assert not (box.outside / "never-created.txt").exists()
    assert [entry["kind"] for entry in tools.fs_list(".")["entries"]] == ["symlink"]


def test_symlink_loop_is_a_clean_error_not_a_crash(box: Sandbox):
    (box.root / "loopa").symlink_to("loopb")
    (box.root / "loopb").symlink_to("loopa")
    with pytest.raises(tools.ToolFailure) as excinfo:
        tools.fs_stat("loopa")
    assert excinfo.value.code in ("PERMISSION_DENIED", "INVALID_ARGUMENT")


def test_nul_byte_in_a_path_is_rejected(box: Sandbox):
    _failure("INVALID_ARGUMENT", tools.fs_read, "a\x00b")


def test_error_messages_never_leak_absolute_paths(box: Sandbox, tmp_path: Path):
    failures = [
        _failure("PERMISSION_DENIED", tools.fs_read, str(box.outside / "secret.txt")),
        _failure("PERMISSION_DENIED", tools.fs_read, "../outside/secret.txt"),
        _failure("NOT_FOUND", tools.fs_read, "missing.txt"),
        _failure("INVALID_ARGUMENT", tools.fs_read, "."),
    ]
    for failure in failures:
        assert str(tmp_path) not in failure.message
        assert str(box.root) not in failure.message
        assert str(box.outside) not in failure.message


def test_list_kind_covers_regular_files_and_directories(box: Sandbox):
    (box.root / "a.txt").write_text("x", encoding="utf-8")
    (box.root / "d").mkdir()
    kinds = {entry["name"]: entry["kind"] for entry in tools.fs_list(".")["entries"]}
    assert kinds == {"a.txt": "file", "d": "dir"}
    assert stat_module.S_ISREG(os.stat(box.root / "a.txt").st_mode)
