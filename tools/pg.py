"""PostgreSQL 连接配置统一入口。

业务库查询（tools/db_query.PostgresQueryTool）、权限库（tools/access_control）、
口径库（metrics/store）、向量库（tools/tools_rag / memory_manager 的 PGVector）
都从这里读取连接参数，避免四处硬编码。

连接参数可用环境变量覆盖（放在项目根 .env 里）：
    PG_HOST / PG_PORT / PG_USER / PG_PASSWORD / PG_DATABASE
"""
import os
from urllib.parse import quote_plus

from dotenv import load_dotenv

load_dotenv(override=False)

PG_HOST = os.getenv("PG_HOST", "127.0.0.1")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER", "admin")
PG_PASSWORD = os.getenv("PG_PASSWORD", "123456")
PG_DATABASE = os.getenv("PG_DATABASE", "mydb")

# 业务表 / 权限表 / 口径表都放 public schema；权限校验按 schema 名对齐
PG_SCHEMA = "public"


def pg_conn_params() -> dict:
    """psycopg2 连接参数（PostgresQueryTool / access_control / metrics / schema_introspection 用）。"""
    return {
        "host": PG_HOST,
        "port": PG_PORT,
        "user": PG_USER,
        "password": PG_PASSWORD,
        "dbname": PG_DATABASE,
    }


def pg_libpq_string() -> str:
    """libpq 连接串（调试 / 备用）。"""
    return (
        f"host={PG_HOST} port={PG_PORT} user={PG_USER} "
        f"password={PG_PASSWORD} dbname={PG_DATABASE}"
    )


# langchain-postgres 的 PGVector 走 SQLAlchemy，用 psycopg(v3) 驱动
PGVECTOR_CONNECTION = (
    f"postgresql+psycopg://{quote_plus(PG_USER)}:{quote_plus(PG_PASSWORD)}"
    f"@{PG_HOST}:{PG_PORT}/{PG_DATABASE}"
)
