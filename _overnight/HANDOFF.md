# moz 交接文档

写于 2026-09-27 20:30 前后　·　接手时最新提交：`675e01c`（事件先后链），工作区干净
这是"冷启动读这一份"用的地图；每一轮的来龙去脉、实测数字和踩坑细节都在 **`_overnight/RUNLOG.md`**（页尾最新，当前到第七轮）。

---

## 0. 这个应用是什么（30 秒）

本地单用户的 AI 陪伴应用。卖点只有两个：**长期记忆** 和 **像人一样主动关心**。
不是 SaaS：没有登录、没有管理员端、没有多租户；界面只有一个用户（`web_user_001`）。
形态：FastAPI 后端 + React/Vite 前端（PWA，可装到桌面）+ Windows 托盘。
**用户不看代码，只看界面和感觉**——所有判断最终都要落到"这话像不像人说的""这条记忆对不对"。

## 1. 现在跑着什么 / 怎么起

| 端口 | 是什么 | 备注 |
|---|---|---|
| 127.0.0.1:8000 | 后端 `uvicorn server:app --reload` | 改任何 `.py` 都会重启 |
| 127.0.0.1:3000 | 前端 Vite dev server | PWA 就装在它上面 |

日常启动：双击项目根的 **`moz-app.bat`** —— 它只在端口没被占时启动后端和前端、开一个无地址栏的应用窗口、
并拉起托盘（托盘自带互斥锁，重复启动会自己退出，所以看到两个 pythonw 进程是正常的）。
`--reload` 的代价必须记住：**重启会把进程内状态清零**——当天的主动关心额度、"话多话少"的观察、
`MemoryManager` 的记忆缓存全部重来。所以别假设后端连续跑着，也别在测时间相关行为时随手改 `.py`。

## 2. 门禁：四条命令，全绿才提交

```bash
# ① 后端全局自测（27 项快检，秒级，不碰大模型、不写用户数据）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/selftest.py
# 想连真实对话/中转看图一起验：--full（31 项，慢、耗额度；跑完自己擦痕迹）
# ② 后端单元测试（122 条）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest backend/tests -q
# ③④ 前端
cd frontend && npx tsc -b && npx vitest run
```

三个必须知道的细节：
- **必须用 `.venv/Scripts/python.exe`**。用系统 python 会报一堆假的 `ModuleNotFoundError: langchain`。
- 两次自测之间**隔 60 秒以上**，否则会被自己的限流打掉（`限流不误伤本地` 那项自己就连发 80 次）。
- pytest 是这一轮才补进门禁的：它之前一直不在门里，结果藏着一条从几轮前就失效的断言（429 文案）。
- 当前基线：27 项 **0 失败 / 1 警告**、122 passed、50 passed、tsc 无输出。唯一那条 warn 见 §8 第 4 条。

## 3. 六条铁律（用户当面交代过，违反＝返工）

1. **绝不碰用户真实数据**：`backend/moz.db`、`backend/conversations/`、`backend/memory_store/`、
   `backend/runtime_model_config.json`、`backend/saved_models.json`（含真实 Key）。
   要写库的测试一律 skip 或 `try/finally` 还原；安全网是 `tools/snapshot_data.py --list / --restore <名字>`。
2. **进展一律写进 `_overnight/RUNLOG.md`**，不要新建规划类文档（本文件是用户点名要的）。
3. **每修一条：跑门禁 → git 提交**。仓库没配身份，用
   `git -c user.name="moz-dev" -c user.email="moz-dev@local" commit`。
4. Windows：**`.bat` 只能纯 ASCII**；含中文的 `.ps1` 必须 UTF-8 带 BOM；Git Bash 会吃掉 `/c` 和 `/tmp`；
   控制台打中文要 `PYTHONIOENCODING=utf-8`。
5. 中转很慢（一句正常回答 25~135 秒）且偶发连接失败：**报错先怀疑网络，再怀疑代码**。
   别给视觉请求加"看不清就别回答"这类约束——实测加了之后它对所有图都改口说看不清，功能等于废掉。
6. 全程不要问用户"能不能/要不要"这类执行细节——他要的是你把事情做完并且合格（见 §9）。

## 4. 代码地图（后端 24 个 py，按职责分三组）

**长期记忆**
- `memory_manager.py`（近 2400 行，核心）：`add_memory` / `search_memories`（多路改写 → 语义+关键词 → RRF → rerank）/
  容量策略（`MAX_ACTIVE_MEMORIES=5000`、归档默认永不硬删）/ 遗忘曲线。
  检索缓存两件套：`_keyword_derived`（每条记忆的分词/词频/清洗正文）+ `_search_index_cache`（向量矩阵），
  作废统一走 `_invalidate_search_cache()`——**任何改记忆状态/重要度/情感的地方都必须调它**（第五轮踩过）。
- `memory_governance.py`：归一化、Grade/Degree 打分、频率分。`memory_layer.py`：层级与遗忘强度。
- `memory_consolidation.py`：合并碎片（会调模型）。`temporal_metadata.py`：相对时间解析（**能力弱**，只认词表，
  解不了"一周后"）。`memory_evaluation.py`：确定性离线评测（沙箱做法的范本：临时库 + embedding 打桩）。

**主动关心（这一夜的主战场）**
- `care_store.py`：`care_items` / `proactive_queue` / `care_log` / `care_settings` 四张表 + 两个独立开关
  （`remind_events` 到点提醒 / `initiate_chat` 平时搭话）+ `talk_score` 观察。
- `care_engine.py`：**唯一判定者**。`collect()` 列候选（生日/事项/带伞/开放话题/链）→ `tick_once()` 过三道闸
  （开关、安静时段、配额与 90 分钟间隔）→ `care_loop()` 60 秒一跳。措辞 `_polish()`（模型）/ `_template()`（兜底）。
- `care_extractor.py`：从对话里自动"记下该惦记的事"。模型主路径 + `_fallback_items()` 规则兜底（分句扫）。
- `care_graph.py`：**事件关联图**（第六、七轮新增）。节点 `item:` / `memory:`，枢纽 `person:` / `topic:`，
  表 `care_links` + 水位表 `care_graph_state`。三种关系：`about`/`topic`（枢纽边）、`together`（同场提及）、
  `after`（**有方向的先后链**：src 发生在 dst 之后 `offset_days` 天）。
  三条不能退让的设计：抽枢纽**纯规则不调模型**；`HUB_DEGREE_CAP=60` 让满库都有的词当不了桥梁；
  `reap()` 保证用户删掉的记忆/事项**必须断边**。
- `working_memory.py`：开放话题 `OpenLoop`（和链是一对容易打架的话源，`care_engine` 里已做"链在场就压掉 open_loop"）。
- `weather.py`：只能用腾讯 `wis.qq.com`（免 key）；Open-Meteo / wttr.in / ip.sb 从本机**全部超时**，别再往它们身上设计方案。

**对话与配置**
- `server.py`（1.5k 行）：所有接口。`/api/care/dry-run` 是"看它此刻会说什么"的演练口（不写库、不调模型）。
- `emotion_graph.py`：LangGraph 工作流。注意 `_background_save()` 是**串行 4 次模型调用**（工作记忆→长期记忆→档案卡→关心抽取），
  实测一轮要 3~7 分钟——§8 第 1 条就是这个。
- `llm_config.py` / `model_config.py` / `model_presets.py` / `llm_errors.py`：模型链路、"我存的模型"、错误翻译。
- 其它：`conversation_store.py`、`summary_service.py`、`user_profile.py`、`file_processor.py`、`tray_app.py`。
  托盘在 Windows 上**一次启动会有两个 pythonw 进程**（父进程跑逻辑，子进程托管图标），不是重复轮询，别去"修"。

## 5. 这一夜做了什么（按轮次，细节在 RUNLOG）

| 轮 | 做了什么 | 关键提交 |
|---|---|---|
| 一~二 | 修好发图必 400、长对话静默失忆、点开「认知」整个应用崩；报错翻译成人话 | `97ec4e2` `34116bd` |
| 三 | 6 项产品问题：图片措辞去承诺、主动关心拆两开关、术语统一、冷启动与空态、清杂物、100vh→100dvh；**并发现 --full 探针污染了用户真实库并清干净** | `c5c4ef3`…`37741e4` `d55abe2` |
| 追加 | 模型可以存好几套、模型列表不再挤成一条、后端没开时第一屏就说"双击 moz-app.bat" | `48968f5` `fee022c` `5cce02b` |
| 四 | 记忆天花板实测（20 万条 8.8s/765MB，线性，召回不掉）；**发现 300 条上限会悄悄归档再物理删除用户历史 → 抬到 5000、默认永不硬删** | `8993f50` |
| 五 | 检索提速：**先分段量才发现大头是向量服务 401 风暴（每轮 338ms）不是算法** → 失败冷却 + 关键词索引按用户缓存；400 条一轮 470ms→7ms，5000 条 440ms→134ms；**排序一个字符没改**（两道等价性证明） | `649524f` |
| 六 | 事件关联图：到点提醒不再孤零零，措辞带上下文（"关于妈妈：用户的妈妈喜欢养花"） | `8e3fda6` |
| 七 | 事件先后链：`答辩 → 7 天 → 出结果`，到点会问"上次那事儿后来怎么样了"；规则兜底 + 模型字段两条路 | `675e01c` |

## 6. 数据现状（别被空库吓到）

**用户目前一条长期记忆都没有**（`memories` 表 0 行、`conversations` 1 行）。
比对 `_overnight_backup` 里 7 个快照确认过：最早那个也是 0，中间出现过的 14 条全是
`[对话摘要] 用户说：只说颜色名…` 这类自测探针垃圾——**历次清理没删掉用户任何东西**。
从这一刻起写进去的记忆才是真的开始攒。

新表 `care_links` / `care_graph_state` 由 `CareGraph._init_db()` + `_migrate()` 自动建/加列，不需要人工迁移。
快照目录在**仓库外**：`项目/机器人/_overnight_backup/`；`tools/snapshot_data.py --label 名字` 手动存一份。

## 7. 复现过的坑（下次别再怀疑代码）

1. `MemoryManager` 把记忆缓存在进程里：**直接 DELETE 数据库里的记忆，界面还是旧的** → 走 `DELETE /api/memory/...` 或重启后端。
2. `_match_score` 这类"每条记忆都要调一次"的函数里**不能有查询侧的重复构造**（第五轮曾因此从 103ms 涨到 578ms，加 `lru_cache` 才回落）。
3. 不缓存的分支里若还照旧写缓存字典，等于每次查询白建一个几万条的临时字典（20k 那档从 718ms 变 822ms）。
4. 三个会**静默吃掉链**的坑（都写在自己的代码里，靠测试才发现）：整批重写枢纽边时把链边一起删；"非 together 即枢纽"的写法把链边当枢纽；后台抽取拿不到图对象。
5. jsdom：`innerText` 未实现（用 `textContent`）、`location.reload` 不可覆盖（`vi.stubGlobal`）。
6. Vite 的 `import.meta.glob` 对 `?raw` 的 `.css` 返回空串 → 曾造出一条"内容为零也算通过"的假测试；文本检查改到 python 侧做。
7. ctypes：`GetCurrentProcess.restype` 必须是 HANDLE；psapi 要声明 `argtypes`，否则 RSS 永远 -1。
8. 子进程日志默认**块缓冲**，`terminate()` 会丢尾部 → 沙箱工具现在带 `PYTHONUNBUFFERED=1`。
9. `async with`/`python - <<'PY'` 在这里会挂；临时脚本一律用 Write 工具落文件，路径用正斜杠绝对路径。

## 8. 没做完的事（按优先级，每条给了从哪下手）

1. **P0｜后台落库要 3~7 分钟，这期间重启就"聊完白聊"**。`emotion_graph._background_save()` 串行 4 次模型调用，
   `--reload` 一重启，这轮的记事和链全丢。要么并行化四步，要么落一个持久化队列（重启后续跑）。
   验收：说完一句带生日的话 → 立刻改 `.py` 触发重启 → 重启后仍然记下了事、建出了链。
2. **P1｜记忆质量两条**（RUNLOG 第四轮 §4 有实例）：同一件事存成 2~3 条近义重复（这台机器没有 embedding，去重只剩字面归一化）；
   **用户的提问原文和模型自己的回答也被当成长期记忆**（以后可能把"我问过团子叫什么"当成用户的事实，甚至套娃引用自己的回答）。
   最小做法：`[对话摘要] 用户说：…？` 这类以问号结尾的条目不该进长期记忆；检索时对"提问形状"的记忆降权。
3. **P1｜关联和链在界面上看不见**。后端接口已备好：`GET /api/care/related?user_id=&type=item&id=`、
   `GET /api/care/graph`、`POST /api/care/graph/sync`、`POST /api/care/dry-run`（会回显 `context`/`chain`）。
   落点是 `frontend/src/components/CareSection.tsx` 的每条事项下面加一行"这件事还连着…"。
4. **P2｜遗忘曲线的归档分支是死的（等用户拍板）**：`decay_importance` 的地板正好等于 `prune_memories` 的阈值 0.1，
   判据是严格小于 → 永远不成立，所以"不重要的事会慢慢淡忘"这句对外说法，实现上只会降到地板不会真忘。
   两种改法：地板降到 0.05，或阈值提到 0.12。selftest 以 warn 记着，**别顺手改**。
5. **P2｜自动滚动的"动的那半"没人真验过**：moz 回话那 20~135 秒里用户正在打字时，新回复会不会被顶出屏幕。
   静态布局在真视口看过没问题；这条**必须真人试一次**才算数。
6. **P2｜`限流不误伤本地` 会污染紧接着跑的检查**（自己连发 80 次打满 IP 桶）。挪到全部检查最后，或改成查 `/api/metrics`。
7. **P3｜头像 404 噪音**：没设头像时 404 是正常回落，看着多是 dev 模式 StrictMode 双挂载。
   要收敛就 404 时直接回 `{"avatar": null}`，前端不用 catch。
8. **P3｜仓库根目录有 11 张截图 + 2 个日志被 git 跟踪**（`final_*.png`、`screenshot_*.png`、`frontend-*.log`）。
   要不要清**先问用户**——他可能正拿这些做参赛材料。
9. **明确不做过、需要有决定才动的**：真倒排索引（能把打分循环从 O(全部记忆) 拿掉，但**必须改排序**）；
   事件之间的因果/条件边（"如果 A 就 B"）；把关联图画成前端图。

## 9. 和这位用户协作的方式

- 他不写代码，**用中文短句驱动**（"这都挤到一起了""写一个交接文档"），期望你自己判断、自己修、迭代到合格。
- 交付物要**能直接看**：界面改动用浏览器/应用自身导出验收，别只说"类型检查过了"。
- 就地改原文件，不要另存副本；**结论要带实测数字**，他信数字不信形容词；上一轮被推翻过的结论要明说推翻了自己。
- 他还有另一条交付线：**专利与竞赛材料**（docx，用 WPS 肉眼验收），和代码门禁是两套东西。
- 有分歧时优先保守：产品对外说法（"会慢慢淡忘""能看图"）和代码行为不一致时，**先报告再动手**，别偷偷改行为。

## 10. 接手后先跑这三条（1 分钟）

```bash
curl -s http://127.0.0.1:8000/api/health                                 # 后端活着（注意是 /api/health，根路径 404）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/selftest.py   # 27 项门禁应 0 失败
curl -X POST "http://127.0.0.1:8000/api/care/dry-run?user_id=web_user_001"   # 看它此刻会主动说什么
```

然后读 `_overnight/RUNLOG.md` 的第七轮。有问题先查 RUNLOG 再查代码——这一夜踩的坑大多已经写在那里了。
