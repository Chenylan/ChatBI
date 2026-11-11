"""
给 PAYMENTS 表添加「含义相近」的字段并填充数据，用于验证模型对模糊口径的识别能力。

新增字段（都是"钱"，但口径不同，容易产生歧义）：
  - 销售收入 : 订单销售金额（等于原 AMOUNT，便于对照）
  - 实收金额 : 实际到账金额（扣除退款/折扣后，<= 销售收入）
  - 销售成本 : 销售环节发生的成本（含运费、销售费用）
  - 采购成本 : 商品采购/进货成本（<= 销售成本）
  - 毛利额   : 销售收入 - 采购成本（毛利润，两种"毛利率"口径的分子）
  - 净利润   : 实收金额 - 销售成本

歧义点（用户自然语言问"收入/成本/利润/毛利率"时，会命中多个候选）：
  - "收入"   → 销售收入 vs 实收金额
  - "成本"   → 销售成本 vs 采购成本
  - "利润"   → 毛利额 vs 净利润
  - "毛利率" → 毛利额/销售收入(销售毛利率) vs 毛利额/采购成本(成本毛利率)

幂等：已存在的列会跳过；随机种子固定，重复运行结果一致。
"""
import os
import random
import sqlite3

current_file_dir = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.path.join(current_file_dir, "example.db")

# 新列定义：(列名, 类型)
NEW_COLUMNS = [
    ("销售收入", "REAL"),
    ("实收金额", "REAL"),
    ("销售成本", "REAL"),
    ("采购成本", "REAL"),
    ("毛利额", "REAL"),
    ("净利润", "REAL"),
]


def existing_columns(cursor) -> set[str]:
    cursor.execute("PRAGMA table_info(PAYMENTS)")
    return {row[1] for row in cursor.fetchall()}


def add_columns(conn) -> None:
    cursor = conn.cursor()
    existing = existing_columns(cursor)
    for name, col_type in NEW_COLUMNS:
        if name in existing:
            print(f"  跳过已存在的列: {name}")
            continue
        cursor.execute(f"ALTER TABLE PAYMENTS ADD COLUMN {name} {col_type}")
        print(f"  已添加列: {name} {col_type}")
    conn.commit()


def fill_data(conn) -> None:
    """基于已有 AMOUNT 推导出 6 个新字段，保证口径关系一致。"""
    random.seed(42)  # 固定种子，结果可复现
    cursor = conn.cursor()
    cursor.execute("SELECT PAYMENT_ID, AMOUNT FROM PAYMENTS ORDER BY PAYMENT_ID")
    rows = cursor.fetchall()

    for payment_id, amount in rows:
        销售收入 = round(amount, 2)
        实收金额 = round(amount * random.uniform(0.85, 1.00), 2)
        销售成本 = round(amount * random.uniform(0.55, 0.75), 2)
        采购成本 = round(销售成本 * random.uniform(0.70, 0.90), 2)  # 保证 <= 销售成本
        毛利额 = round(销售收入 - 采购成本, 2)
        净利润 = round(实收金额 - 销售成本, 2)

        cursor.execute(
            "UPDATE PAYMENTS SET 销售收入=?, 实收金额=?, 销售成本=?, 采购成本=?, 毛利额=?, 净利润=? WHERE PAYMENT_ID=?",
            (销售收入, 实收金额, 销售成本, 采购成本, 毛利额, 净利润, payment_id),
        )
    conn.commit()
    print(f"  已填充 {len(rows)} 行数据")


def print_summary(conn) -> None:
    cursor = conn.cursor()
    print("\n=== 抽样 3 行 ===")
    cursor.execute(
        "SELECT PAYMENT_ID, AMOUNT, 销售收入, 实收金额, 销售成本, 采购成本, 毛利额, 净利润 "
        "FROM PAYMENTS ORDER BY PAYMENT_ID LIMIT 3"
    )
    for r in cursor.fetchall():
        print("  ", r)

    print("\n=== 全表口径校验（验证模型是否算对/问对） ===")
    cursor.execute(
        "SELECT SUM(销售收入), SUM(实收金额), SUM(销售成本), SUM(采购成本), SUM(毛利额), SUM(净利润) FROM PAYMENTS"
    )
    sum_rev, sum_recv, sum_sellcost, sum_buycost, sum_gross, sum_net = cursor.fetchone()
    # 销售毛利率 = 毛利额 / 销售收入；成本毛利率 = 毛利额 / 采购成本
    销售毛利率 = sum_gross / sum_rev if sum_rev else 0
    成本毛利率 = sum_gross / sum_buycost if sum_buycost else 0
    print(f"  销售收入合计 = {sum_rev:.2f}, 实收金额合计 = {sum_recv:.2f}")
    print(f"  销售成本合计 = {sum_sellcost:.2f}, 采购成本合计 = {sum_buycost:.2f}")
    print(f"  毛利额合计 = {sum_gross:.2f}, 净利润合计 = {sum_net:.2f}")
    print(f"  销售毛利率（毛利额/销售收入）= {销售毛利率:.2%}")
    print(f"  成本毛利率（毛利额/采购成本）= {成本毛利率:.2%}")


def main() -> None:
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        print("① 添加列")
        add_columns(conn)
        print("\n② 填充数据")
        fill_data(conn)
        print_summary(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
