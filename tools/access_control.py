"""表级访问控制（ACL）：让「自助问数」的权限与一张用户维度表对齐。

背景：
    ChatBI 的 LLM 会自主生成 SQL 并调用 execute_sqlite_query 查询业务库。
    如果没有约束，任何用户都能查到任何表。本模块提供「用户 → 可查询的库/表」的
    白名单校验，并把它接到 SQL 执行入口，使自助问数的权限与维度表完全一致。

实现分两层：
    1. 数据层：一张用户维度表 ``user_table_acl``，记录「用户 × 库 × 表」的可查询关系。
       示例数据见 SAMPLE_ACL（alice 只能查订单/客户/商品，bob 只能查支付/交易/商品）。
    2. 执行层：把当前用户身份用 ContextVar 从 HTTP 请求一路传到 SQL 执行工具，
       再用 SQLite 的 set_authorizer 在引擎层拦截对「未授权表」的任何读取。
       这是硬闸门（fail-closed）：不靠解析 SQL 文本，子查询 / CTE / JOIN / 视图里的
       越权表都会被 SQLite 自己拦下。

   为什么不靠「正则提取 FROM/JOIN 表名」做主校验：
       正则难以覆盖子查询、CTE、视图、多表别名等写法，漏一个就放行。
       SQLite authorizer 在引擎层拿到的是「真正被读取的表」，天然覆盖所有形态，
       因此把它作为唯一事实来源。
"""
import os
import sqlite3
from contextvars import ContextVar
from typing import Dict, Optional, Set, Tuple

# ==================== 配置 ====================

# 是否启用表级权限。生产环境务必保持 true；本地调试 / 跑旧样例时可设
# ENABLE_TABLE_ACL=false 临时关闭（默认开启，fail-closed 优先）。
ENFORCE_ACL = os.getenv("ENABLE_TABLE_ACL", "true").lower() in ("1", "true", "yes", "on")

# 当前请求的用户标识。ContextVar 会在「请求线程 → 工具执行线程」间自动传递，
# 因此只要在 agent 真正跑起来的那条线程里 set 一次，工具里 get 就能拿到。
# 默认 "anonymous"：未识别用户时 fail-closed（一张表都不放行），宁可拒绝也不越权。
current_user: ContextVar[str] = ContextVar("chatbi_current_user", default="anonymous")

# 权限维度表所在 SQLite 文件。与业务库 example.db 分开存放，
# 避免业务查询直接摸到权限表；生产环境可把 ACL_DB_PATH 指向你们真正的权限库。
_current_file_dir = os.path.dirname(os.path.abspath(__file__))
ACL_DB_PATH = os.getenv("ACL_DB_PATH", os.path.join(_current_file_dir, "user_permissions.db"))

# 权限维度表名与建表 DDL。字段即「用户 × 库 × 表」三要素；
# 真实环境若已有自己的用户维度表（字段名不同），只需改 get_allowed_tables() 里的
# SELECT 语句去读你那张表即可，其余拦截逻辑无需改动。
ACL_TABLE = "user_table_acl"
ACL_DDL = f"""
CREATE TABLE IF NOT EXISTS {ACL_TABLE} (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,             -- 用户标识（工号 / 账号）
    database_name TEXT NOT NULL,             -- 数据库名（SQLite 固定为 main；PG/MySQL 用 schema 名）
    table_name    TEXT NOT NULL,             -- 表名
    UNIQUE (user_id, database_name, table_name)
)
"""

# 示例数据：两个示例用户 + 一个管理员。表名与 tools/example.db 里的真实表对应，
# 便于直接演示「有权限的表能查、没权限的表被拦」。
SAMPLE_ACL = [
    # alice：业务分析，可查订单 / 客户 / 商品 / 用户行为
    ("alice", "main", "CUSTOMER_DETAILS"),
    ("alice", "main", "ORDER_DETAILS"),
    ("alice", "main", "PRODUCTS"),
    ("alice", "main", "USER_INTERACTIONS"),
    # bob：财务，可查支付 / 交易 / 订单 / 商品
    ("bob", "main", "ORDER_DETAILS"),
    ("bob", "main", "PAYMENTS"),
    ("bob", "main", "TRANSACTIONS"),
    ("bob", "main", "PRODUCTS"),
    # admin：全表可查（演示用）
    ("admin", "main", "CUSTOMER_DETAILS"),
    ("admin", "main", "ORDER_DETAILS"),
    ("admin", "main", "PAYMENTS"),
    ("admin", "main", "PRODUCTS"),
    ("admin", "main", "TRANSACTIONS"),
    ("admin", "main", "USER_INTERACTIONS"),
]


# ==================== 数据层：读写权限维度表 ====================

def _connect() -> sqlite3.Connection:
    """打开权限库连接（只在本模块内部使用，不挂 authorizer）。"""
    return sqlite3.connect(ACL_DB_PATH)


def _ensure_table(conn: sqlite3.Connection) -> None:
    """幂等建表：首次使用时自动建好 user_table_acl，不会报「表不存在」。"""
    conn.execute(ACL_DDL)
    conn.commit()


def init_permission_db(seed: bool = True) -> None:
    """初始化权限维度表，并（可选）写入示例数据。

    幂等：重复调用不会重复插入（依赖 (user_id, database_name, table_name) 唯一约束）。
    用法：``python -m tools.access_control``，或首次部署时手动调一次。
    """
    conn = _connect()
    try:
        _ensure_table(conn)
        if seed:
            conn.executemany(
                f"INSERT OR IGNORE INTO {ACL_TABLE} (user_id, database_name, table_name) "
                f"VALUES (?, ?, ?)",
                SAMPLE_ACL,
            )
            conn.commit()
    finally:
        conn.close()


def get_allowed_tables(user_id: Optional[str] = None) -> Set[Tuple[str, str]]:
    """查询某用户有权访问的 (库名, 表名) 集合。

    未指定 user_id 时用 current_user（当前请求用户）；查不到任何授权则返回空集，
    上层据此 fail-closed（一张表都不放行）。
    """
    uid = (user_id or current_user.get()).strip()
    if not uid:
        uid = "anonymous"

    conn = _connect()
    try:
        _ensure_table(conn)
        rows = conn.execute(
            f"SELECT database_name, table_name FROM {ACL_TABLE} WHERE user_id = ?", (uid,)
        ).fetchall()
    finally:
        conn.close()

    # 统一小写：SQLite 表名大小写不敏感，避免大小写差异导致误判
    return {(db.lower(), tbl.lower()) for db, tbl in rows}


# ==================== 执行层：SQLite authorizer 硬闸门 ====================

def build_acl_authorizer(user_id: Optional[str] = None) -> Tuple[callable, Dict]:
    """构造 SQLite authorizer 回调与状态字典，拦截对未授权表的读取。

    返回 (authorizer, state)：
      - authorizer：传给 ``conn.set_authorizer(authorizer)``
      - state["denied"]：第一次被拒绝的「库.表」，用于生成友好错误信息
      - state["allowed"]：该用户有权访问的 (库, 表) 集合（调试 / 报错用）

    拦截逻辑：
      - 只拦 SQLITE_READ（读表/列）动作，其余动作放行（写操作已由 db_query 的
        ensure_read_only_query 白名单挡掉，这里只负责「读哪些表」）。
      - 放行 ``sqlite_*`` 内部表（sqlite_master / sqlite_stat1 等），否则现有
        的 EXPLAIN 大表扫描检测会因读不到统计信息而失效。
    """
    allowed = get_allowed_tables(user_id)
    state: Dict = {"denied": None, "allowed": allowed}

    def _authorizer(action, arg1, arg2, db_name, trigger_name):
        # SQLITE_READ 的入参约定（Python sqlite3）：arg1=表名，arg2=列名，db_name=库名。
        if action == sqlite3.SQLITE_READ:
            db = (db_name or "main").lower()      # COUNT(*) 等场景 db_name 为 None，按 main 处理
            table = (arg1 or "").lower()
            # 放行 SQLite 内部表：它们不是业务数据，且 EXPLAIN 统计依赖 sqlite_stat1
            if table.startswith("sqlite_"):
                return sqlite3.SQLITE_OK
            if (db, table) not in allowed:
                state["denied"] = state["denied"] or f"{db}.{table}"
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return _authorizer, state


def permission_error_message(state: Dict) -> str:
    """根据 authorizer 记录的状态，生成给 LLM 看的友好中文错误信息。"""
    uid = current_user.get()
    if not state.get("allowed"):
        return (f"当前用户（{uid}）未配置任何可查询的表。"
                f"请联系管理员在权限表 {ACL_TABLE} 中为该用户授权。")
    return (f"无权访问表 {state['denied']}：当前用户（{uid}）没有该表的查询权限，"
            f"请改用有权限的表或联系管理员。")


# ==================== 执行层（通用）：SQL 文本级表名白名单 ====================
# 说明：SQLite 有 set_authorizer 可在引擎层逐表拦截；PostgreSQL / MySQL 没有该机制，
# 只能退回到「解析 SQL 文本 → 提取被引用的表 → 比对白名单」的 fail-closed 校验。
# 用 sqlglot 而非正则，是为了正确覆盖子查询 / CTE / JOIN / 别名，避免漏判或误判。

def extract_tables(sql: str, dialect: str = "mysql", default_schema: str = "") -> Set[Tuple[str, str]]:
    """用 sqlglot 提取 SQL 中所有被引用的 (库, 表)。

    参数：
        dialect        ：SQL 方言（"mysql" / "postgres"），影响 sqlglot 的解析规则
        default_schema ：未带库前缀的表名回落到该库名
    返回的库名/表名统一小写，与 get_allowed_tables() 的输出保持可比。
    """
    import sqlglot
    from sqlglot import exp

    tables: Set[Tuple[str, str]] = set()
    for stmt in sqlglot.parse(sql, read=dialect):
        # 先收集 CTE 名：WITH x AS (...) 里的 x 不是真实表，必须从结果里剔除，
        # 否则「SELECT * FROM x」会被误判成引用了一张叫 x 的表。
        cte_names = {cte.alias.lower() for cte in stmt.find_all(exp.CTE) if cte.alias}
        for t in stmt.find_all(exp.Table):
            name = t.name
            if not name or name.lower() in cte_names:
                continue
            db = (t.db or default_schema or "").lower()
            tables.add((db, name.lower()))
    return tables


def check_query_access(
    sql: str,
    allowed: Set[Tuple[str, str]],
    dialect: str = "mysql",
    default_schema: str = "",
) -> Optional[str]:
    """SQL 文本级表权限校验（fail-closed）。

    返回 None 表示放行；否则返回一段给 LLM 看的中文错误信息。
    任何失败（sqlglot 未安装 / 解析失败）一律拒绝，宁可拒绝也不越权。
    """
    try:
        referenced = extract_tables(sql, dialect, default_schema)
    except ImportError:
        return "表级权限校验失败：缺少 sqlglot 依赖（pip install sqlglot），已拒绝执行。"
    except Exception:
        return "SQL 无法解析，已拒绝执行（表级权限校验）"

    denied = referenced - allowed
    if denied:
        db, tbl = sorted(denied)[0]
        return f"无权访问表 {db}.{tbl}：当前用户（{current_user.get()}）没有该表的查询权限。"
    return None


# ==================== 命令行：初始化并预览示例 ====================

if __name__ == "__main__":
    init_permission_db(seed=True)
    print(f"已初始化权限维度表：{ACL_DB_PATH}")
    print(f"示例授权（{ACL_TABLE}）：")
    for uid in ("alice", "bob", "admin"):
        tables = sorted(t for _, t in get_allowed_tables(uid))
        print(f"  {uid}: {', '.join(tables)}")
