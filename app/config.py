"""运行配置：全部通过环境变量覆盖。"""
from __future__ import annotations

import os
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# SQLite 数据库文件路径。
# 本地运行默认使用仓库内 data/ 目录；容器内通过环境变量 DB_PATH=/data/changeover.db 覆盖。
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "changeover.db"
DB_PATH = Path(os.environ.get("DB_PATH", str(_DEFAULT_DB)))

# HTTP 服务监听地址与端口
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))

# 初始化空库时是否写入演示数据（参考原料批号 + 两个批次示例）
SEED_DEMO_DATA = _env_bool("SEED_DEMO_DATA", False)
