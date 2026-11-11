"""把 knowledge/ 下的 Markdown 语料向量化，全量重建 schema 向量集合（存 PostgreSQL + pgvector）。

取代 tools/ingest_chromadb.py：向量库从 Chroma 本地文件迁移到 Postgres（pgvector 扩展），
索引不再需要「本地构建后提交二进制目录」，直接写入共享的 PG 库即可。

用法：
    python tools/ingest_pgvector.py                    # knowledge/ -> PG schema 集合
    python tools/ingest_pgvector.py --docs-dir <目录>  # 自定义语料目录
    python tools/ingest_pgvector.py --keep             # 不清空集合，直接追加（不推荐）
"""
import argparse
import os
import sys

from langchain.text_splitter import CharacterTextSplitter
from langchain_postgres import PGVector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.doc_loader import load_markdown_docs
from tools.embeddings import get_embeddings
from tools.pg import PGVECTOR_CONNECTION

current_file_dir = os.path.dirname(os.path.abspath(__file__))
upper_dir = os.path.dirname(current_file_dir)
DEFAULT_DOCS_DIR = os.path.join(upper_dir, "knowledge")

COLLECTION_NAME = "example_collection"


class Config:
    chunk_size = 1000
    chunk_overlap = 0


def run(docs_dir: str = DEFAULT_DOCS_DIR, clear_existing: bool = True):
    docs = load_markdown_docs(docs_dir)
    print(f"[ingest] 从 {docs_dir} 读取到 {len(docs)} 个文档")

    splitter = CharacterTextSplitter(chunk_size=Config.chunk_size, chunk_overlap=Config.chunk_overlap)
    chunks = splitter.split_documents(docs)
    print(f"[ingest] 切分为 {len(chunks)} 块")

    # pre_delete_collection=True 时初始化会先删同名旧集合，保证全量重建幂等
    store = PGVector(
        embeddings=get_embeddings(),
        collection_name=COLLECTION_NAME,
        connection=PGVECTOR_CONNECTION,
        use_jsonb=True,
        pre_delete_collection=clear_existing,
    )

    if not chunks:
        print("[ingest] 警告：没有可索引的文档，集合为空")
        return store

    store.add_documents(chunks)
    print(f"[ingest] 完成，已写入 {len(chunks)} 块到 PG 集合 {COLLECTION_NAME}")
    return store


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="重建 schema 检索向量集合（PG + pgvector）")
    parser.add_argument("--docs-dir", default=DEFAULT_DOCS_DIR, help="知识文档目录（默认 knowledge/）")
    parser.add_argument("--keep", action="store_true", help="不清空集合，直接追加（不推荐）")
    args = parser.parse_args()
    run(docs_dir=args.docs_dir, clear_existing=not args.keep)
