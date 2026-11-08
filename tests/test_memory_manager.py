"""
记忆管理模块的离线单元测试（不依赖真实 LLM / 真实 embedding）。

用法：
    python tests/test_memory_manager.py          # 直接运行
    pytest tests/test_memory_manager.py -v       # 或 pytest

覆盖：
    - trim_messages：工具结果截断、token 预算、保留最后一条消息
    - ConversationStore：save → 重新实例化 → load 复原（模拟进程重启）
    - score_memory：加权打分纯函数
    - MemoryManager._update_summary：滚动摘要压缩（fake LLM）
    - MemoryManager._extract_facts / SemanticMemoryStore：提取 + 写入 + 召回
    - MemoryManager.persist / build_memory_prefix：端到端组装与沉淀
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from memory_manager import (
    ConversationStore,
    MemoryManager,
    score_memory,
    trim_messages,
)


class FakeEmbeddings(Embeddings):
    """返回全零向量的假 embedding，避免下载模型，可离线测试。"""
    def embed_documents(self, texts):
        return [[0.0] * 8 for _ in texts]

    def embed_query(self, query):
        return [0.0] * 8


class FakeResp:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    """根据 prompt 内容返回预设响应的假 LLM。"""
    def __init__(self, summary="更新后的摘要：用户关注订单与销售口径。",
                 facts='[{"kind":"metric","content":"销售额按含税口径","importance":0.9}]'):
        self.summary = summary
        self.facts = facts
        self.calls = []

    def invoke(self, prompt):
        self.calls.append(prompt)
        if "摘要" in prompt:
            return FakeResp(self.summary)
        if "提取" in prompt:
            return FakeResp(self.facts)
        return FakeResp("")


def test_trim_messages():
    # 1) 超长工具结果被截断
    long_tool = ToolMessage(content="x" * 5000, tool_call_id="t1")
    out = trim_messages([long_tool], max_tool_chars=2000)
    assert "已截断" in out[0].content

    # 2) token 预算：从最旧丢弃，但永远保留最后一条消息
    msgs = [HumanMessage(content="a" * 4000) for _ in range(10)]
    msgs.append(HumanMessage(content="最后一条问题"))
    out = trim_messages(msgs, max_tokens=2000)
    assert out[-1].content == "最后一条问题"   # 最新消息保留
    assert len(out) < len(msgs)               # 确有裁剪

    # 3) 空列表安全
    assert trim_messages([]) == []


def test_conversation_store_persistence():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "mem.sqlite3")
        msgs = [HumanMessage(content="你好"), AIMessage(content="你好，有什么可以帮你？")]

        s1 = ConversationStore(db)
        s1.save("s1", msgs, summary="sum-v1")

        # 模拟进程重启：重新实例化存储
        s2 = ConversationStore(db)
        loaded, summary = s2.load("s1")
        assert len(loaded) == 2
        assert loaded[0].content == "你好"
        assert loaded[1].content == "你好，有什么可以帮你？"
        assert summary == "sum-v1"

        # 不存在的会话返回空
        assert s2.load("nope") == ([], "")


def test_score_memory():
    now = time.time()
    high = score_memory(0.8, 0.9, now, 5, now=now)             # 高重要度/新鲜/复用多
    low = score_memory(0.8, 0.1, now - 100000, 0, now=now)      # 低重要度/陈旧/未复用
    assert high > low
    # 纯函数：相同输入相同输出
    assert score_memory(0.5, 0.5, now, 1, now=now) == score_memory(0.5, 0.5, now, 1, now=now)


def test_update_summary():
    with tempfile.TemporaryDirectory() as d:
        mm = MemoryManager(
            db_path=os.path.join(d, "mem.sqlite3"),
            chroma_path=os.path.join(d, "chroma"),
            llm=FakeLLM(summary="新摘要"),
            embeddings=FakeEmbeddings(),
        )
        mm.conversation_store.save("s1", [HumanMessage(content="hello")], summary="旧摘要")
        new = mm._update_summary("s1", [HumanMessage(content="问题"), AIMessage(content="回答")])
        assert new == "新摘要"
        # 滚动摘要应包含旧摘要作为输入
        assert any("旧摘要" in c for c in mm.llm.calls)


def test_extract_facts_and_recall():
    with tempfile.TemporaryDirectory() as d:
        mm = MemoryManager(
            db_path=os.path.join(d, "mem.sqlite3"),
            chroma_path=os.path.join(d, "chroma"),
            llm=FakeLLM(facts='[{"kind":"metric","content":"销售额按含税口径","importance":0.9}]'),
            embeddings=FakeEmbeddings(),
        )
        facts = mm._extract_facts([HumanMessage(content="销售额按含税算吗")])
        assert len(facts) == 1
        assert facts[0]["content"] == "销售额按含税口径"

        # 写入语义库后能被召回
        mm.semantic_store.add("metric", "销售额按含税口径", 0.9)
        recalled = mm.semantic_store.recall("销售额口径", top_k=5)
        assert "销售额按含税口径" in recalled


def test_persist_and_prefix():
    with tempfile.TemporaryDirectory() as d:
        mm = MemoryManager(
            db_path=os.path.join(d, "mem.sqlite3"),
            chroma_path=os.path.join(d, "chroma"),
            llm=FakeLLM(facts='[{"kind":"metric","content":"销售额按含税口径","importance":0.9}]'),
            embeddings=FakeEmbeddings(),
        )
        msgs = [HumanMessage(content="销售额按含税口径吗？"), AIMessage(content="是的，按含税口径。")]
        mm.persist("s1", msgs)

        # 历史已保存
        loaded, _ = mm.conversation_store.load("s1")
        assert len(loaded) == 2

        # 长期记忆已提取并可召回
        recalled = mm.recall("销售额口径")
        assert "销售额按含税口径" in recalled

        # 记忆前缀能同时拼出摘要与召回
        mm.conversation_store.save("s1", msgs, summary="摘要内容")
        prefix = mm.build_memory_prefix("销售额", "s1")
        assert "摘要内容" in prefix
        assert "销售额按含税口径" in prefix


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"ERROR {fn.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
