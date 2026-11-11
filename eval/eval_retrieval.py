"""
第一层（schema 检索）离线评测：Hit@5 / MRR / Recall@5。

测的是什么：
    给定一个自然语言问题，agent 先用 database_schema_rag（Chroma 向量检索）拿「表结构」，
    这一步如果没把正确的表捞出来，后面的 SQL 生成再强也是用错表。本脚本离线评估这个检索环节。

指标定义（对每个查询 q，标注它需要哪些表 G_q，取 top-K 个检索 chunk）：
    - Recall@K：命中的金表数 / 金表总数。多表场景的「完整性」主指标。
    - Hit@K   ：是否至少命中 1 张金表（0/1）。二值，直观。
    - MRR     ：第一个命中金表的 chunk 的排名的倒数（没命中记 0）。衡量「排序质量」。

ground truth 的关键假设（务必理解）：
    只有「该表专属的 schema 文档」才算命中该表。概览 / 查询示例 / 分析功能 / memory_troubleshooting
    这些文档虽然可能提到表名，但不保证提供完整列定义，因此**不计入命中**。
    这是最严格的判法；真实场景里「示例文档」部分可用，所以真实可用性 ≥ 本脚本数字。

与生产一致：
    - 复用 tools.tools_rag 里同一个 vector_store（同一 collection + 同一 text-embedding-v3）
    - 生产 retriever_tool 用 as_retriever() 默认 k=4；本脚本用 k=5（对齐 Hit@5 命名），
      两者在小语料上差异很小，脚本末尾也附 k=4 的结果。
"""
import sys
from pathlib import Path

# 项目根目录加入 path，保证能 import tools
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.tools_rag import vector_store  # 复用生产同款向量库（同 embedding）

K = 5  # 取 top-K 做命中判断

# ==================== 1. 测试集（问题 → 需要哪些表） ====================
# 金表依据「问题的业务语义」标注。例如「热销产品排行」需要 TRANSACTIONS（销量/金额）+ PRODUCTS（品名）。
TEST_SET = [
    {"query": "各产品类别的销售情况",           "gold": {"PRODUCTS", "TRANSACTIONS"}},
    {"query": "客户的忠诚度等级和购买金额",       "gold": {"CUSTOMER_DETAILS"}},
    {"query": "最近的订单和订单金额",            "gold": {"ORDER_DETAILS"}},
    {"query": "支付金额和支付方式",              "gold": {"PAYMENTS"}},
    {"query": "产品名称、分类和价格",            "gold": {"PRODUCTS"}},
    {"query": "每笔交易的明细",                  "gold": {"TRANSACTIONS"}},
    {"query": "用户点击和搜索行为",              "gold": {"USER_INTERACTIONS"}},
    {"query": "从浏览到购买的转化漏斗",           "gold": {"USER_INTERACTIONS"}},
    {"query": "热销产品排行（销量最高）",         "gold": {"TRANSACTIONS", "PRODUCTS"}},
    {"query": "客户的注册日期和首次购买日期",      "gold": {"CUSTOMER_DETAILS"}},
]

# ==================== 2. 文档 → 表 的映射 ====================
# chunk 的 metadata['source'] 是文件路径；只把「表专属 schema 文档」映射到对应表，
# 其余文档（概览/示例/分析功能/memory_troubleshooting）映射为空集 = 不算命中。
DOC_TO_TABLE = {
    "order_details": "ORDER_DETAILS",
    "payments": "PAYMENTS",
    "customer_details": "CUSTOMER_DETAILS",
    "products": "PRODUCTS",
    "transactions": "TRANSACTIONS",
    "user_interactions": "USER_INTERACTIONS",
}


def chunk_tables(source: str) -> set:
    """把一个检索 chunk 映射到它覆盖的表集合（空集 = 非 schema 文档，不算命中）。"""
    stem = Path(source).stem  # 如 /.../docs/transactions.md -> transactions
    return {DOC_TO_TABLE[stem]} if stem in DOC_TO_TABLE else set()


# ==================== 3. 检索 + 打分 ====================
def evaluate(k: int = K, store=vector_store):
    """对给定向量库打分。默认用生产同款 vector_store，也可传入换过 embedding 的 store 做对比。"""
    recalls, hits, rrs = [], [], []
    rows = []

    for item in TEST_SET:
        query, gold = item["query"], item["gold"]
        # 相似度检索，返回 (Document, 距离)，已按相似度降序
        results = store.similarity_search_with_score(query, k=k)

        found = set()          # 命中的金表
        first_rank = None      # 第一个命中金表的 chunk 排名（1 起）
        for rank, (doc, _score) in enumerate(results, start=1):
            tables = chunk_tables(doc.metadata.get("source", ""))
            hit_here = tables & gold
            if hit_here:
                found |= hit_here
                if first_rank is None:
                    first_rank = rank

        recall = len(found) / len(gold) if gold else 0.0
        hit = 1.0 if found else 0.0
        rr = 1.0 / first_rank if first_rank else 0.0

        recalls.append(recall)
        hits.append(hit)
        rrs.append(rr)

        rows.append({
            "query": query,
            "gold": sorted(gold),
            "retrieved_tables": sorted(found),
            "ranks": [rank for rank, (doc, _) in enumerate(results, start=1)
                      if chunk_tables(doc.metadata.get("source", "")) & gold],
            "recall": recall,
            "hit": hit,
            "rr": rr,
        })

    return rows, {
        "Recall@%d" % k: sum(recalls) / len(recalls),
        "Hit@%d" % k: sum(hits) / len(hits),
        "MRR": sum(rrs) / len(rrs),
    }


if __name__ == "__main__":
    rows, metrics = evaluate(k=K)

    print("=" * 78)
    print(f"第一层（schema 检索）评测  |  K={K}  |  测试集 {len(TEST_SET)} 条")
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

    # 附：生产 retriever_tool 默认 k=4 的结果，便于对照
    _, m4 = evaluate(k=4)
    print("\n对照：生产 as_retriever() 默认 k=4 时的指标：")
    for name, val in m4.items():
        print(f"  {name:10s} = {val:.3f}")
