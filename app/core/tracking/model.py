"""打点数据模型：Content / Ext（对应 Go 结构体）。

type Content struct {
    Type   string `json:"type"`   打点类型
    Page   string `json:"page"`   业务名称
    Source string `json:"source"` 来源
    Ext    *Ext   `json:"ext"`    点位信息
}

type Ext struct {  // 点位信息：P0-P14，按业务名称（page）分配不同含义
    P0 ... P14 string
}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["TrackingExt", "TrackingContent"]


@dataclass
class TrackingExt:
    """点位信息（P0-P14，共 15 个字符串槽位）。

    各槽位的业务含义由 Content.page（业务名称）决定，约定见 docs/打点设计.md；
    未使用的槽位留空（protobuf 编码时自动省略）。
    """

    uid: str = ""
    p0: str = ""
    p1: str = ""
    p2: str = ""
    p3: str = ""
    p4: str = ""
    p5: str = ""
    p6: str = ""
    p7: str = ""
    p8: str = ""
    p9: str = ""
    p10: str = ""
    p11: str = ""
    p12: str = ""
    p13: str = ""
    p14: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> TrackingExt:
        """从 dict 构建（只取 p0-p14 键，其余忽略）。"""
        d = d or {}
        return cls(**{f"p{i}": str(d.get(f"p{i}", "") or "") for i in range(15)})

    def to_dict(self) -> dict[str, str]:
        """转 dict（含全部 15 个槽位）。"""
        return {f"p{i}": getattr(self, f"p{i}") for i in range(15)}


@dataclass
class TrackingContent:
    """打点内容（Content）。"""

    type: str = ""
    """打点类型（TrackingType）。"""
    page: str = ""
    """业务名称（TrackingPage）。"""
    source: str = ""
    """来源（TrackingSource）。"""
    model: str = ""
    """模型角色名（config.yaml models 的 key，监控平台按 model 分组/对比）。"""
    ext: TrackingExt = field(default_factory=TrackingExt)
    """点位信息（按 page 分配含义）。"""

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> TrackingContent:
        """从 dict 构建（ext 支持 dict 或 TrackingExt）。"""
        d = d or {}
        ext = d.get("ext") or {}
        return cls(
            type=str(d.get("type", "") or ""),
            page=str(d.get("page", "") or ""),
            source=str(d.get("source", "") or ""),
            model=str(d.get("model", "") or ""),
            ext=ext if isinstance(ext, TrackingExt) else TrackingExt.from_dict(ext),
        )
