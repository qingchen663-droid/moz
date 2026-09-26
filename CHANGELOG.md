# 更新日志

所有显著变更都会记录在本文件中。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [v0.3] — 记忆治理与巩固

### 新增
- **总结记忆金字塔**（summary_service）：会话摘要持久化 SQLite；同周 ≥3 条 session 聚合为 weekly 周记，≥4 条 weekly 级联为 monthly 月记；prompt 按 月→周→会话 注入
- **记忆巩固引擎**（memory_consolidation）：相似片段聚类（余弦 ≥0.55）后由 LLM 合并为整合记忆；容量阈值触发 巩固→归档→清理 三层自动维护
- **开放话题 OpenLoops**（working_memory）：结构化待跟进事件，24h 后自然追问，30 天自动过期
- **数据自主权 API**：全量导出（对话/记忆/档案/工作记忆）与管理员导入恢复
- **可控性 API**：记忆反馈、等级调整审计、手动巩固 + 总结级联端点
- 检索优化：NumPy 批量矩阵余弦 + 缓存索引；FTS5 会话全文搜索
- 前端：总结标签页、导出/导入按钮、生成停止按钮

### 变更
- 长期语义检索从逐条循环改为批量矩阵运算
- open_topics 存储结构升级为结构化 dict（兼容旧字符串格式）

## [v0.2] — 多层认知记忆系统

### 新增
- 用户档案卡（user_profile）：identity / preferences / relationships / emotional_profile 自动提取并注入
- 记忆分层（memory_layer）：核心 / 重要 / 常规三层差异化遗忘曲线
- 记忆时间标签（temporal_metadata）：识别中文相对时间表达
- 记忆查看器：档案卡 + 全部记忆（层级/情感/时间标签/巩固状态）
- 情感分析语境翻转与情感标签传递
- 档案卡 API、记忆详情 API、层级管理 API、数据库迁移脚本

### 移除
- Mem0 云端记忆依赖

## [v0.1] — 认知记忆系统

### 新增
- 三 Agent 协作工作流（情感分析 / 记忆检索并行 + 对话生成）
- RRF 混合检索与多特征 Reranking
- 工作记忆跨会话持久化
- 流式逐 Token 输出
- 自定义人设、多模型支持、深度思考模式、多模态图片理解
- 双级密钥认证
