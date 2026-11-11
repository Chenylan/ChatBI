"""
指标口径管理 API：页面上自定义指标口径的增删改查。

路由挂在 /api/metrics 下（见 backend/api/__init__.py）：
- GET    /api/metrics            列出全部口径
- POST   /api/metrics            新增/更新一条口径（按 canonical 幂等 upsert）
- DELETE /api/metrics/{canonical} 删除一条口径
"""
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from metrics import store

router = APIRouter()


class MetricPayload(BaseModel):
    """前端提交的一条指标口径。"""
    canonical: str = Field(..., description="口径标准名（唯一，如「客单价」）")
    aliases: List[str] = Field(default_factory=list, description="别名列表")
    formula: str = Field(default="", description="中文公式 / 口径说明")
    sql_expr: str = Field(default="", description="SQL 表达式")
    group: str = Field(default="", description="所属通用触发词（如「毛利率」），用于歧义检测分组；可留空")
    default: bool = Field(default=False, description="是否为该组默认口径")


@router.get("")
def list_metrics():
    """列出全部指标口径（内置 + 自定义）。"""
    return store.list_metrics()


@router.post("")
def save_metric(payload: MetricPayload):
    """新增或更新一条指标口径（按 canonical 幂等 upsert）。"""
    try:
        return store.upsert_metric(
            canonical=payload.canonical,
            aliases=payload.aliases,
            formula=payload.formula,
            sql_expr=payload.sql_expr,
            group=payload.group,
            default=payload.default,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{canonical}")
def delete_metric(canonical: str):
    """删除一条指标口径。"""
    if not store.delete_metric(canonical):
        raise HTTPException(status_code=404, detail=f"口径不存在：{canonical}")
    return {"ok": True, "canonical": canonical}
