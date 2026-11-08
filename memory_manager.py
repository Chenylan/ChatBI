"""
记忆管理模块：实现 ChatBI 的四层记忆管理。

    ① 长短期分层 + 持久化  —— ConversationStore（SQLite 存历史消息 + 滚动摘要）
    ② 上下文剪裁           —— trim_messages()（进 LLM 前裁剪，state 保留完整）
    ③ 上下文压缩           —— MemoryManager._update_summary()（LLM 滚动摘要）
    ④ 关键信息优先召回     —— SemanticMemoryStore（Chroma 向量 + 元数据加权打分）

设计要点：
    - 不依赖 LangGraph 的 checkpointer（进程内存、易失），改为显式"加载 → 调用 → 持久化"。
    - 所有会调用外部服务（LLM / embedding）的地方都支持注入，便于单元测试：
        MemoryManager(llm=..., embeddings=...) 传入 fake 即可离线测试。
    - 存储路径可配置，默认放在项目根目录；长期记忆向量库独立存于 chroma_memory_db/，
      与 schema 索引（chroma_schema_db/）隔离，避免 CI 重建索引时覆盖运行时记忆。
"""

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import tiktoken
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

# 项目根目录（memory_manager.py 与 agent.py 同级）
_PROJECT_ROOT = Path(__file__).resolve().parent

# ======================================================================
# 通用工具：消息序列化 / token 估算
# ======================================================================

# tiktoken 编码器（模块级缓存，cl100k_base 用于近似估算 token；qwen 无对应编码器，近似即可）
_ENCODER: Optional[Any] = None


def _get_encoder():
    """获取（并缓存）tiktoken 编码器；失败返回 None，调用方回退到字符数估算。"""
    global _ENCODER
    if _ENCODER is None:
        try:
            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENCODER = None
    return _ENCODER


def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数（近似，用于剪裁/压缩的预算控制）。"""
    if not text:
        return 0
    enc = _get_encoder()
    if enc is None:
        return max(len(text) // 4, 1)
    try:
        return len(enc.encode(text))
    except Exception:
        return max(len(text) // 4, 1)


def _message_text(msg: BaseMessage) -> str:
    """把一条消息的内容统一转成字符串（用于 token 估算与摘要输入）。"""
    content = msg.content
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def estimate_messages_tokens(messages: Sequence[BaseMessage]) -> int:
    """估算整个消息列表的 token 数。"""
    return sum(estimate_tokens(_message_text(m)) for m in messages)


def _serialize_messages(messages: Sequence[BaseMessage]) -> str:
    """把消息列表序列化成 JSON 字符串（只保留回放所需字段，规避 LangChain 版本易变 API）。"""
    out: List[Dict[str, Any]] = []
    for m in messages:
        content = m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False, default=str)
        item: Dict[str, Any] = {"type": getattr(m, "type", "unknown"), "content": content}
        if isinstance(m, ToolMessage):
            item["tool_call_id"] = m.tool_call_id
        out.append(item)
    return json.dumps(out, ensure_ascii=False)


def _deserialize_messages(data: str) -> List[BaseMessage]:
    """把 JSON 字符串还原成消息列表（序列化时非字符串内容已转成字符串，还原后均为文本消息）。"""
    if not data:
        return []
    try:
        raw = json.loads(data)
    except Exception:
        return []
    messages: List[BaseMessage] = []
    for item in raw:
        t = item.get("type")
        content = item.get("content", "")
        if t == "human":
            messages.append(HumanMessage(content=content))
        elif t == "ai":
            messages.append(AIMessage(content=content))
        elif t == "tool":
            messages.append(ToolMessage(content=content, tool_call_id=item.get("tool_call_id", "")))
        elif t == "system":
            messages.append(SystemMessage(content=content))
        else:
            # 未知类型兜底成 HumanMessage，保证不丢内容
            messages.append(HumanMessage(content=str(content)))
    return messages


# ======================================================================
# ② 上下文剪裁
# ======================================================================

def trim_messages(
    messages: Sequence[BaseMessage],
    max_tokens: int = 6000,
    max_tool_chars: int = 2000,
) -> List[BaseMessage]:
    """剪裁消息列表，控制进入 LLM 的 token 预算。

    规则：
        1. 超长的 ToolMessage（通常是 SQL 查询结果）先截断到 max_tool_chars；
        2. 从最旧一端丢弃消息，直到总 token 数不超过 max_tokens；
        3. 永远保留最后一条消息（最新的 user/ai），即使它本身超预算。

    注意：只返回"进 prompt 的这一份"，不修改原列表；LangGraph 的 state 仍保留完整历史，
    这样 result["messages"] 依旧是全量，便于后续持久化。
    """
    # 1) 截断超长工具结果
    processed: List[BaseMessage] = []
    for m in messages:
        if isinstance(m, ToolMessage) and isinstance(m.content, str) and len(m.content) > max_tool_chars:
            m = ToolMessage(
                content=m.content[:max_tool_chars] + f"\n...[已截断，原 {len(m.content)} 字符]",
                tool_call_id=m.tool_call_id,
            )
        processed.append(m)

    if not processed:
        return []

    # 2) 从最新往前加，预算满了就停（天然保证最后一条一定被保留）
    kept: List[BaseMessage] = []
    total = 0
    for m in reversed(processed):
        cost = estimate_tokens(_message_text(m))
        if kept and total + cost > max_tokens:
            break
        kept.append(m)
        total += cost

    # 兜底：若最后一条自身超预算导致 kept 为空，仍保留它
    if not kept:
        kept = [processed[-1]]

    kept.reverse()  # 恢复为时间正序（旧 → 新）
    return kept


# ======================================================================
# ① 长短期分层 + 持久化：会话历史存储（SQLite）
# ======================================================================

class ConversationStore:
    """会话历史持久化存储（SQLite）。

    表结构：conversation(session_id, messages_json, summary, updated_at)
    - messages_json：完整消息列表（短期记忆，原始消息）
    - summary：滚动摘要（中期记忆，超出窗口部分被压缩后的结果）
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation (
                    session_id    TEXT PRIMARY KEY,
                    messages_json TEXT NOT NULL,
                    summary       TEXT NOT NULL DEFAULT '',
                    updated_at    REAL NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def load(self, session_id: str):
        """读取一个会话的历史消息与摘要；不存在时返回 ([], "")。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT messages_json, summary FROM conversation WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return [], ""
        return _deserialize_messages(row[0]), row[1] or ""

    def save(self, session_id: str, messages: Sequence[BaseMessage], summary: Optional[str] = None) -> None:
        """保存会话历史；summary 为 None 时保留旧摘要（用于仅更新消息的场景）。"""
        messages_json = _serialize_messages(messages)
        now = time.time()
        conn = self._connect()
        try:
            if summary is None:
                conn.execute(
                    """
                    INSERT INTO conversation (session_id, messages_json, summary, updated_at)
                    VALUES (?, ?, '', ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        messages_json = excluded.messages_json,
                        updated_at    = excluded.updated_at
                    """,
                    (session_id, messages_json, now),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO conversation (session_id, messages_json, summary, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        messages_json = excluded.messages_json,
                        summary       = excluded.summary,
                        updated_at    = excluded.updated_at
                    """,
                    (session_id, messages_json, summary, now),
                )
            conn.commit()
        finally:
            conn.close()


# ======================================================================
# ④ 关键信息优先召回：语义记忆存储（Chroma）
# ======================================================================

# 打分权重：向量相似度 / 重要度 / 新鲜度 / 复用次数
_WEIGHT_SIM = 0.50
_WEIGHT_IMPORTANCE = 0.25
_WEIGHT_FRESHNESS = 0.15
_WEIGHT_REUSE = 0.10
# 新鲜度半衰期（秒）：7 天，越近的信息越容易被召回
_FRESHNESS_HALF_LIFE = 7 * 24 * 3600


def score_memory(similarity: float, importance: float, updated_at: float, reuse_count: int, now: Optional[float] = None) -> float:
    """对一条记忆做加权打分（纯函数，便于单测）。

    similarity: [0,1] 向量相似度（由距离换算）
    importance: [0,1] 重要度
    updated_at: epoch 秒，用于新鲜度衰减
    reuse_count: 被复用次数，越用越优先
    """
    now = now if now is not None else time.time()
    freshness = math.exp(-(now - updated_at) / _FRESHNESS_HALF_LIFE)
    reuse_norm = min(reuse_count / 10.0, 1.0)
    return (
        _WEIGHT_SIM * similarity
        + _WEIGHT_IMPORTANCE * importance
        + _WEIGHT_FRESHNESS * freshness
        + _WEIGHT_REUSE * reuse_norm
    )


class SemanticMemoryStore:
    """长期语义记忆存储（Chroma 向量库 + 元数据打分）。

    每条记忆的元数据：kind / importance / reuse_count / updated_at。
    id 由 (kind + content) 做哈希，重复写入时复用计数 +1，实现"越用越优先"。
    """

    def __init__(self, chroma_path: str, collection_name: str = "memory_collection", embeddings: Optional[Embeddings] = None):
        # 延迟导入 Chroma，避免测试（注入 fake embeddings）时触发 chromadb 模型下载等副作用
        from langchain_community.vectorstores import Chroma

        if embeddings is None:
            from tools.embeddings import get_embeddings
            embeddings = get_embeddings()

        self.vector_store = Chroma(
            collection_name=collection_name,
            embedding_function=embeddings,
            persist_directory=chroma_path,
        )

    @staticmethod
    def _memory_id(kind: str, content: str) -> str:
        return hashlib.md5(f"{kind}:{content}".encode("utf-8")).hexdigest()

    def add(self, kind: str, content: str, importance: float = 0.5) -> None:
        """写入（或更新）一条长期记忆；已存在则复用计数 +1。"""
        memory_id = self._memory_id(kind, content)
        now = time.time()

        # 查重：存在则累加复用计数，否则记为首次
        try:
            existing = self.vector_store.get([memory_id])
        except Exception:
            existing = None
        reuse_count = 1
        if existing and existing.get("ids"):
            reuse_count = int((existing.get("metadatas") or [{}])[0].get("reuse_count", 0) or 0) + 1

        meta = {
            "kind": kind,
            "importance": importance,
            "reuse_count": reuse_count,
            "updated_at": now,
        }
        doc = Document(page_content=content, metadata=meta)

        if existing and existing.get("ids"):
            self.vector_store.update_document(memory_id, doc)
        else:
            self.vector_store.add_documents([doc], ids=[memory_id])

    def recall(self, query: str, top_k: int = 5) -> List[str]:
        """按 query 召回 top_k 条长期记忆，返回记忆文本列表（按加权得分降序）。"""
        # 先召回 3 倍候选，再做加权重排，避免高分条目被相似度粗筛漏掉
        try:
            docs_scores = self.vector_store.similarity_search_with_score(query, k=max(top_k * 3, top_k))
        except Exception:
            return []

        now = time.time()
        scored: List[tuple] = []
        for doc, dist in docs_scores:
            similarity = 1.0 / (1.0 + dist)  # 距离 → 相似度（单调映射到 (0,1]）
            meta = doc.metadata or {}
            importance = float(meta.get("importance", 0.5) or 0.5)
            reuse_count = int(meta.get("reuse_count", 0) or 0)
            updated_at = float(meta.get("updated_at", now) or now)
            s = score_memory(similarity, importance, updated_at, reuse_count, now=now)
            scored.append((s, doc.page_content))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [content for _, content in scored[:top_k]]


# ======================================================================
# ③ 上下文压缩 + 门面：MemoryManager
# ======================================================================

# 从 LLM 提取长期记忆的 prompt
_EXTRACT_PROMPT = (
    "从以下对话中提取值得长期记住的信息，例如：用户偏好、指标口径定义、实体别名、关键结论。\n"
    '只输出 JSON 数组，每个元素格式：{{"kind": "preference|metric|alias|conclusion", "content": "...", "importance": 0.0~1.0}}\n'
    "没有可提取的信息则输出 []。\n\n对话：\n{conversation}"
)


def _parse_facts(text: str) -> List[Dict[str, Any]]:
    """从 LLM 输出中解析 JSON 数组（容忍 markdown 代码块包裹）。"""
    if not text:
        return []
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        return []
    try:
        arr = json.loads(match.group(0))
    except Exception:
        return []
    out = []
    for item in arr:
        if isinstance(item, dict) and item.get("content"):
            out.append(item)
    return out


class MemoryManager:
    """记忆管理门面：组装（加载+召回）与沉淀（持久化+摘要+提取）的统一入口。

    用法（backend/api/chat.py）：
        mm = MemoryManager()
        history = mm.load_messages(session_id)
        prefix  = mm.build_memory_prefix(query, session_id)   # 摘要 + 召回
        # ... invoke ...
        mm.persist(session_id, result["messages"])            # 存历史 + 更新摘要 + 提取长期记忆
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        chroma_path: Optional[str] = None,
        llm: Any = None,
        model_name: str = "qwen-plus",
        embeddings: Optional[Embeddings] = None,
        collection_name: str = "memory_collection",
    ):
        self.db_path = db_path or str(_PROJECT_ROOT / "chat_memory.sqlite3")
        self.chroma_path = chroma_path or str(_PROJECT_ROOT / "chroma_memory_db")
        self.model_name = model_name
        self.llm = llm  # 可注入 fake；None 时惰性构建真实 LLM
        self.summary_threshold_tokens = 3000  # 历史超过该 token 数才触发摘要压缩，避免每轮多一次 LLM 调用
        self.top_k = 5
        self._lock = threading.Lock()  # 串行化 SQLite/Chroma 的并发读写

        self.conversation_store = ConversationStore(self.db_path)
        self.semantic_store = SemanticMemoryStore(self.chroma_path, collection_name, embeddings)

    # ---- 组装（加载 + 召回） ----
    def load_messages(self, session_id: str) -> List[BaseMessage]:
        """加载会话的完整历史消息（短期记忆）。"""
        messages, _ = self.conversation_store.load(session_id)
        return messages

    def load_summary(self, session_id: str) -> str:
        """加载会话的滚动摘要（中期记忆）。"""
        _, summary = self.conversation_store.load(session_id)
        return summary

    def recall(self, query: str, top_k: Optional[int] = None) -> List[str]:
        """关键信息优先召回（长期记忆）。"""
        with self._lock:
            try:
                return self.semantic_store.recall(query, top_k or self.top_k)
            except Exception:
                return []

    def build_memory_prefix(self, query: str, session_id: str) -> str:
        """组装注入 prompt 的记忆前缀：滚动摘要 + 召回的关键信息。"""
        parts: List[str] = []
        summary = self.load_summary(session_id)
        if summary:
            parts.append(f"【历史对话摘要】\n{summary}")
        recalled = self.recall(query)
        if recalled:
            parts.append("【相关历史记忆】\n" + "\n".join(f"- {c}" for c in recalled))
        return "\n\n".join(parts)

    # ---- 沉淀（持久化 + 压缩 + 提取） ----
    def persist(self, session_id: str, messages: Sequence[BaseMessage], query: Optional[str] = None) -> None:
        """持久化一轮对话结果：保存历史 + 更新滚动摘要（压缩）+ 提取长期记忆。"""
        with self._lock:
            # ③ 上下文压缩：历史超阈值才更新滚动摘要，避免每轮多一次 LLM 调用
            new_summary = None
            if estimate_messages_tokens(messages) > self.summary_threshold_tokens:
                new_summary = self._update_summary(session_id, messages)

            # ① 持久化历史（summary=None 时保留旧摘要，避免覆盖）
            self.conversation_store.save(session_id, messages, summary=new_summary)

            # ④ 提取并写入长期记忆
            for fact in self._extract_facts(messages):
                try:
                    self.semantic_store.add(
                        kind=fact.get("kind", "conclusion"),
                        content=fact.get("content", ""),
                        importance=float(fact.get("importance", 0.5) or 0.5),
                    )
                except Exception:
                    pass

    def _get_llm(self):
        """惰性构建摘要/提取用的 LLM（可注入 fake 以离线测试）。"""
        if self.llm is None:
            from langchain_openai import ChatOpenAI
            api_key = os.getenv("OPENAI_API_KEY")
            base_url = os.getenv("OPENAI_API_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
            self.llm = ChatOpenAI(
                model=self.model_name,
                api_key=api_key or "sk-placeholder",
                base_url=base_url,
                temperature=0,
            )
        return self.llm

    def _invoke_text(self, prompt: str) -> str:
        """调用 LLM 并统一取出文本内容（兼容返回对象含 .content 或纯字符串）。"""
        try:
            resp = self._get_llm().invoke(prompt)
        except Exception as e:
            # 静默失败会让"摘要/提取没生效"变得不可见，这里必须打日志
            print(f"[MEMORY][LLM] 调用失败：{e}")
            return ""
        content = getattr(resp, "content", None)
        text = (content if content is not None else str(resp)) or ""
        if not text:
            print("[MEMORY][LLM] 返回空内容")
        return text

    def _update_summary(self, session_id: str, messages: Sequence[BaseMessage]) -> Optional[str]:
        """③ 上下文压缩：把旧摘要 + 最近几轮压缩成新的滚动摘要。"""
        old_summary = self.load_summary(session_id)
        # 只取最近 8 条作为摘要输入，控制 LLM 输入长度
        recent = "\n".join(f"{m.type}: {_message_text(m)}" for m in messages[-8:])
        print(f"[MEMORY][摘要] 触发压缩：历史 {len(messages)} 条，旧摘要 {len(old_summary)} 字符")

        if old_summary:
            prompt = (
                "你是一个对话摘要助手。以下是之前的对话摘要：\n"
                f"{old_summary}\n\n"
                "请结合最近几轮对话内容，扩展并更新这段摘要，保留关键结论、指标口径、用户偏好，压缩冗余细节。\n"
                f"最近对话：\n{recent}\n\n"
                "请只输出更新后的摘要，不要额外解释。"
            )
        else:
            prompt = (
                "你是一个对话摘要助手。请为以下对话生成一段简洁摘要，保留关键结论、指标口径、用户偏好。\n"
                f"对话：\n{recent}\n\n"
                "请只输出摘要，不要额外解释。"
            )

        new_summary = self._invoke_text(prompt).strip()
        if not new_summary:
            print("[MEMORY][摘要] 压缩失败，回退到旧摘要")
            return old_summary
        print(f"[MEMORY][摘要] 更新成功：{len(new_summary)} 字符 → {new_summary[:80]!r}")
        return new_summary

    def _extract_facts(self, messages: Sequence[BaseMessage]) -> List[Dict[str, Any]]:
        """④ 用 LLM 从最近对话中提取值得长期记住的信息。"""
        recent = "\n".join(f"{m.type}: {_message_text(m)}" for m in messages[-8:])
        text = self._invoke_text(_EXTRACT_PROMPT.format(conversation=recent))
        facts = _parse_facts(text)
        if facts:
            contents = [str(f.get("content", ""))[:40] for f in facts]
            print(f"[MEMORY][提取] 提取到 {len(facts)} 条长期记忆：{contents}")
        else:
            # 打印 LLM 原始输出，便于判断是「确实没有」还是「JSON 解析失败」
            print(f"[MEMORY][提取] 未提取到长期记忆（LLM 原始输出：{text[:120]!r}）")
        return facts
