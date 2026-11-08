from typing import Dict, Any, Optional, Tuple, List
from langchain_core.tools import tool
from tools.db_query import ensure_read_only_query
from tools.access_control import ENFORCE_ACL, build_acl_authorizer, permission_error_message
import sqlite3
import json
import os
import re
import threading

# 固定的 SQLite 数据库路径
current_file_dir = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.path.join(current_file_dir, "example.db")  # 替换为你的数据库文件路径

# ==================== 执行后：结果集保护 ====================
# 行数上限：SELECT 返回超过该行数即判定「结果集过大」，只返回前 MAX_ROWS 行并标记 too_large
MAX_ROWS = 1000
# 结果集序列化字节数上限：比单纯行数更贴近 LLM 的 token/上下文预算
# （宽表可能行数不多、但每行字节很大，行数上限管不住这种场景）
MAX_RESULT_BYTES = 200_000

# ==================== 执行中：查询超时 ====================
# 墙钟超时（秒）：查询执行超过该时长即中断。SQLite 没有内置 max_execution_time，
# 这里用「看门狗线程 + conn.interrupt()」实现真正的时间超时——这是最贴合「慢」的指标。
QUERY_TIMEOUT_SECONDS = 5.0
# VM 指令数兜底：作为墙钟超时之外的第二道保险。两者触发时抛的都是
# sqlite3.OperationalError("interrupted")，上层按同一个「需改写 SQL」分支处理。
MAX_VM_STEPS = 5_000_000

# ==================== 执行前：静态检测（EXPLAIN QUERY PLAN）====================
# 是否拦截「大表的全表扫描」。全表扫描只有在大表上才慢，小表扫描无害；
# 这里依赖 ANALYZE 生成的 sqlite_stat1 统计来估算表行数，只拦截估算行数超过 LARGE_TABLE_ROWS 的表。
# 未执行 ANALYZE（没有统计）时不会拦截全表扫描，只保留对笛卡尔积的硬拦截。
BLOCK_FULL_SCAN = True
# 大表阈值：估算行数超过该值的表，做全表扫描时才拦截
LARGE_TABLE_ROWS = 100_000

# 读查询（会返回结果集）的常见前缀，仅用于「是否做执行前静态检测」的粗判断
_READ_PREFIXES = ("select", "with", "explain", "pragma", "values")


def _is_read_query(query: str) -> bool:
    """判断是否可能返回结果集的读查询。

    这里只是「要不要做执行前检测」的粗判断；最终是读还是写，
    以 execute 后 cursor.description 是否为空为准（见 execute_sqlite_query）。
    """
    return query.strip().lower().startswith(_READ_PREFIXES)


# FROM 子句（表列表）结束处的边界关键字：遇到这些说明「多表列表」已经结束
_FROM_END_KEYWORDS = ("where", "group by", "order by", "having", "limit", "union", "offset")


def _is_word_boundary(sql: str, i: int) -> bool:
    """判断位置 i 是否处于单词边界（i 在开头/结尾，或前后都不是字母数字）。"""
    return (i == 0 or not sql[i - 1].isalnum()) and (i >= len(sql) or not sql[i].isalnum())


def _from_clause_end(sql: str, start: int) -> int:
    """从 FROM 关键字结束位置开始，找到 FROM 子句（表列表）的结束位置。

    括号深度内的逗号（子查询、函数参数）不算「表列表结束」，跨过括号继续扫；
    遇到 WHERE / GROUP BY / ORDER BY 等边界关键字即返回其位置。
    """
    i, n, depth = start, len(sql), 0
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
        if depth == 0:
            for kw in _FROM_END_KEYWORDS:
                klen = len(kw)
                if sql[i:i + klen].lower() == kw and _is_word_boundary(sql, i) and _is_word_boundary(sql, i + klen):
                    return i
        i += 1
    return n


def _has_cross_join(query: str) -> bool:
    """从 SQL 文本检测笛卡尔积（数据膨胀的典型来源）。

    为什么不用 EXPLAIN QUERY PLAN 检测：SQLite 3.53 的 EXPLAIN QUERY PLAN 对笛卡尔积
    只输出 "SCAN a" / "SCAN b"，并不输出 CARTESIAN 关键字（本项目的 SQLite 版本实测如此），
    所以这里直接对 SQL 文本做静态识别，覆盖两类典型写法：
      1. 显式 CROSS JOIN
      2. 老式逗号连接：FROM 子句里出现「顶层逗号」（如 FROM a, b）
    只识别「缺少 JOIN 条件」的写法；子查询/函数参数里的逗号因括号深度 >0 不会被误判。
    """
    q = query.strip()
    # 显式 CROSS JOIN
    if re.search(r"\bcross\s+join\b", q, re.IGNORECASE):
        return True
    m = re.search(r"\bfrom\b", q, re.IGNORECASE)
    if not m:
        return False
    # 扫描 FROM 之后、子句边界之前的表列表，看有没有顶层逗号（多个表被逗号隔开）
    end = _from_clause_end(q, m.end())
    depth = 0
    for ch in q[m.end():end]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        elif ch == "," and depth == 0:
            return True
    return False


def _has_join_without_on(query: str) -> bool:
    """检测「写了 JOIN 却没写 ON / USING 条件」的笛卡尔积（如 FROM a JOIN b）。

    这是 CROSS JOIN 之外另一种常见的笛卡尔积写法：INNER/LEFT JOIN 后面漏写关联条件。
    做法：在括号深度 0（顶层）扫描关键字，每遇到一个非 CROSS 的 JOIN 就记一个「待配对」，
    遇到 ON / USING 消掉一个，遇到 WHERE 等子句边界或结束还有未配对的即判定为笛卡尔积。
    子查询 / 括号内的 JOIN、ON 因深度 >0 被跳过，不会误判。
    """
    q = query
    i, n, depth, pending = 0, len(q), 0, 0
    while i < n:
        ch = q[i]
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth = max(depth - 1, 0)
            i += 1
            continue
        if depth != 0:
            i += 1
            continue
        low = q[i:].lower()
        # 子句边界：到这里还有没配到 ON/USING 的 JOIN，就是笛卡尔积
        if re.match(r"\b(where|group\s+by|order\s+by|having|limit|union|offset)\b", low):
            return pending > 0
        # CROSS JOIN 本身即笛卡尔积，交给 _has_cross_join 处理，这里跳过不累计
        if re.match(r"\bcross\s+join\b", low):
            i += re.match(r"\bcross\s+join\b", low).end()
            continue
        # 其它 JOIN（裸 JOIN / LEFT / INNER / OUTER 等）：记一个待配对
        m = re.match(r"\b(?:left|right|full|inner|outer)?\s*join\b", low)
        if m:
            pending += 1
            i += m.end()
            continue
        # ON / USING：满足一个待配对的 JOIN
        m = re.match(r"\bon\b|\busing\b", low)
        if m:
            if pending > 0:
                pending -= 1
            i += m.end()
            continue
        i += 1
    return pending > 0


def _table_row_estimates(conn: sqlite3.Connection) -> Dict[str, int]:
    """从 sqlite_stat1（由 ANALYZE 生成）读取各表估算行数。

    返回 {表名: 估算行数}；未执行 ANALYZE（没有统计）时返回空字典。
    只有拿到统计，才能判断「这张表是不是大表」，从而避免误伤小表。
    """
    try:
        rows = conn.execute("SELECT tbl, stat FROM sqlite_stat1 WHERE idx IS NULL").fetchall()
    except sqlite3.Error:
        return {}
    estimates: Dict[str, int] = {}
    for tbl, stat in rows:
        try:
            estimates[tbl] = int(stat)
        except (TypeError, ValueError):
            continue
    return estimates


def _check_query_plan(conn: sqlite3.Connection, query: str) -> Optional[str]:
    """执行前静态检测：用 EXPLAIN QUERY PLAN 判断执行计划是否有数据膨胀风险。

    返回风险描述字符串；无风险返回 None。
    这是静态启发式，只读执行计划、不真正执行查询，开销很小；
    主要拦截两类危险计划：
      - 笛卡尔积（多表关联缺少 JOIN 条件）→ 结果集爆炸的典型来源，无条件拦截
      - 大表全表扫描（未走索引）→ 只有拿到 ANALYZE 统计、且估算行数超过阈值时才拦截，
        避免把「SELECT * FROM 小表」这类无害查询也误杀掉
    """
    try:
        plan = conn.execute("EXPLAIN QUERY PLAN " + query).fetchall()
    except sqlite3.Error:
        # EXPLAIN 失败（比如某些非查询语句）：跳过静态检测，交给实际执行兜底
        return None

    # 只在需要拦截全表扫描时才读一次统计，减少开销
    estimates = _table_row_estimates(conn) if BLOCK_FULL_SCAN else {}

    for row in plan:
        detail = str(row[-1]) if row else ""
        # 笛卡尔积：多表关联缺少 JOIN 条件，结果会爆炸，是最典型的数据膨胀来源
        if "CARTESIAN" in detail:
            return "检测到笛卡尔积（多表关联缺少 JOIN 条件，会导致结果集爆炸）"
        # 全表扫描且未走索引：只有「大表」才拦截（小表全扫无害）
        if BLOCK_FULL_SCAN and "SCAN" in detail and "USING INDEX" not in detail:
            parts = detail.split()
            # detail 形如 "SCAN TRANSACTIONS" 或 "SCAN main.TRANSACTIONS"，表名在 "SCAN" 之后
            tbl = parts[1].split(".")[-1] if len(parts) >= 2 and parts[0] == "SCAN" else None
            if tbl and estimates.get(tbl, 0) > LARGE_TABLE_ROWS:
                return f"检测到大表全表扫描：{detail}"
    return None


def _truncate_by_bytes(rows: List[tuple], max_bytes: int) -> Tuple[List[tuple], bool]:
    """按序列化字节数截断结果行，避免撑爆 LLM 上下文（比单纯行数更贴近 token 预算）。

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


@tool(
    "execute_sqlite_query",
    description="Execute a SQLite query on a fixed database and return the results as JSON. Use this tool to interact with the SQLite database."
)
def execute_sqlite_query(query: str) -> Dict[str, Any]:
    """
    参数:
        query: 要执行的 SQL 查询
    返回:
        查询结果的 JSON 格式，或者错误信息
        - status=success   ：正常结果
        - status=too_large ：结果集过大（行数或字节数超限），已截断，提示上层收窄/聚合
        - status=error     ：普通 SQL 执行错误 / 执行计划风险
        - 慢查询（超时中断）会抛出 sqlite3.OperationalError("interrupted")，
          交由上层分类为「需改写 SQL」，而不是吞成普通错误

    三层防护：
        1) 执行前：EXPLAIN QUERY PLAN 拦截笛卡尔积/全表扫描
        2) 执行中：墙钟超时 interrupt()（主） + VM 指令数（兜底）
        3) 执行后：行数 + 字节数双重截断
    """
    # 只读拦截：执行前校验语句类型，写语句/多语句直接抛 PermissionError，
    # 由上层 AgentToolExecutor 归类为 permanent（不可恢复），不会反复重试。
    ensure_read_only_query(query)

    conn = sqlite3.connect(DATABASE_PATH)

    # ---- 第 0.5 层：表级权限闸门（SQLite authorizer）----
    # 在连接建立后、任何 EXPLAIN / execute 之前装上 authorizer，从引擎层拦截对
    # 未授权表的读取。这是权限硬闸门（fail-closed）：不解析 SQL 文本，也能拦住
    # 子查询 / CTE / JOIN / 视图里藏着的越权表。
    acl_state = {"denied": None, "allowed": set()}
    if ENFORCE_ACL:
        _acl_authorizer, acl_state = build_acl_authorizer()
        conn.set_authorizer(_acl_authorizer)

    timer = None
    try:
        cursor = conn.cursor()

        # ---- 第 1 层：执行前静态检测（仅读查询）----
        if _is_read_query(query):
            # 1a. 纯文本检测笛卡尔积（不依赖 EXPLAIN，先于执行计划检测）
            if _has_cross_join(query) or _has_join_without_on(query):
                return {"status": "error",
                        "error": "SQL 存在笛卡尔积风险（多表关联缺少 JOIN 条件，会导致结果集爆炸）。"
                                 "请改用显式 JOIN ... ON 补全关联条件，或加 WHERE 过滤条件后重试"}
            # 1b. EXPLAIN QUERY PLAN 检测执行计划风险（大表全表扫描等）
            danger = _check_query_plan(conn, query)
            if danger:
                return {"status": "error",
                        "error": f"SQL 执行计划存在风险：{danger}。请改写 SQL（补 JOIN 条件 / 加过滤条件 / 用聚合 / 限制行数）后重试"}

        print("---- Executing SQL Query ----")
        print(query)

        # ---- 第 2 层：执行中超时防护（墙钟为主 + VM 指令兜底）----
        # 看门狗线程：超时后从另一个线程中断正在执行的查询（中断抛 OperationalError("interrupted")）
        timer = threading.Timer(QUERY_TIMEOUT_SECONDS, conn.interrupt)
        timer.start()
        # VM 指令数兜底：纯 CPU 密集型查询（如 JOIN 爆炸）也会被及时中断
        conn.set_progress_handler(lambda: 1, MAX_VM_STEPS)

        cursor.execute(query)

        # 写操作兜底：没有返回行（cursor.description is None）即非查询语句
        # （INSERT/UPDATE/DELETE/DDL 等）。只读模式下这里本不应被走到——
        # ensure_read_only_query 已在执行前拦截写语句——但作为纵深防御，这里一律
        # 显式回滚并拒绝，绝不做 conn.commit()，确保任何写操作都不会落库。
        if cursor.description is None:
            conn.rollback()
            return {"status": "error",
                    "error": "只读模式：禁止执行写操作（INSERT/UPDATE/DELETE/DDL 等），该语句未提交。"}

        columns = [description[0] for description in cursor.description]
        # 多取 1 行，用来判断是否超过 MAX_ROWS 上限
        rows = cursor.fetchmany(MAX_ROWS + 1)

        # ---- 第 3 层：执行后结果集截断（行数 + 字节数）----
        row_overflow = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
        rows, byte_overflow = _truncate_by_bytes(rows, MAX_RESULT_BYTES)

        if row_overflow or byte_overflow:
            return {
                "status": "too_large",
                "result": {
                    "query": query,
                    "columns": columns,
                    "rows": rows,
                    "truncated": True,
                    "truncated_reason": "rows" if row_overflow else "bytes",
                },
            }

        # 回传本次实际执行的 SQL，让最终答案能「列出计算过程」，也便于后续对账、防止编造
        return {"status": "success", "result": {"query": query, "columns": columns, "rows": rows}}

    except sqlite3.OperationalError as e:
        # 慢查询被墙钟超时/VM 指令中断：重新抛出，交给上层 AgentToolExecutor 分类为
        # 「查询需改写」（加过滤/聚合/LIMIT），而不是当成可重试的临时网络错误。
        # 关键：不能在这里吞掉，否则上层无法区分「网络超时」和「慢查询」。
        if "interrupted" in str(e).lower():
            raise
        return {"status": "error", "error": str(e) + "--" + query + "--" + DATABASE_PATH}
    except sqlite3.Error as e:
        # 表级权限被拒：authorizer 拦下了对未授权表的读取，返回友好中文错误
        if ENFORCE_ACL and acl_state.get("denied"):
            return {"status": "error", "error": permission_error_message(acl_state)}
        # 捕获普通 SQLite 错误并返回
        return {"status": "error", "error": str(e) + "--" + query + "--" + DATABASE_PATH}
    finally:
        # 无论成功/失败/中断，都要取消看门狗定时器并关闭连接
        if timer is not None:
            timer.cancel()
        conn.close()
