"""PostgreSQL 查询工具（供 agent 调用）。

取代 tools/tools_execute_sqlite.py：业务库从 SQLite 迁移到 PostgreSQL 后，
查询统一走 tools/db_query.PostgresQueryTool（三层防护：只读白名单 + 表级权限 +
EXPLAIN 预检 + 服务端 statement_timeout + 结果集截断）。
"""
from typing import Any, Dict

from langchain_core.tools import tool

from tools.db_query import PostgresQueryTool
from tools.pg import pg_conn_params

# 模块级单例：连接参数固定，查询时按 ContextVar current_user 动态做权限校验。
# PostgresQueryTool.run() 每次都会读 get_allowed_tables()（内部取 current_user），
# 因此单例不会串用户。
_tool = PostgresQueryTool(**pg_conn_params())


@tool(
    "execute_sql_query",
    description="Execute a PostgreSQL query on the fixed database and return the results as JSON. "
    "Use this tool to query the database.",
)
def execute_sql_query(query: str) -> Dict[str, Any]:
    """执行只读 SQL 查询并返回结果。

    参数:
        query: 要执行的 SQL 查询（只读 SELECT）
    返回:
        {"status": "success", "result": {"query", "columns", "rows"}}
        {"status": "too_large", "result": {...}}
        {"status": "error", "error": "..."}
    """
    out = _tool.run(query)
    # 回传本次实际执行的 SQL，供最终答案「列出计算过程」，也便于对账、防止编造
    if isinstance(out, dict) and out.get("status") == "success" and isinstance(out.get("result"), dict):
        out["result"]["query"] = query
    return out
