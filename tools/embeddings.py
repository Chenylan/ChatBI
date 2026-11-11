"""统一的 embedding 工厂：DashScope text-embedding-v3（多语言，中英通吃）。

为什么集中在这里：
- 索引端（ingest / reindex）和查询端（tools_rag / memory_manager）必须用同一个 embedding，
  否则向量维度/空间不一致，检索全废。这里保证一处定义、多处复用。
- 以后换成别的 embedding（例如 text-embedding-v4）只改这一个文件。

DashScope 特有参数（务必保留，否则报 400）：
- check_embedding_ctx_length=False：langchain 默认会用 tiktoken 把文本编码成 token id 整数列表
  再当 input 发给接口，DashScope 只接受 str / list[str]，会报 InvalidParameter。
  我们的 chunk 都很短，远小于 8191 token，无需长度检查。
- chunk_size=10：text-embedding-v3 单次请求最多 10 条文本，超过报「batch size larger than 10」。
  langchain 会按批拆开请求。
"""
import os

from dotenv import load_dotenv
from langchain_openai import OpenAIEmbeddings

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 读取项目根目录 .env 里的 OPENAI_API_KEY / OPENAI_API_BASE_URL（幂等，已存在的环境变量不覆盖）
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=False)

EMBEDDING_MODEL = "text-embedding-v3"
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def get_embeddings() -> OpenAIEmbeddings:
    """返回生产与索引共用的 text-embedding-v3 embedding 实例。"""
    return OpenAIEmbeddings(
        model=EMBEDDING_MODEL,
        base_url=os.getenv("OPENAI_API_BASE_URL", DASHSCOPE_BASE_URL),
        api_key=os.getenv("OPENAI_API_KEY"),
        check_embedding_ctx_length=False,
        chunk_size=10,
    )
