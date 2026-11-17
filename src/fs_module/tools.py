"""Filesystem tools — pure functions, stdlib only, ZERO SDK imports.

This module's entire reason to exist is the boundary, so the boundary is one
function (`resolve_path`) that every tool calls exactly once, before touching
anything:

* the path is resolved with `Path.resolve()` — `..`, absolute paths and
  symlinks are therefore judged by where they *really* point, not by how they
  are spelled;
* the resolved path must be inside one of the roots from `FS_ROOTS`
  (default: `FS_DATA_DIR`), compared with `Path.is_relative_to` — never a
  string prefix (`/data/workspace-evil` must not pass for `/data/workspace`);
* the roots themselves are resolved too (a root may be a symlink);
* no roots configured means every call is refused: the module fails closed.

Error messages carry the *root-relative* path only. A host path leaking into a
message is a leak the model can use to probe the filesystem, so it never
happens — escape failures deliberately name no path at all.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat as stat_module
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "tool",
    "ToolFailure",
    "MAX_IO_BYTES",
    "DEFAULT_MAX_BYTES",
    "MAX_LIST_ENTRIES",
    "configured_roots",
    "resolve_path",
    "fs_list",
    "fs_stat",
    "fs_read",
    "fs_write",
    "fs_delete",
    "all_tools",
]

#: Hard ceiling for one read or one write, independent of the caller's
#: `max_bytes`. A module that can be asked for an unbounded blob is a module
#: that can be asked to exhaust memory.
MAX_IO_BYTES = 1024 * 1024

#: Default `fs_read` window: a model asking for "a file" usually wants a peek,
#: not 1 MiB of tokens.
DEFAULT_MAX_BYTES = 65_536

#: `fs_list` returns at most this many entries; `truncated` says whether the
#: directory had more. Reading a directory is otherwise unbounded work.
MAX_LIST_ENTRIES = 10_000

#: One plaintext frame of the v1 transport is 65519 bytes and v1 has no
#: fragmentation. `fs_read` can locally return up to `MAX_IO_BYTES`, but a
#: result bigger than a frame cannot be sent to core — see README "Frame
#: ceiling". Kept as a documented constant so the limitation is visible to
#: readers instead of being discovered in production.
FRAME_PLAINTEXT_BYTES = 65_519


class ToolFailure(Exception):
    """A structured failure raised by a pure tool.

    Deliberately NOT `vade_sdk.ToolError`: importing the SDK here would break
    the "write and test tools before the SDK" rule. `server.py` translates
    this into a ToolError with the same C-04 code, so nothing is lost.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def tool(
    name: str,
    description: str,
    *,
    risk: str = "low",
    timeout_ms: int = 30_000,
    idempotent: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Attach manifest metadata to a function (no SDK, no validation logic)."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.tool_meta = {  # type: ignore[attr-defined]
            "name": name,
            "description": description,
            "risk": risk,
            "timeout_ms": timeout_ms,
            "idempotent": idempotent,
        }
        return fn

    return decorate


# --------------------------------------------------------------------- arguments


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ToolFailure("INVALID_ARGUMENT", f"{field} must be a string, got {type(value).__name__}")
    return value


def _require_int(value: Any, field: str) -> int:
    # bool is an int subclass; `max_bytes=True` must not become 1 byte.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolFailure("INVALID_ARGUMENT", f"{field} must be an integer, got {type(value).__name__}")
    return value


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ToolFailure("INVALID_ARGUMENT", f"{field} must be a boolean, got {type(value).__name__}")
    return value


# ------------------------------------------------------------------------ roots


def _split_roots(raw: str) -> list[str]:
    """Split on `os.pathsep` (':' on POSIX) and drop blanks."""
    return [chunk for chunk in raw.split(os.pathsep) if chunk.strip()]


def configured_roots() -> tuple[Path, ...]:
    """Roots, resolved, from `FS_ROOTS` (default `FS_DATA_DIR`), per call.

    Read per call on purpose: a module started with an empty `FS_DATA_DIR` and
    later given a root must not need a restart, and a test can move the
    boundary without re-importing the module.
    """
    raw = os.environ.get("FS_ROOTS")
    if raw is None:
        raw = os.environ.get("FS_DATA_DIR", "")
    roots: list[Path] = []
    for chunk in _split_roots(raw):
        try:
            resolved = Path(chunk).resolve(strict=False)
        except OSError:  # unresolvable root cannot widen the boundary
            continue
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def resolve_path(path: Any) -> tuple[Path, Path, str]:
    """Resolve `path` and prove it is inside a configured root.

    Returns `(resolved_path, matched_root, root_relative_path)` where the last
    element is "." for the root itself.

    Relative paths are taken relative to the **first** configured root; that is
    the only interpretation that stays deterministic with several roots, and it
    is what a model means by "notes.md".
    """
    text = _require_str(path, "path")
    if "\x00" in text:
        raise ToolFailure("INVALID_ARGUMENT", "path must not contain NUL bytes")
    roots = configured_roots()
    if not roots:
        raise ToolFailure(
            "PERMISSION_DENIED",
            "no filesystem roots are configured (set FS_ROOTS or FS_DATA_DIR)",
        )

    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = roots[0] / candidate

    try:
        # One resolve, before any check: symlinks are followed here, so an
        # escape cannot hide behind a link or a "..".
        real = candidate.resolve(strict=False)
    except OSError as exc:
        raise ToolFailure(
            "INVALID_ARGUMENT", f"path cannot be resolved: {exc.strerror or 'invalid path'}"
        ) from None

    for root in roots:
        if real == root or real.is_relative_to(root):
            relative = "." if real == root else real.relative_to(root).as_posix()
            return real, root, relative

    # No path in the message: the caller's spelling may be absolute, and the
    # matched roots are host detail.
    raise ToolFailure("PERMISSION_DENIED", "path escapes the configured roots")


def _raise_os(exc: OSError, relative: str) -> None:
    """Translate an OS error into one of this module's four codes."""
    code = exc.errno
    if code == errno.ENOENT:
        raise ToolFailure("NOT_FOUND", f"{relative} does not exist") from None
    if code in (errno.EACCES, errno.EPERM):
        raise ToolFailure("PERMISSION_DENIED", f"permission denied for {relative}") from None
    if code == errno.ELOOP:
        # O_NOFOLLOW on the last component: the path became a symlink between
        # the check and the open.
        raise ToolFailure("PERMISSION_DENIED", f"{relative} is a symlink; refusing to follow it") from None
    if code == errno.EISDIR:
        raise ToolFailure("INVALID_ARGUMENT", f"{relative} is a directory") from None
    if code == errno.ENOTDIR:
        raise ToolFailure("INVALID_ARGUMENT", f"{relative} is not a directory") from None
    if code in (errno.ENOTEMPTY, errno.EEXIST):
        raise ToolFailure("INVALID_ARGUMENT", f"{relative} is not empty; pass recursive=true") from None
    if code in (errno.EXDEV,):
        raise ToolFailure("INVALID_ARGUMENT", f"{relative} spans devices") from None
    raise ToolFailure("INTERNAL", f"{relative}: {exc.strerror or type(exc).__name__}") from None


def _kind_of(mode: int) -> str:
    if stat_module.S_ISDIR(mode):
        return "dir"
    if stat_module.S_ISREG(mode):
        return "file"
    return "other"


# ------------------------------------------------------------------------ tools


@tool(
    "fs_list",
    "列出目录下的直接子项（不递归）：name/kind/size/mtime；超过 10000 条截断",
    timeout_ms=10_000,
    idempotent=True,
)
def fs_list(path: str) -> dict[str, Any]:
    """List one directory level. Symlinks are reported, never followed."""
    real, _root, relative = resolve_path(path)
    try:
        info = os.stat(real)
    except OSError as exc:
        _raise_os(exc, relative)
    if not stat_module.S_ISDIR(info.st_mode):
        raise ToolFailure("INVALID_ARGUMENT", f"{relative} is not a directory")

    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        with os.scandir(real) as iterator:
            for entry in iterator:
                # Stop *before* appending the (MAX+1)-th entry: scanning a
                # million-entry directory must cost 10 000 records, not a
                # million. The lost determinism is documented: with more than
                # the cap the caller gets the first `MAX_LIST_ENTRIES` in
                # directory order, sorted by name.
                if len(entries) >= MAX_LIST_ENTRIES:
                    truncated = True
                    break
                try:
                    # follow_symlinks=False: the entry is described by its own
                    # inode, so a link to /etc cannot even be measured outside.
                    entry_stat = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue  # vanished between scandir and stat
                if entry.is_symlink():
                    kind = "symlink"
                elif entry.is_dir(follow_symlinks=False):
                    kind = "dir"
                else:
                    kind = "file"
                entries.append(
                    {
                        "name": entry.name,
                        "kind": kind,
                        "size": entry_stat.st_size,
                        "mtime": int(entry_stat.st_mtime),
                    }
                )
    except PermissionError as exc:
        _raise_os(exc, relative)
    except OSError as exc:
        _raise_os(exc, relative)

    entries.sort(key=lambda item: item["name"])
    return {"entries": entries, "truncated": truncated}


@tool(
    "fs_stat",
    "返回路径的 kind/size/mtime/mode（软链先解析，越界软链拒绝）",
    timeout_ms=10_000,
    idempotent=True,
)
def fs_stat(path: str) -> dict[str, Any]:
    """Metadata of the resolved object. Never follows a link out of the roots."""
    real, _root, relative = resolve_path(path)
    try:
        info = os.stat(real)
    except OSError as exc:
        _raise_os(exc, relative)
    return {
        "kind": _kind_of(info.st_mode),
        "size": info.st_size,
        "mtime": int(info.st_mtime),
        "mode": stat_module.S_IMODE(info.st_mode),
    }


@tool(
    "fs_read",
    "读取 UTF-8 文本文件；超过 max_bytes 截断，二进制或非法 UTF-8 拒绝",
    timeout_ms=10_000,
    idempotent=True,
)
def fs_read(path: str, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """Read at most `max_bytes` of UTF-8 text (hard ceiling `MAX_IO_BYTES`)."""
    window = _require_int(max_bytes, "max_bytes")
    if window < 1 or window > MAX_IO_BYTES:
        raise ToolFailure("INVALID_ARGUMENT", f"max_bytes must be within [1, {MAX_IO_BYTES}]")

    real, _root, relative = resolve_path(path)
    try:
        info = os.stat(real)
    except OSError as exc:
        _raise_os(exc, relative)
    if stat_module.S_ISDIR(info.st_mode):
        raise ToolFailure("INVALID_ARGUMENT", f"{relative} is a directory; use fs_list")
    if not stat_module.S_ISREG(info.st_mode):
        raise ToolFailure("INVALID_ARGUMENT", f"{relative} is not a regular file")

    try:
        # +1 byte is how truncation is detected without stat-ing twice: if the
        # file grew after the stat above, this still cuts at the window.
        with open(real, "rb") as handle:
            chunk = handle.read(window + 1)
    except OSError as exc:
        _raise_os(exc, relative)

    truncated = len(chunk) > window
    data = chunk[:window]
    if b"\x00" in data:
        raise ToolFailure("INVALID_ARGUMENT", f"{relative} is a binary file (NUL byte found)")

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = _decode_truncated(data) if truncated else None
        if text is None:
            raise ToolFailure("INVALID_ARGUMENT", f"{relative} is not valid UTF-8 text") from None

    return {"text": text, "bytes": len(text.encode("utf-8")), "truncated": truncated}


def _decode_truncated(data: bytes) -> str | None:
    """Decode a window that may have cut a multi-byte character in half.

    A 64 KiB window ending mid-`你` is not a binary file. At most the three
    bytes of an incomplete UTF-8 sequence are suspect, so up to three trailing
    bytes are dropped; anything still undecodable after that really is not
    UTF-8. A window that is *entirely* an incomplete character (a 1-byte
    window over `你`) decodes to "" — the honest answer is "the window ended
    before one character fit", not "binary file".
    """
    for cut in range(1, min(4, len(data)) + 1):
        try:
            return data[:-cut].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


@tool(
    "fs_write",
    "写入 UTF-8 文本文件（高风险：会创建或覆盖文件）",
    risk="high",
    timeout_ms=15_000,
    idempotent=False,
)
def fs_write(path: str, content: str, create_parents: bool = False) -> dict[str, Any]:
    """Write `content` as UTF-8. `created` says whether the file is new."""
    body = _require_str(content, "content")
    parents = _require_bool(create_parents, "create_parents")
    payload = body.encode("utf-8")
    if len(payload) > MAX_IO_BYTES:
        raise ToolFailure(
            "PAYLOAD_TOO_LARGE",
            f"content is {len(payload)} bytes; the write limit is {MAX_IO_BYTES}",
        )

    real, root, relative = resolve_path(path)
    if real == root:
        raise ToolFailure("PERMISSION_DENIED", "refusing to write to a configured root")

    try:
        info = os.stat(real)
    except FileNotFoundError:
        info = None
    except OSError as exc:
        _raise_os(exc, relative)
    if info is not None and stat_module.S_ISDIR(info.st_mode):
        raise ToolFailure("INVALID_ARGUMENT", f"{relative} is a directory")
    if info is not None and not stat_module.S_ISREG(info.st_mode):
        # Writing a FIFO would block the worker thread until a reader appears.
        raise ToolFailure("INVALID_ARGUMENT", f"{relative} is not a regular file")

    parent = real.parent
    if not parent.is_dir():
        if not parents:
            raise ToolFailure("NOT_FOUND", f"parent directory of {relative} does not exist")
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _raise_os(exc, relative)

    created = not os.path.lexists(real)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(real, flags, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
    except OSError as exc:
        _raise_os(exc, relative)
    return {"bytes": len(payload), "created": created}


@tool(
    "fs_delete",
    "删除文件或目录（高风险：recursive=true 才删非空目录）",
    risk="high",
    timeout_ms=15_000,
    idempotent=True,
)
def fs_delete(path: str, recursive: bool = False) -> dict[str, Any]:
    """Remove a file, an empty directory, or (recursive) a whole tree."""
    deep = _require_bool(recursive, "recursive")
    real, root, relative = resolve_path(path)
    if real == root:
        raise ToolFailure("PERMISSION_DENIED", "refusing to delete a configured root")

    try:
        info = os.lstat(real)
    except OSError as exc:
        _raise_os(exc, relative)

    if stat_module.S_ISDIR(info.st_mode):
        if deep:
            try:
                # rmtree does not follow symlinks: a link inside the tree is
                # unlinked, never descended into.
                shutil.rmtree(real)
            except OSError as exc:
                _raise_os(exc, relative)
        else:
            try:
                os.rmdir(real)
            except OSError as exc:
                _raise_os(exc, relative)
    else:
        try:
            os.unlink(real)
        except OSError as exc:
            _raise_os(exc, relative)
    return {"deleted": True}


def all_tools() -> tuple[Callable[..., Any], ...]:
    """Every decorated tool, in declaration order (manifest order is stable)."""
    return (fs_list, fs_stat, fs_read, fs_write, fs_delete)
