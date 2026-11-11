"""把 tools/example.db（SQLite）一次性迁移到 PostgreSQL。

用法：
    python tools/migrate_sqlite_to_pg.py

说明：
- 目标库来自 tools/pg.py（默认 127.0.0.1:5432/mydb，可用 PG_* 环境变量覆盖）。
- 表名 / 列名统一转小写（PG 未加引号的标识符会折叠成小写，正好匹配 LLM 生成的大写 SQL）。
- PAYMENTS 里的中文列名映射为 ASCII，避免 PG 引用中文标识符需要双引号：
      销售收入 → sales_revenue          实收金额 → actual_received_amount
      销售成本 → sales_cost            采购成本 → purchase_cost
      毛利额   → gross_profit          净利润   → net_profit
- 幂等：先 DROP 再 CREATE，重跑会覆盖旧表（不建外键，业务是只读查询，关系靠知识文档维护）。
"""
import os
import sqlite3
import sys

import psycopg2
from psycopg2.extras import execute_batch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.pg import pg_conn_params  # noqa: E402

SQLITE_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "example.db")

# 中文列名 → ASCII（仅 PAYMENTS 表用到）
CHINESE_COLUMN_MAP = {
    "销售收入": "sales_revenue",
    "实收金额": "actual_received_amount",
    "销售成本": "sales_cost",
    "采购成本": "purchase_cost",
    "毛利额": "gross_profit",
    "净利润": "net_profit",
}

_SQLITE_TYPE_MAP = {
    "INTEGER": "integer",
    "INT": "integer",
    "TEXT": "text",
    "REAL": "double precision",
    "FLOAT": "double precision",
    "NUMERIC": "double precision",
    "BOOLEAN": "boolean",
}


def _pg_type(sqlite_type: str) -> str:
    if not sqlite_type:
        return "text"
    base = sqlite_type.upper().split("(")[0].strip()
    return _SQLITE_TYPE_MAP.get(base, "text")


def _pg_column_name(name: str) -> str:
    return CHINESE_COLUMN_MAP.get(name, name.lower())


def _sqlite_tables(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def migrate() -> None:
    sconn = sqlite3.connect(SQLITE_DB)
    pconn = psycopg2.connect(**pg_conn_params())
    pconn.autocommit = True
    pcur = pconn.cursor()

    tables = _sqlite_tables(sconn)
    print(f"[migrate] SQLite 表：{tables}")

    for table in tables:
        cols = sconn.execute(f"PRAGMA table_info({table})").fetchall()
        # cols: (cid, name, type, notnull, dflt_value, pk)
        pg_table = table.lower()
        pg_cols = [(_pg_column_name(c[1]), _pg_type(c[2]), c[5] > 0) for c in cols]

        pcur.execute(f'DROP TABLE IF EXISTS "{pg_table}" CASCADE')
        ddl_cols = []
        for name, ptype, is_pk in pg_cols:
            col_sql = f'"{name}" {ptype}'
            if is_pk:
                col_sql += " PRIMARY KEY"
            ddl_cols.append(col_sql)
        pcur.execute(f'CREATE TABLE "{pg_table}" ({", ".join(ddl_cols)})')

        rows = sconn.execute(f"SELECT * FROM {table}").fetchall()
        col_names = [f'"{name}"' for name, _, _ in pg_cols]
        placeholders = ", ".join(["%s"] * len(pg_cols))
        insert_sql = f'INSERT INTO "{pg_table}" ({", ".join(col_names)}) VALUES ({placeholders})'
        if rows:
            execute_batch(pcur, insert_sql, rows)
        print(f"[migrate] {table} -> {pg_table}: {len(rows)} 行")

    sconn.close()
    pcur.close()
    pconn.close()
    print("[migrate] 完成")


if __name__ == "__main__":
    migrate()
