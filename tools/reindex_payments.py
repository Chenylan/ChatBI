"""把 knowledge/payments.md 重新向量化并「追加」进 example_collection（不删除已有文档）。

用于修改 payments.md 后快速更新单文档。非破坏：只新增文档块，不清空 collection。
注意：追加会产生旧版本的重复 chunk；正式重建请用 `python tools/ingest_chromadb.py`。
"""
import os
import sys

from langchain.text_splitter import CharacterTextSplitter
from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document

# 项目根目录加入 sys.path，保证 `from tools.xxx import ...` 可用
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.doc_loader import parse_frontmatter
from tools.embeddings import get_embeddings

current_file_dir = os.path.dirname(os.path.abspath(__file__))
upper_dir = os.path.dirname(current_file_dir)
CHROMADB_PATH = os.path.join(upper_dir, "chroma_schema_db")


def main():
    path = os.path.join(upper_dir, "knowledge", "payments.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    meta, body = parse_frontmatter(text)  # 剥离 front-matter，避免元数据混入正文
    doc = Document(page_content=body, metadata={"source": path, **meta})

    splitter = CharacterTextSplitter(chunk_size=1000, chunk_overlap=0)
    chunks = splitter.split_documents([doc])
    print(f"payments.md 切分为 {len(chunks)} 块")

    vector_store = Chroma(
        collection_name="example_collection",
        embedding_function=get_embeddings(),
        persist_directory=CHROMADB_PATH,
    )
    vector_store.add_documents(chunks)
    print(f"已追加 {len(chunks)} 块，当前 collection 总数 = {vector_store._collection.count()}")


if __name__ == "__main__":
    main()
