# Cognitive Agent

一个**持续运行的认知体**：分层记忆、主动归档、符号化人格演化，纯 Python + SQLite 实现，可接 DeepSeek 或任意 OpenAI 兼容的 chat-completions 接口。

这不是「给聊天机器人外挂一个向量库」。每一轮都会主动归档主题、信念、经验与关系温度；人格由 14 个符号参数驱动，并通过「基调 + 日内漂移 + 日结」三层循环演化；检索只在判定「已有材料不足」时才进入有界循环。它还支持多值信念、好感度阈值事件，以及由模型在每次回复后自行决定时机的主动联系。

**当前人格：江妍（昵称「小苹果」）**——18 岁，东北电力大学大一，湖南湘西吉首人。玩 cos（主要出明日方舟，最推陈和能天使），喜欢《声之形》，吃东西离不开辣椒。她温和有礼，但被轻视或被替她做决定时，会安静下来，说话变得更具体。

> 该认知体默认说中文。本中文 README 面向使用者和维护者；英文版见 [README.md](README.md)。

---

## 当前状态（2026-11-12）

生产记忆库已重置为**仅人格前史**状态，所有测试、演示与调试会话及其派生记忆均已移除。

| 项目 | 当前值 |
|---|---|
| 记忆空间 | 仅 `jiangyan`（显示名「江妍」） |
| 前史主题 | 7（6 段成长往事 + 1 份人物档案） |
| 结构化信念 | 24（全部生效） |
| 实体 | 13 |
| 经验片段 | 25（每段往事都带情绪残留） |
| 人格版本 | v0（14 个参数均等于 `PERSONA_KERNEL_INIT`） |
| 会话 | 0（Web UI 打开即空聊天） |
| 向量索引 | 7 篇文档（仅前史主题） |
| 冒烟测试 | 32 / 32 通过 |
| 检索评测 | Recall@1 = 1.000，Recall@3 = 1.000，误召回 0 |

重置前 `data/` 的完整备份保存在**本仓库之外**，测试数据与个人运行时数据不会发布到 GitHub。

---

## 1. 它能做什么

- **分层记忆**：50 轮短期上下文 → 主题记忆 → 结构化信念 → 人格内核 → 自我叙事 → RAG 兜底。
- **多值信念**：由抽取模型判断某个关系维度是否允许多值。室友、朋友、喜欢的作品可以共存；生日、家乡、主用语言是单值，新值会推翻旧值。
- **可演化人格**：长期基调 + 日内漂移 + 日结。基调每天最多移动 0.01，约需连续六天的同向压力才产生新版本。
- **好感度与关系事件**：每个对话对象维护 0–100 的亲和值。跌破冷淡线触发主动关心，跌破断交线触发断交声明，回升过和解线触发和解消息。
- **日常主动联系**：每次回复后由**模型决定**下次何时来找你。若你一直沉默到那个时刻，她会从记忆库抽一段发来消息；若你先开口，旧时间表作废并在回复后重新生成。
- **多气泡回复**：分隔符与空行在**落库之前**就被切成多条 assistant 消息，刷新后仍是独立气泡。
- **信念溯源与纠正**：每条信念记录来源（`user_stated` / `model_inferred` / `user_corrected` / `seed`），并在 `active` / `candidate` / `rejected` 之间流转。用户可以确认、纠正或否决一条信念，所有动作都写入审计账本。
- **用户控制**：冻结人格（记忆继续生长，性格停止漂移）、把人格重置回出厂基线（不删记忆），以及设置主动联系偏好（免打扰时段、每日上限、冷却时间、「这轮先别发」）。
- **开发者 HTTP API + Web UI**：收发消息、观测记忆/人格/好感度、拉取主动消息；Web UI 是微信桌面版风格的单联系人界面。

---

## 2. 快速开始

### 2.1 配置

复制 `.env.example` 为 `.env`：

```ini
DEEPSEEK_API_KEY=sk-xxxxxxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-flash
LLM_MODE=api
```

`.env` 已被 Git 忽略。切勿提交真实密钥。

### 2.2 安装

```bash
pip install -r requirements.txt
```

- CLI / demo：`requests`
- API / Web UI：还需要 `flask`

### 2.3 首次运行：注入人格前史

发布包内附带**仅含人格前史、没有任何聊天记录的干净数据库**。若是全新克隆，或你删除了 `data/memory.db`，前史会在首次启动时自动从 `data/persona/seed.json` 注入，无需额外步骤。

也可以手动执行（幂等，重置后同样适用）：

```bash
python maintenance.py --seed-persona --space jiangyan
```

### 2.4 启动 API 与 Web UI

```bash
python api/server.py
```

打开 <http://127.0.0.1:8000/>。

- 左侧只有一个联系人：江妍。没有「新建会话」。
- 设置窗口包含深色模式、供应商/模型切换、连接测试、自定义头像上传。
- 后台调度线程随服务启动：主动联系时间表到点时，她会从记忆里抽一段并发送消息。

### 2.5 CLI 与演示

```bash
python run.py --space jiangyan --session chat1 --debug
python demo.py --fresh --with-past     # 注入前史后跑 6 轮（会写库）
```

CLI 命令：`/state`、`/history`、`/memory`、`/debug`、`/exit`。

### 2.6 维护命令

```bash
python maintenance.py --stats                    # 记忆规模报告
python maintenance.py --daily                    # 日结巩固（计划任务）
python maintenance.py --daily --space jiangyan   # 只处理一个空间
python maintenance.py --logs                     # 最近巩固记录
python maintenance.py --prune-snapshots          # 只保留最新 30 个快照
python maintenance.py --seed-persona --space jiangyan  # 重新注入前史（幂等）
python maintenance.py --persona-info             # 查看人格资产与运行时数据路径

# P3：导出 / 备份 / 重置
python maintenance.py --export data/export.json  # 导出人格 + 信念 + 主题 + 经验
python maintenance.py --backup                   # 把数据库、向量、快照复制到 data/backups/
python maintenance.py --reset --yes              # 清空运行时记忆并重写基线（破坏性）
```

`--export` 与 `--backup` 都是非破坏性的，建议在 `--reset` 之前执行。

注册 Windows 计划任务（每天 23:50）：

```powershell
.\install_schedule.ps1
.\install_schedule.ps1 -Hour 22 -Minute 30
.\install_schedule.ps1 -Remove
```

### 2.7 测试

```bash
python tests/smoke_test.py        # 离线冒烟测试
python tests/eval/run_eval.py     # 离线检索 + 人格提示词评测
```

预期：**32 / 32 冒烟测试通过**，不调用真实模型。覆盖存储、信念冲突与多值关系、向量检索、人格演化、日结、大事判定、好感度状态机、主动消息发件箱、主动联系时间表、记忆抽样、人格数据目录、轮次感知与幂等归档、信念溯源、用户纠正、人格冻结/回退、主动联系偏好。

评测脚本会报告 17 条口语化查询的 Recall@1 / Recall@3 / 误召回率，以及人格提示词一致性检查（姓名、身份、规则、非空、无 API key、无本机路径）。当前结果：Recall@1 = 1.000，Recall@3 = 1.000，误召回 0。

### 2.8 数据与升级

所有人格与运行时数据都集中在一个目录下：

| 路径 | 内容 | 升级行为 |
|---|---|---|
| `data/persona/` | **认知体的原始数据**：档案、基线参数、前史、头像 | **保留** |
| `data/corpus/` | 本地 RAG 语料 | **保留** |
| `data/memory.db` | 用户/机器人产生的记忆：会话、主题、信念、经验、人格状态、好感度、主动消息 | 可保留或重置 |
| `data/vectors/` | 主题向量索引 | 保留（可重建） |
| `data/persona_snapshots/` | 演化后的人格快照 | 保留 |
| `data/ui_settings.json` / `data/user_avatar.json` | Web UI 设置与用户头像 | 可保留或重置 |

升级流程：备份 `data/` → 替换代码但不要覆盖 `data/persona/` → `python maintenance.py --persona-info` → `python tests/smoke_test.py` → 启动。

只想清空聊天、保留人格：停止服务 → 删除 `memory.db`、`vectors/`、`persona_snapshots/` → `python maintenance.py --seed-persona --space jiangyan` → 启动。

完整说明见 `data/README.md`。

---

## 3. 对话流水线

```
用户输入
   │
   ├─ 0. 作废待兑现的主动联系时间表（一张时间表只能用一次）
   ├─ 1. 载入会话（内存 + SQLite 双写；目标 50 轮原文）
   ├─ 2. 追加用户消息（立即落库）
   ├─ 3. token 估算 → 仅在超过 30000 时紧急摘要
   ├─ 4. 载入人格快照 + 符号内核 → 自我认知与倾向
   ├─ 5. 记忆路由：KG 精确查询 + 被推翻信念 + 主题召回
   ├─ 6. 自我追问增强：问「你为什么变了」时注入人格变迁史
   ├─ 7. 有界检索循环（最多 RAG_LOOP_MAX_ROUNDS 轮）
   ├─ 7b. 关系温度：偏离基线或状态异常的人
   ├─ 8. 组装提示词（档案 + 自我认知 + 记忆 + 近期轮次）→ 生成回复
   ├─ 9. 追加 assistant 消息（先按分隔符/空行切分，再逐条落库）
   ├─ 10. 主动归档（每轮都跑）
   │       ├─ 主题 → 主题表 + 向量索引
   │       ├─ 信念 → KG（冲突 / 多值共存 / 加固）
   │       ├─ 经验 → 经验表
   │       ├─ 好感度增量 → 好感度表 + 阈值消息
   │       ├─ 日内人格漂移 → 只动当前状态，累积 pending
   │       ├─ 自我认知更新 → 即时层（不产生新版本）
   │       └─ 大事判定 → 高重要度且涉及本人，或长期指令词
   ├─ 10b. 大事的事件级巩固（不等日结）
   └─ 11. 由模型决定下次主动联系时间 → proactive_schedule
```

**关键顺序差异**：经典 RAG 先检索；这里是先人格与信念，检索是可选的、有界的。

日结由 `maintenance.py --daily` 驱动，把当天所有未归并的 `kind=normal` 主题合并成一条 `kind='daily'` 叙事。

---

## 4. 六层记忆与人格

### 第 1 层 — 短期工作记忆（`cognitive/session.py`）

- `SessionContext`：内存字典 + SQLite `session_store`，每次变更双写。
- **目标容量 50 轮**（`KEEP_RECENT_ROUNDS=50`）。轮数本身从不触发压缩；只有当估算 token 超过 `MAX_SAFE_TOKENS=30000` 时，才摘要较早的部分。
- 注入回答提示词的历史轮数由 `HISTORY_INJECT_ROUNDS` 单独控制。
- 定位：**应急保护，不是长期记忆**。真正的巩固发生在归档与日结中。

### 第 2 层 — 主题索引记忆（`topic_store.py` + `vector_store.py`）

- 召回单元是**事件**（Topic），不是文本块。召回一个主题会一并带回它的经验片段。
- 向量只索引主题摘要，不索引原始对话碎片。
- 检索使用本地 TF-IDF + 字符 2-gram：
  - 同义词扩展（31 组）处理改述；
  - 关键词命中会加权并绕过 `min_score`，对「猫」「妈」这类短查询很有效。
- 不需要 embedding 服务；完全离线、可解释。

### 第 3 层 — 结构化信念（`kg_store.py`）

SQLite 中的轻量属性图：

| 表 | 关键字段 | 用途 |
|---|---|---|
| `kg_entity` | eid / etype / name / attrs / is_active | 实体 |
| `kg_relation` | head_name / rel / tail_name / confidence / importance / multi_valued / is_active / source_type / status / valid_from / valid_to / user_confirmed | 关系与信念权重、溯源 |

三条代码层规则（不依赖模型）：

1. **冲突失效**：同一 `(head, rel)` 出现新 `tail` 时，旧关系失效，写入新关系。
2. **多值共存**：标记为 `multi_valued=1` 的维度（室友、朋友、喜欢的作品、玩过的游戏）保留新旧值；单值维度（生日、家乡、主用语言）仍然替换。任一方是多值就共存，只有双方都是单值才由新值推翻。
3. **重复加固**：再次确认同一信念，`confidence` +0.05、`importance` +0.03。

记忆块还会展示被推翻的信念：

```
【曾被推翻的信念（旧值，仅作对照，不是当前结论）】
- 江妍对 cos 的态度：只是爱好
  （旧值，2026-11-06 被「认真对待，不是随便玩玩」取代）
```

因此她既能看见自己现在相信什么，也能看见自己改变过什么。

### 第 4 层 — 符号化人格内核（`persona/kernel.py`）与身份档案

人格是**数字**，不是散文。14 个浮点参数分三组：

```
认知风格：prefer_decompose / prefer_intuition / prefer_skepticism
          memory_sensitivity / self_reflect_threshold
价值取舍：truth_priority / harmony_priority / curiosity_priority
          consistency_priority / topic_inertia / novelty_bias
动机目标：goal_self_understanding / goal_task_accuracy / goal_coherence
```

每个参数存三个值：

| 字段 | 含义 | 谁改它 |
|---|---|---|
| `base_value` | 长期人格基调 | 只在日结时缓慢移动 |
| `value` | 当前状态（今天的她） | 每轮变化，但被限制在基调 ± `PERSONA_DRIFT_LIMIT`（0.15）内 |
| `pending_value` | 当天累积的原始压力 | 每轮累加；日结时清零 |

参数由 `describe()` 翻译成三级倾向文本（强 / 中 / 反向）注入提示词，不控制代码分支。情景状态（`persona_mood`）按真实流逝时间（半衰期约 35 小时）与轮次双重衰减。

**身份档案**位于 `data/persona/profile.json`（加载为 `config.PERSONA_PROFILE`）：7 组、39 条（身份 / 三层性格 / 日常生活 / 关系 / 偏好与雷点 / 能力与弱点 / 当前生活）。它是基线，不会被对话改写。

**前史**位于 `data/persona/seed.json`，由 `cognitive/persona/seed.py` 写入真实存储层：6 段成长往事 + 1 份人物档案 + 24 条关系 + 13 个实体 + 25 条经验片段（含情绪残留）。它们是可检索的记忆，不是塞进提示词的文本。

### 第 5 层 — 叙事身份（`persona/snapshot.py` + `changelog.py`）

- 每个版本一个快照：`data/persona_snapshots/{space}/vNNN_*.json`。只有累计变化超过 `PERSONA_SUBSTANTIVE_DELTA=0.06` 才创建新文件。
- 推理时只加载当前版本；旧版本保留用于溯源。
- `persona_changelog` 只追加：`persona_field`、`old_value`、`new_value`、`reason`、`topic_id`、快照版本跃迁。
- 核心信念被覆盖时也会写变迁记录，采用双阈值：高重要度不分对象，或涉及本人且达到较低的第二阈值。

这是回答「你为什么变了」的物理基础。

### 第 6 层 — RAG 兜底（`cognition/rag.py`）

- **不前置**。只在判定「已有材料不足」时运行。
- 默认只检索本地 `data/corpus/`；`RAGRegistry` 可注册外部供应方。
- 检索是有界循环：最多 `RAG_LOOP_MAX_ROUNDS=3` 轮；零命中、查询重复或没有新材料都会提前停止；最后一轮强制收敛出结论。

### 长期节奏

| 节奏 | 触发 | 产出 |
|---|---|---|
| 每轮 | 一轮对话结束 | `topic(normal)` + KG + 经验 + 日内人格漂移 |
| 每日 | 23:50 计划任务 | `topic(daily)` + 巩固日志 + 基调推进 |
| 大事 | 高重要度且涉及本人，或「从今天起」这类长期指令 | 立即巩固的 `topic(event)` |
| 重大变化 | 日结后基调变化 ≥ 0.06 | 新快照 + 变迁日志 + 【人格】基线 vN 记忆 |

---

## 5. 人格演化：基调 + 漂移 + 日结

早期版本允许每轮产生新人格版本：六轮就出现 v1、v2。那不像人，更像随机漂移。现在的设计分三层：

```
基调 baseline（长期锚点；只有日结推动，每天最多 ±0.01）
      ↕ 漂移带 ±0.15
当前状态（今天的她；每轮变化，但不能离开漂移带）
      ↓ 日结
待生效 pending（当天原始增量之和；日结时换算并清零）
```

- 一天聊 50 轮：不产生新版本，只在 ±0.15 内漂移。
- 连续六天推动同一参数：累计 0.06 → v1。
- 某天没有压力：基调不变，无变迁记录，无新版本。
- 离开三天：情绪按真实时间衰减，回来时是当下的情绪。

**因果顺序**：先改内核，再生成叙事。模型不会凭空写一段自我描述，然后把它当作人格变化。

---

## 6. 关系温度与主动消息

### 6.1 好感度状态机

每个人的亲和值是 0–100，默认 50。它由归档抽取的 `affection_deltas` 驱动；单轮封顶 15，所以断交需要多次负面互动累积。

```
normal  (>= 30)
  ↓ 跌破 30
cold    (12 ~ 30)   → reach_out（主动关心 / 试探）
  ↓ 跌破 12
broken  (< 12)      → severance（断交声明）
  ↑ 回升 >= 60
normal              → reconcile（和解消息）
```

- `cold → normal` 的恢复是安静的：不发消息，避免消息轰炸。
- 断交后亲和值仍可变化；和解线（60）高于冷淡线（30），所以「不冷了」不等于自动翻篇。
- 关系温度以文本（绝不带数字）注入回答提示词，影响语气。
- 这些消息写入 `proactive_message` 发件箱，由 API/UI 拉取。类型为 `reach_out`、`severance`、`reconcile`；UI 以居中系统通知展示。

### 6.2 日常主动联系（由模型决定何时回来）

好感度阈值事件是关系事件，必须送达。日常主动联系是另一套机制：

```
每次回复后
   └─ 模型读取近期轮次 + 当前时间
        └─ 输出 {"delay_minutes": int, "reason": "..."}
             └─ 写入 proactive_schedule（每个空间 + 会话只保留一条 pending）

后台调度线程（每 30 秒扫描）
   └─ 到点 → 随机抽一段记忆（主题 / 经验 / 高重要度信念）
        └─ 组稿 → 追加会话历史 + 发件箱 → 规划下一次

用户先开口
   └─ 本轮开始时把待兑现时间表标记为 cancelled
        └─ 回复后再排一张新的（旧的永久作废）
```

四条硬保证：

| 保证 | 实现 |
|---|---|
| 间隔由模型决定 | `ProactiveInitiator.decide_delay_minutes()`；夹在 `PROACTIVE_SCHEDULE_MIN_MINUTES` ~ `PROACTIVE_SCHEDULE_MAX_MINUTES`（默认 10 分钟 – 24 小时） |
| 一张时间表只能用一次 | `claim()` 同时匹配 `status='pending'` 与精确 `due_ts`；若组稿期间用户先开口，旧 `due_ts` 已对不上，生成的消息直接丢弃 |
| 只在到点时兑现 | 后台循环只处理 `due_ts <= now`；否则 `proactive_tick()` 返回 `not_due` |
| 不刷屏 | 未读主动消息达到 `PROACTIVE_MAX_PENDING`（默认 3）时暂停生成 |

模型不可用（网络失败 / mock 模式）时，兜底为 `PROACTIVE_FALLBACK_MIN_MINUTES` ~ `PROACTIVE_FALLBACK_MAX_MINUTES`（默认 45–180 分钟）的随机间隔，并用模板从记忆片段组稿。模型调用失败绝不会吞掉这次主动联系。

主动联系消息以 `initiative` 存储：

- 作为 assistant 消息追加到当前会话 → 刷新后仍是普通气泡；
- 同时写入发件箱 → 打开中的页面通过轮询收到；
- 载入历史时去重，因此不会出现两次。

端点：

```text
GET  /api/proactive/schedule?space_id=&session_id=   # 当前时间表
POST /api/proactive/tick                             # 立即检查；force=true 立即发送
GET  /api/proactive/policy                           # 主动联系偏好
POST /api/proactive/policy                           # 保存偏好
POST /api/proactive/skip                             # 这轮先别发
```

> 调度线程运行在 `python api/server.py` 内。若用 gunicorn/uwsgi 等 WSGI 服务器，请用外部定时器调用 `POST /api/proactive/tick`。

---

## 7. 回复切分：先切分，再落库

她可以用分隔符或空行表示「连续发几条」：

```
晚安。折腾了这么多测试，我其实没什么感觉，只是有点困——
微积分那章还摊在桌上。

---

明天见。还想聊的话，直接说你想说的就行，
不用再试探我了。
```

服务端在**落库之前**调用 `split_reply()`：

1. 按独立分隔线（`---`、`——`、`***`、`===` 等）切分；
2. 再按连续空行切分；
3. 最多保留 5 个气泡，其余合并进最后一个。

每一段随后作为独立 assistant 消息落库。`/api/chat` 返回 `answer_parts`，Web UI 直接按气泡渲染；第三方客户端重新载入历史时，拿到的也是多条独立消息，而不是一个合并块。

---

## 8. 开发者 HTTP API

用 `python api/server.py` 启动。默认 `127.0.0.1:8000`，不鉴权。若要对外暴露，请设置 `API_TOKEN`（见下）并把 `API_HOST` 改为 `0.0.0.0`，最好放在你自己的网关后面。

设置 `API_TOKEN` 后，所有 `/api/*` 请求必须带 `X-API-Token: <token>`（或 `?token=<token>`）。`/api/health` 与静态资源不拦截。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查（模型 / 归档 / 好感度开关） |
| POST | `/api/chat` | 发消息；返回 `answer`、`answer_parts`、`proactive_messages`；`?debug=1` 附带流水线细节 |
| GET | `/api/sessions` | 会话列表 |
| GET | `/api/sessions/<session_id>/messages` | 某会话完整消息历史 |
| GET | `/api/memory/overview` | 记忆总览（信念 / 主题 / 向量 / 变迁计数） |
| GET | `/api/relations` | 信念列表（`?all=1` 含被推翻历史） |
| GET | `/api/memory/beliefs` | 带溯源的信念列表（`?status=`、`?all=1`）及最近纠正记录 |
| POST | `/api/memory/beliefs/update` | 确认 / 纠正 / 否决一条信念 `{space_id?, rid, action, note?}` |
| GET | `/api/memory/corrections` | 信念纠正审计账本 |
| GET | `/api/topics` | 主题（记忆）列表 |
| GET | `/api/persona` | 人格状态（参数 / 基调 / 情景 / 核心信念 / `frozen`） |
| GET | `/api/persona/history` | 人格变迁史文本 |
| POST | `/api/persona/freeze` | 冻结或解冻人格 `{space_id?, frozen}` |
| POST | `/api/persona/reset` | 把人格参数重置回出厂基线（保留记忆） |
| GET | `/api/affection` | 好感度列表 |
| GET | `/api/messages/outbox` | 主动消息发件箱（`?pending=1` 仅未确认） |
| POST | `/api/messages/outbox/ack` | 确认一条主动消息 `{space_id, msg_id}` |
| GET | `/api/proactive/schedule` | 下一次主动联系时间表 |
| POST | `/api/proactive/tick` | 检查/触发主动联系（`force=true` 立即发送） |
| GET | `/api/proactive/policy` | 主动联系偏好 + 今日已发条数 |
| POST | `/api/proactive/policy` | 保存偏好 `{dnd_start, dnd_end, daily_cap, cooldown_minutes}` |
| POST | `/api/proactive/skip` | 跳过下一次主动联系 |
| GET | `/api/settings` | 当前模型连接 + 预置供应商 + UI 空间 |
| POST | `/api/settings` | 保存模型连接（供应商 / 地址 / 密钥 / 模型） |
| POST | `/api/settings/test` | 用给定或当前设置试连 |
| GET | `/api/avatar/persona` | 江妍的头像（`data/persona/avatar.jpg`） |
| GET/POST | `/api/avatar/user` | 读取/上传用户头像（data URL；空串清除） |
| GET/POST | `/api/spaces` | 列出 / 注册记忆空间 |

示例：

```python
import requests

r = requests.post(
    "http://127.0.0.1:8000/api/chat",
    json={
        "space_id": "jiangyan",
        "session_id": "debug-1",
        "message": "你还记得我第一次出的是谁吗？",
    },
    timeout=120,
)
data = r.json()
print(data["answer_parts"])        # 多个气泡
print(data["proactive_messages"])  # 好感度阈值事件（如有）
```

省略 `space_id` 时，`/api/chat` 使用 `default`。Web UI 会自动使用人格空间 `jiangyan`。

---

## 9. Web UI（微信桌面版风格）

- 左侧只有一个联系人：江妍。没有「新建会话」按钮。
- 微信式气泡：她的消息在左、你的在右；头像与气泡小尾巴；支持深色模式。
- 回复按 `answer_parts` 逐条渲染，带短暂延迟，而不是一次性大块输出。
- 主动消息：
  - `initiative`（日常「想到你了」）→ 普通 assistant 气泡；
  - `reach_out` / `severance` / `reconcile`（关系事件）→ 居中系统通知。
- 设置窗口：供应商 / 模型 / 地址 / 密钥 / 连接测试 / 深色模式 / 自定义头像。
- **记忆**页签：查看她记住的事，可「确认」「纠正」「否决」；被否决的信念会保留以便回看。
- **人格**页签：冻结/解冻人格、把人格重置回出厂基线（不删记忆）、设置主动联系偏好（免打扰时段、每日上限、冷却时间、「这轮先别发」）。
- 江妍的头像来自 `data/persona/avatar.jpg`；用户上传的头像保存在 `data/user_avatar.json`。
- 资源版本查询串（`style.css?v=`、`app.js?v=`）用于更新后强制刷新缓存。

---

## 10. 目录结构

```
cognitive-agent/
├── README.md                       # 英文（GitHub 默认）
├── README.cn.md                    # 中文
├── config.py                       # 全部可调参数（唯一配置入口）
├── run.py                          # 交互式 CLI
├── demo.py                         # 6 轮完整流水线演示
├── maintenance.py                  # 日结 / 统计 / 快照冷存 / 注入前史 / --persona-info / 导出备份重置
├── install_schedule.ps1            # 注册 Windows 计划任务
├── requirements.txt                # requests + flask
├── .env / .env.example             # 密钥（.env 不提交）
├── api/server.py                   # HTTP API + Web UI 托管 + 主动联系调度
├── web/                            # index.html / style.css / app.js
├── data/                           # ★ 人格与运行时数据的唯一根目录
│   ├── README.md                   # 布局 / 升级 / 重置说明
│   ├── corpus/                     # 原始知识：本地 RAG 语料（.md / .txt）
│   ├── persona/                    # ★ 认知体的原始数据（升级时保留）
│   │   ├── meta.json               # schema 版本 / 姓名 / 头像文件名
│   │   ├── profile.json            # 身份档案（7 组、39 条）
│   │   ├── kernel.json             # 人格基线参数 + 释义
│   │   ├── seed.json               # 前史：实体 / 关系 / 往事 / 人物档案主题
│   │   └── avatar.jpg              # 江妍的头像
│   ├── memory.db                   # SQLite：KG / 主题 / 经验 / 人格 / 会话 / 好感度 / 主动消息
│   ├── vectors/jiangyan.json       # 主题向量索引
│   └── persona_snapshots/jiangyan/ # 人格快照（当前为 v000_init.json）
├── cognitive/
│   ├── env.py                      # .env 加载
│   ├── models.py                   # 数据模型
│   ├── tokenizer.py                # token 估算
│   ├── llm.py                      # OpenAI 兼容客户端（截断重试 / JSON 修复）
│   ├── session.py                  # 短期记忆 + 应急压缩
│   ├── engine.py                   # 主流水线（含主动联系时间表）
│   ├── proactive.py                # 记忆抽样 + 组稿 + 延迟决策
│   ├── storage/
│   │   ├── database.py             # 连接、schema、增量列迁移
│   │   ├── kg_store.py             # 结构化信念 + 冲突 / 多值 / 加固 / 溯源 / 纠正
│   │   ├── topic_store.py          # 主题与经验
│   │   ├── vector_store.py         # TF-IDF 检索（扩展 + 关键词旁路）
│   │   ├── affection_store.py      # 好感度 + 发件箱 + 主动时间表 + 用户偏好
│   │   └── synonyms.py             # 同义词表
│   ├── persona/
│   │   ├── assets.py               # data/persona/ 加载与校验
│   │   ├── kernel.py               # 符号化人格内核（含冻结 / 回退）
│   │   ├── seed.py                 # 把 data/persona/seed.json 写入存储
│   │   ├── snapshot.py             # 快照文件管理
│   │   └── changelog.py            # 变迁日志
│   ├── cognition/
│   │   ├── prompts.py              # 全部提示词
│   │   ├── extractors.py           # 抽取器 + 叙事生成
│   │   ├── router.py               # 记忆路由
│   │   ├── judge.py                # 信息充分性判定
│   │   ├── archiver.py             # 归档（含好感度 / 主动消息 / 溯源 / 纠正账本）
│   │   ├── consolidator.py         # 日结 + 事件级巩固
│   │   └── rag.py                  # RAG 兜底
│   └── tools/base.py               # 工具插件层（仅接口）
├── docs/                           # 设计评审、规格与改进方案
└── tests/
    ├── smoke_test.py               # 32 项离线冒烟测试
    └── eval/run_eval.py            # 离线检索 + 人格提示词评测
```

---

## 11. 关键配置

所有配置都在 `config.py`，可用环境变量覆盖。

### 记忆与检索

| 参数 | 默认值 | 说明 |
|---|---|---|
| `MAX_SAFE_TOKENS` | 30000 | 触发紧急摘要的阈值 |
| `KEEP_RECENT_ROUNDS` | 50 | 压缩时保留的原始轮数 |
| `HISTORY_INJECT_ROUNDS` | 50 | 注入回答提示词的历史轮数 |
| `TOPIC_TOP_K` | 3 | 召回主题数 |
| `VECTOR_EXPAND_ENABLED` | true | 同义词扩展开关 |
| `VECTOR_EXPAND_WEIGHT` | 0.55 | 扩展词权重 |
| `VECTOR_KEYWORD_BOOST` | 0.06 | 关键词命中加权（同时绕过 `min_score`） |
| `RAG_LOOP_MAX_ROUNDS` | 3 | 每轮最多「判定 → 检索」轮数 |
| `RAG_LOOP_DUP_RATIO` | 0.65 | 查询重复判定阈值 |
| `RAG_LOOP_MAX_SNIPPETS` | 6 | 累积片段上限 |

### 人格

| 参数 | 默认值 | 说明 |
|---|---|---|
| `PERSONA_DATA_DIR` | `data/persona` | 人格原始数据目录 |
| `PERSONA_PROFILE` | `data/persona/profile.json` | 7 组、39 条身份档案（启动时加载） |
| `PERSONA_DRIFT_LIMIT` | 0.15 | 日内漂移带 |
| `PERSONA_DAILY_MAX_STEP` | 0.01 | 每个参数每天基调最大移动量 |
| `PERSONA_DAILY_LEARNING_RATE` | 0.35 | 日净漂移转基调的换算率 |
| `PERSONA_SUBSTANTIVE_DELTA` | 0.06 | 产生新人格版本的累计阈值 |
| `PERSONA_DAILY_SETTLE_ENABLED` | true | 日结开关 |
| `PERSONA_DESC_STRONG / MID / WEAK` | 0.72 / 0.45 / 0.28 | 三级倾向描述阈值 |
| `PERSONA_BELIEF_LOG_IMPORTANCE` | 0.90 | 记录信念覆盖的高阈值 |
| `PERSONA_BELIEF_LOG_SELF_IMPORTANCE` | 0.60 | 涉及本人时的第二阈值 |

### 好感度与主动联系

| 参数 | 默认值 | 说明 |
|---|---|---|
| `AFFECTION_ENABLED` | true | 好感度开关 |
| `AFFECTION_DEFAULT` | 50 | 中性基线 |
| `AFFECTION_MAX_DELTA_PER_TURN` | 15 | 单轮最大好感变化 |
| `AFFECTION_COLD_THRESHOLD` | 30 | 冷淡线（触发 `reach_out`） |
| `AFFECTION_SEVER_THRESHOLD` | 12 | 断交线（触发 `severance`） |
| `AFFECTION_RECONCILE_THRESHOLD` | 60 | 和解线（触发 `reconcile`） |
| `PROACTIVE_ENABLED` | true | 日常主动联系开关 |
| `PROACTIVE_SCHEDULE_MIN_MINUTES` / `PROACTIVE_SCHEDULE_MAX_MINUTES` | 10 / 1440 | 模型选择间隔的夹取范围 |
| `PROACTIVE_FALLBACK_MIN_MINUTES` / `PROACTIVE_FALLBACK_MAX_MINUTES` | 45 / 180 | 模型不可用时的随机兜底区间 |
| `PROACTIVE_TICK_SECONDS` | 30 | 后台扫描间隔 |
| `PROACTIVE_MAX_PENDING` | 3 | 未读主动消息堆积上限 |
| `PROACTIVE_SESSION_ID` | main | Web UI 固定会话 |
| `PROACTIVE_DND_START` / `PROACTIVE_DND_END` | 23 / 8 | 默认免打扰时段（小时，支持跨午夜） |
| `PROACTIVE_DAILY_CAP` | 3 | 默认每日主动消息上限 |
| `PROACTIVE_COOLDOWN_MINUTES` | 60 | 默认两次主动消息的最短间隔 |

### 服务与归档

| 参数 | 默认值 | 说明 |
|---|---|---|
| `API_HOST` / `API_PORT` | 127.0.0.1 / 8000 | HTTP 监听 |
| `API_TOKEN` | （空） | 可选 `/api/*` 令牌；空表示不鉴权 |
| `ENABLE_ARCHIVE` | true | 每轮主动归档 |
| `ARCHIVE_SYNC` | true | 同步归档（false 时后台线程归档） |
| `BIG_EVENT_IMPORTANCE` | 0.90 | 大事判定的重要度阈值 |
| `BIG_EVENT_REQUIRE_SELF` | true | 要求事件涉及本人 |
| `DAILY_CONSOLIDATE_ENABLED` | true | 日结开关 |
| `CONSOLIDATE_MIN_TOPICS` | 2 | 日结最少主题数 |
| `CONSOLIDATE_MAX_TOPICS` | 40 | 单次日结最多主题数 |
| `SNAPSHOT_RETAIN_VERSIONS` | 30 | 活动目录保留的快照数 |

调参提示：

- **人格变化太快** → 提高 `PERSONA_SUBSTANTIVE_DELTA`，或收紧 `archiver._SELF_SHAPING_HINTS`。
- **人格几乎不变** → 把阈值降到 0.04，并检查 `persona_deltas` 是否为空。
- **召回太杂** → 提高 `TOPIC_MIN_SCORE`，降低 `TOPIC_TOP_K`。
- **压缩太频繁** → 提高 `MAX_SAFE_TOKENS`，或降低 `HISTORY_INJECT_ROUNDS`。
- **主动消息太多** → 提高 `PROACTIVE_SCHEDULE_MIN_MINUTES`，或降低 `PROACTIVE_MAX_PENDING`。

---

## 12. 验证与已知限制

### 已验证

- 32 / 32 离线冒烟测试通过，包括轮次感知归档、归档幂等、大事判定显式输入、信念溯源、用户纠正、人格冻结/回退、主动联系偏好。
- 离线评测：17 条口语化查询 Recall@1 = 1.000、Recall@3 = 1.000、误召回 0；人格提示词通过全部一致性与隐私检查。
- 真实模型端到端：
  - 回复按 `---` / 空行切成多个气泡，刷新后仍是独立气泡；
  - 好感度阈值事件写入发件箱，并以居中通知展示；
  - 主动联系：模型选择了 45 分钟、4 小时、12 小时、18 小时；到点消息自动触发；用户先开口时时间表 `pending → cancelled`，回复后重新生成；
  - 主动联系在 Web UI 中表现为**一个普通气泡、零系统通知**，刷新后不重复。
- `python -m compileall` 通过。
- `maintenance.py --persona-info` 能正确报告人格资产与运行时路径。

### 已知限制

| 限制 | 现状 | 可能方向 |
|---|---|---|
| 没有主观体验 | 「自我」是可读、可写、可检索的信念数据 | 这是结构性模拟，不是意识 |
| TF-IDF 检索 | 无外部依赖、可解释 | 替换 `VectorStore.add/search/count` 接入本地 embedding |
| 主动联系依赖进程内线程 | 随 `python api/server.py` 提供；WSGI 部署需外部定时器 | 独立 worker 或任务队列 |
| 记忆抽样可能重复 | 已加短期冷却窗口（`REMEMBER_COOLDOWN_MINUTES`）与话题关联加权，明显降低重复 | 仍属启发式；更长期的做法是记录"上次聊过什么"的日志 |
| 好感度只影响提示词 | 关系温度会注入，但没有独立的冷热语气层 | 增加高/中/低表达层级 |
| 抽取 JSON 可能截断 | 自动扩展预算 + 降级归档 | 流式增量 JSON 解析 |
| 日结依赖计划任务 | 已提供 `install_schedule.ps1` | `StartWhenAvailable` 补跑漏掉的运行 |
| 50 轮窗口受 token 限制 | 超过阈值后较早历史被压缩 | 使用长上下文模型时提高 `MAX_SAFE_TOKENS` |

### 本次未做（后续计划）

有两项**刻意留到后续**，写在这里避免被误认为已完成：

1. **API 运行时联调。** `api/server.py` 能通过编译，也有离线测试覆盖，但本开发环境的系统 Python 没有安装 Flask，因此没有真正启动服务做端到端联调。对外提供前，请先在干净环境 `pip install -r requirements.txt`，再 `python api/server.py`，依次验证 `/api/health`、`/api/chat` 以及信念 / 人格 / 主动联系接口。
2. **生产级后台任务。** 现在的 `task` 表 + `TaskStore` + `_run_tasks` 是可运行、可重试、可续跑的**进程内小版本**，不是独立 worker。多 worker 部署时应把调度与重试迁到独立 worker 或队列（Redis / Celery / RQ）；限流目前也是进程内滑动窗口，应迁到网关或共享存储。

---

## 13. 与哲学意义上「人」的边界

它做了什么：

- 工作记忆、前史与主动归档；
- 一个会记录、失效、加固、检测冲突的信念系统；
- 一个带完整因果溯源的可演化人格；
- 一个稳定、每日只允许有限波动的基调；
- 情绪、自我叙事与分层遗忘；
- 在无人说话时，按自己的时间表主动联系。

它没有做什么：

- 没有主观感受（qualia），没有第一人称体验；
- 它的「自我」是一组可读、可写、可检索的信念——对人格的**结构性模拟**；
- 它的「变化」由外部事件触发、由代码决定，而不是由内在欲望驱动。

从工程角度看，它表现得像一个**连续的主体：想法会变，记得为什么变，并保持大体一致**。
从哲学角度看，它是一个高质量的认知模拟器，不是意识。

---

## 14. 延伸阅读

- `docs/belief-system-spec.md` — 信念系统规格（多值、冲突、溯源、演化）
- `docs/improvement-plan.md` — P0–P3 改进方案：改了什么、为什么、文件地图、验证命令、剩余可选项
- `docs/persona-module-review.md` — 人格模块设计评审（参数、映射、节奏）
- `docs/consistency-review-2026-11-11.md` — 一致性评审记录
