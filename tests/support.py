"""测试共享路径与辅助函数。"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

LAKE = ROOT / "examples" / "lake"
GOLDEN = ROOT / "tests" / "golden"


def copy_lake(destination: Path) -> Path:
    shutil.copytree(LAKE, destination)
    return destination


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def finding_codes(result) -> list[str]:
    return [item.code for item in result.findings]
