"""把 knowledge/ 下的 Markdown 语料向量化，全量重建 example_collection（schema 索引）。

与旧版（docs/ 全量 + TextLoader）的区别：
1. 语料从 docs/ 迁移到 knowledge/，只索引带有效 front-matter 的知识文档；
   docs/ 下的项目文档（rag.md / request_flow.md / memory_troubleshooting.md 等）不再进库。
2. 每个文档的 front-matter（title / category / ...）会被解析：
   - 从正文剥离，避免把元数据当正文向量化；
   - 写入 chunk 的 metadata，便于后续按类别过滤、检索后溯源。
3. 全量重建：默认先删除 example_collection 再写入，保证幂等（不产生重复 chunk）。
4. 索引输出到独立的 chroma_schema_db/（与长期记忆 chroma_memory_db/ 隔离），
   方便 CI 只重建/部署 schema 索引，绝不覆盖运行时记忆。
5. embedding 统一走 tools/embeddings.py 的 text-embedding-v3（与查询端一致）。

用法：
    python tools/ingest_chromadb.py                                      # 本地：knowledge/ -> chroma_schema_db/
    python tools/ingest_chromadb.py --docs-dir <文档目录> --out <输出目录>   # CI 用
    python tools/ingest_chromadb.py --keep                               # 不清空，直接追加（不推荐）
"""
import argparse
import os
import sys

import chromadb
from langchain.text_splitter import CharacterTextSplitter
from langchain_community.vectorstores import Chroma

# 项目根目录加入 sys.path，保证 `from tools.xxx import ...` 可用（无论从哪启动脚本）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.doc_loader import load_markdown_docs
from tools.embeddings import get_embeddings

current_file_dir = os.path.dirname(os.path.abspath(__file__))
upper_dir = os.path.dirname(current_file_dir)  # 项目根目录
DEFAULT_DOCS_DIR = os.path.join(upper_dir, "knowledge")
DEFAULT_OUT_DIR = os.path.join(upper_dir, "chroma_schema_db")


class Config:
    chunk_size: int = 1000
    chunk_overlap: int = 0


def clear_collection(out_dir: str) -> None:
    """删除 out_dir 里的 example_collection，保证全量重建幂等（不影响长期记忆）。"""
    client = chromadb.PersistentClient(path=out_dir)
    try:
        client.delete_collection("example_collection")
        print(f"[ingest] 已删除 {out_dir} 里旧的 example_collection")
    except Exception as e:
        print(f"[ingest] 无旧 collection 可删（或删除失败，可忽略）：{e}")


def run(docs_dir: str = DEFAULT_DOCS_DIR, out_dir: str = DEFAULT_OUT_DIR, clear_existing: bool = True):
    if clear_existing:
        clear_collection(out_dir)

    docs = load_markdown_docs(docs_dir)
    print(f"[ingest] 从 {docs_dir} 读取到 {len(docs)} 个文档")

    splitter = CharacterTextSplitter(
        chunk_size=Config.chunk_size, chunk_overlap=Config.chunk_overlap
    )
    chunks = splitter.split_documents(docs)
    print(f"[ingest] 切分为 {len(chunks)} 块")

    vector_store = Chroma(
        collection_name="example_collection",
        embedding_function=get_embeddings(),
        persist_directory=out_dir,
    )

    if not chunks:
        print("[ingest] 警告：没有可索引的文档，collection 为空")
        return vector_store

    vector_store.add_documents(chunks)
    print(f"[ingest] 完成，已写入 {out_dir}，collection 总数 = {vector_store._collection.count()}")
    return vector_store


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="重建 schema 检索向量库")
    parser.add_argument("--docs-dir", default=DEFAULT_DOCS_DIR, help="知识文档目录（默认 knowledge/）")
    parser.add_argument("--out", dest="out_dir", default=DEFAULT_OUT_DIR, help="索引输出目录（默认 chroma_schema_db/）")
    parser.add_argument("--keep", action="store_true", help="不清空 collection，直接追加（不推荐）")
    args = parser.parse_args()
    run(docs_dir=args.docs_dir, out_dir=args.out_dir, clear_existing=not args.keep)
