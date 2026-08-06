# RAG 知识库方案（语雀 + ElasticSearch）

> 状态：**方案规划**（备份待用，尚未实现）
> 用途：给 agent-service 的 general_agent 提供知识库检索能力

---

## 1. 背景与目标

**目标**：将语雀（Yuque）知识库文档拉取下来，切分后向量化存入 ElasticSearch，
为 general_agent 提供 `rag_search` 检索工具，使 agent 回答问题时能检索并引用知识库内容。

**适用场景**：团队用语雀维护技术文档/操作手册，agent 需要基于这些内部文档回答问题。

---

## 2. 整体架构

```
语雀 API ──拉取──> Markdown 文档 ──切分──> chunk ──embedding──> ElasticSearch 索引
                                                                      │
agent 对话时 ──> rag_search 工具 ──> ES knn 检索 ──> 相关 chunk ──> 喂给 LLM
```

### 数据流

1. **拉取**：`scripts/ingest_yuque.py` 调用语雀开放 API，拉取知识库全部文档（Markdown 原文）
2. **切分**：按标题层级/字符长度切分为 chunk（保留上下文语义）
3. **向量化**：chunk 经 embedding 模型转为向量
4. **入 ES**：写入 ElasticSearch 的 `dense_vector` 索引
5. **检索**：`rag_search` 工具对用户查询做 embedding，ES `knn` 查询返回最相关 chunk
6. **回答**：chunk 注入 LLM 上下文，agent 据此回答

---

## 3. 语雀 API 要点

- **基础 URL**：`https://www.yuque.com/api/v2`
- **认证**：`Authorization: Bearer <token>`（语雀个人令牌）
- **关键端点**：

| 端点 | 用途 |
|------|------|
| `GET /api/v2/repos` | 列出知识库 |
| `GET /api/v2/repos/{namespace}/docs` | 列出知识库下文档 |
| `GET /api/v2/repos/{namespace}/docs/{slug}` | 获取单个文档元信息 |
| `GET /api/v2/repos/{namespace}/docs/{slug}?raw=1` | **获取 Markdown 原文（RAG 核心）** |

- **命名空间**：知识库的 `namespace` 形如 `org/repo`（组织/知识库），在知识库设置里可查

---

## 4. ElasticSearch 向量检索

### 版本要求

- **ES 8.x**（推荐 8.11+）
  - 8.0 起内置 `dense_vector` 字段 + `knn` 查询（近似最近邻）
  - 8.11+ 支持 `semantic_text`（内嵌 embedding，简化 RAG）
  - 8.8+ 的 `knn` 支持 filter 扩展

### 索引 Mapping（dense_vector）

```json
{
  "mappings": {
    "properties": {
      "content": { "type": "text", "analyzer": "ik_max_word" },
      "content_vector": {
        "type": "dense_vector",
        "dims": 1536,
        "index": true,
        "similarity": "cosine"
      },
      "metadata": {
        "properties": {
          "source": { "type": "keyword" },
          "doc_title": { "type": "keyword" },
          "url": { "type": "keyword" },
          "chunk_index": { "type": "integer" }
        }
      }
    }
  }
}
```

- `dims` 必须与 embedding 模型输出维度一致
- `similarity: cosine` 适合语义检索

### knn 检索查询

```json
{
  "knn": {
    "field": "content_vector",
    "query_vector": [...],
    "k": 5,
    "num_candidates": 50
  }
}
```

---

## 5. 技术选型决策

| 组件 | 选型 | 理由 |
|------|------|------|
| 文档源 | 语雀开放 API | 用户明确要求 |
| 拉取方式 | 官方 API（`raw=1` 拿 Markdown） | 不需要第三方爬取服务 |
| 向量库 | ElasticSearch 8.x | 成熟、复用现有 ES、无需额外部署 |
| ES 客户端 | **官方 `elasticsearch` Python 客户端** | 可控性强，自写 knn 查询 |
| 切分 | `langchain_text_splitters` 或自写 | 按标题/长度切分 |
| embedding | 待定（OpenAI / DeepSeek / 本地 bge） | 需确认维度匹配 ES |

---

## 6. 文件布局（规划）

```
agent-service/
├── scripts/
│   └── ingest_yuque.py      # 语雀拉取 + 切分 + 入 ES（CLI 入口）
├── app/
│   ├── rag/
│   │   ├── __init__.py
│   │   ├── yuque_client.py  # 语雀 API 客户端
│   │   ├── splitter.py      # 文档切分
│   │   ├── es_store.py      # ES 索引/写入/检索
│   │   └── tools.py         # rag_search 工具（供 general_agent）
│   └── agents/tools.py      # get_execute_tools 加 rag_search
├── config.yaml              # 加 rag 配置段
├── .env                     # YUQUE_TOKEN, ES 地址, embedding key
└── docs/
    └── RAG_方案.md           # 本文档
```

---

## 7. config.yaml 新增段（规划）

```yaml
rag:
  provider: yuque
  elasticsearch:
    url: http://localhost:9200
    index: rag_docs
    dims: 1536
  embedding:
    provider: openai        # 或 deepseek / bge
    model: text-embedding-3-small
    api_key: $EMBEDDING_API_KEY
  yuque:
    namespace: "org/repo"
    # token 走环境变量 YUQUE_TOKEN
```

---

## 8. rag_search 工具（规划）

```python
@tool
def rag_search(query: str, top_k: int = 5) -> str:
    """在知识库中检索与问题最相关的文档片段。

    Args:
        query: 用户的查询问题。
        top_k: 返回最相关的片段数。
    """
    # 1. query 转 embedding
    # 2. ES knn 查询
    # 3. 返回 top_k 个 chunk 拼接
```

**接入方式**：`get_execute_tools()` 返回值里加 `rag_search`，
general_agent 提示词的 `{{tools_desc}}` 会自动注入其描述。

---

## 9. 待确认事项

- [ ] **embedding 模型**：OpenAI `text-embedding-3-small` / DeepSeek / 本地 bge？（决定 ES `dims`）
- [ ] **ES 具体版本**：确认 ≥8.0（knn 可用），建议 ≥8.11（semantic_text）
- [ ] **语雀 token**：是否有个人令牌？
- [ ] **语雀命名空间**：知识库的 `org/repo`？
- [ ] 中文分词是否用 `ik` 插件（纯向量检索可不依赖，但混合检索有用）

---

## 10. 后续扩展（不在本期）

- **混合检索**：BM25 全文 + kNN 向量融合（`rrf` 排名融合）
- **增量同步**：按语雀文档更新时间增量拉取
- **权限隔离**：按团队/知识库隔离索引
- **检索评估**：对 rag 效果做命中率评估
