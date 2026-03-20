"""项目入口：保留 `app:app` 启动方式，内部使用模块化实现。"""

from wt_server.routes import create_app


# 兼容现有启动命令：uvicorn app:app
app = create_app()
