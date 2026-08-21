"""监控子包：Web 监控平台的接口与数据层。

router.py   /monitor/* 接口（组件配置 CRUD / 槽位含义 / 聚合查询 / token 汇总）
store.py    PostgreSQL 存储（组件配置 / 字段含义 / 用户 token 汇总）
query.py    打点数据日志聚合查询（时间范围 / 粒度 / 统计方式 / 按模型分组）
"""

from app.monitor import query, router, store

__all__ = ["router", "store", "query"]
