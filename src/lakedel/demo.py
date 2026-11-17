"""仓库自带 fixture 的离线演示入口。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import AuditResult
from .service import produce_outputs
from .util import FIXED_RUN_AT


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def run_demo(
    out_dir: str | Path | None = None,
    formats: list[str] | None = None,
    run_at: str = FIXED_RUN_AT,
) -> tuple[AuditResult, list[dict[str, Any]], Path, Path]:
    root = project_root()
    workspace = root / "examples" / "lake"
    out = Path(out_dir) if out_dir else root / "out" / "demo"
    selected = formats or ["json", "md", "html"]
    ledger = out / "ledger.sqlite"
    label = "out/demo/ledger.sqlite" if out_dir is None else str(ledger)
    result, artifacts, ledger = produce_outputs(
        workspace, out, selected, ledger, None, run_at, label
    )
    return result, artifacts, ledger, workspace
