#!/usr/bin/env python
"""
Debug script for Deer Agent Service.
在 Trae/VS Code 中直接运行（F5），可设置断点调试。

Requirements:
    Run with `uv run` from the agent-service/ directory:

        cd agent-service && uv run python debug.py

Usage:
    1. 在 agent.py 或 plan_model_node.py 等文件设置断点
    2. 按 F5 或使用 "Run and Debug" 面板
    3. 观察控制台输出规划/执行/最终答案
"""

import asyncio
import os
import uuid

from langchain_core.messages import HumanMessage

from app.agents.graph.agent import GraphAgent
from app.config import get_app_config
from app.core.context import trace_id_ctx_var
from app.core.log import logger
from app.core.runtime import RunContext


async def main():
    trace_id = uuid.uuid4().hex
    trace_id_ctx_var.set(trace_id)
    logger.info("debug test start")

    app_config = get_app_config()

    from app.core.checkpointer import create_checkpointer

    # 独立服务：从 config.yaml 的 database 段创建共享 checkpointer。
    # postgres 模式返回 PostgresCheckpointerHandle，需 async with 进入
    # （打开连接池 + setup 建表）；memory 模式直接返回 saver。
    checkpointer = create_checkpointer(app_config)

    # 用 async with 管理生命周期（兼容 Handle 和直接 saver）
    enter_ctx = checkpointer if hasattr(checkpointer, "__aenter__") else None
    saver = None
    if enter_ctx:
        saver = await enter_ctx.__aenter__()
    else:
        saver = checkpointer

    try:
        runcontext = RunContext(checkpointer=saver, app_config=app_config)
        # 无状态图：全局复用，thread_id 每次传入
        agent = GraphAgent(runcontext)
        userquery = "查询北京今天的天气"
        state = {"messages": [HumanMessage(content=userquery)]}
        # 默认每次跑一个全新线程：复用固定 thread_id 会让 checkpointer 里堆积历史消息，
        # 规划节点看到被污染的上下文（同一问题反复出现）后会给出退化计划。
        # 需要接着上一次的状态调试时，用 THREAD_ID=xxx 显式指定。
        thread_id = os.getenv("THREAD_ID") or f"debug-{uuid.uuid4().hex[:8]}"

        # 使用成熟的消息打印器
        async for chunk in agent.astream(state, thread_id=thread_id, trace_id=trace_id):
            print("###############最外层输出流######################")
            if chunk["type"] == "updates":
                data = chunk["data"]
                if "__interrupt__" in data:
                    for intr in data["__interrupt__"]:
                        print("中断值:", intr.value)  # 字符串形式的 JSON
                        print("中断 ID:", intr.id)
            if chunk["type"] == "custom":
                print(chunk)
    finally:
        if enter_ctx:
            await enter_ctx.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(main())
