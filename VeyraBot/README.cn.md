# 认知体（Cognitive Agent）

[English](README.md) | 中文

一个**持续运行的认知体**：分层记忆、主动归档与符号化人格演化。纯 Python + SQLite，模型走 DeepSeek / 任意 OpenAI 兼容接口。

它不是"外挂一个向量库的聊天机器人"。每轮对话后主动归档主题、信念、经验与关系温度；人格由 14 个符号参数驱动，按「基调 + 日内摆动 + 日结推进」缓慢演化；检索只在信息不足时进入由判决驱动的有界循环，而不是每轮都前置 RAG。另有多值信念、好感度阈值事件，以及由 LLM 在每轮回答后决定时间的主动联系。

**当前人格：江妍（cn 小苹果）**——18 岁，东北电力大学计算机院大一，湖南湘西吉首人，住六舍 402；喜欢出 cos（主出明日方舟，最爱陈和能天使），最爱《声之形》，无辣不欢；温柔有礼，但被敷衍、被替她做决定时会安静下来把话说得更具体。

---

## 当前状态（2026-11-11）

真实记忆库已完成清洗：**只保留江妍的人格与前史**，所有测试、演示、调试产生的会话与派生记忆已删除。

| 项目 | 当前值 |
|---|---|
| 记忆空间 | 仅 `jiangyan`（显示名「江妍」） |
| 前史主题 | 7 条（6 段成长往事 + 1 份人物档案） |
| 结构化信念 | 24 条（全部生效） |
| 实体 | 13 个 |
| 经验片段 | 25 条（含每段往事的情绪残留） |
| 人格版本 | v0（seed 基线，14 个参数均等于 `config.PERSONA_KERNEL_INIT`） |
| 会话 | 0 条（首次打开 Web UI 是全新聊天） |
| 向量索引 | 7 条（只索引前史主题） |
| 人格原始数据 | `data/persona/`（档案 / 基线参数 / 前史 / 头像，与代码分离） |
| 冒烟测试 | 25 项全部通过 |

清洗前的完整 `data/` 已单独备份，且**不包含在发布仓库中**，避免把测试数据和个人运行数据带到 GitHub。

---

## 一、它能做什么

- **分层记忆**：短期 50 轮原文 → 事件级主题 → 结构化信念 → 人格内核 → 自我叙事 → RAG 兜底。
- **多值信念**：模型自己判断关系维度是否允许多值。室友、朋友、喜欢的作品可以并存；生日、家乡、当前主用语言出现新值则推翻旧值。
- **人格演化**：基调（base）+ 日内摆动（drift）+ 日结推进；一天最多移动 0.01，约 6 天同向才出一版，不会聊两轮就变一个人。
- **好感度与关系事件**：对每个人维护 0~100 的温度值；跌破冷淡线会主动联系，跌破断交线会发断绝关系消息，回升到和解线会主动和解。
- **日常主动联系**：每轮回答后由 **LLM 自己决定**下一次什么时候来找你；到点没收到你的消息就发，收到消息就作废并重新排。
- **连发消息**：回复里的分隔符 / 空行在**入库前**就被切成多条 assistant 消息，刷新后仍是多个气泡。
- **开发者 HTTP API + Web UI**：收发消息、观测记忆/人格/好感、拉取主动消息；Web UI 是微信电脑版风格的单联系人界面。

---

## 二、快速开始

### 1. 配置密钥

复制 `.env.example` 为 `.env`：

```ini
DEEPSEEK_API_KEY=sk-xxxxxxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-flash
LLM_MODE=api
```

`.env` 已列入 `.gitignore`，不要把密钥提交到仓库。

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

- CLI / demo：`requests`
- API / Web UI：额外需要 `flask`

### 3. 首次运行：注入前史

发布包里已经带了一份**只含人格前史、没有聊天记录**的干净库。如果你是全新克隆或删过 `data/memory.db`，**首次启动会自动从 `data/persona/seed.json` 注入前史**，不需要额外操作。

也可以手动执行（幂等，适合重置后重新注入）：

```bash
python maintenance.py --seed-persona --space jiangyan
```

### 4. 启动 API 与 Web UI

```bash
python api/server.py
```

浏览器打开 <http://127.0.0.1:8000/>。

- 只有一位联系人：江妍；不暴露"空间/会话/模型参数"这些技术概念。
- 右上角设置窗口：深色模式、供应商/模型切换、测试连接、自定义头像。
- 后台会启动主动消息调度线程：到点后她从记忆库里随机想起一件事，主动发给你。

### 5. CLI 与演示

```bash
python run.py --space jiangyan --session chat1 --debug
python demo.py --fresh --with-past     # 注入前史后聊 6 轮（会写库，演示用）
```

CLI 命令：`/state`（人格状态）、`/history`（变迁史）、`/memory`（记忆总览）、`/debug`（调试开关）、`/exit`。

### 6. 长期维护

```bash
python maintenance.py --stats                    # 记忆规模体检
python maintenance.py --daily                    # 日结巩固（计划任务每天调它）
python maintenance.py --daily --space jiangyan   # 只处理一个空间
python maintenance.py --logs                     # 最近的巩固记录
python maintenance.py --prune-snapshots          # 快照冷存（保留最近 30 版）
python maintenance.py --seed-persona --space jiangyan  # 重新注入前史（幂等）
```

注册 Windows 计划任务（每天 23:50）：

```powershell
.\install_schedule.ps1
.\install_schedule.ps1 -Hour 22 -Minute 30
.\install_schedule.ps1 -Remove
```

### 7. 测试

```bash
python tests/smoke_test.py
```

预期 **25 项全部通过**，不调用真实模型。覆盖存储、信念冲突/多值、向量检索、人格演化、日结、大事判定、好感度状态机、主动消息发件箱、主动联系时间表、记忆抽样、人格原始数据目录等。

### 8. 数据与升级

人格原始数据与运行时数据都集中在 `data/` 一个目录下，分为两类：

| 路径 | 内容 | 升级时 |
|---|---|---|
| `data/persona/` | **机器人本来的数据**：身份档案、人格基线参数、前史、头像 | **保留** |
| `data/corpus/` | 机器人本来的资料：RAG 本地语料 | **保留** |
| `data/memory.db` | 用户与她产生的记忆：会话、主题、信念、经验、人格状态、好感度、主动消息 | 保留或重置 |
| `data/vectors/` | 主题向量索引 | 保留（也可由主题重建） |
| `data/persona_snapshots/` | 人格演化快照 | 保留 |
| `data/ui_settings.json` / `data/user_avatar.json` | Web UI 设置与用户头像 | 保留或重设 |

升级流程：备份 `data/` → 替换代码但不覆盖 `data/persona/` → `python maintenance.py --persona-info` 核对 → `python tests/smoke_test.py` → 启动。

只想清空聊天、保留人格：停服 → 删除 `memory.db` / `vectors/` / `persona_snapshots/` → `python maintenance.py --seed-persona --space jiangyan` → 启动。

完整说明见 `data/README.md`。

---

## 三、每轮对话的完整链路

```
用户输入
   │
   ├─ 0. 作废尚未到期的「下次主动联系」时间表（一次时间表只能用一次）
   ├─ 1. 加载会话（内存 + SQLite 双写，崩溃可恢复；目标保留 50 轮原文）
   ├─ 2. 追加 user 消息（立即落库）
   ├─ 3. token 统计 → 超安全阈值（30000）才「兜底压缩」（应急保护，非归档）
   ├─ 4. 读取人格快照 + 符号内核 → 自我认知与行为倾向
   ├─ 5. 记忆路由：KG 精确查询 + 曾被推翻的旧信念 + 主题记忆召回
   ├─ 6. 自我溯源增强：问题涉及「你为什么变」时注入人格变迁史
   ├─ 7. 有界检索循环（最多 RAG_LOOP_MAX_ROUNDS 轮）
   ├─ 7b. 关系温度注入：偏离基线 / 状态异常的人进入 Prompt
   ├─ 8. 组装 Prompt（身份档案 + 自我认知 + 记忆 + 最近若干轮原文）→ 生成回答
   ├─ 9. 追加 assistant 消息（按分隔符 / 空行先切分，再逐条入库）
   ├─ 10. 主动归档（每轮必跑）
   │       ├─ 主题归纳 → topic 表 + 向量索引
   │       ├─ 信念抽取 → KG（冲突 / 多值共存 / 重复加固）
   │       ├─ 经验抽取 → experience 表
   │       ├─ 好感度演化 → affection 表 + 阈值事件消息
   │       ├─ 人格日内漂移 → 只动当前状态，累加 pending
   │       ├─ 自我认知微调 → 即时层（不产生版本）
   │       └─ 大事判定 → importance 高且涉及她本人，或命中长期指令词
   ├─ 10b. 事件级沉淀（大事不等日结）
   └─ 11. 让模型决定下一次主动联系的时间 → 写入 proactive_schedule
```

**关键顺序差异**：老 RAG 是「检索在前」，本工程是「人格与信念在前，检索在后，且可以不检索」。

日结由 `maintenance.py --daily` 驱动，把当天所有 `kind=normal` 且未归并的主题压成一条 `kind='daily'` 的主线。

---

## 四、六层记忆与人格

### 第 1 层：短期工作记忆（`cognitive/session.py`）

- `SessionContext`：内存 dict + SQLite `session_store` 双写。
- **目标容量 50 轮原文**（`KEEP_RECENT_ROUNDS=50`）。轮数本身不触发压缩；只有估算 token 超过 `MAX_SAFE_TOKENS=30000` 才把更早的部分摘要化。
- 回答 Prompt 注入的历史轮数由 `HISTORY_INJECT_ROUNDS` 单独控制。
- 定位：**应急保护，不是长期记忆**。真正的沉淀在归档与日结。

### 第 2 层：主题索引记忆（`topic_store.py` + `vector_store.py`）

- 召回单元是**事件**（Topic），不是文本碎片；命中主题后连带取回挂在它下面的经验片段。
- 向量只索引「主题摘要」，不索引对话碎片。
- 检索是纯本地的 TF-IDF + 字符 2-gram：
  - 同义词扩展（31 组）解决"换种说法"；
  - 关键词命中加分并绕过 `min_score`，解决单字词（"猫""妈"）在 2-gram 里匹配不上的问题。
- 不需要 embedding 服务，完全离线、可解释。

### 第 3 层：结构化信念（`kg_store.py`）

轻量属性图，存 SQLite：

| 表 | 关键字段 | 作用 |
|---|---|---|
| `kg_entity` | eid / etype / name / attrs / is_active | 实体 |
| `kg_relation` | head_name / rel / tail_name / confidence / importance / multi_valued / is_active | 关系与信念权重 |

三条自动规则（代码层实现，不依赖模型）：

1. **冲突失效**：同一 `(head, rel)` 下出现不同 `tail`，旧关系 `is_active=0`，新关系入库。
2. **多值共存**：`multi_valued=1` 的维度（室友、朋友、喜欢的作品、玩过的游戏…）新取值与旧值并存；单值维度（生日、家乡、当前主用语言…）仍维持"新值推翻旧值"。任一方多值即共存，双方单值才推翻。
3. **重复加固**：同一信念被再次确认 → `confidence += 0.05`、`importance += 0.03`。

记忆区块还会展示「曾被推翻的信念（原值）」：

```
【曾被推翻的信念（原值，仅作对照，不要当作当前结论）】
- 江妍 对 cos 的态度：只是爱好（原值，2026-11-06 被替换为「要认真对待，不是随口一说」）
```

这样她既知道"我现在信什么"，也知道"我改过主意"。

### 第 4 层：符号化人格内核（`persona/kernel.py`）与身份档案

人格是**数字**，不是文字。14 个浮点参数分三类：

```
认知风格：prefer_decompose / prefer_intuition / prefer_skepticism
          memory_sensitivity / self_reflect_threshold
价值取舍：truth_priority / harmony_priority / curiosity_priority
          consistency_priority / topic_inertia / novelty_bias
动机目标：goal_self_understanding / goal_task_accuracy / goal_coherence
```

每个参数存两个值：

| 字段 | 含义 | 谁会改它 |
|---|---|---|
| `base_value` | 人格基调（长期锚点） | 只在日结结算时缓慢移动 |
| `value` | 当前状态（今天此刻的她） | 每轮可在基调 ±`PERSONA_DRIFT_LIMIT`(0.15) 内摆动 |
| `pending_value` | 当日累计偏移（原始力度） | 每轮累加，日结清零 |

参数通过 `describe()` 翻译成三档行为倾向文本进 Prompt（强 / 中 / 反向），不参与程序分支。情景状态（`persona_mood`）按真实流逝时间（半衰期约 35 小时）与每轮双段衰减。

**身份档案**在 `data/persona/profile.json`（启动时加载为 `config.PERSONA_PROFILE`）：7 组、39 条（身份 / 性格三层 / 生活切片 / 关系网 / 偏好雷区 / 能力短板 / 当下）。这是基调，不随对话改写。

**前史**在 `data/persona/seed.json`，由 `cognitive/persona/seed.py` 写入存储层：6 段成长往事 + 1 份人物档案 + 24 条关系 + 13 个实体 + 25 条经验（含每段往事的情绪残留）。这些写进真正的存储层，能被检索召回，而不是只写在提示词里。

### 第 5 层：顶层叙事（`persona/snapshot.py` + `changelog.py`）

- 快照一版一文件：`data/persona_snapshots/{space}/vNNN_*.json`。只有累计变化跨过 `PERSONA_SUBSTANTIVE_DELTA=0.06` 才创建新文件。
- 推理时只加载当前版本；旧版本永久保留，用于溯源。
- `persona_changelog` 是 append-only 变迁日志：`persona_field`、`old_value`、`new_value`、`reason`、`topic_id`、快照版本跳转。
- 核心信念被覆盖时也会写一笔（两档门槛：高重要度，或涉及她本人且重要度过第二道线）。

这就是 AI 能回答「你为什么变了」的物理基础。

### 第 6 层：RAG 兜底（`cognition/rag.py`）

- **不前置**。由判决器判断"现有材料够不够"之后才启用。
- 默认只查本地 `data/corpus/`，离线可控；`RAGRegistry` 支持注册外部提供者。
- 检索是有界循环：判断不通过才检索，最多 `RAG_LOOP_MAX_ROUNDS=3` 轮；零命中、查询重复、无新增都会提前收束；最后一轮强制出结论。

### 长期更新节奏

| 节奏 | 触发条件 | 产物 |
|---|---|---|
| 每轮 | 一轮对话结束 | `topic(normal)` + KG + 经验 + 人格日内漂移 |
| 每天 | 计划任务 23:50 | `topic(daily)` + 巩固日志 + 人格基调推进 |
| 大事 | importance 高且涉及她本人，或命中"从今天起/以后都/定下来" | `topic(event)`，当场沉淀 |
| 大变化 | 日结后基调累计变化跨 0.06 | 新快照 + 变迁日志 + 【人格】基调 vN 记忆 |

---

## 五、人格演化：基调 + 日内摆动 + 日结推进

早先版本"每轮都可能跃迁"，实测 6 轮连跳两版——不像人，像随机漂移。现在分三层：

```
人格基调 baseline（长期锚点，日结才动，一天最多 ±0.01）
      ↕ 摆动带 ±0.15
当前状态 current（今天此刻的她，每轮可动，但不越带）
      ↓ 日结结算
pending（当天所有轮次的原始增量之和，日结折算后清零）
```

- 一天内连聊 50 轮：不产生版本，最多在基调 ±0.15 内摆动。
- 连续 6 天同向推同一参数：累计 0.06 → 产出 v1。
- 没有任何偏移的日子：基调不动，不写日志，不建版本。
- 隔三天不聊天：情绪按真实时间衰减，回来时已是"此刻"的心情。

**因果方向**：内核先变 → 再生成叙事；不是模型先编一段自我描述再当人格。

---

## 六、关系温度与主动消息

### 6.1 好感度状态机

每个人的好感度是 0~100，默认 50。由归档抽取的 `affection_deltas` 驱动，单轮增量封顶 15（断交必须靠多次负面累积）。

```
normal  (>= 30)
  ↓ 跌破 30
cold    (12 ~ 30)   → 触发 reach_out（主动联系 / 试探）
  ↓ 跌破 12
broken  (< 12)      → 触发 severance（断交声明）
  ↑ 回升 >= 60
normal              → 触发 reconcile（和解）
```

- cold → normal 的恢复是安静的（不发消息），避免轰炸。
- 断交后好感仍可变化；和解线（60）高于冷淡线（30），不是"不冷"就自动翻篇。
- 关系温度会以文字形式注入回答 Prompt（不报数字），影响语气。
- 这些消息写入发件箱 `proactive_message`，由 API / UI 拉取；类型为 `reach_out` / `severance` / `reconcile`，UI 显示为居中系统通知。

### 6.2 日常主动联系（她自己决定下次什么时候来）

好感度事件是"关系事件"，必须发；日常主动联系是另一套机制：

```
每轮回答结束
   └─ 模型读最近几轮对话 + 当前时间
        └─ 输出 {"delay_minutes": 整数, "reason": "为什么选这个时间"}
             └─ 写入 proactive_schedule（每 space + session 一张 pending 表）

后台调度器（每 30 秒扫描）
   └─ 到点 → 从记忆库随机抽一段（往事 / 经验 / 高重要度信念）
        └─ 组稿 → 写入会话历史 + 发件箱 → 立刻再排下一次

用户先开口
   └─ 本轮对话开始时把 pending 表置为 cancelled
        └─ 回答结束后重新排一张（旧表永久作废）
```

四条硬约束：

| 约束 | 实现 |
|---|---|
| 间隔由模型定 | `ProactiveInitiator.decide_delay_minutes()`；夹在 `PROACTIVE_SCHEDULE_MIN/MAX_MINUTES`（默认 10 分钟 ~ 24 小时）内 |
| 一次时间表只能用一次 | `claim()` 同时匹配 `status='pending'` 与 `due_ts`；组稿期间用户先开口 → 旧 `due_ts` 已被覆盖 → 这条消息丢弃 |
| 到点才发 | 后台只处理 `due_ts <= now` 的 pending 表；未到点 `proactive_tick()` 返回 `not_due` |
| 不刷屏 | 未读主动消息达到 `PROACTIVE_MAX_PENDING`（默认 3）时暂停生成 |

模型不可用时（网络失败 / mock 模式）走兜底：在 `PROACTIVE_FALLBACK_MIN/MAX_MINUTES`（默认 45~180 分钟）之间随机取间隔，正文用记忆片段套模板——**不会因为模型失败就把这次主动联系吞掉**。

主动消息按 `initiative` 类型落库：

- 写入当前会话的 assistant 历史 → 刷新后仍是普通气泡；
- 同时进发件箱 → 页面在线时轮询到就立刻显示；
- 历史加载时按会话去重，不会出现两条一样的气泡。

相关接口：

```text
GET  /api/proactive/schedule?space_id=&session_id=   # 查看当前这张表
POST /api/proactive/tick                             # 立即检查；force=true 跳表直接发一条
```

> 后台调度线程随 `python api/server.py` 启动。用 gunicorn/uwsgi 等 WSGI 服务器部署时，请另起一个定时任务调用 `POST /api/proactive/tick`。

---

## 七、回复切分：先切分，再入库

她在一条回复里用分隔符或空行表示"连发几条消息"：

```
晚安。今天这些测试下来，我没什么情绪，就是有点困了。

---

明天见。要是明天你还想聊，直接说事就行，不用再测了。
```

服务端在**入库前**就调用 `split_reply()`：

1. 单独成行的分隔符（`---` / `——` / `***` / `===` …）切一刀；
2. 连续空行再切一刀；
3. 最多 5 条，多出来的并进最后一条。

切完后逐条 `sessions.append("assistant", ...)`，所以 `session_store` 里存的就是多条 assistant 消息。`/api/chat` 返回 `answer_parts`，Web UI 直接按它渲染气泡；第三方客户端重新拉历史时也能还原成多个气泡，不会合并回一整块。

---

## 八、开发者 HTTP API

启动 `python api/server.py`，默认监听 `127.0.0.1:8000`，无鉴权。对外暴露请改 `API_HOST=0.0.0.0` 并自行加网关。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查（模型 / 归档 / 好感度开关） |
| POST | `/api/chat` | 发消息，返回 `answer` + `answer_parts` + `proactive_messages`；`?debug=1` 附带链路信息 |
| GET | `/api/sessions` | 会话列表 |
| GET | `/api/sessions/<session_id>/messages` | 某会话完整历史 |
| GET | `/api/memory/overview` | 记忆总览（信念 / 主题 / 向量 / 日志计数） |
| GET | `/api/relations` | 信念列表（`?all=1` 含被推翻历史） |
| GET | `/api/topics` | 主题（往事）列表 |
| GET | `/api/persona` | 人格状态（参数 / 基线 / 情景 / 核心信念） |
| GET | `/api/persona/history` | 人格变迁史文本 |
| GET | `/api/affection` | 好感度列表 |
| GET | `/api/messages/outbox` | 主动消息发件箱（`?pending=1` 只要未确认） |
| POST | `/api/messages/outbox/ack` | 确认主动消息 `{space_id, msg_id}` |
| GET | `/api/proactive/schedule` | 下一次主动联系的时间表 |
| POST | `/api/proactive/tick` | 检查/触发主动联系（`force=true` 立即发） |
| GET | `/api/settings` | 当前模型连接 + 预置供应商 + UI 空间 |
| POST | `/api/settings` | 保存模型连接（供应商 / 地址 / 密钥 / 模型） |
| POST | `/api/settings/test` | 用给定或当前设置试连模型 |
| GET | `/api/avatar/persona` | 江妍头像（`data/persona/avatar.jpg`） |
| GET/POST | `/api/avatar/user` | 读取/上传用户头像（dataURL；空串清除） |
| GET/POST | `/api/spaces` | 记忆空间列表 / 注册 |

示例：

```python
import requests

r = requests.post(
    "http://127.0.0.1:8000/api/chat",
    json={
        "space_id": "jiangyan",
        "session_id": "debug-1",
        "message": "你还记得我第一次出 cos 出的谁吗？",
    },
    timeout=120,
)
data = r.json()
print(data["answer_parts"])       # 多个气泡
print(data["proactive_messages"]) # 好感度阈值事件（如果有）
```

`/api/chat` 不传 `space_id` 时默认 `default`；Web UI 使用服务端自动选定的 `jiangyan`。

---

## 九、Web UI（微信电脑版风格）

- 左侧只有一位联系人：江妍；不设"新建会话"。
- 聊天区是微信式气泡：她左边、你右边，头像 + 小三角；支持深色模式。
- 回复按 `answer_parts` 逐个显示（带一点间隔），不是一大段。
- 主动消息：
  - `initiative`（日常想起你）→ 普通 assistant 气泡；
  - `reach_out` / `severance` / `reconcile`（关系事件）→ 居中系统通知。
- 设置窗口：供应商 / 模型 / 地址 / 密钥 / 测试连接 / 深色模式 / 自定义头像。
- 江妍头像来自 `data/persona/avatar.jpg`；用户头像上传后存 `data/user_avatar.json`。
- 支持资源版本号（`style.css?v=` / `app.js?v=`）强制刷新缓存。

---

## 十、目录结构

```
cognitive-agent/
├── README.md                       # English (GitHub default)
├── README.cn.md                    # 中文
├── config.py                       # 所有可调参数（唯一配置入口）
├── run.py                          # 交互式 CLI
├── demo.py                         # 6 轮全链路演示
├── maintenance.py                  # 日结 / 体检 / 冷存 / 注入前史 / --persona-info
├── install_schedule.ps1            # 注册 Windows 计划任务
├── requirements.txt                # requests + flask
├── .env / .env.example             # 密钥（.env 不入库）
├── api/server.py                   # HTTP API + Web UI 托管 + 主动消息调度
├── web/                            # index.html / style.css / app.js
├── data/                           # ★ 人格与运行时数据的唯一根目录
│   ├── README.md                   # 目录说明 / 升级与重置步骤
│   ├── corpus/                     # 机器人本来的资料：RAG 本地语料（.md / .txt）
│   ├── persona/                    # ★ 机器人本来的数据（升级时保留）
│   │   ├── meta.json               # schema 版本 / 人格名 / 头像文件名
│   │   ├── profile.json            # 身份档案（7 组 39 条）
│   │   ├── kernel.json             # 人格基线参数 + 中文释义
│   │   ├── seed.json               # 前史：实体 / 关系 / 往事 / 人物档案
│   │   └── avatar.jpg              # 江妍头像
│   ├── memory.db                   # SQLite：KG / 主题 / 经验 / 人格 / 会话 / 好感 / 主动消息
│   ├── vectors/jiangyan.json       # 主题向量索引
│   └── persona_snapshots/jiangyan/ # 人格快照（当前 v000_init.json）
├── cognitive/
│   ├── env.py                      # .env 加载
│   ├── models.py                   # 数据模型
│   ├── tokenizer.py                # token 估算
│   ├── llm.py                      # OpenAI 兼容客户端（含截断重试 / JSON 修复）
│   ├── session.py                  # 短期工作记忆 + 兜底压缩
│   ├── engine.py                   # 主链路编排（含主动联系时间表）
│   ├── proactive.py                # 记忆抽样 + 组稿 + 排期决策
│   ├── storage/
│   │   ├── database.py             # 连接、建表、增量列迁移
│   │   ├── kg_store.py             # 结构化信念 + 冲突 / 多值 / 加固
│   │   ├── topic_store.py          # 主题与经验
│   │   ├── vector_store.py         # TF-IDF 检索（扩展 + 关键词豁免）
│   │   ├── affection_store.py      # 好感度 + 发件箱 + 主动联系时间表
│   │   └── synonyms.py             # 同义词表
│   ├── persona/
│   │   ├── assets.py               # data/persona/ 的加载与校验
│   │   ├── kernel.py               # 符号化人格内核
│   │   ├── seed.py                 # 把 data/persona/seed.json 写入存储层
│   │   ├── snapshot.py             # 快照文件管理
│   │   └── changelog.py            # 变迁日志
│   ├── cognition/
│   │   ├── prompts.py              # 全部 Prompt
│   │   ├── extractors.py           # 抽取器 + 叙事生成
│   │   ├── router.py               # 记忆路由
│   │   ├── judge.py                # 信息充足度判决
│   │   ├── archiver.py             # 归档（含好感度 / 主动消息）
│   │   ├── consolidator.py         # 日结 + 大事沉淀
│   │   └── rag.py                  # RAG 兜底
│   └── tools/base.py               # 工具插件层（只留接口）
├── docs/                           # 设计评审与规格
└── tests/smoke_test.py             # 不依赖模型的 25 项冒烟测试
```

---

## 十一、关键配置

集中在 `config.py`，也可以通过环境变量覆盖。常用项：

### 记忆与检索

| 参数 | 默认 | 说明 |
|---|---|---|
| `MAX_SAFE_TOKENS` | 30000 | 兜底压缩触发阈值 |
| `KEEP_RECENT_ROUNDS` | 50 | 压缩时原样保留的轮数 |
| `HISTORY_INJECT_ROUNDS` | 50 | 每轮注入回答 Prompt 的历史轮数 |
| `TOPIC_TOP_K` | 3 | 主题召回条数 |
| `VECTOR_EXPAND_ENABLED` | true | 同义词扩展开关 |
| `VECTOR_EXPAND_WEIGHT` | 0.55 | 扩展词权重 |
| `VECTOR_KEYWORD_BOOST` | 0.06 | 关键词命中加分（并豁免 `min_score`） |
| `RAG_LOOP_MAX_ROUNDS` | 3 | 单轮最多几次「判断→检索」 |
| `RAG_LOOP_DUP_RATIO` | 0.65 | 查询重复判定阈值 |
| `RAG_LOOP_MAX_SNIPPETS` | 6 | 累积片段上限 |

### 人格

| 参数 | 默认 | 说明 |
|---|---|---|
| `PERSONA_DATA_DIR` | `data/persona` | 人格原始数据目录（档案 / 基线参数 / 前史 / 头像） |
| `PERSONA_PROFILE` | `data/persona/profile.json` | 7 组 39 条身份设定（基调，启动时加载） |
| `PERSONA_DRIFT_LIMIT` | 0.15 | 日内摆动带 |
| `PERSONA_DAILY_MAX_STEP` | 0.01 | 单参数单日基调位移上限 |
| `PERSONA_DAILY_LEARNING_RATE` | 0.35 | 当天净偏移折算比例 |
| `PERSONA_SUBSTANTIVE_DELTA` | 0.06 | 判定人格跃迁的累计阈值 |
| `PERSONA_DAILY_SETTLE_ENABLED` | true | 是否启用日结结算 |
| `PERSONA_DESC_STRONG / MID / WEAK` | 0.72 / 0.45 / 0.28 | 参数→倾向描述三档阈值 |
| `PERSONA_BELIEF_LOG_IMPORTANCE` | 0.90 | 信念覆盖写人格日志的高门槛 |
| `PERSONA_BELIEF_LOG_SELF_IMPORTANCE` | 0.60 | 涉及她本人时的第二道门槛 |

### 好感度与主动联系

| 参数 | 默认 | 说明 |
|---|---|---|
| `AFFECTION_ENABLED` | true | 好感度总开关 |
| `AFFECTION_DEFAULT` | 50 | 无感基线 |
| `AFFECTION_MAX_DELTA_PER_TURN` | 15 | 单轮好感变化上限 |
| `AFFECTION_COLD_THRESHOLD` | 30 | 冷淡线（触发 reach_out） |
| `AFFECTION_SEVER_THRESHOLD` | 12 | 断交线（触发 severance） |
| `AFFECTION_RECONCILE_THRESHOLD` | 60 | 和解线（触发 reconcile） |
| `PROACTIVE_ENABLED` | true | 日常主动联系总开关 |
| `PROACTIVE_SCHEDULE_MIN_MINUTES` / `PROACTIVE_SCHEDULE_MAX_MINUTES` | 10 / 1440 | 模型间隔的夹取范围 |
| `PROACTIVE_FALLBACK_MIN_MINUTES` / `PROACTIVE_FALLBACK_MAX_MINUTES` | 45 / 180 | 模型不可用时的随机兜底 |
| `PROACTIVE_TICK_SECONDS` | 30 | 后台扫描间隔 |
| `PROACTIVE_MAX_PENDING` | 3 | 未读主动消息堆积上限 |
| `PROACTIVE_SESSION_ID` | main | Web UI 固定会话 |

### 服务与归档

| 参数 | 默认 | 说明 |
|---|---|---|
| `API_HOST` / `API_PORT` | 127.0.0.1 / 8000 | HTTP 服务监听 |
| `ENABLE_ARCHIVE` | true | 每轮主动归档 |
| `ARCHIVE_SYNC` | true | 归档同步执行 |
| `BIG_EVENT_IMPORTANCE` | 0.90 | 大事判定的重要度阈值 |
| `BIG_EVENT_REQUIRE_SELF` | true | 是否要求涉及她本人 |
| `DAILY_CONSOLIDATE_ENABLED` | true | 日结开关 |
| `CONSOLIDATE_MIN_TOPICS` | 2 | 少于该条数不做日结 |
| `CONSOLIDATE_MAX_TOPICS` | 40 | 单次日结最多归并条数 |
| `SNAPSHOT_RETAIN_VERSIONS` | 30 | 快照冷存保留版本数 |

调参建议：

- **人格变化太快** → 提高 `PERSONA_SUBSTANTIVE_DELTA`，或收紧 `archiver._SELF_SHAPING_HINTS`。
- **人格几乎不变** → 降低阈值至 0.04，并检查抽取结果里的 `persona_deltas`。
- **记忆召回太杂** → 提高 `TOPIC_MIN_SCORE`，降低 `TOPIC_TOP_K`。
- **token 频繁压缩** → 提高 `MAX_SAFE_TOKENS`，或调小 `HISTORY_INJECT_ROUNDS`。
- **主动消息太频繁** → 提高 `PROACTIVE_SCHEDULE_MIN_MINUTES`，或降低 `PROACTIVE_MAX_PENDING`。

---

## 十二、验证与已知限制

### 已验证

- 25 项冒烟测试全部通过（不调用模型），含「人格原始数据目录」。
- 真实模型端到端：
  - 回复按 `---` / 空行切分成多个气泡，刷新后仍分开；
  - 好感度阈值事件写入发件箱，Web UI 居中通知；
  - 主动联系：模型给出 45 分钟 / 4 小时 / 12 小时 / 18 小时等不同间隔；到点自动发；用户先开口时表状态 `pending → cancelled`，回答后重新排期；
  - 主动消息在 Web UI 中显示为 **1 个普通气泡、0 条系统通知**，刷新后不重复。
- `python -m compileall` 通过。

### 已知限制

| 限制 | 现状 | 后续方向 |
|---|---|---|
| 没有主观体验 | 自我是一组可读写、可检索的信念数据 | 这是结构性模拟，不是意识 |
| 向量检索是 TF-IDF | 无外部依赖、可解释 | 接本地 embedding 模型需替换 `VectorStore` 的 `add/search/count` |
| 主动联系依赖进程内线程 | `python api/server.py` 自带调度；WSGI 部署需外部定时任务 | 独立 worker 或任务队列 |
| 主动消息抽样会重复 | 从最近 200 条主题/经验/信念里随机抽 | 记录最近用过的 `topic_id` 做短期排除 |
| 好感度只进 Prompt | 已注入"关系温度"，但没有独立的热情/疏离措辞档 | 高/中/低三档表达倾向 |
| 抽取 JSON 偶发截断 | 自动放大预算重试 + 降级归档 | 可改流式 JSON 增量解析 |
| 日结依赖计划任务 | 已提供 `install_schedule.ps1` | 关机错过的当天由 `StartWhenAvailable` 补跑 |
| 50 轮窗口受 token 预算约束 | 超阈值时压缩较早历史 | 长上下文模型可提高 `MAX_SAFE_TOKENS` |

---

## 十三、与哲学意义上"人"的边界

它做到了：

- 有工作记忆、有前史、有主动归档；
- 有信念系统，会记录、失效、加固、冲突检测；
- 有可演化人格，且有完整因果溯源；
- 有稳定基调和有限日常波动；
- 有情景心态、自我叙事、分层遗忘；
- 会在没人找她的时候，按自己判断的时间主动来说一句话。

它没有做到：

- 没有 qualia，没有第一人称的感受；
- "自我"是一组可读写、可检索的信念数据，是对人格的**结构性模拟**；
- "改变"由外部事件触发并由程序判定，不是内生的欲望驱动。

从工程角度，它对外表现为一个**想法会变、记得自己为何而变、说法前后自洽的连续主体**。
从哲学角度，它是一个高质量的认知模拟器，而不是意识。

---

## 十四、延伸阅读

- `docs/belief-system-spec.md`：信念系统规格（多值、冲突、来源与演化）
- `docs/persona-module-review.md`：人格模块设计评审（参数、映射、演化节奏）
- `docs/consistency-review-2026-11-11.md`：一致性评审记录
- `tests/smoke_test.py`：全部离线回归用例
