from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from app.agents.lead_agent import GraphContext
from app.agents.thread_state import ThreadState


# 摘要节点
async def summarization(state: ThreadState, config: RunnableConfig, runtime: Runtime[GraphContext]):
    pass
