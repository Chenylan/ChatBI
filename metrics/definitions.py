"""指标口径字典 + 模糊口径检测。

用户问题里出现「通用词」（毛利率 / 收入 / 成本 / 利润）但没有点名具体口径时，
detect_ambiguous 会返回该词的全部候选口径，供 agent 在上层触发澄清（列出选项让用户选）。

检测采用"减法匹配"：先把问题里明确点名的具体口径（canonical 名 + 明确别名）抠掉，
再在剩余文本里找通用词，从而避免中文术语嵌套子串造成的误判（如"成本毛利率"里的"成本"）。

说明：
- METRICS / GENERIC_TRIGGERS 是内置的「种子口径」，首次启动会被 metrics/store.py 写入 SQLite，
  之后以前端页面上对指标口径的增删改为准（source of truth 是 DB）。
- detect_ambiguous / format_metric_dict 都通过 _load_metrics() 读取合并后的口径，
  因此页面上的修改会实时生效。
"""
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel


class MetricDef(BaseModel):
    canonical: str                   # 标准口径名（用户回复它即可确认）
    specific_aliases: list[str] = []  # 明确的同义别名（须为完整词，避免嵌套子串）
    formula: str                     # 中文公式 / 口径说明
    sql_expr: str                    # 计算用的 SQL 表达式
    default: bool = False            # 是否默认口径


# ======================================================================
# 内置种子口径（首次启动写入 DB，之后以 DB 为准）
# ======================================================================

METRICS: dict[str, MetricDef] = {
    # ---- 毛利率 ----
    "销售毛利率": MetricDef(
        canonical="销售毛利率",
        specific_aliases=["gross margin"],
        formula="毛利额 ÷ 销售收入",
        sql_expr="SUM(毛利额) / SUM(销售收入)",
        default=True,
    ),
    "成本毛利率": MetricDef(
        canonical="成本毛利率",
        formula="毛利额 ÷ 采购成本",
        sql_expr="SUM(毛利额) / SUM(采购成本)",
    ),
    # ---- 收入 ----
    "销售收入": MetricDef(
        canonical="销售收入",
        specific_aliases=["销售额", "营业额"],
        formula="PAYMENTS.销售收入（订单销售金额，不含退款）",
        sql_expr="SUM(销售收入)",
        default=True,
    ),
    "实收金额": MetricDef(
        canonical="实收金额",
        specific_aliases=["实际到账"],
        formula="PAYMENTS.实收金额（扣除退款/折扣后）",
        sql_expr="SUM(实收金额)",
    ),
    # ---- 成本 ----
    "销售成本": MetricDef(
        canonical="销售成本",
        specific_aliases=["营业成本"],
        formula="PAYMENTS.销售成本（销售环节成本，含运费/销售费用）",
        sql_expr="SUM(销售成本)",
        default=True,
    ),
    "采购成本": MetricDef(
        canonical="采购成本",
        specific_aliases=["进货成本"],
        formula="PAYMENTS.采购成本（商品采购/进货成本）",
        sql_expr="SUM(采购成本)",
    ),
    # ---- 利润 ----
    "毛利额": MetricDef(
        canonical="毛利额",
        specific_aliases=["毛利润"],
        formula="销售收入 − 采购成本",
        sql_expr="SUM(毛利额)",
        default=True,
    ),
    "净利润": MetricDef(
        canonical="净利润",
        specific_aliases=["纯利润"],
        formula="实收金额 − 销售成本",
        sql_expr="SUM(净利润)",
    ),
}


# 通用歧义词 → 该词对应的候选口径（canonical 名）
GENERIC_TRIGGERS: dict[str, list[str]] = {
    "毛利率": ["销售毛利率", "成本毛利率"],
    "收入": ["销售收入", "实收金额"],
    "成本": ["销售成本", "采购成本"],
    "利润": ["毛利额", "净利润"],
}


# ======================================================================
# 动态加载：从 DB 读取合并后的口径（内置 + 自定义）
# ======================================================================

def _load_metrics() -> Tuple[Dict[str, MetricDef], Dict[str, List[str]]]:
    """从 DB 读取合并后的口径。延迟导入 store，避免与 store.py 的循环依赖。"""
    from metrics.store import load_merged
    return load_merged()


def detect_ambiguous(
    text: str,
    metrics: Optional[Dict[str, MetricDef]] = None,
    triggers: Optional[Dict[str, List[str]]] = None,
) -> dict:
    """返回 {通用词: [候选口径...]}，空 dict 表示无歧义。

    规则：先把问题里明确点名的具体口径抠掉，再在剩余文本里找通用词；
    若某个通用词仍在（且该组没有已被点名的口径），则视为歧义，列出该组全部候选。
    """
    if metrics is None or triggers is None:
        metrics, triggers = _load_metrics()

    # 具体口径标识（canonical 名 + 明确别名），按长度降序，长词优先匹配
    specific: List[Tuple[str, str]] = []
    for canonical, m in metrics.items():
        specific.append((canonical, canonical))
        for a in m.specific_aliases:
            specific.append((a, canonical))
    specific.sort(key=lambda x: -len(x[0]))

    resolved: set = set()
    remaining = text
    for term, canonical in specific:
        if term in remaining:
            resolved.add(canonical)
            remaining = remaining.replace(term, " ")

    result: dict = {}
    for trigger, canonicals in triggers.items():
        if trigger not in remaining:
            continue
        if any(c in resolved for c in canonicals):
            continue
        result[trigger] = [metrics[c] for c in canonicals if c in metrics]
    return result


def format_metric_dict(
    metrics: Optional[Dict[str, MetricDef]] = None,
    triggers: Optional[Dict[str, List[str]]] = None,
) -> str:
    """生成口径字典说明，注入 system prompt，让模型知道各口径的准确定义。"""
    if metrics is None or triggers is None:
        metrics, triggers = _load_metrics()

    lines = ["指标口径字典："]
    grouped: set = set()
    for trigger, canonicals in triggers.items():
        lines.append(f"- 「{trigger}」包含：")
        for c in canonicals:
            m = metrics.get(c)
            if m is None:
                continue
            grouped.add(c)
            lines.append(f"    * {m.canonical} = {m.formula}（SQL: {m.sql_expr}）")

    # 未分组（无触发词）的口径也列出来，避免自定义口径不进 prompt
    ungrouped = [m for c, m in metrics.items() if c not in grouped]
    if ungrouped:
        lines.append("- 其他口径：")
        for m in ungrouped:
            lines.append(f"    * {m.canonical} = {m.formula}（SQL: {m.sql_expr}）")

    lines.append("计算指标时，若用户未指明具体口径，必须列出上述选项让用户选择，不得擅自取其一。")
    return "\n".join(lines)
