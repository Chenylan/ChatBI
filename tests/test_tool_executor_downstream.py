"""
测试 tool_executor.AgentToolExecutor 在下游服务 hang / 504 时的表现。

用一个真实工具 text2sqlite_query（它内部会调用下游 LLM）作为被测工具，
把它的 base_url 指向 tests/mock_openai_server.py 模拟的下游，观察分类与重试。

用法：
    1) 先启动模拟下游（二选一）：
       python tests/mock_openai_server.py hang        # 下游挂死，不回包
       python tests/mock_openai_server.py error 504   # 下游立即返回 504
    2) 运行本脚本（模式参数要和 mock 一致）：
       python tests/test_tool_executor_downstream.py hang
       python tests/test_tool_executor_downstream.py 504

关键点：
    - request_timeout=2 / max_retries=0 是**临时注入**的：为了快速触发超时、并关掉
      openai SDK 自带的 2 次重试，好让「tool_executor 自己的重试」成为唯一可观察对象。
      生产代码里没有这几行（默认 600s 超时 + SDK 重试 2 次）。
    - tools.tools_text2sqlite 内部缓存了 llm，需要重置，才能用新的 base_url/短超时重建。
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MODE = sys.argv[1] if len(sys.argv) > 1 else "hang"

# 必须在 import 工具之前设置：text2sqlite 在 get_llm() 时才读 OPENAI_API_BASE_URL
os.environ["OPENAI_API_BASE_URL"] = "http://127.0.0.1:18080/v1"
os.environ.setdefault("OPENAI_API_KEY", "sk-test-mock")

# ---- 注入短超时 + 关闭 SDK 自带重试（生产代码没有这几行）----
from langchain_openai import ChatOpenAI

_orig_init = ChatOpenAI.__init__


def _patched_init(self, *args, **kwargs):
    kwargs.setdefault("request_timeout", 2)
    kwargs.setdefault("max_retries", 0)
    _orig_init(self, *args, **kwargs)


ChatOpenAI.__init__ = _patched_init
# ------------------------------------------------------------

from tools.tool_executor import AgentToolExecutor
from tools.tools_text2sqlite import text2sqlite_tool
import tools.tools_text2sqlite as t2s

# 重置 text2sqlite 内部缓存的 LLM，强制用上面的 base_url + 短超时重新初始化
t2s.llm = None
t2s._current_model_name = None


def main():
    executor = AgentToolExecutor([text2sqlite_tool], max_retries=3)
    # 一个模拟 LLM 发来的工具调用
    tool_call = {
        "name": "text2sqlite_query",
        "id": "call_demo",
        "args": {"text": "查询销售总额", "table_schema": ""},
    }

    t0 = time.time()
    msg = executor._run_one(tool_call)
    print(f"[{MODE}] 总耗时 {time.time() - t0:.1f}s")
    print(f"[{MODE}] 最终 ToolMessage 内容：")
    print(f"       {msg.content[:300]}")


if __name__ == "__main__":
    main()
