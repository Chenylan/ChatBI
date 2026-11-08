import os

from langchain.tools.retriever import create_retriever_tool
from langchain_community.tools import DuckDuckGoSearchRun
from langchain_community.vectorstores import Chroma

from tools.embeddings import get_embeddings

search = DuckDuckGoSearchRun()

current_file_dir = os.path.dirname(os.path.abspath(__file__))
# 统一使用项目根目录的向量数据库路径
upper_dir = os.path.dirname(current_file_dir)  # 项目根目录
CHROMADB_PATH = os.path.join(upper_dir, "chroma_schema_db")

vector_store = Chroma(
    collection_name="example_collection",
    embedding_function=get_embeddings(),
    persist_directory=CHROMADB_PATH,
)

retriever_tool = create_retriever_tool(
    vector_store.as_retriever(),
    name="database_schema_rag",
    description="Search for database schema details",
)
