"""数据源 schema 明细查询。

从「当前连接的数据源」（本项目为 PostgreSQL，public schema）读取出所有业务表的结构明细：
表名、行数、每个字段的名称 / 类型 / 是否可空 / 是否主键，供前端「数据表明细」页面展示。

与 tools/tools_execute_sql.py 共用同一个 PG 数据源，保证页面展示的就是自助问数真正在查的那份数据，
避免「页面一套、查询一套」的错位。内部的权限表（user_table_acl）、口径表（metric_definitions /
metrics_meta）与 pgvector 内部表（langchain_pg_*）会被过滤，不在此处展示。
"""
from typing import Any, Dict, List


def get_pg_schema() -> List[Dict[str, Any]]:
    """返回当前 PostgreSQL 数据源下所有业务表的明细列表（表名 + 字段 + 类型 + 行数）。

    从 information_schema 读取，只列 public schema 下的普通业务表；表名 / 列名统一转大写，
    与知识文档（knowledge/*.md）里的大写标识符保持一致，便于前端展示。
    """
    import psycopg2

    from tools.pg import pg_conn_params

    conn = psycopg2.connect(**pg_conn_params())
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
              AND table_name NOT IN ('metric_definitions', 'metrics_meta', 'user_table_acl')
              AND table_name NOT LIKE 'langchain_pg_%'
            ORDER BY table_name
            """
        )
        tables = [r[0] for r in cur.fetchall()]

        # 主键：一次查出所有表的主键列
        cur.execute(
            """
            SELECT tc.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = 'public'
            """
        )
        pk_map: Dict[str, set] = {}
        for tbl, col in cur.fetchall():
            pk_map.setdefault(tbl, set()).add(col)

        result: List[Dict[str, Any]] = []
        for name in tables:
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
                """,
                (name,),
            )
            columns = [
                {
                    "name": col_name.upper(),
                    "type": data_type,
                    "nullable": is_nullable == "YES",
                    "primaryKey": col_name in pk_map.get(name, set()),
                }
                for col_name, data_type, is_nullable in cur.fetchall()
            ]

            try:
                cur.execute(f'SELECT COUNT(*) FROM "public"."{name}"')
                row_count = cur.fetchone()[0]
            except Exception:
                row_count = None

            result.append({"tableName": name.upper(), "rowCount": row_count, "columns": columns})

        cur.close()
        return result
    finally:
        conn.close()
