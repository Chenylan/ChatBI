"""
第一层（schema 检索）评测：把 embedding 从 DefaultEmbeddingFunction(MiniLM，英文) 换成
DashScope 的 text-embedding-v3（多语言），其余条件全部不变，量化「换 embedding」这一单一变量的收益。

对比基准：eval/eval_retrieval.py（生产同款 MiniLM embedding）在同样 10 条中文问题上 Recall@5=Hit@5=MRR=0.000。

做法（保证唯一变量是 embedding 模型）：
    1. 直接从生产已持久化的 collection（example_collection）里读出**同一批 24 个 chunk**
       （同样的切分、同样的 metadata['source']，含 6 个 memory_troubleshooting.md 干扰项），
       不重新切文档、不增删内容——排除「换语料」带来的干扰。
    2. 用 text-embedding-v3 把这些 chunk 重新嵌入，写到一个**新的独立 persist 目录**，
       绝不覆盖生产 collection。
    3. 复用 eval_retrieval 里同一套测试集与打分函数 evaluate(store=...)，输出同口径指标。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()  # 读取项目根目录 .env 里的 OPENAI_API_KEY / OPENAI_API_BASE_URL

import chromadb
from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings

# 复用同一套测试集、表映射、打分函数（evaluate 已参数化为可传任意 store）
from eval.eval_retrieval import TEST_SET, K, evaluate, chunk_tables

# 生产 collection 的持久化目录（与 tools.tools_rag.CHROMADB_PATH 一致）
PROD_DB_PATH = str(Path(__file__).resolve().parent.parent / "chroma_schema_db")
# v3 独立目录：绝不覆盖生产 collection
V3_DB_PATH = str(Path(__file__).resolve().parent.parent / "chroma_langchain_db_v3")
V3_COLLECTION = "example_collection_v3"


def build_v3_store() -> Chroma:
    """读出生产 collection 的同一批 chunk，用 text-embedding-v3 重新嵌入到新 collection。"""
    # 1) 读生产 collection 的原始 chunk（文本 + metadata['source']），保持切分/来源完全一致
    client = chromadb.PersistentClient(path=PROD_DB_PATH)
    prod = client.get_collection("example_collection")
    data = prod.get(include=["documents", "metadatas"])
    docs = [
        Document(page_content=text, metadata=meta or {})
        for text, meta in zip(data["documents"], data["metadatas"])
    ]
    print(f"从生产 collection 读出 {len(docs)} 个 chunk，来源分布：")
    from collections import Counter
    for src, n in Counter(Path(d.metadata.get('source', '')).name for d in docs).most_common():
        print(f"   {src:<28} {n} 个")

    # 2) text-embedding-v3（多语言，中英通吃），走与生产一致的 DashScope compatible-mode 端点
    emb = OpenAIEmbeddings(
        model="text-embedding-v3",
        base_url=os.getenv("OPENAI_API_BASE_URL"),
        api_key=os.getenv("OPENAI_API_KEY"),
        # text-embedding-v3 默认 1024 维；不传 dimensions 即用默认
        # 关键：关掉长度安全检查。否则 langchain 会用 tiktoken 把文本编码成「token id 整数列表」
        # 再当 input 发给接口，DashScope 只接受 str / list[str]，会报 400 InvalidParameter。
        # 我们的 chunk 都很短，远小于 8191 token 上下文，无需长度检查。
        check_embedding_ctx_length=False,
        # DashScope text-embedding-v3 单次请求最多 10 条文本，超过会报 400「batch size ... larger than 10」。
        # 这里把每批上限设为 10，langchain 会按批拆开请求。
        chunk_size=10,
    )

    # 3) 写入独立目录的新 collection
    store = Chroma(
        collection_name=V3_COLLECTION,
        embedding_function=emb,
        persist_directory=V3_DB_PATH,
    )
    store.add_documents(docs)
    print(f"已用 text-embedding-v3 重建索引 -> {V3_COLLECTION}（{len(docs)} chunks）\n")
    return store


if __name__ == "__main__":
    store = build_v3_store()

    rows, metrics = evaluate(k=K, store=store)
    print("=" * 78)
    print(f"第一层（schema 检索）评测  |  embedding=text-embedding-v3  |  K={K}  |  测试集 {len(TEST_SET)} 条")
    print("=" * 78)
    print(f"{'查询':<26}{'金表':<28}{'命中':<18}{'命中排名':<10}{'R@5':<6}{'Hit':<5}{'RR':<6}")
    for r in rows:
        print(f"{r['query']:<26}{','.join(r['gold']):<28}"
              f"{','.join(r['retrieved_tables']) or '-':<18}"
              f"{str(r['ranks']) if r['ranks'] else '-':<10}"
              f"{r['recall']:<6.2f}{r['hit']:<5.0f}{r['rr']:<6.2f}")

    print("-" * 78)
    for name, val in metrics.items():
        print(f"{name:12s} = {val:.3f}")

    # 对照：同样 k=4 下的 v3 指标（对齐生产 as_retriever 默认 k=4）
    _, m4 = evaluate(k=4, store=store)
    print("\n对照：text-embedding-v3 在 k=4 时的指标：")
    for name, val in m4.items():
        print(f"  {name:10s} = {val:.3f}")
