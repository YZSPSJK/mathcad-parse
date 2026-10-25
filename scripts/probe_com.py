"""最小真实 COM 探测；只打开本脚本的空白文档，按专用进程所有权回收资源。"""
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.automation import Prime  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.worker import cleanup_owned, host_lock  # noqa: E402


if __name__ == "__main__":
    work = settings().directory() / "smoke" / f"probe-{uuid4()}"
    work.mkdir(parents=True)
    with host_lock():
        try:
            with Prime(work, allow_unverified=True) as prime:
                print("version:", prime.version, flush=True)
                prime.open("")
                print("inputs:", prime.aliases("input"), "outputs:", prime.aliases("output"), flush=True)
                matrix = prime.sheet.CreateMatrix(2, 3)
                print("matrix set:", matrix.SetMatrixElement(0, 1, 23.5), flush=True)
                print("matrix get:", matrix.GetMatrixElement(0, 1), flush=True)
                prime.calculate()
                prime.save(work / "blank.mcdx")
                print("saved:", work / "blank.mcdx", flush=True)
        finally:
            cleanup_owned(work)
