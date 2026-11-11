# -*- coding: utf-8 -*-
"""
ChatBI 后端服务器
基于 FastAPI，提供 Agent API 接口
"""
import os
import warnings
from contextlib import asynccontextmanager
from pathlib import Path
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

load_dotenv()

# 抑制 Streamlit 在非 Streamlit 环境中的警告
# 这些警告在 FastAPI 后端环境中是正常的，可以安全忽略
warnings.filterwarnings("ignore", category=UserWarning, module="streamlit")
os.environ["STREAMLIT_SERVER_RUNNING"] = "false"


def log_setting():
    """配置日志"""
    log_path = Path(os.getenv("LOG_PATH", Path(__file__).resolve().parent.parent / "logs" / "server.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_format = "{time:YYYY-MM-DD HH:mm:ss.SSS} {level} {module}.{function} {message}"
    logger.add(log_path, format=log_format, rotation="200 MB")


def init_runtime_stores():
    """启动时初始化运行时数据（权限库 + 口径库）。

    权限表（user_table_acl）与口径表（metric_definitions）都存放在 PostgreSQL
    （public schema），这里在服务启动时幂等建表 + 写入示例数据，避免首次查询时
    因表未初始化而报「当前用户未授权」或口径字典为空。
    """
    try:
        from tools.access_control import init_permission_db
        init_permission_db(seed=True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"权限库初始化失败（请确认 PostgreSQL 已启动、连接参数正确）：{e}")

    try:
        from metrics.store import _init as init_metrics_db
        init_metrics_db(seed=True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"口径库初始化失败（请确认 PostgreSQL 已启动、连接参数正确）：{e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时配置日志、初始化运行时数据。"""
    log_setting()
    init_runtime_stores()
    yield


def create_app() -> FastAPI:
    """创建 FastAPI 应用"""
    _app = FastAPI(
        title="ChatBI API",
        description="ChatBI 智能数据对话助手 API",
        version="1.0.0",
        lifespan=lifespan,
    )

    register_middleware(_app)
    register_router(_app)

    return _app


def register_middleware(app: FastAPI):
    """注册中间件"""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # 生产环境应该限制具体域名
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )


def register_router(app: FastAPI):
    """注册路由"""
    import sys
    from pathlib import Path
    
    # 确保项目根目录在 Python 路径中
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    
    from backend.api import router
    app.include_router(router, prefix="/api")


app = create_app()


if __name__ == "__main__":
    import sys
    import os
    
    # 添加项目根目录到 Python 路径
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))
    
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    
    logger.info(f"Starting ChatBI server on {host}:{port}")
    
    uvicorn.run(
        app="backend.server:app",
        host=host,
        port=port,
        reload=os.getenv("ENV", "local") == "local",
    )

