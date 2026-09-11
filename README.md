# 个人 RAG 知识库助手

把散落在本机的 Markdown、PDF、Word、PPT 变成**可溯源**的问答知识库。
问它问题时，它会**只依据你的文档**作答，并标注每条结论来自哪份文件的哪个片段。

## 功能特性

| 功能 | 说明 |
|---|---|
| 文档上传与解析 | 支持 PDF / Word(docx) / PPT(pptx) / Markdown / 纯文本 / 常见代码文件，网页上传（含拖拽）或直接放进 `data/` |
| 智能分块 | 滑动窗口 + 自然断句优先，相邻块带重叠，避免语义被硬切断 |
| 向量化入库 | 调用智谱 `embedding-3` 生成向量，持久化到本地 ChromaDB |
| 增量索引 | 按文件内容 SHA-256 判断变化，只重建新增/修改的文件，自动清理已删除文件 |
| 相似度检索 | Top-K 余弦相似度召回，K 值可调 |
| 基于上下文的生成 | 召回片段按编号拼进 Prompt，要求模型"看着材料回答"、不得编造 |
| 来源引用 | 回答附带引用卡片，可展开查看原文；PDF 精确到页码，PPT 到页序 |
| 网页设置面板 | 在界面上直接填写 API Key 与模型参数，保存即写回 `.env` 并立即生效，无需重启 |
| 本地网页界面 | 聊天式交互，支持拖拽上传 |

## 技术选型

| 环节 | 选型 |
|---|---|
| 后端 | FastAPI（自带 Swagger 文档，访问 `/docs`） |
| 向量库 | ChromaDB（本地持久化，零运维） |
| 大模型 / Embedding | 智谱 GLM（OpenAI 兼容接口，一个 Key 通用） |
| 文档解析 | PyMuPDF / python-docx / python-pptx / 原生读取 |
| 前端 | 原生 HTML + JS，无需构建 |
| 测试 | pytest |

> 说明：未引入 LangChain。本项目的链路只有「检索 → 拼 Prompt → 调用模型」，
> 原生实现更短更透明；等将来需要多路检索或 Agent 编排时再评估。

## 目录结构

```
.
├─ src/
│  ├─ config.py      # 配置（环境变量 / .env，支持运行期更新）
│  ├─ loaders.py     # 各格式文档解析，产出 (文本, 位置)
│  ├─ chunking.py    # 分块策略
│  ├─ llm.py         # 智谱 Embedding 与 Chat 调用
│  ├─ store.py       # Chroma 集合管理
│  ├─ ingest.py      # 建库流水线 + 增量索引
│  ├─ retrieve.py    # Top-K 检索
│  ├─ generate.py    # Prompt 组装与带引用生成
│  └─ server.py      # FastAPI 接口
├─ web/index.html    # 前端聊天界面 + 设置面板
├─ tests/            # pytest 测试
├─ data/             # 你的文档放这里（不入版本库）
└─ storage/          # 向量库与索引清单落盘（不入版本库）
```

## 快速开始

**1. 安装依赖**

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**2. 启动服务**

```bash
uvicorn src.server:app --reload
```

打开 <http://127.0.0.1:8000> 。

**3. 配置 API Key（二选一）**

- **方式 A（推荐）**：在网页右上角点「设置」，粘贴 Key 后保存。配置会写入
  项目根目录的 `.env` 并**立即生效**，不需要重启服务。未配置时页面会自动弹出设置面板。
- **方式 B**：复制 `.env.example` 为 `.env`，填入在
  <https://bigmodel.cn/usercenter/apikeys> 申请的 Key，然后重启服务：

  ```
  ZHIPUAI_API_KEY=你的key
  ```

配好后上传文档即可提问，接口文档在 `/docs`。

## 配置项

全部通过环境变量或 `.env` 覆盖，也可以在网页「设置」面板里改。默认值见 `src/config.py`。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ZHIPUAI_API_KEY` | 无 | 必填，智谱 API Key |
| `CHAT_MODEL` | `glm-5.2` | 对话模型（如不可用可改 `glm-5.1` 等） |
| `EMBED_MODEL` | `embedding-3` | 向量模型，改动后需重建索引 |
| `EMBED_DIM` | `1024` | 向量维度，改动后需重建索引 |
| `CHUNK_SIZE` | `800` | 分块最大字符数 |
| `CHUNK_OVERLAP` | `120` | 相邻块重叠字符数 |
| `TOP_K` | `5` | 默认召回条数 |

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 聊天界面 |
| GET | `/health` | 健康检查 |
| GET | `/settings` | 读取当前配置（API Key 只返回脱敏提示） |
| POST | `/settings` | 保存配置并写回 `.env`，立即生效 |
| POST | `/upload` | 上传文档（multipart，字段名 `files`），自动增量建库 |
| POST | `/ingest` | 重建索引，body `{"force": false}`；`force=true` 全量重建 |
| POST | `/query` | 提问，body `{"question": "...", "top_k": 5}` |

## 测试

```bash
pytest
```

测试全部使用桩对象，不会发起真实 API 调用，也不会改动真实的 `.env`。

## 后续规划（V2+）

- 混合检索（BM25 + 向量）与 Rerank 重排
- 元数据过滤（按文件 / 标签 / 时间）
- 多轮对话与追问改写
- 文档管理界面（列表、删除、重新索引）
