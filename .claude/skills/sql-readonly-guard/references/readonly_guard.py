"""零依赖的只读 SQL 白名单守卫（可整体拷入任何项目）。

用法：在 SQL 真正 execute 之前调用 `ensure_read_only_query(query)`，
写语句 / 多语句会抛 `PermissionError`，上层据此拒绝执行（fail-closed 默认拒绝）。

为什么用白名单而非黑名单：黑名单枚举写关键字容易漏（REPLACE、MERGE、
WITH ... INSERT），漏掉即放行；白名单只放行确定的读语句开头，未识别的一律拒绝。

为什么必须在 execute 之前拦截：PostgreSQL/MySQL 的 autocommit=True 会让写语句
一旦 execute 就立即提交，事后看 cursor.description 已来不及回滚。
"""
from typing import List

# 只读语句首关键字白名单：只有这些开头才允许执行。
# 故意不放 PRAGMA——PRAGMA 既能读也能写（query_only=OFF、user_version=...），
# 整体放行会被绕过；如需只读 PRAGMA 请显式补充。
_READ_ONLY_KEYWORDS = {"select", "explain", "values"}

# 语句引入关键字全集：用于解析 WITH ... 之后「真正的主语句类型」。
# WITH 本身不分读写：WITH cte AS (...) SELECT ... 是读，WITH cte AS (...) INSERT ... 是写，
# 必须取 CTE 之后的主语句来判定。
_STATEMENT_KEYWORDS = {
    "select", "with", "explain", "values", "pragma",
    "insert", "update", "delete", "replace", "drop", "alter", "create",
    "vacuum", "attach", "detach", "reindex", "merge", "truncate",
    "grant", "revoke", "begin", "commit", "rollback", "savepoint", "release",
}


def _skip_quoted(sql: str, i: int, quote: str) -> int:
    """跳过从 i 开始的字符串/标识符字面量，返回其结束位置的下标。"""
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

    扫描中跳过字符串字面量 / 注释 / 各种引号标识符，因此字符串里的 select、
    分号不会干扰判定，CTE/子查询（括号内）的 SELECT 不会干扰 WITH 主语句判定。
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

    不满足时抛 PermissionError。
    """
    if not isinstance(query, str):
        raise PermissionError("只读模式：SQL 必须是字符串")

    if _has_multiple_statements(query):
        raise PermissionError("只读模式：禁止一次执行多条语句")

    tokens = _top_level_tokens(query)
    if not tokens:
        raise PermissionError("只读模式：空 SQL 或无法识别的语句")

    first = tokens[0]
    # WITH 可能包裹写语句（WITH cte AS (...) INSERT ...），需解析 CTE 之后的真实主语句
    if first == "with":
        first = next((t for t in tokens[1:] if t in _STATEMENT_KEYWORDS), None)
        if first is None:
            raise PermissionError("只读模式：无法识别 WITH 后的主语句")

    if first not in _READ_ONLY_KEYWORDS:
        raise PermissionError(f"只读模式：拒绝执行非查询语句（检测到 {first.upper()}）")
