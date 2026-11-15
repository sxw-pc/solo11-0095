"""确定性、路径安全与文件工具。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import AuditError

FIXED_RUN_AT = "2024-06-01T00:00:00Z"
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AuditError("PATH_MISSING", f"无法读取文件: {exc}", str(path)) from exc
    return digest.hexdigest()


def safe_join(root: Path, relative: str) -> Path:
    """拒绝绝对路径、反斜杠、点段和符号链接逃逸。"""
    if not isinstance(relative, str) or not relative:
        raise AuditError("PATH_ESCAPE", "路径必须是非空相对路径", str(relative))
    if "\x00" in relative or "\\" in relative:
        raise AuditError("PATH_ESCAPE", "路径包含非法字符", relative)
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise AuditError("PATH_ESCAPE", "路径包含空段、点段或上跳段", relative)
    if PurePosixPath(relative).is_absolute() or _WINDOWS_DRIVE.match(relative):
        raise AuditError("PATH_ESCAPE", "不允许绝对路径", relative)
    root_resolved = Path(root).resolve()
    candidate = (root_resolved / Path(*parts)).resolve(strict=False)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise AuditError("PATH_ESCAPE", "路径逃逸出 workspace", relative) from exc
    return candidate


def require_file(path: Path, label: str = "") -> Path:
    if not path.is_file():
        raise AuditError("PATH_MISSING", f"文件不存在: {label or path.name}", str(path))
    return path


def read_text(path: Path, encoding: str = "utf-8") -> str:
    require_file(path)
    try:
        return path.read_text(encoding=encoding)
    except (OSError, UnicodeError) as exc:
        raise AuditError("FILE_DECODE_ERROR", f"无法读取文本: {exc}", str(path)) from exc


def load_json_file(path: Path) -> Any:
    text = read_text(path)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuditError(
            "JSON_INVALID", f"JSON 解析失败: 第 {exc.lineno} 行 {exc.msg}", str(path)
        ) from exc


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
    )
    try:
        with handle:
            handle.write(text)
        os.replace(handle.name, path)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def validate_run_at(value: str) -> str:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditError("CLI_USAGE", f"--run-at 不是合法 ISO 时间: {value}") from exc
    return value


def relative_posix(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise AuditError("PATH_ESCAPE", "文件不在 workspace 内", str(path)) from exc
