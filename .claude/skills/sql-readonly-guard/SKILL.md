---
name: sql-readonly-guard
description: 给任何「Agent/LLM 会执行 SQL」的工具加上 fail-closed 只读守卫：语句白名单、多语句检测、EXPLAIN 笛卡尔积/大表全扫拦截、服务端超时、结果集截断。Use when 新建/评审一个会运行模型生成 SQL 的执行工具，或需要保证"只允许查询、不允许写库"。
---

# 只读 SQL 执行守卫（写权限限制）

目标：在**代码层**保证「只允许查询，不允许写库」，独立于数据库层面的只读账号/只读连接，可叠加使用。

核心原则一句话：**fail-closed（默认拒绝）+ 执行前拦截**。用白名单而非黑名单——黑名单枚举写关键字容易漏（`REPLACE`、`MERGE`、`WITH ... INSERT`），漏掉即放行；白名单只放行确定的读语句，未识别的一律拒绝。

## 五层防线（按顺序，缺一不可）

| 层 | 时机 | 做什么 | 拦什么 |
|----|------|--------|--------|
| 0 | 执行前 | 语句白名单 + 多语句检测（抛 `PermissionError`） | `INSERT/UPDATE/DELETE/DDL`、`SELECT 1; DROP TABLE t`、`WITH ... INSERT` |
| 1 | 执行前 | `EXPLAIN QUERY PLAN` 静态检测 | 笛卡尔积、大表全表扫描、高成本计划 |
| 2 | 执行中 | 服务端超时（SQLite 用看门狗线程 + `interrupt()`；PG 用 `statement_timeout`；MySQL 用 `max_execution_time`） | 慢查询 / JOIN 爆炸把连接拖死 |
| 3 | 执行后 | 行数 + 字节数双重截断 | 结果集撑爆 LLM 上下文 |

## 应用步骤

1. **复制白名单守卫**：把 [references/readonly_guard.py](references/readonly_guard.py) 拷进新项目，在 `execute` 之前调用 `ensure_read_only_query(query)`。它零依赖、纯函数，`select/explain/values` 之外一律 `PermissionError`。
2. **放到 try 之外**：白名单校验必须在 `connect` / `execute` **之前**抛出，不能吞成普通 error。原因见下一条。
3. **理解「为什么必须执行前拦截」**：PostgreSQL/MySQL 实现里 `autocommit=True`，写语句一旦 `execute` 就**立即提交**，事后看 `cursor.description` 已经来不及回滚。SQLite 默认隔离级别下「不 commit 即回滚」能兜底，但那只对 SQLite 成立——可靠拦截只能靠执行前的白名单。
4. **接线到错误分类**：让 `PermissionError` 被上层归为 `permanent`（不可恢复），直接让 LLM 基于已有信息给答案，不重试。本项目对应 [tools/tool_executor.py:71-72](../../../tools/tool_executor.py#L71-L72)。
5. **写操作兜底**：`execute` 后若 `cursor.description is None`（无结果集 = 写语句），一律 `rollback`/拒绝、**绝不 commit**，作为纵深防御。
6. **只读白名单不要放 `PRAGMA`**：`PRAGMA` 既能读也能写（`query_only=OFF`、`user_version=...`），整体放行会被绕过；如需只读 PRAGMA 请显式枚举。

## 关键实现细节（写代码前必读）

- **`WITH` 不能只看首关键字**：`WITH cte AS (...) INSERT ...` 是写语句。要取 CTE 之后**第一个「语句引入关键字」**当主语句判定（[db_query.py:85-90](../../../tools/db_query.py#L85-L90) 的 `_STATEMENT_KEYWORDS`）。
- **多语句检测看顶层分号**：跳过字符串/注释里的分号，只在括号深度 0 处判断，分号后还有语句内容即为多语句。
- **全表扫描别误伤小表**：只有拿 `ANALYZE` 统计、估算行数超过阈值（本项目 10 万）才拦截；`SELECT * FROM 小表` 无害，不能杀。

## 本仓库的完整参考实现

- 白名单守卫（零依赖）：[tools/db_query.py:68-227](../../../tools/db_query.py#L68-L227)
- 三层执行防护的数据库无关基类 + PG/MySQL 适配：[tools/db_query.py:229-459](../../../tools/db_query.py#L229-L459)
- PG 查询工具（接入基类 + statement_timeout + EXPLAIN JSON 预检 + 截断）：[tools/tools_execute_sql.py](../../../tools/tools_execute_sql.py)

## 验证

接上守卫后至少过一遍这 16 类用例，确认「该放行的放行、该拦的拦住」：
`SELECT`（放行）、`EXPLAIN`（放行）、`VALUES`（放行）、`WITH ... SELECT`（放行）、
`INSERT` / `UPDATE` / `DELETE` / `DROP` / `CREATE` / `ALTER`（拒绝）、
`WITH ... INSERT`（拒绝）、`SELECT 1; DROP TABLE t`（拒绝）、字符串里的 `'select'` 不干扰判定（放行）、注释里的关键字不干扰（放行）。
