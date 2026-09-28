# Moz

带「长期记忆」的本地 AI 对话应用。前端 React + TypeScript，后端 FastAPI，围绕对话构建一套可持续积累、衰减与检索的记忆系统，让 AI 真正"记得"和你聊过什么。

## 功能特性

**记忆系统**
- 多层记忆：核心 / 重要 / 常规三层分级存储，检索时按层加权
- 语义检索：智谱 embedding 向量 + 关键词的混合检索（RAG）
- 遗忘曲线：基于艾宾浩斯遗忘曲线对记忆做重要性衰减
- 记忆巩固：定期将零散对话沉淀为结构化记忆，支持摘要级联
- 记忆治理与评估：记忆质量治理、检索效果评估
- 工作记忆：当前会话的短期上下文管理
- 时间元数据：记忆的时间维度建模

**对话与情感**
- 情感图谱：识别对话情绪（EmotionType），构建并可视化情感关系
- 用户画像：从对话中自动提取并维护用户画像
- 多模态输入：支持向视觉模型发送图片（jpg / png / gif / webp）

**工程**
- 多用户：用户 ID 隔离数据，支持访问密钥（`MOZ_ACCESS_KEY`）与管理员密钥（`MOZ_ADMIN_KEY`）
- 多模型预设：DeepSeek / OpenAI / 智谱 GLM / 通义千问 / 硅基流动，内置 thinking 推理模式配置
- 对话数据导出 / 导入（管理员接口）
- 前端内置记忆查看器、日志查看器、统计面板

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.11+ · FastAPI · LangChain / LangGraph · Uvicorn |
| 前端 | React 18 · TypeScript · Vite · Zustand · react-markdown |
| 测试 | pytest（后端） · Vitest + Testing Library（前端） |
| 存储 | 本地文件（`backend/conversations/`、`backend/memory_store/`） |

## 目录结构

```
moz/
├── backend/                 # FastAPI 后端
│   ├── server.py            # API 入口（含 /api 与 /api/v1 双前缀路由）
│   ├── model_config.py      # 模型配置中心：切换 LLM 只改这里
│   ├── llm_config.py        # LLM 客户端工厂（自动应用 thinking 等特性）
│   ├── memory_manager.py    # 记忆管理核心（检索 / 衰减 / 分层）
│   ├── memory_layer.py      # 记忆分层
│   ├── memory_consolidation.py  # 记忆巩固
│   ├── memory_governance.py # 记忆治理
│   ├── memory_evaluation.py # 检索评估
│   ├── emotion_graph.py     # 情感图谱
│   ├── user_profile.py      # 用户画像
│   ├── working_memory.py    # 工作记忆
│   ├── summary_service.py   # 摘要服务
│   ├── conversation_store.py# 对话存储
│   ├── file_processor.py    # 图片等多模态处理
│   ├── conversations/       # 对话数据（本地生成）
│   ├── memory_store/        # 记忆数据（本地生成）
│   └── tests/               # pytest 测试
└── frontend/                # React 前端
    └── src/
        ├── api.ts           # API 客户端
        ├── store.ts         # Zustand 状态
        ├── App.tsx
        └── components/      # 聊天区 / 会话列表 / 记忆查看器 / 模型设置等
```

## 快速开始

### 1. 准备环境

- Python 3.11+
- Node.js 18+
- 至少一家 LLM 服务商的 API Key（记忆向量化固定使用智谱 embedding，因此 `ZHIPU_API_KEY` 必须配置）

### 2. 启动后端

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows（Linux/macOS: source .venv/bin/activate）
pip install -r ../requirements.txt

# 配置环境变量
cp .env.example .env            # 编辑 .env，填入你的 API Key

python server.py                # 启动于 http://127.0.0.1:8000
```

### 3. 启动前端

```bash
cd frontend
npm install
npm run dev                     # 启动于 http://localhost:3000，/api 自动代理到后端
```

打开 http://localhost:3000 即可开始对话。

## 模型配置

两种方式，任选：

1. **改配置文件**：编辑 `backend/model_config.py` 中的 `CHAT_MODEL` 与 `CHAT_BASE_URL`，程序根据 base_url 自动识别服务商并从 `.env` 读取对应 Key；特殊参数（如 thinking）在 `MODEL_PROFILES` 中按模型名前缀配置。
2. **界面配置**：前端侧边栏「模型设置」中选择预设服务商并填入 Key。

> 注意：记忆向量化固定使用智谱 `embedding-3`，无论对话模型用哪家，都需要在 `.env` 中配置 `ZHIPU_API_KEY`。

## 环境变量

| 变量 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek 对话模型 Key |
| `OPENAI_API_KEY` | OpenAI 对话模型 Key |
| `ZHIPU_API_KEY` | 智谱 Key（对话 + Embedding，**必填**） |
| `DASHSCOPE_API_KEY` | 通义千问 Key |
| `SILICONFLOW_API_KEY` | 硅基流动 Key |
| `MOZ_ACCESS_KEY` | 访问密钥，设置后前端需输入才能使用 |
| `MOZ_ADMIN_KEY` | 管理员密钥，导出 / 导入等管理接口使用 |
| `MOZ_ALLOWED_USER_IDS` | 允许的用户 ID 白名单（逗号分隔，留空不限制） |
| `CORS_ORIGINS` | 允许的跨域来源，默认 `http://localhost:3000,http://127.0.0.1:3000` |

参见 `backend/.env.example`。

## 测试

```bash
# 后端
cd backend && pytest

# 前端
cd frontend && npm test
```

## 数据与隐私

- 对话与记忆全部保存在本地 `backend/conversations/` 与 `backend/memory_store/`，不会上传到任何第三方
- 唯一的外部通信是你自己配置的 LLM 服务商 API
- `.env`、数据目录均已列入 `.gitignore`，不会被提交
- ## 致谢
感谢 [f-api.site](https://www.f-api.site) 为本项目提供的接口服务与技术支持。
感谢所有为本项目提交 Issue、PR 的贡献者，也感谢开源社区。
如果本项目对你有帮助，欢迎 Star ⭐！


