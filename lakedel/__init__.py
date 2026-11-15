"""源码目录 shim，使 `python -m lakedel` 无需安装即可运行。"""

from pathlib import Path

__version__ = "0.1.0"
__path__.append(str(Path(__file__).resolve().parents[1] / "src" / "lakedel"))
