# 本项目 RAG（向量检索）实现梳理

> **2026-11-09 更新**：向量库已从 Chroma（本地 `chroma_schema_db/`、`chroma_memory_db/`）迁移到 PostgreSQL + pgvector。schema 索引与长期记忆现在是同一个 PG 实例里的两个 collection（`example_collection`、`memory_collection`）。语料仍只索引 `knowledge/` 下带 front-matter（含 `category`）的知识文档；embedding 用 DashScope `text-embedding-v3`（多语言），统一在 [tools/embeddings.py](../tools/embeddings.py) 里定义、索引端与查询端共用。索引链路为 `tools/doc_loader.py`（front-matter 解析）+ `tools/ingest_pgvector.py`（全量重建，`pre_delete_collection=True`）。

本文梳理 ChatBI 里两套 RAG 的完整链路：**schema 检索 RAG**（模型查表结构用）和**长期记忆 RAG**（多轮记忆召回用）。两者已隔离到 pgvector 的**不同 collection**（`example_collection` 与 `memory_collection`），用途也各自不同。

> 相关代码入口：
> - schema 检索工具：[tools/tools_rag.py](../tools/tools_rag.py)
> - 索引脚本（全量）：[tools/ingest_pgvector.py](../tools/ingest_pgvector.py)
> - PG 连接配置：[tools/pg.py](../tools/pg.py)
> - 语料：[knowledge/*.md](../knowledge/)（6 张表结构 + `database_overview / query_examples / ANALYTICS_FEATURES` 等参考文档）
> - 长期记忆 RAG：[memory_manager.py](../memory_manager.py) 的 `SemanticMemoryStore`
> - 评测：[eval/eval_retrieval.py](../eval/eval_retrieval.py)

---

## 1. 两套 RAG 各自解决什么问题

| | schema 检索 RAG | 长期记忆 RAG |
|---|---|---|
| 工具名 | `database_schema_rag` | （无工具，`MemoryManager.recall()` 内部调用） |
| collection | `example_collection` | `memory_collection` |
| 语料来源 | `knowledge/*.md` 里的表结构文档 | LLM 每轮从对话中抽取的事实 |
| 检索内容 | 表名 / 字段定义 / 表关系 | 用户偏好、指标口径、实体别名、关键结论 |
| 用途 | 喂给模型生成 SQL 用 | 作为「记忆前缀」注入 prompt |
| 代码位置 | [tools/tools_rag.py](../tools/tools_rag.py) | [memory_manager.py](../memory_manager.py) |

本文重点讲 schema 检索 RAG（模型查数的第一跳），长期记忆 RAG 在 [memory_manager.py](../memory_manager.py) 里有完整注释，此处只做对照。

---

## 2. schema 检索 RAG 的整体链路

```
                    ┌─────────────────── 离线（写索引） ───────────────────┐
                    │                                                     │
  knowledge/*.md ──> doc_loader ──> CharacterTextSplitter ──> PGVector.add_documents
  （带 front-matter） (剥 front-matter) (chunk_size=1000, overlap=0)    example_collection
                    │                                                     │
                    └──────────────────────┬──────────────────────────────┘
                                           │ 持久化到 PostgreSQL（pgvector）
                    ┌──────────────────────▼──────────────────────────────┐
                    │                在线（查询）                          │
  用户问题 ──> 模型决定调 database_schema_rag(query)                        │
                    └──> query 转向量 ──> similarity search ──> 返回 top-4 schema 文本
                                                              └──> 模型据此生成 SQL
```

一句话：**把表结构文档切块、向量化存进 PGVector；模型要查数时，先拿用户问题做语义检索，捞回最相关的表结构，再生成 SQL。**

---

## 3. 离线索引（写）是怎么做的

全量索引脚本是 [tools/ingest_pgvector.py](../tools/ingest_pgvector.py)，核心流程：

```python
# 1) 读文档：knowledge/ 下所有带 front-matter 的 .md，剥 front-matter 后切块
docs = ...   # DocumentProcessor 解析 + CharacterTextSplitter(chunk_size=1000, overlap=0)

# 2) 向量化写入（pre_delete_collection=True 会先清空旧索引再全量重建）
vector_store = PGVector(
    embeddings=get_embeddings(),          # DashScope text-embedding-v3
    collection_name="example_collection",
    connection=PGVECTOR_CONNECTION,       # postgresql+psycopg://...（见 tools/pg.py）
    use_jsonb=True,
)
vector_store.add_documents(docs)
```

三个关键点：

1. **（已修复）语料曾为 `docs/*.md` 全量**，`glob="**/*.md"` 把项目文档也索引进去了。现已改为只索引 `knowledge/` 下带 front-matter 的知识文档，项目文档不再进库。

2. **（已更新）embedding 现为 DashScope `text-embedding-v3`**（多语言，中英通吃），统一在 [tools/embeddings.py](../tools/embeddings.py) 的 `get_embeddings()` 里定义，索引端（`ingest_pgvector.py`）与查询端（`tools_rag.py`）共用同一份，保证向量可对齐。

3. **切块很粗**：`chunk_size=1000`、`overlap=0`。schema 文档都很短（一个表通常就几行字段），基本一个文档就是一块。

---

## 4. 在线检索（读）是怎么做的

检索工具定义在 [tools/tools_rag.py](../tools/tools_rag.py)：

```python
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
```

模型绑定这个工具后，当它需要知道表结构时，就调用 `database_schema_rag(用户问题)`：

1. `vector_store.as_retriever()` 默认取 **top-4**（`k=4`）——这是 LangChain `as_retriever()` 的默认值，本代码没显式传 `search_kwargs`。
2. 把 query 用同一个 `text-embedding-v3` 转成向量，在 `example_collection` 里做余弦相似度检索。
3. 返回 top-4 个最相似的 chunk 文本，作为 `ToolMessage` 回给模型。

模型拿到这些 schema 文本后，要么自己直接写 SQL，要么把它们作为 `table_schema` 参数传给 `text2sql_query` 生成 SQL（见 [docs/request_flow.md](./request_flow.md) 第 6 节）。

---

## 5. 几个容易踩的坑

1. **（已修复）中文检索**：旧版用英文 `all-MiniLM-L6-v2`，中文 query 几乎捞不对表，评测 10 条中文问题得到 `Recall@5 = Hit@5 = MRR = 0.000`。换成 `text-embedding-v3` 后实测 `Recall@5 = 0.800`、`Hit@5 = 0.900`、`MRR = 0.750`。唯一没命中金表的是「从浏览到购买的转化漏斗」——相关语料在 `ANALYTICS_FEATURES.md`（漏斗查询示例），而 ground truth 只把它映射到 `USER_INTERACTIONS`，属评测标注局限，非检索问题。

2. **三段式表名前缀**：schema 文档里的表名带 `STREAM_HACKATHON.STREAMLIT.` 前缀（如 `STREAM_HACKATHON.STREAMLIT.ORDER_DETAILS`）。PostgreSQL 的 public schema 里表名就是裸名（如 `order_details`），所以 `text2sql_query` 的 prompt 里专门要求"只保留表名、去掉前缀"（[tools/tools_text2sql.py](../tools/tools_text2sql.py)）。如果模型自己直接写 SQL，也可能照抄带前缀的表名导致 SQL 报错。

3. **（已修复）语料含干扰项**：历史版本把非 schema 的项目文档也索引进去了；现已通过 `knowledge/` 目录 + front-matter 过滤解决，项目文档不再进库。

---

## 6. 评测是怎么做的

- **基准**：[eval/eval_retrieval.py](../eval/eval_retrieval.py)，测 `database_schema_rag` 这一层（"第一层"），指标 `Recall@K / Hit@K / MRR`。
- **测试集**：10 条中文问题，每条标注需要哪些「金表」（如「热销产品排行」→ `TRANSACTIONS + PRODUCTS`）。
- **ground truth 的关键假设**：只有「表专属 schema 文档」才算命中该表；概览 / 查询示例 / 分析功能等文档不计入命中（即使提到表名）。

---

## 7. 两套 RAG 的对照

| 维度 | schema 检索 | 长期记忆 |
|---|---|---|
| 文件 | [tools/tools_rag.py](../tools/tools_rag.py) | [memory_manager.py](../memory_manager.py) |
| collection | `example_collection` | `memory_collection` |
| 写入方式 | 手动跑索引脚本 | 每轮 `persist()` 时 LLM 提取后 `add()` |
| 查重/复用 | 无 | `(kind+content)` 哈希去重，`reuse_count +1` |
| 召回排序 | 纯相似度 | 相似度 + 重要度 + 新鲜度（半衰期）+ 复用次数加权 |
| top-K | 4（`as_retriever` 默认） | 5（`MemoryManager.top_k`） |

两者已隔离到 pgvector 的**不同 collection**（`example_collection` / `memory_collection`）。这样重建 schema 索引（`ingest_pgvector.py` 里 `pre_delete_collection=True` 只会清空 `example_collection`）时，绝不会碰到运行时长期记忆。
