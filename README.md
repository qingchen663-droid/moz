<div align="center">

# moz — AI 情感陪伴助手

**拥有多层认知记忆系统的 AI 情感伴侣，基于艾宾浩斯遗忘曲线、RRF 混合检索、用户档案卡与三 Agent 协作工作流**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat&logo=python&logoColor=white)](https://python.org)
[![React](https://img.shields.io/badge/React-18-61DAFB?style=flat&logo=react&logoColor=black)](https://react.dev)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.5-3178C6?style=flat&logo=typescript&logoColor=white)](https://www.typescriptlang.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104+-009688?style=flat&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Vite](https://img.shields.io/badge/Vite-8-646CFF?style=flat&logo=vite&logoColor=white)](https://vitejs.dev)
[![License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](./LICENSE)
[![Version](https://img.shields.io/badge/Version-v0.4-blue.svg)](./CHANGELOG.md)

</div>

---

## 项目简介

**moz** 是一个 AI 情感陪伴助手，不只是聊天机器人——她能真正「记住」关于你的事，并像朋友一样关心你的情绪。名字源自《可塑性记忆》中拥有感情的人形智能机器人，当今社会的请与爱太过稀缺与昂贵，希望未来一天人类能开发出真正的情感陪伴机器人。

与传统聊天 AI 不同，moz 拥有**多层认知记忆系统**——用户档案卡 + 三层分级记忆（核心/重要/常规）+ 时间标签 + 艾宾浩斯遗忘曲线 + RRF 混合检索。她能模拟人类记忆的编码、存储、检索、衰减四个阶段：你告诉她的事情，她不会轻易忘记；
不重要的事情，她**也不会删掉或忘掉**——只是权重一路降到最低，很少再主动想起来、再提起来。

moz 由三个协作的 AI Agent 分工驱动：**情感分析** 与 **记忆检索** 提供上下文，**对话生成** 综合它们用心地与你对话。
运行时序上前两者**不挡在第一个字前面**（情感判断走毫秒级规则 + 后台攒下的基线与对策），否则每次开口都要先等一次上游往返。

---

## 核心特性

- **多层认知记忆系统** — 用户档案卡（结构化画像）+ 三层分级记忆（核心/重要/常规）+ 时间标签 + 艾宾浩斯遗忘曲线，模拟人类记忆的编码-存储-检索-遗忘全流程
- **用户档案卡** — 自动从对话中提取身份信息、喜好偏好、人际关系、情感模式，生成结构化用户画像，每次对话自动注入
- **记忆分层与遗忘** — 核心记忆（永不遗忘 S=10）、重要记忆（慢速遗忘 S=2）、常规记忆（正常遗忘 S=1），按重要性和情感强度自动分级
- **记忆时间标签** — 自动识别「昨天」「去年夏天」「大学时期」等时间表达，为记忆附加时间上下文
- **情感分析引擎** — 规则+LLM 混合情感分析，支持语境翻转（「喜欢+没结果」→ 难过而非开心），情感标签自动传递给记忆存储
- **工作记忆层** — 跨对话持久化的短期上下文，解决切换对话后 AI "失忆"的问题
- **记忆查看器** — Web 端可查看档案卡、全部记忆（含层级/情感/时间标签）、工作记忆摘要
- **三 Agent 协作工作流** — 概念分工是「情感分析 ‖ 记忆检索 → 对话生成」；运行时序上**前两者不挡第一个字**（情感判断走毫秒级规则 + 后台基线/预热，模型调用排在落库前），详见 `docs/响应时间账.md`
- **RRF 混合检索** — 语义 + 关键词两路召回 → RRF 融合（k=60）→ 多特征 Reranking（RRF 0.5 + 时间衰减 0.2 + 情感匹配 0.15 + 重要性 0.1 + 层级权重 0.05）；关键词那一路会**剪掉词面上必然 0 分的记忆**（结果逐条对拍不变）
- **流式逐 Token 输出** — 基于 `asyncio.Queue` 实现逐 token 推送，回复像真人打字一样自然
- **可观测性** — 请求耗时中间件 + `/api/metrics` 端点，暴露各端点 QPS / 平均延迟 / 错误率，以及**首字分段计时**（`first_token` 端到端／`first_token_relay` 只量中转／`first_token_stages` 各段）
- **自定义人设** — Web 端侧栏点击按钮即可修改 AI 性格、身份和说话风格，立即生效
- **多模型支持** — DeepSeek / OpenAI / 智谱 / 通义千问 / SiliconFlow，切换只需改两个变量
- **深度思考模式** — 各家的开/关键由 `MODEL_PROFILES` **自己声明**，没声明的维持不发（不猜参数名）。
  **注意有坑**：中转可能 200 收下参数然后静默忽略——发没发出去看 `emotion.thinking.thinking_param_sent/absent`
- **多模态对话** — 图片能发过去（需视觉模型），微信风格图片气泡。**不承诺看得准**：实测中转会随机丢图，
  所以措辞只说"图会发过去，不一定准"（v0.4 起按实测去掉承诺）
- **安全认证** — 双级密钥认证（访问密钥 + 管理员密钥），保护对话数据安全
- **总结记忆金字塔** — 会话摘要持久化 SQLite，同周自动聚合为周记、周记级联为月记，AI 能自然提起「上周/上个月」的话题
- **记忆巩固引擎** — 相似片段语义聚类后 LLM 合并为整合记忆，容量超限时自动执行巩固→归档→清理三层维护
- **开放话题跟进** — 结构化跟踪未完成事件，隔天后 AI 自然追问进展，长期未闭环自动过期
- **数据自主权** — 一键导出对话/记忆/档案/工作记忆/头像 JSON 快照，管理员可整包导入恢复；会话全文搜索
- **主动关心** — 个人关心数据库（生日/事件/约定/跟进/健康/关系人）+ 天气带伞提醒 + 安静时段与话多话少自适应，到点由 AI 先开口而不是等你问

---

## 版本迭代

### v0.4 — 桌面化与主动关心 `当前版本`

> 从"能聊能记"走到"像装在自己电脑上的一个软件"，并把"她答不上来"从静默丢数据变成能看见、能补记的失败。
> 逐条清单见 [CHANGELOG.md](./CHANGELOG.md)（v0.4 之后还有一串未发布的响应时间偿还，见同文件顶部）。

**新增**
- 一键启动与桌面外壳：`moz-app.bat`、PWA（**零缓存** Service Worker）、系统托盘（页面关着也能弹 Windows 通知）
- 主动关心引擎：后端每 60 秒判定"现在该不该说句话"，带安静时段、当日名额、最小间隔；事项（生日/约定/复诊/答辩）从对话里自己长出来，用户不填表
- 自测与保命工具：`tools/selftest.py`（现 51 项快检）、`tools/snapshot_data.py`（快照/回滚）、`tools/clean_probe_data.py`
- 情感预热三层（L0 规则 / L1 基线 / L2 触发式对策）+ 界面「她猜的」页签可看可撤

**变更/修复（挑对外感受最明显的）**
- 长期记忆上限从 300 抬到 5000、默认**永不物理删除**（旧策略会悄悄把用户历史归档再删掉）
- 首字路径上摘掉三次上游往返：「组织语言」从 10.4/178.6/64.1 秒 → 1.6/1.3 秒
- 相对日期不再「今天+7 天」糊弄（下周三记成下周一，会在错的日子当面说错话）
- 发图措辞去承诺、报错翻译成人话并带上**实际等了多久**、主动关心不再对着没人听的房间说

### v0.3 — 记忆治理与巩固

> 在 v0.2 分层记忆基础上引入 governed lifecycle：相似记忆自动巩固、总结金字塔持久化、开放话题主动跟进，以及用户数据自主权（导出/导入）。

**新增**
- **总结记忆金字塔**（`summary_service.py`）— 会话摘要 SQLite 持久化；同周 ≥3 条 session 自动聚合为周记，≥4 条周记级联为月记；对话时按时间标签注入 prompt（月→周→会话顺序）
- **记忆巩固 MemoryConsolidator**（`memory_consolidation.py`）— 语义聚类相似片段（相似度 ≥0.55），LLM 合并为整合记忆并标注来源 consolidation；记忆量达阈值触发三层自动维护（巩固 → 归档低价值 → 硬清理过期归档）
- **开放话题 OpenLoops**（`working_memory.py`）— LLM 更新工作记忆时同步产出结构化待跟进事件；隔 24h 后 AI 自然追问进展，30 天未闭环自动过期不再注入
- **检索性能优化** — 语义检索改为 NumPy 批量矩阵余弦 + 用户级缓存索引（写入失效）；新增 FTS5 会话全文搜索 `GET /api/search/{user_id}?q=`
- **数据自主权 API** — `GET /api/export/{user_id}` 一键导出对话/记忆/档案/工作记忆 JSON 快照；管理员 `POST /api/import/{user_id}` 整包恢复（内容哈希去重）
- **可控性补齐** — 记忆反馈（helpful/wrong/outdated/irrelevant 影响排序权重）、等级调整审计、`POST /api/memory/{user_id}/consolidate` 手动巩固+级联
- 前端：总结标签页（对话/周记/月记）、导出与导入按钮、生成停止按钮（AbortController）

### v0.2 — 多层认知记忆系统

> 从双层存储架构升级为完全本地的多层认知记忆系统，新增用户档案卡、记忆分层、时间标签、记忆查看器，移除 Mem0 外部依赖。

**新增**
- 用户档案卡（`user_profile.py`）— 自动提取 identity / preferences / relationships / emotional_profile，结构化用户画像
- 记忆分层（`memory_layer.py`）— 核心记忆（S=10）/ 重要记忆（S=2）/ 常规记忆（S=1），按重要性和情感强度自动分级
- 记忆时间标签（`temporal_metadata.py`）— 识别「昨天」「去年夏天」「大学时期」等时间表达
- 记忆查看器（`MemoryViewerModal`）— Web 端查看档案卡 + 全部记忆（含层级/情感/时间标签）+ 工作记忆摘要
- 情感分析语境翻转 — 「喜欢 + 没结果」→ 难过而非开心，「努力 + 失败」→ 难过而非希望
- 情感标签传递 — 情感分析 Agent 结果自动传递给记忆存储，不再退回关键词匹配
- 档案卡 API（`GET/PUT /api/profile/{user_id}`）+ 记忆详情 API（`GET /api/memory/{user_id}/detail`）+ 层级管理 API
- 数据库迁移脚本（`migration_v2.py`）— v1 → v2 平滑升级，补 schema_version 记录

**移除**
- Mem0 云端记忆 — 本地系统已完整覆盖其能力（事实提取 + 向量生成 + 混合检索），移除后系统更简单、更快、无外部依赖

**修复**
- 前端 useEffect 无限循环导致 429 速率限制
- 流式聊天消息状态管理（消息 ID 精确定位，防止用户消息消失和重复回复）
- `run_emotion_workflow_streaming` 中 `profile_manager` 未传递导致的 NameError

---

### v0.1 — 认知记忆系统 `初始版本`

> 基础架构搭建：三 Agent 工作流 + 认知记忆模型 + Mem0 双层存储 + Web 前端。

- 艾宾浩斯遗忘曲线 — `R = e^(-t/S)`，记忆保留率按时间自然衰减
- RRF 混合检索 — 语义 + 关键词多路召回 → RRF 融合 → 多特征 Reranking
- 查询改写 — LLM 将口语化输入扩展为 2-3 个检索查询
- 工作记忆层 — 跨对话持久化的短期上下文（`working_memory.py`）
- Mem0 双层存储 — 云端向量检索 + 本地 SQLite 语义检索，冗余备份
- 三 Agent 协作工作流（LangGraph）— 情感分析 ‖ 记忆检索 → 对话生成
- 流式逐 Token 输出 — SSE 推送 + `asyncio.Queue`
- Web 前端（React + TypeScript + Vite）— 对话界面 + 侧栏管理
- 多模型支持 — DeepSeek / OpenAI / 智谱 / 通义千问 / SiliconFlow
- 自定义人设 + 多模态对话 + 安全认证 + 可观测性

---

## 系统架构

```
                          用户输入
                             │
                 ┌───────────┴───────────┐
                 │                       │
          ┌──────▼──────┐        ┌───────▼──────┐
          │ 情感分析 Agent│        │ 记忆检索 Agent │  ← 并行执行
          └──────┬──────┘        └───────┬──────┘
                 │                       │
                 └───────────┬───────────┘
                             │
           ┌─────────┼─────────┼─────────┐
           │         │         │         │
       档案卡    记忆上下文    情感摘要   工作记忆
           │         │         │         │
           └─────────┴─────────┴─────────┘
                             │
                    ┌────────▼────────┐
                    │   对话生成 Agent   │  ← 综合信息，流式生成共情回复
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │    记忆存储      │  ← 事实提取 + 情感标签传递 + 时间标签 + 层级分配
                    └─────────────────┘
```

> **这张图画的是三 Agent 的概念分工，不是运行时序。** 界面上真正跑的只有流式路径
> （`run_emotion_workflow_streaming`）：情感分析与记忆检索**不再挡在第一个字前面**——
> 情感判断改由毫秒级规则（L0）+ 后台基线（L1）+ 后台预热对策（L2）供措辞，模型调用排在落库前。
> 第廿四轮把这条路上"建好却从没被调用"的非流式 LangGraph 图删了——它曾长期遮住一个真 bug
> （档案卡只有那个节点会填进 prompt，等于存了却从来不用）。
> 三层情感信号的设计见 `docs/情感预热系统设计.md`，首字时间的实测账本见 `docs/响应时间账.md`。

### 记忆检索流程（RAG）

```
用户输入 → 每个查询跑语义 + 关键词两路召回（查询向量批量生成并短时缓存）
         → RRF 融合排序（k=60）
         → BM25/IDF 关键词加权 + 多特征 Reranking（RRF 0.5 + 时间衰减 0.2 + 情感匹配 0.15 + 重要性 0.1 + 层级权重 0.05）
         → Top N 记忆注入 Prompt
```

**回话那一句用的是用户原话这一个查询**（首字路径上不调模型做查询改写，见 `docs/响应时间账.md` §3）：
`rewrite_query` / `_REWRITE_POOL` / `search_memories(queries=)` 多路扇出已在第廿四轮整块删除
（唯一调用方就是那条死图）。
超长对话另有第三层：被历史窗口丢掉的早期轮次会折成一段「之前聊了什么」，**后台补、下一句生效、按会话分键**。
关键词那一路会先**剪掉词面上必然 0 分的记忆**（第廿一轮，`(id, 分数)` 逐条对拍不变：5000 条 111.6ms → 60.6ms）。

语义索引按用户缓存归一化向量矩阵，记忆写入、状态变化或补算向量时失效并重建。
**整库补向量已交给后台单飞线程**：本轮不再替整库付一次注定失败的往返，下一轮起语义自动生效。
检索阶段耗时、索引命中率和平均阶段延迟通过管理员接口 `GET /api/metrics` 的 `search` 字段查看。

### 记忆分层架构

```
┌─ 工作记忆（当前状态摘要，每次对话注入，覆盖式更新）
│
├─ 档案卡（结构化用户画像，每次对话注入，增量式更新）
│   └─ identity / preferences / relationships / emotional_profile
│
└─ 长期记忆（原始事实，按需检索，按层级遗忘）
    ├─ 核心记忆（重要性≥0.8 或 情感强度≥0.8）  ← S=10，几乎不忘
    ├─ 重要记忆（重要性≥0.6 或 情感强度≥0.7）  ← S=2，慢速遗忘
    └─ 常规记忆（其余）                          ← S=1，正常遗忘
```

---

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| **前端** | React 18 + TypeScript + Vite 8 | 响应式 SPA，温暖治愈风格 UI |
| **状态管理** | Zustand | 轻量级状态管理 |
| **后端** | FastAPI + Uvicorn | 高性能异步 API，SSE 流式响应 |
| **Agent 编排** | 自研流式编排（LangChain 客户端） | 三 Agent 分工；第廿四轮删掉了那条建好却从没被调用的非流式 LangGraph 图 |
| **LLM 客户端** | langchain-openai (ChatOpenAI) | 统一接口，支持多模型 |
| **记忆系统** | 多层认知记忆模型 | 档案卡 + 三层分级 + 时间标签 + 遗忘曲线 + RRF 混合检索 + Reranking |
| **语义向量** | 智谱 Embedding API (embedding-3) | 余弦相似度记忆匹配；令牌失效时自动指数退避、检索走关键词 |
| **存储** | SQLite (WAL 模式) | 对话 & 记忆 & 档案卡持久化，增量写入 |
| **测试** | pytest (145 tests) + Vitest (67 tests) + `tools/selftest.py`（51 项全局快检门禁） | 后端核心算法 + 前端交互 + 端到端行为契约 |
| **质量门禁** | `tools/selftest.py` + pytest + tsc/vitest | 四条命令全绿才提交（见上面「度量与自测」）。**没有 CI 流水线**——门禁靠人跑 |

---

## 快速启动

### 环境要求

- Python 3.11+
- Node.js 20.19+ 或 22.12+

### 1. 克隆项目

```bash
git clone https://github.com/wenbo-zhang1/moz.git
cd moz
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入 API Key：

```env
DEEPSEEK_API_KEY=sk-your-key-here
# ZHIPU_API_KEY=your-zhipu-key-here    # 语义向量检索需要
```

### 3. 启动服务

```bash
# Windows
.\start.bat

# macOS / Linux
chmod +x start.sh && ./start.sh
```

启动后访问 `http://127.0.0.1:3000`。API 文档：`http://127.0.0.1:8000/docs`

> 请固定使用 `127.0.0.1:3000`。`localhost:3000` 在浏览器里是**另一个 origin**，访问密钥等本地设置按 origin 隔离，换着打开会读不到；头像已改为存在后端，不受此影响。

### 4. 当作桌面应用使用（可选）

**零点击方式**：双击 `moz-app.bat`（或开始菜单 / 桌面上的 **moz** 快捷方式）。它只在端口没被监听时才拉起后端和前端（服务窗口最小化），等前端就绪后以无边框应用窗口打开界面，不会重复启动已在跑的服务。

> `moz-app.bat` 必须保持纯 ASCII：cmd.exe 按 OEM 代码页解析 `.bat`，写 UTF-8 中文注释会啃掉命令行。

**更像原生软件的方式**：在 `http://127.0.0.1:3000` 页面点地址栏右侧的安装图标（或浏览器菜单 → 应用 → 将此网站安装为应用），之后从开始菜单 / 任务栏的 **moz** 启动：独立窗口、自带图标，看起来就是一个软件，而改前端代码仍然即时热更新。

- 图标资源由 `frontend/public/icons/` 提供，换 logo 后重新生成：`.venv/Scripts/python.exe frontend/scripts/gen_icons.py`
- 安装用的 service worker（`frontend/public/sw.js`）**刻意不缓存任何资源**，否则会破坏热更新，不要给它加缓存策略
- 卸载：浏览器地址栏进 `chrome://apps` 右键 moz 移除，或 Windows 设置 → 应用
- 改了 `manifest.webmanifest` 不会自动生效，需先卸载再重装（浏览器会缓存 manifest）

### 5. 主动关心（到点 AI 先开口）

入口在侧栏「设置」→「主动关心」。后端每 60 秒判定一次，够格才说话，一次只说一件事。

- **提醒事项自动记录**：聊到生日/纪念日、约定、复诊、面试答辩、正在推进的事，后台自动抽取并归一日期（"下周三"、"6月3号"都能折成时间戳），**不需要用户手动填表**。抽取走模型，模型不可用时回落到 `TemporalExtractor` 的相对日期解析，宁缺毋滥；同名事项只更新不重复。抽取在对话结束后的后台任务里跑，不拖慢回复
- 界面只保留**只读列表 + 忘掉（删除）**，供用户核对它记错了什么
- **所在城市**也会自动补：聊到"我在宁波"就顺带解析出省份+城市，用于带伞提醒
- **话多还是话少**：默认 `auto`，按你回复的长短和是否主动开口慢慢调（0~1，界面上显示当前值）；也可手动选话少/适中/话多，对应每天最多 1/2/4 次
- **安静时段**：默认 23:00~08:00 不说话，支持跨零点
- **带伞提醒**：城市聊到了自动填（也可在界面手动改），只在早上 7~10 点说、每天最多一次。天气源用腾讯 `wis.qq.com`（免 key；本机访问 Open-Meteo / wttr.in 不通）
- **措辞**：优先交给模型润色，模型不可用时自动回落到模板，不会因为中转抽风就不说话
- **送达**：页面开着时前端每 20 秒取一次并落进当前对话；页面关着时由托盘（`backend/tray_app.py`）弹 Windows 通知。谁先 ack 谁负责写进对话历史，不会重复
- 托盘带命名互斥体守卫，重复启动会自动退让；`moz-app.bat` 已顺带拉起托盘

数据落在 `backend/moz.db` 的 `care_items` / `proactive_queue` / `care_log` / `care_settings` 四张表。新增依赖 `pystray`、`winotify`（已写进 `requirements.txt`）。

---

## 配置说明

### 切换 LLM 模型

修改 `backend/model_config.py`：

```python
CHAT_MODEL = "deepseek-v4-flash"
CHAT_BASE_URL = "https://api.deepseek.com/v1"
```

系统根据 `base_url` 自动识别提供商并匹配 API Key。

| 提供商 | base_url 关键词 | .env 变量名 |
|--------|----------------|-------------|
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` |
| OpenAI | `openai.com` | `OPENAI_API_KEY` |
| 智谱 AI | `bigmodel.cn` | `ZHIPU_API_KEY` |
| 通义千问 | `dashscope` | `DASHSCOPE_API_KEY` |
| SiliconFlow | `siliconflow` | `SILICONFLOW_API_KEY` |

### 访问认证

```env
MOZ_ACCESS_KEY=your-secret-key   # 前端访问密钥
# 用户白名单；未配置时仅允许 web_user_001。升级前如有其他用户 ID，请先加入此列表。
MOZ_ALLOWED_USER_IDS=web_user_001
MOZ_ADMIN_KEY=your-admin-key     # 管理员密钥（日志、Metrics、Prompt 修改）
```

### 响应时间（首字）

**完整账本在 [`docs/响应时间账.md`](./docs/响应时间账.md)**——里面写的是量出来的数、哪些办法已被数字否掉、
以及三条口径不同的计时线（`first_token` 端到端／`first_token_relay` 只量中转／`first_token_stages` 各段）。
全部旋钮在 `.env.example` 里有注释，改了要**重启后端**（正在跑的后端不会自己改）。常用的三个：

```env
MOZ_CHAT_THINKING=0            # 回话那一句带不带思考；默认关（开着更慢、回复更长）
MOZ_SENSITIVE_WAIT_SECONDS=45  # 命中敏感话题时，开口之前最多为那一次情感判断等几秒；0 = 不等
MOZ_EMBED_FAILURE_COOLDOWN_MAX=1800  # 向量服务失败后的退避封顶（越退越长，成功一次复位）
```

现在实测到的样子（`tools/bench_latency.py`，每档 ≥3 次采样才算结论）：0/10/100 条记忆下的普通句
**首字中位 1.0~2.1 秒**；敏感轮 7.1 秒开口；带图 4.6 秒。
**但每个后端起来之后的第一句是 17~50 秒**（原因未查清，别把它读成"她平时 2 秒开口"）。

### 度量与自测

- 只读指标：`GET /api/metrics`（Admin Key）——`first_token*`、`search`、`emotion.sensitive_wait`（含兑现率）、
  `emotion.thinking`（思考参数发没发出去）、`emotion.prewarm`
- 门禁：`tools/selftest.py` **51 项快检**（秒级、全打桩、不碰大模型、不写用户数据；
  `--full` 另加真实对话与中转看图）。**其中 11 项走 HTTP，后端没起会全红——那是环境不是代码**
- 单元测试：`pytest backend/tests`（152 条）+ 前端 `npx tsc -b && npx vitest run`

---

## 项目目录结构

```
moz/
├── backend/                        # 后端（Python / FastAPI）
│   ├── server.py                   # FastAPI 主服务：路由、认证、SSE、Metrics 中间件
│   ├── emotion_graph.py            # 三 Agent 分工的流式编排 + 落库队列入口
│   ├── memory_manager.py           # 记忆系统核心：遗忘曲线、RRF 混合检索、Reranking、事实提取
│   ├── memory_layer.py             # 记忆分层：核心/重要/常规，遗忘强度与检索权重
│   ├── temporal_metadata.py        # 时间标签：时间表达识别、时间上下文提取
│   ├── user_profile.py             # 用户档案卡：结构化画像、自动更新
│   ├── working_memory.py           # 工作记忆：跨对话上下文持久化
│   ├── llm_config.py               # LLM 客户端工厂
│   ├── model_config.py             # 模型配置中心：Provider 检测、Key 解析
│   ├── model_presets.py            # 预设模型列表
│   ├── file_processor.py           # 图片处理：格式校验、多模态检测
│   ├── conversation_store.py       # 会话持久化（SQLite）
│   ├── migration_v2.py             # 数据库迁移脚本（v1 → v2：档案卡 + 分层 + 时间标签）
│   └── tests/                      # 单元测试（145 tests）
│       ├── test_core.py            # 遗忘曲线、Provider 检测、序列化
│       ├── test_api.py             # API 端点、SSE、速率限制
│       ├── test_rag.py             # RRF 检索、查询改写、Reranking、工作记忆
│       └── test_memory_upgrade.py  # 档案卡、时间标签、记忆分层、迁移脚本
├── tools/                          # 量数与门禁（一律用 .venv 里的 python 跑）
│   ├── selftest.py                 # 51 项快检门禁（不碰模型、不写用户数据；--full 走真实往返）
│   ├── bench_latency.py            # 端到端首字分布，按档报采样数
│   ├── probe_first_token.py        # 裸连中转：实际发出的键 + 5 段计时 + reasoning 字数
│   ├── compare_wording.py          # 两份沙箱开/关思考交替问，并排看原话
│   ├── profile_search.py           # 检索分段耗时 + 剪枝配对 A/B（结果逐条对拍，不花额度）
│   ├── sandbox_chat.py             # 隔离沙箱问答（答对/想不起来/答错/没回话）
│   ├── seed_ui_sandbox.py          # 给界面造可见数据，全程不碰 backend/moz.db
│   └── snapshot_data.py            # 数据快照与回滚（快照目录在仓库外）
├── docs/                           # 设计文档与实测账本
│   ├── 响应时间账.md               # 首字时间的口径、数字、被否掉的办法、旋钮
│   ├── 情感预热系统设计.md         # 三层情感信号（L0/L1/L2）设计
│   └── MODEL_CONFIG_GUIDE.md       # 模型配置指南
├── _overnight/                     # 交接文档与逐轮实测记录（冷启动先读 HANDOFF.md）
│   ├── HANDOFF.md
│   └── RUNLOG.md
├── frontend/                       # 前端（React / TypeScript / Vite）
│   └── src/
│       ├── App.tsx                 # 根组件（布局 + 认证门控）
│       ├── api.ts                  # API 调用层（含 SSE 流式）
│       ├── store.ts                # Zustand 状态管理
│       ├── types.ts                # TypeScript 类型定义
│       └── components/             # UI 组件（13 个，含记忆查看器）
├── .env.example                    # 环境变量模板
├── requirements.txt                # Python 依赖
└── README.md
```

---

## API 接口

### 对话

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|------|
| `POST` | `/api/chat/{user_id}` | 发送消息（SSE 流式返回） | Access Key |
| `GET` | `/api/conversations/{user_id}` | 获取对话列表 | Access Key |
| `GET` | `/api/conversations/{user_id}/{conv_id}` | 获取对话详情 | Access Key |
| `POST` | `/api/conversations/{user_id}` | 新建对话 | Access Key |
| `PATCH` | `/api/conversations/{user_id}/{conv_id}` | 重命名对话 | Access Key |
| `DELETE` | `/api/conversations/{user_id}/{conv_id}` | 删除对话 | Access Key |

### 用户与记忆

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|------|
| `GET` | `/api/users` | 获取用户列表 | Access Key |
| `POST` | `/api/users?user_id=xxx` | 创建用户 | Access Key |
| `GET` | `/api/memory/{user_id}/stats` | 获取记忆统计 | Access Key |
| `GET` | `/api/memory/{user_id}/detail` | 获取全部记忆详情（按层级分组） | Access Key |
| `GET` | `/api/memory/{user_id}/layers` | 获取各层级记忆统计 | Access Key |
| `PATCH` | `/api/memory/{user_id}/{memory_id}/layer` | 手动调整记忆层级 | Access Key |
| `GET` | `/api/memory/{user_id}/{memory_id}` | 获取单条记忆治理详情 | Access Key |
| `POST` | `/api/memory/{user_id}/{memory_id}/correct` | 修正并替换旧记忆 | Access Key |
| `DELETE` | `/api/memory/{user_id}/{memory_id}` | 软删除单条记忆 | Access Key |
| `POST` | `/api/memory/{user_id}/{memory_id}/feedback` | 提交记忆召回反馈 | Access Key |
| `GET` | `/api/memory/{user_id}/{memory_id}/history` | 获取等级变更审计 | Access Key |
| `PATCH` | `/api/memory/{user_id}/{memory_id}/grade` | 手动设置并锁定等级 | Access Key |
| `DELETE` | `/api/memory/{user_id}` | 清除所有记忆 | Access Key |
| `GET` | `/api/profile/{user_id}` | 获取用户档案卡 | Access Key |
| `PUT` | `/api/profile/{user_id}` | 手动更新档案卡 | Access Key |

### 配置与系统

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|------|
| `GET` | `/api/config/model` | 获取当前模型配置 | Access Key |
| `GET` | `/api/config/model-presets` | 获取预设模型列表 | Access Key |
| `GET` | `/api/config/prompt` | 获取当前人设 Prompt | Access Key |
| `PUT` | `/api/config/prompt` | 修改人设 Prompt | Admin Key |
| `GET` | `/api/logs` | 获取服务日志 | Admin Key |
| `GET` | `/api/metrics` | 服务指标（QPS / 延迟 / 错误率） | Admin Key |
| `GET` | `/api/health` | 进程健康检查（不调用 LLM） | 无 |
| `GET` | `/api/health/llm` | LLM 连通性检查（会发起一次请求） | Admin Key |

### SSE 事件格式

```json
{"type": "status", "text": "moz 正在感受你的情绪并回忆..."}
{"type": "token", "text": "你"}
{"type": "reply", "text": "完整的回复文本"}
{"type": "done", "conversation_id": "uuid"}
{"type": "error", "text": "对话处理失败，请稍后重试"}
```

---

## 核心算法：艾宾浩斯遗忘曲线

```
R = e^(-t/S)
```

- **R**：记忆保留率（0-1）
- **t**：经过时间（小时）
- **S**：记忆强度 = 基础强度 `0.3` + 重要性加成 `importance × 0.5` + 复习次数加成 `min(access_count × 0.15, 1.0)`

记忆生命周期：编码（事实提取 + 情感标注 + 程度评分 + `G0-G4` 分级 + 来源追溯）→ 存储（SQLite）→ 治理（去重、动态再分级、修正覆盖、反馈和软删除）→ 检索（RRF + Reranking，仅激活记忆）→ 降到最低权重（**不遗忘、不删除**：`prune_memories()` 只降权，归档永不物理删；唯一会转归档的是超过 5000 条软上限的性能阀门）。

---

## 开发指南

```bash
# 后端开发（热重载）
python backend/server.py

# 前端开发（HMR）
cd frontend && npm run dev

# 后端测试
cd backend && python -m pytest tests/ -v

# 长程记忆治理评测（离线、确定性）
python backend/memory_evaluation.py

# 前端测试
cd frontend && npx vitest run

# 代码质量
cd frontend && npm run lint && npm run build
```

### 量响应时间（Windows 上必须用 `.venv` 里的 python）

```bash
# 快检门禁：51 项，秒级，不碰大模型、不写用户数据（11 项走 HTTP，需要后端在跑）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/selftest.py

# 端到端首字分布（每档至少 3 次采样才允许当结论；花中转账度）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/bench_latency.py --sizes 10 --repeats 3

# 裸连中转：看实际发出的键 + 5 段计时 + reasoning 字数；--hedge-ab 配对量"补打一枪值不值"
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/probe_first_token.py

# 检索分段与剪枝配对 A/B（不花额度，结果逐条对拍）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/profile_search.py
```

> 量数的四条纪律（配对交替、桩不许替换被测对象、绝对毫秒阈值不稳、别边改代码边测量）
> 写在 `docs/响应时间账.md` §7，都是踩出来的。

---

## 常见问题

**Q: 启动报错「未找到 API Key」？**
A: 检查 `.env` 文件，确保至少配置了当前模型对应的 API Key。

**Q: 记忆检索效果不好？**
A: 需配置 `ZHIPU_API_KEY`（语义向量检索）。未配置时降级到纯关键词匹配，检索效果会下降。
**注意**：这台机器上向量令牌目前是 **401 永久失效**的状态，属预期——她会自动退避
（120 秒起、越退越长、封顶 30 分钟，成功一次复位），期间检索走关键词那一路，**不会每轮白等一次失败请求**。

**Q: 切换模型后 Embedding 报 401？**
A: Embedding 使用独立的 `EMBED_PROVIDER`（默认 `zhipu`），与对话模型解耦，需单独配置 `ZHIPU_API_KEY`。

**Q: 流式回复卡住？**
A: 先看 `GET /api/metrics` 的 `first_token_stages`（各段分开），确认等在中转（`relay_ttfb`）
还是本地（`local_prep`）——**这两个数不是一回事**，别拿"只量中转"的数当界面秒表。
已知：每个后端起来之后的**第一句**要 17~50 秒（原因未查，见 `docs/响应时间账.md` §6），
同批后面几句 0.8~2.1 秒。对话过长可开启新对话。

---

## 贡献指南

1. Fork 本项目
2. 创建功能分支：`git checkout -b feature/your-feature-name`
3. 提交代码：`git commit -m '描述你的改动'`
4. 推送并创建 Pull Request

PR 提交前请确保：前端 `npm run lint && npm run build` 通过，后端 `python -m pytest tests/ -v` 通过。

---

## License

[AGPL-3.0License](./LICENSE) | Copyright (c) 2026 wenbo-zhang1
