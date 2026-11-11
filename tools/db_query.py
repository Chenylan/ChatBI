"""数据库无关的 SQL 查询工具基类，及 PostgreSQL / MySQL 适配实现。

与已废弃的 SQLite 实现的差异：SQLite 是嵌入式单进程、没有服务端，
超时只能靠「看门狗线程 + conn.interrupt()」在客户端模拟。而 PostgreSQL / MySQL 是
客户端-服务器架构，超时由**服务端原生执行并真正杀掉查询**：

  - PostgreSQL / Hologres：statement_timeout（毫秒，作用于当前会话）
  - MySQL：               max_execution_time（毫秒，只作用于 SELECT）

三层防护在本基类里统一实现，子类只填「连接 / 设服务端超时 / EXPLAIN 预检」这几个
数据库差异点：
  1. 执行前：EXPLAIN 看执行计划（大表全扫 / 高成本 / 嵌套循环膨胀）
  2. 执行中：服务端超时（服务端 kill 查询，而不是客户端放弃等待）
  3. 执行后：行数 + 字节数双重截断（与数据库无关，保留在工具层）
"""
from typing import Any, Dict, List, Optional, Tuple
import json

from tools.access_control import ENFORCE_ACL, check_query_access, get_allowed_tables


# ==================== 异常分类辅助 ====================

def is_db_timeout(e: Exception) -> bool:
    """判断异常是否是「数据库服务端超时」（查询被服务端杀掉）。

    这类异常应归类为「慢查询 query」（让 LLM 改写 SQL），而不是「临时 temporary」
    （不该重试，重试同样的慢查询只会继续超时）。

    各库对应的异常：
      - PostgreSQL / Hologres：psycopg2.errors.QueryCanceled / psycopg.errors.QueryCanceled
        （SQLSTATE 57014，由 statement_timeout 或 pg_cancel_backend 触发）
      - MySQL：错误码 3024（ER_QUERY_TIMEOUT，由 max_execution_time 触发），
        pymysql 放在 args[0]，mysql-connector 放在 .errno

    这里只按类名 / SQLSTATE / 错误码判断，不 import 任何驱动，避免未装驱动时报错。
    """
    # psycopg2 / psycopg3 的 QueryCanceled（statement_timeout 超时）
    if type(e).__name__ == "QueryCanceled":
        return True
    # 更稳妥：按 PostgreSQL SQLSTATE 判断（57014 = query_canceled）
    if getattr(e, "pgcode", None) == "57014":
        return True
    # MySQL：pymysql 把错误码放在 args[0]
    args = getattr(e, "args", ())
    if args and isinstance(args[0], int) and args[0] == 3024:
        return True
    # MySQL：mysql-connector 放在 .errno
    if getattr(e, "errno", None) == 3024:
        return True
    return False


def _truncate_by_bytes(rows: List[tuple], max_bytes: int) -> Tuple[List[tuple], bool]:
    """按序列化字节数截断结果行，避免撑爆 LLM 上下文（比单纯行数更贴近 token 预算）。

    与旧 SQLite 实现的 _truncate_by_bytes 逻辑一致，这里独立实现保持模块自包含。
    返回 (截断后的行, 是否发生了截断)。
    """
    out, total = [], 0
    for row in rows:
        size = len(json.dumps(row, ensure_ascii=False, default=str))
        if total + size > max_bytes:
            return out, True
        out.append(row)
        total += size
    return out, False


# ==================== 只读白名单校验（写权限限制） ====================
# 目的：在 SQL 真正执行前拦截一切写语句（INSERT/UPDATE/DELETE/DDL）与多语句，
# 保证「只允许查询、不允许写库」。这是代码层的写权限防护，独立于数据库层面的
# 只读账号 / 只读连接（mode=ro、只读用户等），可叠加使用。
#
# 为什么用「白名单」而不是「黑名单」：
#   黑名单枚举写关键字容易漏（REPLACE、MERGE、WITH ... INSERT 等），漏掉即放行；
#   白名单只放行确定的读语句开头，未识别的语句一律拒绝，属于 fail-closed（默认拒绝）。

# 只读语句首关键字白名单：只有这些开头才允许执行。
# 注意：故意不把 PRAGMA 放进白名单——PRAGMA 既能读（table_info）也能写
# （query_only=OFF、user_version=...），整体放行会被绕过；如需只读 PRAGMA 请显式补充。
_READ_ONLY_KEYWORDS = {"select", "explain", "values"}

# 语句引入关键字全集：用于解析 WITH ... 之后「真正的主语句类型」。
# WITH 本身不分读写，它只是包裹：WITH cte AS (...) SELECT ... 是读，
# 而 WITH cte AS (...) INSERT ... 是写，必须取 CTE 之后的主语句来判定。
_STATEMENT_KEYWORDS = {
    "select", "with", "explain", "values", "pragma",
    "insert", "update", "delete", "replace", "drop", "alter", "create",
    "vacuum", "attach", "detach", "reindex", "merge", "truncate",
    "grant", "revoke", "begin", "commit", "rollback", "savepoint", "release",
}


def _skip_quoted(sql: str, i: int, quote: str) -> int:
    """跳过从 i 开始的字符串/标识符字面量，返回其结束位置的下标。

    quote 为起始引号字符：单引号、双引号、反引号、方括号。
    跳过它们是为了不让字符串内容里的关键字/分号干扰只读判定。
    """
    n = len(sql)
    if quote in ("'", '"'):
        j = i + 1
        while j < n:
            if sql[j] == quote:
                # 成对出现的两个引号（'' 或 ""）是转义，跳过
                if j + 1 < n and sql[j + 1] == quote:
                    j += 2
                    continue
                return j + 1
            j += 1
        return n
    # 反引号 ` 与方括号 [ 没有转义，直接找下一个闭合符
    close = "]" if quote == "[" else quote
    j = sql.find(close, i + 1)
    return n if j == -1 else j + 1


def _skip_line_comment(sql: str, i: int) -> int:
    """跳过 -- 行注释，返回注释结束位置（换行处）的下标。"""
    j = sql.find("\n", i + 2)
    return len(sql) if j == -1 else j


def _skip_block_comment(sql: str, i: int) -> int:
    """跳过 /* */ 块注释，返回其结束位置的下标。"""
    j = sql.find("*/", i + 2)
    return len(sql) if j == -1 else j + 2


def _top_level_tokens(sql: str) -> List[str]:
    """线性扫描 SQL，返回「括号深度 0」处的单词 token（已小写）。

    扫描过程中跳过字符串字面量 / 注释 / 各种引号标识符，因此：
      - 字符串里的 select、分号不会干扰类型判定
      - CTE / 子查询（括号内）的 SELECT 不会干扰 WITH 主语句判定
      - 注释里的关键字不会干扰
    """
    tokens: List[str] = []
    i, n, depth = 0, len(sql), 0
    while i < n:
        ch = sql[i]
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth = max(depth - 1, 0)
            i += 1
            continue
        if ch in ("'", '"', "`", "["):
            i = _skip_quoted(sql, i, ch)
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            i = _skip_line_comment(sql, i)
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            i = _skip_block_comment(sql, i)
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            if depth == 0:
                tokens.append(sql[i:j].lower())
            i = j
            continue
        i += 1
    return tokens


def _has_multiple_statements(sql: str) -> bool:
    """检测是否一条 SQL 里塞了多条语句（顶层分号后还有语句内容）。"""
    i, n, depth = 0, len(sql), 0
    while i < n:
        ch = sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        elif ch in ("'", '"', "`", "["):
            i = _skip_quoted(sql, i, ch)
            continue
        elif ch == "-" and i + 1 < n and sql[i + 1] == "-":
            i = _skip_line_comment(sql, i)
            continue
        elif ch == "/" and i + 1 < n and sql[i + 1] == "*":
            i = _skip_block_comment(sql, i)
            continue
        elif ch == ";" and depth == 0:
            # 分号后面若还有语句内容（忽略空白/注释），即多条语句
            if _top_level_tokens(sql[i + 1:]):
                return True
        i += 1
    return False


def ensure_read_only_query(query: str) -> None:
    """只读白名单校验：拒绝写语句与多语句，只放行 SELECT/EXPLAIN/VALUES 等读查询。

    校验顺序：
      1) 多语句：SELECT 1; DROP TABLE t 这类「藏写语句」直接拒绝
      2) 语句类型：首关键字（含 WITH 包裹的主语句）必须落在读白名单

    不满足时抛 PermissionError。上层 tools/tool_executor.py 的 _classify_exception
    已把 PermissionError 归类为 permanent（不可恢复），因此不会反复重试，而是让
    LLM 直接基于已有信息给出答案。
    """
    if not isinstance(query, str):
        raise PermissionError("只读模式：SQL 必须是字符串")

    if _has_multiple_statements(query):
        raise PermissionError("只读模式：禁止一次执行多条语句")

    tokens = _top_level_tokens(query)
    if not tokens:
        raise PermissionError("只读模式：空 SQL 或无法识别的语句")

    first = tokens[0]
    # WITH 可能包裹写语句（WITH cte AS (...) INSERT ...），需解析 CTE 之后的真实主语句：
    # 在顶层 token 里找第一个「语句引入关键字」（CTE 名 / AS / RECURSIVE 等会被跳过）。
    if first == "with":
        first = next((t for t in tokens[1:] if t in _STATEMENT_KEYWORDS), None)
        if first is None:
            raise PermissionError("只读模式：无法识别 WITH 后的主语句")

    if first not in _READ_ONLY_KEYWORDS:
        raise PermissionError(f"只读模式：拒绝执行非查询语句（检测到 {first.upper()}）")


# ==================== 数据库无关基类 ====================

class BaseSqlQueryTool:
    """数据库无关查询工具基类：实现三层防护的统一流程。

    子类只需实现 _connect / _set_statement_timeout / _check_query_plan，
    以及按需覆盖 _is_read_query。run() 返回 {"status": ...} 字典；服务端超时
    会原样抛出（不吞成 error），交由上层 tool_executor._classify_exception 归为「慢查询」。
    """

    MAX_ROWS = 1000               # 结果集行数上限
    MAX_RESULT_BYTES = 200_000    # 结果集序列化字节数上限
    QUERY_TIMEOUT_SECONDS = 5.0   # 服务端超时（秒），子类换算成毫秒
    COST_LIMIT = 100_000          # EXPLAIN 成本阈值，超过即视为高风险计划
    LARGE_TABLE_ROWS = 100_000    # 全表扫描只有超过该估算行数才拦截（避免误伤小表）

    # SQL 方言：表级权限校验用 sqlglot 解析 SQL 文本时按此方言解析（子类覆盖）
    DIALECT = "mysql"

    def __init__(self, **conn_params):
        self.conn_params = conn_params
        # 默认 schema：解析「未带库前缀」的表名时回落到这里。
        # 各库连接参数里存库名的键不同：MySQL 用 database/db，PostgreSQL 用 dbname。
        self.default_schema = (
            conn_params.get("database") or conn_params.get("db") or conn_params.get("dbname") or ""
        )

    # ---- 数据库差异点（子类实现）----
    def _connect(self):
        raise NotImplementedError

    def _set_statement_timeout(self, conn, milliseconds: int) -> None:
        """在会话上设置服务端超时。这是「服务端自己处理超时」的关键。"""
        raise NotImplementedError

    def _check_query_plan(self, conn, query: str) -> Optional[str]:
        """用 EXPLAIN 做执行前静态检测，返回风险描述；无风险返回 None。"""
        raise NotImplementedError

    def _is_read_query(self, query: str) -> bool:
        """粗判断是否读查询（决定是否做执行前 EXPLAIN 预检）。"""
        return query.strip().lower().startswith(("select", "with", "values"))

    # ---- 通用三层流程 ----
    def run(self, query: str) -> Dict[str, Any]:
        # 只读拦截（第 0 层）：执行前校验语句类型，写语句/多语句直接抛 PermissionError。
        # 放在 try 之外，让异常原样上抛，由上层 AgentToolExecutor._classify_exception
        # 归类为 permanent（不可恢复），从而不会反复重试。
        # 关键：必须在 _connect / execute 之前拦截——PostgreSQL/MySQL 本实现 autocommit=True，
        # 写语句一旦 execute 就立即提交，事后看 description 已经来不及回滚。
        ensure_read_only_query(query)

        # ---- 表级权限校验（第 0.5 层）----
        # PG/MySQL 没有 SQLite 的 set_authorizer 引擎回调，退回到「SQL 文本解析 + 白名单」，
        # 在 _connect / execute 之前拦截越权表（子查询/CTE/JOIN 里藏的表也能被 sqlglot 提出来）。
        # 校验不通过抛 PermissionError，同样由上层归为 permanent，不会反复重试。
        if ENFORCE_ACL:
            denied = check_query_access(
                query,
                get_allowed_tables(),                 # 当前用户可查的 (库, 表) 集合
                dialect=self.DIALECT,
                default_schema=self.default_schema,
            )
            if denied:
                raise PermissionError(denied)

        conn = self._connect()
        try:
            # ---- 第 1 层：执行前 EXPLAIN 静态检测（仅读查询）----
            if self._is_read_query(query):
                danger = self._check_query_plan(conn, query)
                if danger:
                    return {"status": "error",
                            "error": f"SQL 执行计划存在风险：{danger}。请改写 SQL（补 JOIN 条件 / 加过滤 / 聚合 / 限制行数）后重试"}

            # ---- 第 2 层：服务端超时（真正的服务端 kill）----
            self._set_statement_timeout(conn, int(self.QUERY_TIMEOUT_SECONDS * 1000))

            cur = conn.cursor()
            cur.execute(query)

            # 写操作兜底：没有结果集（description 为 None）即非查询语句。
            # 只读模式下这里本不应被走到（ensure_read_only_query 已提前拦截写语句），
            # 但作为纵深防御：一律拒绝、不提交。注意 SQLite 默认隔离级别下不 commit 即回滚，
            # 而 PG/MySQL 本实现 autocommit=True，真正可靠的拦截只能靠 execute 前的白名单。
            if cur.description is None:
                return {"status": "error",
                        "error": "只读模式：禁止执行写操作（INSERT/UPDATE/DELETE/DDL 等），该语句未提交。"}

            columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(self.MAX_ROWS + 1)

            # ---- 第 3 层：执行后行数 + 字节数双重截断 ----
            row_overflow = len(rows) > self.MAX_ROWS
            rows = rows[:self.MAX_ROWS]
            rows, byte_overflow = _truncate_by_bytes(rows, self.MAX_RESULT_BYTES)

            if row_overflow or byte_overflow:
                return {"status": "too_large",
                        "result": {"columns": columns, "rows": rows, "truncated": True,
                                   "truncated_reason": "rows" if row_overflow else "bytes"}}

            return {"status": "success", "result": {"columns": columns, "rows": rows}}

        except Exception as e:
            # 服务端超时要原样抛出，交给上层分类为「慢查询 query」，不能吞成普通 error
            # （否则会误走「可重试 temporary」分支，越重试越慢）
            if is_db_timeout(e):
                raise
            return {"status": "error", "error": f"{type(e).__name__}: {e} -- {query}"}
        finally:
            conn.close()

    def invoke(self, args: dict) -> Dict[str, Any]:
        """与 langchain 工具 invoke 保持一致的入口，便于接到 AgentToolExecutor。

        AgentToolExecutor._run_one 会调用 tool.invoke({"query": "..."})，
        服务端超时异常会从这里向上抛出，被归类为「慢查询」。
        """
        return self.run(args["query"])


# ==================== PostgreSQL 适配 ====================

def _walk_pg_plan(node: Any, info: Dict[str, Any]) -> None:
    """递归遍历 EXPLAIN (FORMAT JSON) 的计划树，收集成本/大表全扫/嵌套循环膨胀。"""
    if not isinstance(node, dict):
        return
    ntype = node.get("Node Type", "")
    rows = node.get("Plan Rows", 0) or 0
    info["max_cost"] = max(info["max_cost"], node.get("Total Cost", 0) or 0)
    if ntype == "Seq Scan" and rows > info["large_table_rows"]:
        info["large_seq_scan"].append((node.get("Relation Name", "?"), rows))
    if ntype == "Nested Loop" and rows > info["row_explosion_limit"]:
        info["nested_loop_explosion"].append(rows)
    for child in node.get("Plans", []) or []:
        _walk_pg_plan(child, info)


class PostgresQueryTool(BaseSqlQueryTool):
    """PostgreSQL 适配（Hologres 兼容 PG 协议，同样适用）。

    服务端超时：statement_timeout。
    执行前检测：EXPLAIN (FORMAT JSON) 拿成本/估算行数，识别大表 Seq Scan、Nested Loop 膨胀。
    """

    DIALECT = "postgres"            # 表级权限解析 SQL 用 PG 方言

    # 嵌套循环关联的估算行数超过该值即视为「结果膨胀」风险（笛卡尔积的典型表现）
    ROW_EXPLOSION_LIMIT = 1_000_000

    def __init__(self, **conn_params):
        super().__init__(**conn_params)
        # PG 的表在 schema 里（默认 public），表级权限解析/比对按 schema 名对齐，
        # 而不是按 dbname（BaseSqlQueryTool 默认取 dbname）。业务表、权限表都放 public。
        self.default_schema = conn_params.get("schema", "public")

    def _connect(self):
        try:
            import psycopg2
        except ImportError as e:
            raise RuntimeError("需要安装 psycopg2-binary：pip install psycopg2-binary") from e
        conn = psycopg2.connect(**self.conn_params)
        conn.autocommit = True
        return conn

    def _set_statement_timeout(self, conn, milliseconds: int) -> None:
        cur = conn.cursor()
        # statement_timeout 的单位是毫秒；超过即服务端抛 QueryCanceled(57014)
        cur.execute("SET statement_timeout = %s", (milliseconds,))
        cur.close()

    def _check_query_plan(self, conn, query: str) -> Optional[str]:
        cur = conn.cursor()
        try:
            cur.execute("EXPLAIN (FORMAT JSON) " + query)
            plan = json.loads(cur.fetchone()[0])
        except Exception:
            # EXPLAIN 失败（非查询语句等）：跳过预检，交给实际执行兜底
            return None
        finally:
            cur.close()

        info = {"max_cost": 0.0, "large_seq_scan": [],
                "nested_loop_explosion": [], "large_table_rows": self.LARGE_TABLE_ROWS,
                "row_explosion_limit": self.ROW_EXPLOSION_LIMIT}
        for node in plan:
            if isinstance(node, dict) and "Plan" in node:
                _walk_pg_plan(node["Plan"], info)

        if info["large_seq_scan"]:
            rel, rows = info["large_seq_scan"][0]
            return f"大表全表扫描：{rel}（估算 {rows:.0f} 行）"
        if info["nested_loop_explosion"]:
            return f"嵌套循环关联导致结果膨胀（估算 {info['nested_loop_explosion'][0]:.0f} 行）"
        if info["max_cost"] > self.COST_LIMIT:
            return f"执行计划成本过高（cost={info['max_cost']:.0f}）"
        return None


# ==================== MySQL 适配 ====================

def _walk_mysql_plan(obj: Any, info: Dict[str, Any]) -> None:
    """递归遍历 EXPLAIN FORMAT=JSON 的 JSON，收集成本/大表全扫。"""
    if isinstance(obj, dict):
        if obj.get("access_type") == "ALL" and (obj.get("rows_examined_per_scan") or 0) > info["large_table_rows"]:
            info["large_full_scan"].append((obj.get("table_name", "?"), obj.get("rows_examined_per_scan")))
        cost = obj.get("cost_info", {})
        if "query_cost" in cost:
            try:
                info["max_cost"] = max(info["max_cost"], float(cost["query_cost"]))
            except (TypeError, ValueError):
                pass
        for v in obj.values():
            _walk_mysql_plan(v, info)
    elif isinstance(obj, list):
        for v in obj:
            _walk_mysql_plan(v, info)


class MysqlQueryTool(BaseSqlQueryTool):
    """MySQL 适配。

    服务端超时：max_execution_time（毫秒，只作用于 SELECT）。
    执行前检测：EXPLAIN FORMAT=JSON 拿 query_cost / access_type=ALL，识别高成本/大表全扫。
    """

    def _connect(self):
        try:
            import pymysql
        except ImportError as e:
            raise RuntimeError("需要安装 pymysql：pip install pymysql") from e
        conn = pymysql.connect(**self.conn_params, autocommit=True)
        return conn

    def _set_statement_timeout(self, conn, milliseconds: int) -> None:
        cur = conn.cursor()
        # max_execution_time 只作用于 SELECT；非 SELECT 语句设置也不会报错
        cur.execute("SET SESSION max_execution_time = %s", (milliseconds,))
        cur.close()

    def _check_query_plan(self, conn, query: str) -> Optional[str]:
        cur = conn.cursor()
        try:
            cur.execute("EXPLAIN FORMAT=JSON " + query)
            plan = json.loads(cur.fetchone()[0])
        except Exception:
            return None
        finally:
            cur.close()

        info = {"max_cost": 0.0, "large_full_scan": [], "large_table_rows": self.LARGE_TABLE_ROWS}
        _walk_mysql_plan(plan, info)

        if info["large_full_scan"]:
            tbl, rows = info["large_full_scan"][0]
            return f"大表全表扫描：{tbl}（估算 {rows} 行）"
        if info["max_cost"] > self.COST_LIMIT:
            return f"执行计划成本过高（query_cost={info['max_cost']:.0f}）"
        return None
