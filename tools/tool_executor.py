"""带错误分类与重试策略的 LangGraph 工具执行节点（等价于 ToolNode 的增强版）。

把工具执行中遇到的问题分类处理：
1. 参数错误       → 把具体错误反馈给 LLM，让它修正参数后重试
2. 临时服务错误   → 网络超时/5xx/429，节点内部指数退避重试（不消耗 LLM 往返）
3. 慢查询         → SQL 执行过慢被中断（数据膨胀/JOIN 爆炸），让 LLM 改写 SQL，不重试
4. 结果集过大     → 返回行数超上限，让 LLM 收窄条件/聚合/限制行数
5. 空结果         → 让 LLM 泛化条件后重试
6. 永久性错误     → 4xx/权限错误，立即返回「不可恢复」，让 LLM 直接给最终答案
"""
import json
import sqlite3
import time
from typing import Any, List

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

from tools.db_query import is_db_timeout


def _classify_exception(e: Exception) -> str:
    """把异常归类为 temporary / permanent / query / param 之一。

    - temporary：临时服务错误（网络抖动、超时、5xx、429），可指数退避重试
    - permanent：永久性错误（4xx、权限），立即终止
    - query    ：慢查询被中断（数据膨胀导致），需让 LLM 改写 SQL，**不能重试**
    - param    ：参数错误，让 LLM 修正参数后重试
    """
    # 慢查询被 SQLite progress_handler 中断 → 单独归类为 query。
    # 关键：必须放在最前面，且不能归入 temporary——同样的 JOIN 重跑照样爆炸，重试只会更糟。
    if isinstance(e, sqlite3.OperationalError) and "interrupted" in str(e).lower():
        return "query"

    # 数据库服务端超时（PostgreSQL statement_timeout / MySQL max_execution_time）→ 慢查询 query。
    # 同样是「重试只会更慢」，必须和网络超时（temporary）区分开。
    if is_db_timeout(e):
        return "query"

    # openai 异常延迟导入，避免硬依赖
    try:
        import openai
        if isinstance(e, openai.APITimeoutError):       # 网络超时 → 可重试
            return "temporary"
        if isinstance(e, (openai.RateLimitError, openai.InternalServerError)):
            return "temporary"                          # 429 / 5xx → 可重试
        if isinstance(e, openai.APIConnectionError):    # 连接失败 → 可重试
            return "temporary"
        if isinstance(e, (openai.BadRequestError, openai.AuthenticationError,
                          openai.PermissionDeniedError, openai.NotFoundError)):
            return "permanent"                          # 4xx → 终止
    except ImportError:
        pass

    # 通用网络库超时
    try:
        import httpx
        if isinstance(e, httpx.TimeoutException):
            return "temporary"
    except ImportError:
        pass
    try:
        import requests
        if isinstance(e, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            return "temporary"
    except ImportError:
        pass

    if isinstance(e, (TimeoutError, ConnectionError)):
        return "temporary"
    if isinstance(e, PermissionError):
        return "permanent"
    # ValueError / TypeError / pydantic.ValidationError 等 → 参数问题，让 LLM 修正
    return "param"


class AgentToolExecutor:
    """LangGraph 工具执行节点：带错误分类 + 临时错误指数退避重试 + 慢查询/过大结果改写。

    用法（替换 ToolNode）：
        from tools.tool_executor import AgentToolExecutor
        builder.add_node("tools", AgentToolExecutor(tools, max_retries=3))
    """

    # 结果集行数兜底阈值，与 tools.tools_execute_sqlite.MAX_ROWS 保持一致
    MAX_ROWS = 1000

    def __init__(self, tools: List[BaseTool], max_retries: int = 3, backoff_base: int = 2):
        self.tools = {t.name: t for t in tools}
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    def __call__(self, state) -> dict:
        """LangGraph 节点入口：执行最后一条 AIMessage 里的所有 tool_calls，返回对应 ToolMessage。

        注意：MessagesState 是 dataclass，LangGraph 传给节点的是 dataclass 实例而非 dict，
        因此这里用属性访问 state.messages（不能用 state["messages"]）。
        """
        last = state.messages[-1]
        out: List[ToolMessage] = []

        for tc in getattr(last, "tool_calls", []) or []:
            out.append(self._run_one(tc))
        # bind_tools 解析 JSON 失败时，调用会进 invalid_tool_calls（参数错误）
        for tc in getattr(last, "invalid_tool_calls", []) or []:
            out.append(ToolMessage(
                content=f"工具参数解析失败：{tc.get('error') or tc.get('args')}，请修正 JSON 后重试",
                tool_call_id=tc["id"],
            ))
        return {"messages": out}

    def _run_one(self, tc: dict) -> ToolMessage:
        """执行单个 tool_call，按错误分类决定重试 / 改写 / 终止。"""
        name = tc.get("name")
        tool = self.tools.get(name)
        if tool is None:
            return ToolMessage(content=f"错误：工具 {name} 不存在", tool_call_id=tc["id"])

        args = tc.get("args") or {}

        for attempt in range(self.max_retries):
            try:
                output = tool.invoke(args)
            except Exception as e:
                kind = _classify_exception(e)
                if kind == "permanent":   # 永久性错误 → 终止，不再重试
                    return ToolMessage(
                        content=f"错误（不可恢复）：{e}。请直接基于已有信息给出答案。",
                        tool_call_id=tc["id"])
                if kind == "query":       # 慢查询中断 → 让 LLM 改写 SQL（不重试）
                    return ToolMessage(
                        content=f"查询执行过慢被中断：{e}。请优化 SQL（加过滤条件、按维度聚合或限制返回行数）后重试。",
                        tool_call_id=tc["id"])
                if kind == "temporary" and attempt < self.max_retries - 1:
                    time.sleep(self.backoff_base ** attempt)  # 指数退避，参数不变
                    continue
                # 参数错误 / 其他未分类 → 把具体错误反馈给 LLM，让它修正
                return ToolMessage(
                    content=f"工具调用失败（{type(e).__name__}）：{e}。请检查参数后重新调用。",
                    tool_call_id=tc["id"])

            # 工具自己吞异常、返回 status 字典的情况（本项目大部分工具如此）
            if isinstance(output, dict) and output.get("status") == "error":
                return ToolMessage(
                    content=f"工具返回错误：{output.get('error') or output.get('raw') or output}",
                    tool_call_id=tc["id"])

            # 结果集过大 / 慢查询 → 让 LLM 收窄条件、聚合、LIMIT（方向与空结果相反）
            if self._is_oversized_result(output):
                return ToolMessage(
                    content=json.dumps(
                        {"warning": "结果集过大，已截断",
                         "hint": "请增加过滤条件、按维度聚合、或限制返回行数后重试",
                         "result": output},
                        ensure_ascii=False, default=str),
                    tool_call_id=tc["id"])

            # 空结果 → 让 LLM 泛化条件后重试
            if self._is_empty_result(output):
                return ToolMessage(
                    content=json.dumps(
                        {"warning": "查询无结果",
                         "hint": "请放宽筛选条件、更换关键词或检查口径后重试",
                         "result": output},
                        ensure_ascii=False, default=str),
                    tool_call_id=tc["id"])

            return ToolMessage(content=json.dumps(output, ensure_ascii=False, default=str),
                               tool_call_id=tc["id"])

        return ToolMessage(content=f"工具 {name} 在 {self.max_retries} 次重试后仍失败",
                           tool_call_id=tc["id"])

    def _is_empty_result(self, output: Any) -> bool:
        """判断是否为空结果：None / 空容器 / no_data、not_found 状态 / rows 为空。"""
        if output is None:
            return True
        if isinstance(output, dict):
            if output.get("status") in ("no_data", "not_found", "empty"):
                return True
            # execute_sqlite_query 的空结果：{"status":"success","result":{"rows":[]}}
            result = output.get("result")
            if isinstance(result, dict) and result.get("rows") == []:
                return True
        if isinstance(output, (list, str, dict)) and len(output) == 0:
            return True
        return False

    def _is_oversized_result(self, output: Any) -> bool:
        """判断是否为「结果集过大」：too_large 状态，或 rows 行数超上限。

        注意：这与「空结果」方向相反——空结果要泛化（放宽条件），
        过大结果要收窄（加过滤/聚合/LIMIT）。慢查询中断走的是异常分支（_classify_exception 的 query），
        不在这里处理。
        """
        if isinstance(output, dict):
            if output.get("status") == "too_large":
                return True
            # 兜底：某些工具不返回 too_large 状态、直接返回超量 rows 时，按行数判断
            result = output.get("result")
            if isinstance(result, dict) and len(result.get("rows") or []) > self.MAX_ROWS:
                return True
        return False
