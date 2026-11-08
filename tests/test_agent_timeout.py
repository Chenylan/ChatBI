"""
测试 agent 在下游 LLM 超时时的表现。

依赖 tests/mock_openai_server.py 先启动一个模拟下游。

用法：
    1) 先启动模拟下游（例如挂起模式）：
       python tests/mock_openai_server.py hang
    2) 另开终端运行本脚本：
       python tests/test_agent_timeout.py

说明：
    - 生产代码里 ChatOpenAI 没有设置任何超时（request_timeout=None → openai SDK 默认 600s）。
      为了快速看到结果，这里临时把 request_timeout 注入成 3 秒、max_retries=0，
      抛出的异常类型和真实超时完全一致，只是更快。
    - 若不注入短超时，可以注释掉 patch 那几行，体验一次默认 10 分钟超时（不推荐）。
"""
import os
import sys
import time
from pathlib import Path
from agentlens.adapters.langgraph import AgentLensCallbackHandler

# 把项目根目录加入 sys.path，保证直接 `python tests/test_agent_timeout.py` 也能 import agent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 必须在 import agent 之前设置，否则 tools_charts 等模块在 import 时就读取了 base_url
os.environ["OPENAI_API_BASE_URL"] = "http://127.0.0.1:18080/v1"
os.environ.setdefault("OPENAI_API_KEY", "sk-test-mock")

# ---- 注入短超时，方便快速观察（生产代码没有这几行）----
from langchain_openai import ChatOpenAI

_orig_init = ChatOpenAI.__init__


def _patched_init(self, *args, **kwargs):
    kwargs.setdefault("request_timeout", 10)
    kwargs.setdefault("max_retries", 0)
    _orig_init(self, *args, **kwargs)


ChatOpenAI.__init__ = _patched_init
# --------------------------------------------------------

from agent import create_agent, MessagesState
from langchain_core.messages import HumanMessage
from backend.api.callback import StreamingCallbackHandler


def main():
    handler = StreamingCallbackHandler(token_callback=lambda t: print(f"[token] {t[:40]!r}"))
    graph = create_agent(handler, "qwen-plus")

    state = MessagesState(messages=[HumanMessage(content="查询一下销售总额")])
    config = {
        "configurable": {"thread_id": "timeout-test"}, 
        "callbacks": [AgentLensCallbackHandler(project="chatbi")],
              "recursion_limit": 100}

    t0 = time.time()
    try:
        result = graph.invoke(state, config=config)
        print(f"[result] 正常完成，耗时 {time.time() - t0:.1f}s")
        print(f"[result] final_message = {handler.final_message!r}")
    except Exception as e:
        print(f"[result] {time.time() - t0:.1f}s 后抛出异常")
        print(f"[result] 异常类型: {type(e).__module__}.{type(e).__name__}")
        print(f"[result] 异常信息: {e}")


if __name__ == "__main__":
    main()
