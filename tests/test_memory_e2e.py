"""
记忆管理端到端验证脚本（需要真实 OPENAI_API_KEY / OPENAI_API_BASE_URL）。

验证"更长久 + 更准确"的两个关键性质：
    1. 持久性：一轮对话写入后，重新实例化 MemoryManager（模拟进程重启），历史仍在。
    2. 长期召回：LLM 提取的口径类长期记忆，换一个 query 能被召回。

用法（未设置 key 时会跳过）：
    OPENAI_API_KEY=xxx \
    OPENAI_API_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1 \
    python tests/test_memory_e2e.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import HumanMessage, AIMessage
from memory_manager import MemoryManager


def main():
    if not os.getenv("OPENAI_API_KEY"):
        print("跳过：未设置 OPENAI_API_KEY。")
        print("设置后运行：OPENAI_API_KEY=xxx OPENAI_API_BASE_URL=... python tests/test_memory_e2e.py")
        return

    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "mem.sqlite3")
        chroma = os.path.join(d, "chroma")

        # 第一轮：用户确认一个指标口径（应被提取为长期记忆）
        mm = MemoryManager(db_path=db, chroma_path=chroma)
        msgs = [
            HumanMessage(content="我们公司销售额统一按含税口径统计，请记住这一点。"),
            AIMessage(content="好的，已记住：销售额按含税口径统计。"),
        ]
        mm.persist("s1", msgs)

        # 模拟进程重启：重新实例化 MemoryManager（同一 db/chroma 文件）
        mm2 = MemoryManager(db_path=db, chroma_path=chroma)
        loaded, summary = mm2.conversation_store.load("s1")
        print("[持久性] 重启后历史消息数:", len(loaded), "| 摘要:", summary or "(空)")

        # 换一个 query 召回长期记忆
        recalled = mm2.recall("销售额的口径是什么")
        print("[召回] 命中的长期记忆:", recalled)

        prefix = mm2.build_memory_prefix("销售额口径", "s1")
        print("[记忆前缀]\n", prefix or "(空)")


if __name__ == "__main__":
    main()
