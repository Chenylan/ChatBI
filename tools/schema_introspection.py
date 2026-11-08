"""数据源 schema 明细查询。

从「当前连接的数据源」（本项目为 SQLite 的 tools/example.db）读取出所有表的结构明细：
表名、行数、每个字段的名称 / 类型 / 是否可空 / 是否主键，供前端「数据表明细」页面展示。

与 tools/tools_execute_sqlite.py 共用同一个 DATABASE_PATH，保证页面展示的就是
自助问数真正在查的那份数据源，避免「页面一套、查询一套」的错位。
"""
import sqlite3
from typing import Any, Dict, List, Optional

# 复用 execute_sqlite_query 的数据源路径（当前连接的数据源）。
# 注意：这里会连带加载 langchain 等依赖，但 schema 接口与 agent 本就共用同一进程，
# 属于一次性成本，不会额外引入新依赖。
from tools.tools_execute_sqlite import DATABASE_PATH


def get_sqlite_schema(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """返回当前数据源下所有用户表的明细列表。

    返回形如：
        [
            {
                "tableName": "ORDER_DETAILS",
                "rowCount": 30,
                "columns": [
                    {"name": "ORDER_ID", "type": "INTEGER", "nullable": False, "primaryKey": True},
                    ...
                ],
            },
            ...
        ]

    只列出用户表（name NOT LIKE 'sqlite_%'），排除 SQLite 内部表（sqlite_sequence 等）。
    """
    db_path = db_path or DATABASE_PATH
    conn = sqlite3.connect(db_path)
    try:
        # 从 sqlite_master 拿所有用户表名（视图/索引不在此列）
        tables = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()

        result: List[Dict[str, Any]] = []
        for (name,) in tables:
            # PRAGMA table_info 列顺序固定为：cid, name, type, notnull, dflt_value, pk
            cols = conn.execute(f"PRAGMA table_info({name})").fetchall()
            columns = [
                {
                    "name": c[1],            # 字段名
                    "type": c[2],            # 类型（SQLite 为宽松类型，如实展示）
                    "nullable": c[3] == 0,   # notnull=0 表示可空
                    "primaryKey": c[5] > 0,  # pk>0 表示是主键（复合主键为序号）
                }
                for c in cols
            ]

            # 行数：COUNT(*) 只读、开销小；失败则置 None，前端显示 "-"
            try:
                row_count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            except sqlite3.Error:
                row_count = None

            result.append({"tableName": name, "rowCount": row_count, "columns": columns})
        return result
    finally:
        conn.close()
