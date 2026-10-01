"""本地/容器统一入口：python -m app 启动 HTTP 服务（建表与种子在 create_app 内完成）。"""
from __future__ import annotations

import uvicorn

from . import config

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT)
