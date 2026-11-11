from langchain.tools.retriever import create_retriever_tool
from langchain_community.tools import DuckDuckGoSearchRun
from langchain_postgres import PGVector

from tools.embeddings import get_embeddings
from tools.pg import PGVECTOR_CONNECTION

search = DuckDuckGoSearchRun()

# schema 检索向量库：从 Chroma 本地文件迁移到 PostgreSQL + pgvector。
# 索引不再随代码提交，改为直接写共享 PG 库（重建见 tools/ingest_pgvector.py）。
vector_store = PGVector(
    embeddings=get_embeddings(),
    collection_name="example_collection",
    connection=PGVECTOR_CONNECTION,
    use_jsonb=True,
)

retriever_tool = create_retriever_tool(
    vector_store.as_retriever(),
    name="database_schema_rag",
    description="Search for database schema details",
)
