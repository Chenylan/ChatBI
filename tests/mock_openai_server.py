"""
模拟一个会「挂起 / 慢响应 / 报错」的 OpenAI 兼容下游服务，用于测试 agent 在下游超时时的表现。

不需要任何第三方依赖，只用 Python 标准库。

用法（三种模式）：
    python tests/mock_openai_server.py hang       # 收到请求后一直不响应（模拟下游挂死）
    python tests/mock_openai_server.py slow 5     # 5 秒后才返回一个正常补全
    python tests/mock_openai_server.py error 504  # 立即返回 504（模拟网关超时）
    python tests/mock_openai_server.py error 500

监听地址：http://127.0.0.1:18080/v1
"""
import sys
import time
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 18080
MODE = sys.argv[1] if len(sys.argv) > 1 else "hang"
ARG = sys.argv[2] if len(sys.argv) > 2 else None


def completion_payload() -> dict:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "qwen-plus",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "mock 响应：下游服务正常返回。"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        print(f"[mock] {self.command} {self.path}", flush=True)

    def _reply(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _reply_sse(self, payload: dict) -> None:
        """以 SSE 流返回一个补全（stream=True 时使用）。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        chunk = json.dumps(
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "qwen-plus",
                "choices": [
                    {"index": 0, "delta": {"content": "mock 流式响应"}, "finish_reason": None}
                ],
            }
        )
        self.wfile.write(f"data: {chunk}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        print(f"[mock] body preview: {body[:160]!r}", flush=True)

        is_stream = b'"stream": true' in body or b'"stream":true' in body

        if MODE == "hang":
            print("[mock] HANGING：不返回任何内容", flush=True)
            time.sleep(3600)

        elif MODE == "slow":
            secs = float(ARG or 5)
            print(f"[mock] SLOW：{secs}s 后才响应", flush=True)
            time.sleep(secs)
            if is_stream:
                self._reply_sse(completion_payload())
            else:
                self._reply(200, completion_payload())

        elif MODE == "error":
            code = int(ARG or 504)
            print(f"[mock] ERROR：返回 HTTP {code}", flush=True)
            self._reply(code, {"error": {"message": f"simulated downstream {code}", "type": "server_error"}})

        else:
            self._reply(500, {"error": {"message": f"unknown mode: {MODE}"}})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[mock] listening on http://127.0.0.1:{PORT}/v1  mode={MODE} arg={ARG}", flush=True)
    server.serve_forever()
