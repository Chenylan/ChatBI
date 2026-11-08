# 用户提问是如何从请求一路走到模型与 SQL 的

本文梳理「前端发起一次提问 → 后端解析 → 交给模型 → 模型调工具查数 → 流式返回」的完整链路。
适合第一次读本项目的同学建立全局认知，也作为排查问题时的路线图。

> 相关代码入口：
> - 服务装配：[backend/server.py](../backend/server.py)
> - 路由聚合：[backend/api/__init__.py](../backend/api/__init__.py)
> - 接口实现：[backend/api/chat.py](../backend/api/chat.py)
> - Agent 图：[agent.py](../agent.py)
> - 工具执行：[tools/tool_executor.py](../tools/tool_executor.py)
> - SQL 执行：[tools/tools_execute_sqlite.py](../tools/tools_execute_sqlite.py)
> - 记忆管理：[memory_manager.py](../memory_manager.py)

---

## 1. 组件关系总览

```
浏览器 / 前端
    │  POST /api/chat/query  (JSON)
    ▼
backend/server.py          ── uvicorn 启动 FastAPI，负责装配（不是业务逻辑）
    │  app.include_router(router, prefix="/api")
    ▼
backend/api/__init__.py    ── 聚合路由，再套一层 prefix="/chat"
    │  router.include_router(chat_router, prefix="/chat")
    ▼
backend/api/chat.py        ── 真正的接口逻辑（解析请求体 → 建图 → 流式返回）
    │
    ├─ memory_manager.py   加载历史 / 摘要 / 长期记忆
    ├─ agent.py            构建 LangGraph 状态机（模型 + 工具循环）
    └─ tools/*             模型真正调用的工具（RAG / NL→SQL / 执行 SQL / 画图）
```

URL 是怎么拼出来的：`/query`（chat.py）→ 加 `/chat`（__init__.py）→ 加 `/api`（server.py）= **`POST /api/chat/query`**。

---

## 2. 请求入口与请求体解析

前端只发一个 POST，JSON 请求体对应 `ChatRequest`（Pydantic 模型，见 [backend/api/chat.py](../backend/api/chat.py)）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `query` | str | 用户提问原文（必填） |
| `session_id` | str | 会话 ID，不传则默认 `"default"` |
| `request_id` | str | 请求 ID，不传则自动生成 uuid |
| `model` | str | 模型名，默认 `qwen-plus` |
| `user_id` | str | 当前用户标识，用于表级权限校验（见 [tools/access_control.py](../tools/access_control.py)） |

```python
@router.post("/query")
async def chat_query(request: ChatRequest):
    session_id = request.session_id or "default"
    request_id = request.request_id or str(uuid.uuid4())
    return EventSourceResponse(stream_agent_response(...))   # 以 SSE 流式返回
```

关键点：

- **Pydantic 负责"解析"**：请求体字段会被强校验、类型转换，字段缺失/类型不对直接返回 422，到不了业务逻辑。
- **`query` 只是原样文本**，后端不会在这里做任何 SQL 解析——"把自然语言变成 SQL"这件事是后面交给模型去做的。
- 返回的是 **SSE（Server-Sent Events）流**，不是一次性 JSON，所以 token 能边生成边推给前端。

---

## 3. 流式响应是怎么组织的

核心在 [backend/api/chat.py](../backend/api/chat.py) 的 `stream_agent_response()`，它是一个**异步生成器**，负责把「后台线程里跑 Agent」和「主循环里推 token」串起来。

结构如下：

```
stream_agent_response(query, session_id, request_id, model, user_id)
│
├─ 1. 建 token_queue（线程安全队列）+ StreamingCallbackHandler(on_token)
│        └─ 模型每吐一个 token → on_llm_new_token → on_token → 塞进 queue
│
├─ 2. 记忆加载（见第 4 节）
│        history       = memory_manager.load_messages(session_id)
│        memory_prefix = memory_manager.build_memory_prefix(query, session_id)
│
├─ 3. 建图  create_agent(callback_handler, model, memory_prefix)
│        构造 state = MessagesState(messages = history + [HumanMessage(query)])
│
├─ 4. 先 yield 一条 "start" 事件，告诉前端"开始处理"
│
├─ 5. 在后台线程跑 run_agent()（loop.run_in_executor）
│        └─ current_user.set(user_id)      # 注入身份，供工具做表级权限校验
│        └─ react_graph.invoke(state, config)   # 真正跑 Agent
│
├─ 6. 主循环不停从 token_queue 取 token，yield "response" 事件（流式增量）
│
└─ 7. 后台线程结束后：取最终回答 → 拼图表配置 → yield 最终 "response"（finished=True）
        最后 memory_manager.persist(...) 沉淀本轮对话
```

SSE 事件格式（前端据此渲染）：

```json
{ "event": "message", "data": { "type": "start", ... } }
{ "event": "message", "data": { "type": "response", "message": "...", "finished": false } }
{ "event": "message", "data": { "type": "response", "message": "...", "finished": true } }
{ "event": "error",   "data": { "type": "error", "message": "..." } }
```

> 为什么「后台线程 + 队列」：LangGraph 的 `invoke` 是同步阻塞的，而 SSE 需要主循环不停 `await` 推送。
> 于是把阻塞调用丢给线程池，主循环用非阻塞 `get_nowait()` 轮询队列，兼顾两者。

---

## 4. 记忆是怎么注入进这一轮的

每次请求都会走一次「加载 → 注入 → 沉淀」，由 [memory_manager.py](../memory_manager.py) 的 `MemoryManager` 统一封装：

- **短期（历史消息）**：`load_messages(session_id)` 从 SQLite 读出完整历史，拼进 `state.messages`。
- **中期（滚动摘要）+ 长期（语义召回）**：`build_memory_prefix(query, session_id)` 组装成一段「记忆前缀」文本，作为一条额外的 `SystemMessage` 塞进模型上下文。
- **沉淀**：Agent 跑完后 `persist(...)` 存历史、按需更新摘要、用 LLM 提取长期记忆写回向量库。

模型拿到的最终上下文（见 [agent.py](../agent.py) 的 `llm_agent`）是：

```python
context = [sys_msg]                        # 系统提示词 + 指标口径定义
if memory_prefix:
    context.append(SystemMessage(memory_prefix))   # 摘要 + 召回的记忆
context += trim_messages(state.messages)           # 裁剪后的历史（含本轮提问）
```

> 注意：`trim_messages` 只裁剪「进 prompt 的那份」，LangGraph 的 `state` 里仍保留完整历史，方便最后持久化。

---

## 5. 核心：LangGraph 状态机怎么跑

`create_agent()` 在 [agent.py](../agent.py) 里构建一个有向图，节点和边如下：

```
        START
          │  (check_ambiguity：最新用户消息是否含模糊指标口径？)
          ├── 有模糊 ──> clarify 节点 ──> END   (列出口径选项，等用户确认)
          └── 无模糊 ──> llm_agent 节点
                            │  模型产出 AIMessage（可能是纯文本，也可能带 tool_calls）
                            │  (tools_condition：有 tool_calls 吗？)
                            ├── 无 tool_calls ──> END
                            └── 有 tool_calls ──> tools 节点（AgentToolExecutor 执行工具）
                                                        │
                                                        └──> 回到 llm_agent（循环，最多 recursion_limit 次）
```

两个关键节点：

- **`llm_agent`**：把上面第 4 节的 `context` 丢给 `llm_with_tools.invoke(...)`。模型绑定了一组工具，它自己决定「要不要调工具、调哪个、传什么参数」。
- **`tools`**：`AgentToolExecutor`（[tools/tool_executor.py](../tools/tool_executor.py)）真正执行模型发起的 `tool_calls`，把结果包成 `ToolMessage` 塞回消息，再回到 `llm_agent` 让模型基于结果继续。

**「模型 + 工具」循环就是本项目处理一个提问的引擎**：模型不会一次性答完，而是多轮「想一步 → 调工具 → 看结果 → 再想」。

---

## 6. 用户提问是如何被"解析"成 SQL 的

这一步**不是后端硬编码解析**，而是模型根据系统提示词和工具描述自主决策。系统提示词（[agent.py](../agent.py) 的 `sys_msg`）明确要求：

> 必须真正调用工具查数，禁止只说"我会执行 SQL"；需要时先用 `database_schema_rag` 查 schema、再用 `text2sqlite_query` 生成 SQL、最后用 `execute_sqlite_query` 执行。

绑定给模型的工具（`tools` 列表）：

| 工具 | 作用 |
|---|---|
| `database_schema_rag` | 向量检索表结构 / schema 说明 |
| `text2sqlite_query` | 把自然语言转成 SQLite SQL |
| `execute_sqlite_query` | 真正执行 SQL 并返回结果 |
| `high_charts_json` | 把数字转成 Highcharts 图表配置 |
| `export_artifacts_tool` | 导出产物 |
| `search`（DuckDuckGo） | 联网搜索 |
| MCP `time` 等 | 获取时间等外部能力 |

一个典型提问「查下近 7 天的订单金额」会触发这样的工具调用链：

```
模型读 sys_msg + 工具描述
  └─> 调 database_schema_rag("order 金额 字段")     # 拿到表结构
  └─> 调 text2sqlite_query("近7天订单金额", schema)  # 生成 SELECT ... 
  └─> 调 execute_sqlite_query(SQL)                  # 真正执行
  └─>（若用户要画图）调 high_charts_json(数字, 类型)  # 生成图表配置
  └─> 模型汇总，输出最终答案（含数据来源表 / 计算过程 / 查询结果 / 结论）
```

---

## 7. SQL 真正执行时会发生什么（多层防护）

`execute_sqlite_query`（[tools/tools_execute_sqlite.py](../tools/tools_execute_sqlite.py)）不是裸跑 SQL，而是层层设防：

1. **只读白名单** `ensure_read_only_query`：写语句 / 多语句直接拒绝（防删库改库）。
2. **表级权限闸门**（新增，[tools/access_control.py](../tools/access_control.py)）：用 SQLite `set_authorizer` 在引擎层拦截对未授权表的读取，JOIN/子查询/CTE 里的越权表也拦得住。
3. **执行计划预检**：`EXPLAIN QUERY PLAN` 拦笛卡尔积、大表全扫。
4. **执行中超时**：看门狗线程 + VM 指令数兜底，慢查询被中断。
5. **结果截断**：行数 + 字节数双重截断，防止撑爆模型上下文。

工具返回的 `{"status": "success"/"too_large"/"error", "result": ...}` 会作为 `ToolMessage` 回给模型；`AgentToolExecutor` 还会按错误类型分类（临时错误重试、慢查询让模型改写、权限错误直接终止等）。

---

## 8. 最终答案怎么回到前端

Agent 跑完后，`stream_agent_response` 从图的最终状态里取**最后一条 AIMessage 的 content** 作为最终回答（而不是流式缓冲，避免混入"让我执行 SQL…"这类过渡语）：

- 若过程中调用过画图工具，把 `chart_config` 以 JSON 代码块追加到回答末尾。
- 最终 `yield` 一条 `finished=True` 的 SSE 事件，前端结束加载、展示全文。

---

## 9. 一图流总结（端到端）

```
前端 POST /api/chat/query
  └─> ChatRequest 校验（query/model/user_id/...）
        └─> stream_agent_response（SSE）
              ├─ 加载记忆（历史 + 摘要 + 长期召回）
              ├─ create_agent 建 LangGraph 图
              ├─ 后台线程 invoke： llm_agent ⇄ tools 循环
              │      ├─ LLM 决策调工具
              │      ├─ database_schema_rag / text2sqlite_query / execute_sqlite_query / high_charts_json
              │      └─ execute_sqlite_query：只读 + 表级权限 + 执行计划 + 超时 + 截断
              ├─ 主循环推 token → SSE response 事件
              └─ 结束：取最终 AIMessage → persist 记忆 → finished=True
```

---

## 10. 排查问题时的关键看日志点

- `[DEBUG] Received token` / `Starting agent execution` —— 确认请求进入、token 是否在流。
- `---- Executing SQL Query ----` 及其后打印的 SQL —— 看模型实际生成了什么 SQL、是否被权限/只读拦截。
- `[MEMORY][摘要]` / `[MEMORY][提取]` —— 记忆压缩与提取是否正常。
- 工具返回里的 `status`（success / too_large / error）—— 判断是"查不到"还是"结果过大被截断"还是"权限被拒"。
