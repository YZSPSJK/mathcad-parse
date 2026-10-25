"""停止项目服务后，仅清理带有本项目所有权记录的 Mathcad 进程。"""
from backend.config import settings
from backend.worker import cleanup_owned, host_lock


if __name__ == "__main__":
    with host_lock():
        directory = settings().directory() / "work"
        if directory.exists():
            for work in directory.iterdir():
                if work.is_dir():
                    cleanup_owned(work)
