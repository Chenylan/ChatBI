from pydantic import BaseModel

class MetricDef(BaseModel):
    canonical: str                 # 规范口径名
    aliases: list[str]             # 别名（都会命中"毛利率"）
    formula: str                   # 计算公式
    sql_expr: str                  # 对应 SQL 片段
    default: bool = False          # 是否可作为默认口径



METRICS: dict[str, MetricDef] = {
    "销售毛利率": MetricDef(
        canonical="销售毛利率",
        aliases=["毛利率", "销售毛利率", "gross margin"],
        formula="(销售收入 - 销售成本) / 销售收入",
        sql_expr="(SUM(收入) - SUM(成本)) / SUM(收入)",
        default=True,
    ),
    "成本毛利率": MetricDef(
        canonical="成本毛利率",
        aliases=["毛利率", "成本毛利率", "成本加成率"],
        formula="毛利额 / 成本",
        sql_expr="SUM(毛利额) / SUM(成本)",
    ),
}

def resolve(term: str) -> list[MetricDef]:
    """返回别名命中的所有口径；len>1 即歧义。"""
    return [m for m in METRICS.values() if term in m.aliases]


def detect_ambiguous(text: str) -> list[MetricDef]:
    """从用户文本中检测指标口径，返回命中的所有定义（>1 表示存在歧义）。

    优先识别具体口径（如「销售毛利率」「成本毛利率」），命中即视为已明确；
    只有泛称「毛利率」才可能同时命中多个口径。
    """
    # 1) 具体口径优先：命中带限定词的别名即视为用户已指明
    for m in METRICS.values():
        for alias in m.aliases:
            if alias != "毛利率" and alias in text:
                return [m]
    # 2) 仅出现泛称「毛利率」
    if "毛利率" in text:
        return resolve("毛利率")
    return []