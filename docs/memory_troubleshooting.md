# 记忆丢失排查手册

> 适用场景：多轮对话进行到较后轮次（如第 10 轮）时，模型"忘记"了先前出现过的关键信息（用户偏好、指标口径、实体别名、关键结论等）。

核心结论：**关键信息丢失，几乎都是四条链路同时断了**——原始消息被剪裁掉、滚动摘要没兜住、长期记忆没召回。排查就是逐条验证这四条链路，定位断点。

---

## 一、心智模型：关键信息走的四条链路

```
关键信息（如"销售额按含税口径"）
   │
   ├─[链路① 持久化] 存进 SQLite 历史 → 每轮 load 回来
   ├─[链路② 剪裁]   历史 → trim_messages → 进 LLM 的那份 prompt
   ├─[链路③ 压缩]   历史 → LLM 滚动摘要 → 作为前缀注入
   └─[链路④ 召回]   历史 → LLM 提取 → Chroma → recall → 作为前缀注入
```

信息要"活到第 10 轮"，至少需要满足其一：**①历史里有 → ②没被裁掉**，或 **③摘要里有**，或 **④能召回**。

| 链路 | 实现位置 | 存储 |
|---|---|---|
| ① 持久化 | `memory_manager.ConversationStore` | `chat_memory.sqlite3`（表 `conversation`） |
| ② 剪裁 | `memory_manager.trim_messages()` | 无（只影响进 prompt 的那份） |
| ③ 压缩 | `MemoryManager._update_summary()` | `conversation.summary` |
| ④ 召回 | `memory_manager.SemanticMemoryStore` | `chroma_langchain_db/`（collection `memory_collection`） |

---

## 二、第一步：一键诊断，定位断点

在第 10 轮出问题后，直接跑下面的脚本，把四条链路一次性 dump 出来：

```python
# debug_memory.py
from memory_manager import MemoryManager, trim_messages, estimate_messages_tokens

mm = MemoryManager()
sid = "s1"                          # 换成你的 session_id
query = "销售额总共多少"              # 第 10 轮的实际问题

# ① 持久化：历史里到底存了什么
msgs, summary = mm.conversation_store.load(sid)
print(f"[①历史] 共 {len(msgs)} 条，token={estimate_messages_tokens(msgs)}")
for i, m in enumerate(msgs):
    print(f"   {i:2d} {m.type:6s} {str(m.content)[:50]!r}")

# ② 剪裁：真正进 LLM 的是哪几条
kept = trim_messages(msgs)
print(f"[②剪裁] 保留 {len(kept)}/{len(msgs)} 条")
for i, m in enumerate(kept):
    print(f"   {i:2d} {m.type:6s} {str(m.content)[:50]!r}")

# ③ 摘要
print(f"[③摘要] {summary!r}")

# ④ 召回
print(f"[④召回] {mm.recall(query)}")
print(f"[④前缀]\n{mm.build_memory_prefix(query, sid)}")
```

对照下表判断：

| 现象 | 结论 | 断的链路 |
|---|---|---|
| ①历史里**根本没有**第 1 轮那条信息 | 持久化断（串号 / persist 失败） | ① |
| ①有，但②里**被裁掉了**，且③④都为空 | 剪裁丢信息，摘要/召回没兜底 | ② |
| ③摘要里没有关键信息 | 摘要没触发或有损压缩 | ③ |
| ④召回为空 / 不含关键信息 | 长期记忆没提取或没命中 | ④ |

---

## 三、逐条链路排查

> 贯穿示例：**第 1 轮用户说"销售额统一按含税口径，别算错"，第 10 轮问"销售额总共多少"时模型答错口径。**

### 链路① 持久化：历史有没有真的累积

```bash
# 直接看 SQLite 原始数据
sqlite3 chat_memory.sqlite3 \
  "SELECT session_id, length(messages_json), length(summary), datetime(updated_at,'unixepoch') FROM conversation;"
```

**判断**：该 `session_id` 的 `messages_json` 很短、或压根没有记录 → 持久化没发生。最常见两个原因：

1. **session_id 串号**：每轮请求的 `session_id` 不一致，导致每轮都开新会话。看 `backend/api/chat.py` 的 `session_id = request.session_id or "default"`——**确认前端每轮都带上同一个 session_id**。
2. **persist 抛异常被吞了**：看后端日志有没有 `[WARN] 记忆持久化失败`（`backend/api/chat.py` 的 `run_agent` 内）。

### 链路② 剪裁：是不是被 `trim_messages` 裁掉了

这是**第 10 轮丢信息的最高频根因**。前几轮 SQL 查询产生的 `ToolMessage` 结果很大，会把 `max_tokens` 预算占满，最早的几轮（含第 1 轮的关键信息）就从最旧端被丢弃。

看 `[②剪裁]` 输出：第 1 轮信息在 `[①历史]` 里有、在 `[②剪裁]` 里没了 → 就是剪裁裁掉的。

```python
kept = trim_messages(msgs)
print("含税口径还在吗:", any("含税" in str(m.content) for m in kept))
```

**修法**：

- 调大 `max_tokens`（qwen 上下文是 128k，可放到 20000+）——改 `memory_manager.py` 里 `trim_messages` 的默认值，或调用处传参。
- 调小 `max_tool_chars`（把工具结果占的预算压下去，给真正的对话留空间）。

### 链路③ 压缩：摘要有没有兜住

摘要只在历史 `> summary_threshold_tokens`（默认 3000）时才触发，且摘要是 LLM 压缩的、**有损**——可能把"含税口径"这类精确信息概括没了。

```python
print("摘要内容:", summary)          # 看有没有"含税口径"
print("摘要是否触发过:", len(summary) > 0)
```

**修法**：

- 降低 `summary_threshold_tokens`（3000 → 1500），让摘要更早兜底。
- 改 `_update_summary` 的 prompt，明确要求"必须原样保留指标口径、用户偏好等精确信息，不得概括丢失"。

### 链路④ 召回：长期记忆有没有提取 + 命中

```python
# 看语义库里到底存了什么（直接查 Chroma）
docs = mm.semantic_store.vector_store.similarity_search_with_score("销售额口径", k=10)
for d, s in docs:
    print(s, d.page_content, d.metadata)
```

**判断**：

- **库里根本没有**"含税口径" → `_extract_facts` 没提取到。原因：提取 prompt 不够明确 / LLM 返回 `[]` / `_parse_facts` 解析失败（LLM 没输出纯 JSON）。
- **库里有，但 `recall` 没返回** → 召回/打分问题：`top_k` 太小、query 措辞与存储内容差异大导致向量没命中、或被加权打分挤掉。

**修法**：

- 提取没到 → 改 `_EXTRACT_PROMPT`，显式列举"口径定义、用户偏好、实体别名"等类别。
- 召回没中 → 调大 `top_k`；或加关键词/BM25 混合检索（BI 场景术语精确匹配比纯向量更可靠）。

---

## 四、贯穿示例：一个完整的排查闭环

假设诊断脚本输出如下：

```
[①历史] 共 38 条，token=12480
   0  human '销售额统一按含税口径，别算错'      ← 关键信息在
   1  ai    '好的，已记住含税口径'
   2  tool  '{"result":{"rows":[...3000字...]}}'  ← 大量 SQL 结果
   ...
[②剪裁] 保留 9/38 条
   0  tool  '{"result":...}'          ← 最早的 user/ai 已被裁掉
[③摘要] ''                            ← 摘要没触发
[④召回] []                            ← 长期记忆也没提取
```

**结论**：①有 → ②裁掉了 → ③④都空。根因是"剪裁丢信息 + 摘要/召回都没兜底"。

**对应修复**：

1. `max_tokens` 6000 → 20000（治标：让第 1 轮别被裁掉）
2. `summary_threshold_tokens` 3000 → 1500，并强化摘要 prompt 保留口径（治本：压缩兜底）
3. 强化 `_EXTRACT_PROMPT`，让"口径"这类信息沉淀到长期记忆，换 query 也能召回（治本：召回兜底）

---

## 五、日志参考

`memory_manager.py` 的摘要/提取/LLM 调用已加了 `[MEMORY][...]` 日志，排查时后端日志会直接告诉你断在哪：

| 日志 | 含义 | 下一步 |
|---|---|---|
| `[MEMORY][LLM] 调用失败：...` | 摘要/提取的 LLM 调用没成功（网络/key/超时） | 查 LLM 调用链路 |
| `[MEMORY][LLM] 返回空内容` | LLM 成功但没返回文本 | 查 prompt / 模型输出 |
| `[MEMORY][摘要] 触发压缩：...` | 开始滚动摘要压缩 | — |
| `[MEMORY][摘要] 更新成功：N 字符 → ...` | 摘要更新成功，含前 80 字预览 | 检查预览里有没有关键信息 |
| `[MEMORY][摘要] 压缩失败，回退到旧摘要` | 摘要链路断了 | 查上一条 LLM 日志 |
| `[MEMORY][提取] 提取到 N 条长期记忆：[...]` | 长期记忆提取成功 | 检查有没有覆盖关键信息 |
| `[MEMORY][提取] 未提取到长期记忆（LLM 原始输出：...）` | 没提取到；原始输出能区分"确实没有 `[]`" vs "JSON 解析失败" | 改 `_EXTRACT_PROMPT` |

---

## 六、参数速查表

| 参数 | 位置 | 默认值 | 调大/调小的影响 |
|---|---|---|---|
| `max_tokens` | `trim_messages()` | 6000 | 调大 → 剪裁窗口变大，丢得更晚 |
| `max_tool_chars` | `trim_messages()` | 2000 | 调小 → 工具结果占预算更少，给对话留空间 |
| `summary_threshold_tokens` | `MemoryManager` | 3000 | 调小 → 摘要更早触发，压缩兜底更及时 |
| `top_k` | `MemoryManager` / `recall()` | 5 | 调大 → 召回更多记忆，命中率更高 |
| 打分权重 | `score_memory()` | α0.5/β0.25/γ0.15/δ0.10 | 调大 β → 高重要度记忆更优先 |
