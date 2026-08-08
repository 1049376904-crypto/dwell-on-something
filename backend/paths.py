"""统一确定数据库位置。

以前 DB_PATH 默认是相对路径 dwell.db，它依赖进程启动目录。同一份代码在
不同目录启动，就会生成不同的 dwell.db，看起来就像“重启后聊天、日历、
悄悄话全部消失”。这里把数据库固定到 backend/data/dwell.db，并在首次运行时
自动迁移旧文件。
"""

import os
import shutil
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parent
DATA_DIR = BACKEND_DIR / "data"
CANONICAL_DB = DATA_DIR / "dwell.db"

# 过去可能产生过数据库的位置（按优先级）。
LEGACY_DB_PATHS = [
    BACKEND_DIR / "dwell.db",
    BACKEND_DIR.parent / "dwell.db",
    Path.home() / "dwell.db",
]


def _db_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def resolve_db_path() -> Path:
    """返回最终使用的数据库路径，必要时迁移最大的旧数据库。"""
    env_value = os.getenv("DWELL_DB_PATH") or os.getenv("DB_PATH")
    if env_value:
        target = Path(env_value).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if _db_size(CANONICAL_DB) > 0:
        return CANONICAL_DB

    candidates = [p for p in LEGACY_DB_PATHS if _db_size(p) > 0]
    if candidates:
        source = max(candidates, key=_db_size)
        # 保留原文件，只复制，便于出错时回退。
        shutil.copy2(source, CANONICAL_DB)
        for extra in ("-wal", "-shm"):
            side = source.with_name(source.name + extra)
            if side.exists():
                shutil.copy2(side, CANONICAL_DB.with_name(CANONICAL_DB.name + extra))
        print(f"[dwell] 已迁移旧数据库: {source} -> {CANONICAL_DB}")

    return CANONICAL_DB
