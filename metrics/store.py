"""
指标口径存储层：把「指标口径字典」从代码里的静态 METRICS 变成可持久化、可自定义。

设计：
- 建表 metric_definitions，存放口径的 名称 / 别名 / 公式 / SQL 表达式 / 所属触发词 / 是否默认 / 是否内置。
- 首次启动时用 metrics/definitions.py 里的内置 METRICS + GENERIC_TRIGGERS 做一次 seed，
  之后前端页面的增删改都落库，成为唯一数据源（source of truth）。
- agent 侧的 detect_ambiguous / format_metric_dict 通过 load_merged() 读取合并后的口径，
  因此页面上的修改会实时生效（本进程内带缓存，写操作会失效缓存）。
"""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 项目根目录（metrics/store.py -> metrics/ -> 项目根）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 口径库文件路径，可用环境变量 METRICS_DB_PATH 覆盖（默认放在 tools/ 下，与权限库同目录）
METRICS_DB_PATH = os.getenv("METRICS_DB_PATH", str(_PROJECT_ROOT / "tools" / "metrics.db"))

_META_SEED_KEY = "seeded"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_definitions (
    canonical    TEXT PRIMARY KEY,          -- 口径标准名（唯一，如「销售毛利率」）
    aliases      TEXT NOT NULL DEFAULT '',  -- 别名，JSON 数组字符串，如 ["gross margin"]
    formula      TEXT NOT NULL DEFAULT '',  -- 中文公式 / 口径说明
    sql_expr     TEXT NOT NULL DEFAULT '',  -- 计算用 SQL 表达式
    group_name   TEXT NOT NULL DEFAULT '',  -- 所属通用触发词（如「毛利率」），用于歧义检测分组
    is_default   INTEGER NOT NULL DEFAULT 0,
    is_builtin   INTEGER NOT NULL DEFAULT 0,
    updated_at   REAL NOT NULL
)
"""

# 进程内缓存：{metrics: {canonical: MetricDef}, triggers: {触发词: [canonical,...]}}
# 避免每轮 LLM 调用都查库；写操作（增删改）会失效缓存。
_CACHE: Dict[str, Optional[object]] = {"metrics": None, "triggers": None}
_CACHE_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    """打开一个新的 SQLite 连接（每次操作独立连接，避免跨线程共享连接）。"""
    return sqlite3.connect(METRICS_DB_PATH)


def _invalidate_cache() -> None:
    """写操作后使缓存失效，下次 load_merged() 会重新读库。"""
    with _CACHE_LOCK:
        _CACHE["metrics"] = None
        _CACHE["triggers"] = None


def _init(seed: bool = True) -> None:
    """建表 +（首次）写入内置口径。"""
    os.makedirs(Path(METRICS_DB_PATH).parent, exist_ok=True)
    conn = _connect()
    try:
        conn.execute(_SCHEMA)
        conn.execute("CREATE TABLE IF NOT EXISTS metrics_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        if seed:
            _seed_if_needed(conn)
    finally:
        conn.close()


def _seed_if_needed(conn: sqlite3.Connection) -> None:
    """只在从未 seed 过时，把内置 METRICS + GENERIC_TRIGGERS 写入一次（用 meta 标记）。

    用 meta 标记而非「表为空」判断，是为了保证：用户把内置口径都删掉后，不会在下次请求时又被自动加回来。
    """
    row = conn.execute("SELECT value FROM metrics_meta WHERE key = ?", (_META_SEED_KEY,)).fetchone()
    if row and row[0] == "1":
        return

    # 延迟导入，避免与 definitions.py 形成顶层循环依赖（definitions 不会在顶层 import store）
    from metrics.definitions import METRICS, GENERIC_TRIGGERS

    # 反向生成 canonical -> 触发词 映射
    canonical_to_group: Dict[str, str] = {}
    for trigger, canonicals in GENERIC_TRIGGERS.items():
        for c in canonicals:
            canonical_to_group[c] = trigger

    now = time.time()
    for canonical, m in METRICS.items():
        conn.execute(
            """
            INSERT OR IGNORE INTO metric_definitions
                (canonical, aliases, formula, sql_expr, group_name, is_default, is_builtin, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                canonical,
                json.dumps(m.specific_aliases, ensure_ascii=False),
                m.formula,
                m.sql_expr,
                canonical_to_group.get(canonical, ""),
                1 if m.default else 0,
                1,  # 内置
                now,
            ),
        )
    conn.execute(
        "INSERT INTO metrics_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_META_SEED_KEY, "1"),
    )
    conn.commit()


def _row_to_metric(row: tuple) -> dict:
    """把一行数据库记录转成前端友好的 dict。"""
    canonical, aliases, formula, sql_expr, group_name, is_default, is_builtin, updated_at = row
    try:
        alias_list = json.loads(aliases) if aliases else []
    except Exception:
        alias_list = []
    return {
        "canonical": canonical,
        "aliases": alias_list,
        "formula": formula,
        "sql_expr": sql_expr,
        "group": group_name,
        "default": bool(is_default),
        "builtin": bool(is_builtin),
        "updated_at": updated_at,
    }


def list_metrics() -> List[dict]:
    """列出全部指标口径（内置 + 自定义），供前端页面展示。每次直接读库，保证实时。"""
    _init(seed=True)
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT canonical, aliases, formula, sql_expr, group_name, is_default, is_builtin, updated_at
            FROM metric_definitions
            ORDER BY is_builtin DESC, canonical
            """
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_metric(r) for r in rows]


def upsert_metric(
    canonical: str,
    aliases: Optional[List[str]] = None,
    formula: str = "",
    sql_expr: str = "",
    group: str = "",
    default: bool = False,
) -> dict:
    """新增或更新一条口径（按 canonical 幂等 upsert）。

    编辑已存在的口径时保留 is_builtin 原值；新增的口径 is_builtin=0（自定义）。
    """
    canonical = (canonical or "").strip()
    if not canonical:
        raise ValueError("口径名不能为空")

    _init(seed=True)
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO metric_definitions
                (canonical, aliases, formula, sql_expr, group_name, is_default, is_builtin, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(canonical) DO UPDATE SET
                aliases    = excluded.aliases,
                formula    = excluded.formula,
                sql_expr   = excluded.sql_expr,
                group_name = excluded.group_name,
                is_default = excluded.is_default,
                updated_at = excluded.updated_at
            """,
            (
                canonical,
                json.dumps(aliases or [], ensure_ascii=False),
                formula or "",
                sql_expr or "",
                group or "",
                1 if default else 0,
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    _invalidate_cache()
    return {
        "canonical": canonical,
        "aliases": aliases or [],
        "formula": formula or "",
        "sql_expr": sql_expr or "",
        "group": group or "",
        "default": bool(default),
    }


def delete_metric(canonical: str) -> bool:
    """删除一条口径，返回是否真的删掉了。"""
    _init(seed=True)
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM metric_definitions WHERE canonical = ?", (canonical,))
        conn.commit()
        deleted = cur.rowcount > 0
    finally:
        conn.close()

    _invalidate_cache()
    return deleted


def load_merged():
    """读取合并后的口径，还原成 agent 需要的两套结构：

    - metrics: {canonical: MetricDef}
    - triggers: {触发词: [canonical, ...]}

    带进程内缓存，写操作（upsert/delete）会失效缓存，保证页面修改实时生效。
    """
    with _CACHE_LOCK:
        if _CACHE["metrics"] is not None:
            return _CACHE["metrics"], _CACHE["triggers"]

        from metrics.definitions import MetricDef

        _init(seed=True)
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT canonical, aliases, formula, sql_expr, group_name, is_default FROM metric_definitions"
            ).fetchall()
        finally:
            conn.close()

        metrics: Dict[str, MetricDef] = {}
        triggers: Dict[str, List[str]] = {}
        for canonical, aliases, formula, sql_expr, group_name, is_default in rows:
            try:
                alias_list = json.loads(aliases) if aliases else []
            except Exception:
                alias_list = []
            metrics[canonical] = MetricDef(
                canonical=canonical,
                specific_aliases=alias_list,
                formula=formula,
                sql_expr=sql_expr,
                default=bool(is_default),
            )
            if group_name:
                triggers.setdefault(group_name, []).append(canonical)

        _CACHE["metrics"] = metrics
        _CACHE["triggers"] = triggers
        return metrics, triggers
