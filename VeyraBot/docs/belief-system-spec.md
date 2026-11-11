# 信念系统规格（Belief System Spec）

> 定位：`README` 第三节「第 3 层：结构化信念（kg_store）」的升级规格。
> 把"拟人化认知"的口径落成可实现的字段、判定流程、算法参数与代码落点。
> 状态：**设计规格，本文件未改动任何代码**。落地顺序见第 15 节。

---

## 0. 一句话定义

一条信念 = **一句完整命题** + **一个终身不复用的 13 位标识** + **一条会衰减、会加固、会被更强证据覆盖的置信曲线**。

人际好感是同构的第二条曲线（锚点是"最后一次正向互动"，不是"最后一次确认"），但它没有覆盖、只有并存。

---

## 1. 四条硬约束

| # | 约束 | 违反后的后果 |
|---|---|---|
| 1 | 一条信念只写一句完整命题，`belief_sentence` 是正文，不是摘要 | 整句命题塞进 `tail`，图不连通，退化成事实日志 |
| 2 | 同一命题只有一个 `belief_id`；判定由模型做，但**判定前必须给候选、判定后必须可审计** | 无候选的"让 AI 自己判断"= 空转，等于没判 |
| 3 | 衰减**直接改写存量值**，不做读时派生；归零只改状态**不删行** | 派生值 + 定时 UPDATE 两套并存，必然对不上 |
| 4 | 同一件事出现多条时，**只把弱的那条衰减掉**；不否决、不删除、不判新旧。人关系完全不参与这条 | 两条兴趣互相作废（现有库已实测 21 条 `hold_belief` 里 19 条被误杀） |

---

## 2. 现状与差距

| 能力 | 现状（`kg_store.py` + `database.py`） | 本规格要求 |
|---|---|---|
| 信念载体 | `kg_relation(space_id, rid, head_name, rel, tail_name, confidence, importance, is_active)` | 增列 `belief_id / belief_type / belief_sentence / cardinality / status / settle_ts / ...` |
| 身份 | `rid = md5(space\|head\|rel\|tail)[:12]`，内部哈希 | 对外 `belief_id` 13 位数字，语义派生、碰撞可探测 |
| 多条 | 同 `(head, rel)` 出现不同 `tail` → 旧行 `is_active=0`，无条件、不比置信 | 同一件事多条 → 比置信，高的留、低的减半加速衰减（原行改）；`multi` 并存不比较 |
| 加固 | `confidence += 0.05`（**实际走不到，见第 8 节**） | 饱和增益 + 证据分级；写前必须先结算衰减 |
| 时间 | 只有 `create_ts / update_ts`，无衰减 | 指数衰减，`settle_ts` 做增量结算锚点 |
| 状态 | `is_active` 二值 | `is_active`（被覆盖）与 `status`（active/dormant，淡忘）**正交** |
| 变化记录 | 无 | 月度快照表 + 事件型快照 |
| 类型 | 无 | `fact` / `interpersonal` 两套曲线 |
| 来源 | `source_topic_id` | 增 `provenance`（用户直述 / 用户纠正 / 复述 / AI 推断） |

**正交关系必须先记住**：`is_active=0` 表示**被更强的证据推翻**（有 `superseded_by`）；`status=dormant` 表示**淡忘了**（没有替代者）。一条信念可以"仍有效但已被淡忘"，也可以"被推翻但因刚被提到而还没淡忘"。

---

## 3. 数据模型

### 3.1 `kg_relation` 增列（幂等 ALTER，走 `_COLUMN_MIGRATIONS`）

| 列 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `belief_id` | text | 空 | 事物 = 13 位纯数字；人际 = `p` + 12 位数字；空值 = 尚未回填的老行 |
| `belief_type` | text | `'fact'` | `fact` / `interpersonal` |
| `belief_sentence` | text | 空 | 完整命题正文，召回与展示的主载体 |
| `cardinality` | text | `'single'` | `single` 可覆盖 / `multi` 可并存（写入时由 `kg_rel_schema` 覆写） |
| `settle_ts` | integer | 0 | 上一次衰减结算的时刻，衰减增量从它算起 |
| `last_reinforce_ts` | integer | 0 | 上一次被加固的时刻；人际信念里这是"最后正向互动" |
| `status` | text | `'active'` | `active` / `dormant` |
| `provenance` | text | `'ai_infer'` | `user_direct` / `user_correct` / `user_deny` / `restate` / `ai_infer` |
| `decay_lambda` | real | 0 | 该信念的衰减系数快照（`importance` 变化时重算） |
| `superseded_by` | text | 空 | 被哪条 `belief_id` 覆盖（用户纠正时强制写） |
| `pre_dormant_conf` | real | 0 | 归零前最后的置信值，供复活时取回 |
| `negative_sentiment` | real | 0 | 仅 `interpersonal`：负向互动累计（0~5），**不参与**好感主值 |
| `refined_from` | text | 空 | 被"细化"前的旧句子（`refinement` 判定的留痕） |
| `trial` | integer | 0 | 1=试用期信念（新关系词的首次使用，见 3.3） |

> **命名约定**：`confidence` 列即本文的 `base_conf`，语义改为"**已经结算到当下**的当前置信度"。不新增 `base_conf` 列——两列并存必然双写不一致。文档里写 `base_conf`，代码里读 `confidence`，落地时在 `kg_store` 顶部注释写清同义。

### 3.2 新增三张表

```sql
-- 月度 / 事件型变化快照：只追加，不覆盖，不参与计算
create table if not exists kg_belief_snapshot (
    space_id    text,
    belief_id   text,
    snap_ts     integer,          -- 记录时刻
    snap_month  text,             -- 'YYYY-MM'，本地时区
    base_conf   real,             -- 当时的 confidence
    status      text,             -- 当时的 active / dormant
    changed     integer,          -- 与上一条快照是否不同（0=无变化，只留痕不写值）
    trigger     text,             -- create / monthly / override / dormant / revive / refine / collision
    note        text,             -- 变更原因（覆盖者、加固来源、碰撞槽位）
    primary key (space_id, belief_id, snap_ts)
)
```

```sql
-- 关系本体词表：把开放词表收成"半封闭"，缺元数据不许注册
create table if not exists kg_rel_schema (
    space_id      text,
    rel           text,           -- 关系名（小写英文蛇形）
    description   text,           -- 必填，≥8 字
    domain        text,           -- 主语类型：User / AgentSelf / Topic / Component
    range         text,           -- 取值类型：Person / Place / Scalar / Statement
    cardinality   text,           -- single / multi
    is_core       integer,        -- 1=核心词表，永不计试用期
    first_seen_ts integer,
    use_count     integer default 1,
    status        text default 'active',
    primary key (space_id, rel)
)
```

```sql
-- 被拒候选：弱证据不许覆盖强信念，但不能凭空消失（否则"我当时其实没改主意"无据可查）
create table if not exists kg_belief_candidate (
    space_id    text,
    cand_id     text,
    target_id   text,             -- 被它挑战的 belief_id
    head_name   text,
    rel         text,
    tail_name   text,
    sentence    text,
    confidence  real,             -- 候选自带置信（已结算）
    evidence    text,             -- provenance 同枚举
    topic_id    text,
    verdict     text,             -- rejected（弱于现存）/ pending（同分未决）/ merged（后期合并）
    create_ts   integer,
    primary key (space_id, cand_id)
)
```

### 3.3 `belief_id` 生成规则（这是最容易做错的地方）

用户要求"同一个东西打同一个标识，出现重复标识就说明重复信念"。这件事必须**两套编号、物理隔开**，人不能占事物的号段：

| 类型 | 格式 | 区间 | 容量 |
|---|---|---|---|
| 事物 `fact` | **13 位纯数字** | `0000000000000` ~ `9999999999999` | 10^13 |
| 人际 `interpersonal` | **`p` + 12 位数字** | `p000000000000` ~ `p999999999999` | 10^12 |
| 预留 | 其他字母前缀 | 如 `e…`（经验）/ `r…`（规则） | 各自独立 |

人的编号带 `p` 前缀，**不占用那 13 位数字空间**，所以事物仍有完整的 10^13 可用。

**派生流程**

1. **指纹**：`fp = int(blake2b(f"{belief_type}|{space_id}|{head_norm}|{rel}|{tail_norm}", digest_size=8))`（类型进指纹，同一句话换类型必然不同号）
2. **按类型取号**
   - `fact`：`id = fp % 10^13`，零填充 13 位
   - `interpersonal`：`id = "p" + (fp % 10^12)`，补零到 12 位
3. **二次校验（强制）**：命中同 id 时回读该行的 `(head_name, rel, tail_name, belief_type)`；不一致 → 判为碰撞，**在同一种形态内** +1 探测（事实在 `10^13` 内回绕，人在 `10^12` 内回绕），落 `kg_belief_snapshot(trigger='collision')`
4. **不复用**：id 一旦绑定某命题，即使 `dormant`、即使被否决，也永不重新分配给新命题（墓碑语义）
5. **不用自增分配器**：自增会丢掉"同命题同 id"的语义，也就丢掉"重复标识即重复信念"的判据；自增仅作人工合并的 fallback

**形态判别（代码必须统一用这一套，别各写各的）**

```
len(id) == 13 且全为数字                    → fact
len(id) == 14 且以 'p' 开头、后 12 位全数字  → interpersonal
其余                                        → 脏数据，月度体检时修
```

**三条硬保证**

| 保证 | 机制 |
|---|---|
| 人永不被拉进事物的归并 | 归并候选集按形态过滤（只取 13 位纯数字），人的条目结构上不在里面 |
| 编号一眼可读 | `p…` 是关系，纯数字是事实，排查时不用查字段 |
| 跨类碰撞不可能 | 前缀不同，哈希撞了也落不到同一段 |

### 3.4 字段映射（旧 → 新）

| 旧 | 新 | 说明 |
|---|---|---|
| `rid` | 保留，内部索引 | 不对外暴露；`belief_id` 是新主键 |
| `confidence` | `confidence`（=base_conf） | 语义改为"已结算值"，历史行回填不改数值 |
| `is_active` | `is_active` + `status` | 正交拆分，老行 `status='active'` |
| `importance` | 保留 | 继续参与大事判定，并决定衰减速度 |
| `source_topic_id` | 保留 + `provenance` | 前者管"哪件事"，后者管"谁说的、算多重的证据" |

---

### 3.5 向量库是记忆本体，信念库是认知结论（双路检索）

先把关系摆正：**向量库就是记忆库，所有对话记忆都存在里面**；信念库不存记忆，它存的是从这些记忆里提炼出来的"当前认知"。所以这两层不是"信念为主、往事为辅"，而是**原始记录 + 提炼结论**，缺一层都不成立：只有信念，AI 会变成一张没有经历的立场表；只有记忆，AI 会拿三年前的旧结论当现在的自己。

| | 向量库（记忆本体） | 信念库（认知结论） |
|---|---|---|
| 存什么 | **每一轮对话记忆，一条不漏** | 从这些记忆里提炼出的命题 |
| 回答什么 | 当时到底怎么说的、发生了什么 | 我现在怎么想 |
| 写入时机 | **每轮必写**（寒暄、一次性提问、抽取失败都写） | 只在检出可复用命题时写 |
| 单位 | 一件往事（topic 摘要 + 原始对话） | 一句命题 |
| 检索 | 2-gram + TF-IDF + 同义词扩展，模糊召回 | 实体精确命中 |
| 时间性 | 不衰减，只在日结归并时压缩 | 衰减 / 加固 / 淡化 / 否决 |
| 会不会被推翻 | 不会，发生过就是发生过 | 会 |

#### 写入：每轮无条件进向量库

- 抽取成功 → topic（摘要 + 关键词 + **原文**）入向量库，同一轮再抽信念入信念库
- 抽取失败 → 仍写一条兜底记录（现有 `_save_fallback_topic` 就是这个作用），保证这轮对话不会从记忆里消失
- 寒暄、一次性提问、调试过程，也照样是"她经历过的"，一样入库；只是不会提炼成信念
- topic 行同时留 `abstract`（摘要，进索引）与 `raw_text`（当轮原文，命中后回填 Prompt）

**为什么索引只吃摘要、原文随行**：全文进索引会稀释相似度（一轮对话八成是废话），还会把 Prompt 撑爆。**摘要负责被找到，原文负责被读懂。**

#### 检索：一次查询，两库同时查

不是"先查信念、不够再降级查向量"，是**双路并行**：

```
一次记忆检索
  ├─ 路 A：信念库精确命中 → 【我现在的想法】
  │        生效 / 曾被推翻 / 隐约记得（三档，见 §9.2）
  └─ 路 B：向量库模糊召回 → 【当时的经过】
           topic 摘要 + 命中后回填的原文片段
        ↓
  按 source_topic_id 关联：同一条往事的"结论"与"经过"合并成一组，不重复占位
        ↓
  两个区块分开标注，绝不混成一段
```

**为什么必须分开标注**：向量库里躺着大量"当时成立、现在已经不成立"的说法。一旦把往事和结论混成一段无标注的文本喂给模型，它就会拿旧结论当现在的立场——这才是"记得越多反而越乱"的真实原因，不是记忆量的错。

#### 冲突时：结论归信念，经过归向量

- 事实层面以信念为准（那是更新过的结论）
- 表达层面允许并置："我记得当时是说讨厌橘子的，后来改口了，现在喜欢蜜橘"
- 前提是 Prompt 同时给出原值和现值，这正是【曾被推翻】区块存在的理由

#### 一条铁律：召回不算证据

向量召回了一批往事，**不等于**用户提到了这些事。如果被召回就加息，会自激：召回 → 置信升高 → 排名更前 → 更容易被召回，最后整个库被少数几条自我强化的信念占满。

- 只有本轮对话文本里真实出现该命题（用户说，或 AI 说且用户没否），才算提及 → 加固
- 想观测"被回忆的频率"，另设 `recall_count`，**只计数、绝不改置信**
- 验收办法：同一轮连跑三次，置信度不应有变化

#### 淡忘 / 被否决在两层里的表现

- 信念 `dormant`：往事还在。检索时先出经过，再补一句"我现在不太确定了"——这才是人的说法
- 信念被否决：往事保留，在【曾被推翻】里并列原值与现值
- 不论哪种，**向量层都不做删除、不做降权**

#### 两库各管各的去重

- 向量库：同一件事不重复建 topic（用相似度阈值收敛）
- 信念库：靠 `belief_id`
- 唯一的桥是 `source_topic_id`，别用一层的结论吞另一层：向量相似 ≠ 同一信念，信念相同 ≠ 同一件事

---


## 4. 同一信念的判定（用户第 1 点：严格，且由 AI 判断）

### 4.1 三级流程

```
写入请求（新抽取的候选命题）
  │
  ├─ ① 粗筛（代码，无模型）
  │     同 space、is_active=1 或 status=dormant 全部在池
  │     head 实体名（含别名归一）精确命中 或 tail 关键词命中
  │     按 importance × confidence 取 top K（K=8）
  │     K 为空 → 直接新建，不调用判定（省一次模型调用）
  │
  ├─ ② 精判（模型，一次调用，输入=候选清单 + 新命题）
  │     输出三态：same / refinement / distinct（外加 reason 与 match_belief_id）
  │
  └─ ③ 落档（代码，按三态决定写路径）
```

### 4.2 精判四问（全部通过才算 `same`）

| 问 | 判据 | 反例 |
|---|---|---|
| 主体同一？ | 别名归一后指向同一实体（我/江妍/她 → `config.PERSONA_NAME`） | "江妍喜欢橘子" vs "室友喜欢橘子" |
| 关系语义同一？ | 关系词在同义组内，或 `kg_rel_schema.description` 指向同一维度 | `like` vs `dislike` |
| 取值同一？ | 指向同一取值；允许一方更细（→ `refinement`），但不允许"同类不同物" | "橘子" vs "蜜橘" → same/refinement；"橘子" vs "苹果" → distinct |
| 限定不冲突？ | 时间、范围、条件、程度必须能对齐 | "周一有空" vs "周末有空" → distinct |

### 4.3 三态与写路径

| 判定 | 写路径 |
|---|---|
| `same` | 复用 `belief_id`，执行**加固**（§5.2）；`cardinality=multi` 时同时并存 |
| `refinement` | 复用 `belief_id`，用更细的句子**替换** `belief_sentence`，旧句写入 `refined_from` + 快照 `trigger='refine'`；随后加固 |
| `distinct` | 新建 `belief_id`，走**竞逐覆盖**（§5.3）或并存 |

### 4.4 严格兜底（宁可多一条，不可错覆盖）

- 模型输出 `same` 但 `match_belief_id` 不在候选清单 → **降级为 `distinct`**（防模型编 id）
- 模型输出任何态但缺 `reason` → 降级为 `distinct`
- 候选超过 K 被截断、且新命题与"被截断部分"可能同源 → 留 `kg_belief_candidate(verdict='pending')`，等月度"同一性体检"再合并
- **不允许模型返回 13 位 id**：id 一律由代码按 §3.3 派生，模型只能选候选

### 4.5 定期"同一性体检"（严格判定的必要代价）

严格判定的代价是重复条目会攒下来。月度任务里加一步：同一 `(head, rel)` 下 `belief_sentence` 相似度超过阈值的 `active` 条目，列出来做二次合并（保留 `confidence` 高的，另一条 `is_active=0` + `superseded_by` + 快照留痕）。不做这一步，库会以每月十几条的速度长毛。

---

## 5. 置信算法（用户第 4 点，由我定）

### 5.1 `fact`：指数衰减 + 直接改写存量

```
λ_eff = BELIEF_DECAY_LAMBDA × (1 − 0.6 × importance)        # 重要的记得久
Δd    = (now − settle_ts) / 86400
confidence ← confidence × exp(−λ_eff × Δd)                  # 写回同一行，不做读时派生
settle_ts ← now
```

| 参数 | 默认 | 含义 |
|---|---|---|
| `BELIEF_DECAY_LAMBDA` | 0.011 /天 | 基线半衰期 ≈ 63 天；`importance=1` 时 ≈ 157 天 |
| `BELIEF_DORMANT_EPSILON` | 0.05 | 低于此值即归零 |
| 归零动作 | — | `pre_dormant_conf = confidence`；`confidence = 0`；`status = 'dormant'`；**行不删** |
| 复活 | — | `confidence = max(pre_dormant_conf × 0.5, 0.15)`，再走一次加固，`status='active'`，快照 `trigger='revive'` |

> 为什么把衰减写回存量而不是读时算：用户要的是"直接在原有信念上改动"。读时派生会让 `confidence` 与真实值长期不一致，而 `query_by_text` 又会按 `confidence` 排序——一套派生、一套排序，必然对不上。

### 5.2 加固（饱和增益，永不越界）

```
confidence ← confidence + w × (1 − confidence)
last_reinforce_ts ← now
```

| `provenance` | `w` | 场景 |
|---|---|---|
| `user_direct` | 0.20 | 用户第一次亲口陈述 |
| `user_confirm` | 0.12 | 用户重复确认 / 顺着说"对，就是这样" |
| `restate` | 0.06 | AI 在回答里复述，被用户默认接受 |
| `ai_infer` | 0.03 | 纯模型推断，无用户背书 |

> 不用 `confidence += 0.05`：线性加法会饱和到 1.0，之后所有覆盖比较失去意义。饱和增益永远逼近上限、永不越过，且弱证据抬不动强信念。

### 5.3 同一件事多条：留强的，衰弱的（仅 `fact`）

**只有一条规则**，不分"重复"还是"改主意"：

> 同一件事在库里出现多条——同一个 `belief_id` 的多行，或同一 `(head, rel)` 单值槽位下的多个取值——**比当前置信度，高的留着，低的那条衰减掉**。

| 步骤 | 动作 |
|---|---|
| 认弱条 | 按 `confidence` 排序，非最高的都算弱条 |
| 衰减弱条 | `confidence ← confidence × 0.5`；`decay_lambda ← decay_lambda × 2`（之后掉得更快）；`superseded_by` 指向强条 |
| 写回位置 | **在原行上改**，不新建行、不删行 |
| 强条 | 不动。赢了一次不加分——加分是"被提及加固"的事 |
| 后续 | 弱条继续走日常衰减，到 ε 归零 → `status='dormant'`，句子与快照都留着 |
| N≥3 | 取最高者为强条，其余全部按上式处理 |
| 触发 | 每次写入该 `belief_id` / 该槽位后立刻检查，最多 2~3 行，成本可忽略；不等月度 |
| 候选集 | **按形态过滤：只取 13 位纯数字的 id**（人的 `p…` 天然落在候选集之外，不存在"忘了排除人"的可能） |

**三个必须守住的细节**：

1. **比的是已结算值**，不是刚抽取的原始值。拿新鲜值去比老行的衰减值，等于每轮都把旧信念判死刑。
2. **这是"淡化"，不是"否决"**。弱条 `is_active` 仍为 1，只是置信低；读路径按 `(head, rel)` 去重、只呈现最高一条，不让两条并列。真正关掉一条信念只有一种情况：用户明确说"不对"（§5.4）。
3. **`cardinality=multi` 不比较**。喜欢橘子、喜欢甜食是两条各自成立的事，没有"弱"可言。

### 5.4 否决与纠正（唯一会真正关掉一条信念的路径）

前面 §5.3 只是"让弱的淡下去"，行还在、`is_active` 还是 1。真正把一条信念关掉，只有下面两种：

| 证据 | 动作 |
|---|---|
| `user_correct`（"不对，是杭州不是上海"） | 旧行 `is_active=0` + `superseded_by=新id`；新行 `confidence = max(0.85, 候选)`。**用户的话是最高权威，不比置信** |
| `user_deny`（"我没这么说过"） | 旧行 `is_active=0`、`superseded_by` 留空（判为伪信念），进"我曾不确定"档 |

### 5.5 `interpersonal`（用户第 3 点：好感曲线）

人际条目的编号一律以 `p` 开头（`p000000000000` ~ `p999999999999`），与事物的 13 位纯数字物理隔离——不是靠类型字段区分，是号段本身就分开了，也不占用那 13 位空间。

```
λ_int = INTERPERSONAL_DECAY_LAMBDA × (1 − 0.3 × closeness)   # closeness 可用 importance 代理
Δd    = (now − last_reinforce_ts) / 86400                    # 锚点是"最后正向互动"
affection ← affection × exp(−λ_int × Δd)
```

| 参数 | 默认 | 含义 |
|---|---|---|
| `INTERPERSONAL_DECAY_LAMBDA` | 0.015 /天 | 半衰期 ≈ 46 天；比事实信念挥发得快 |
| `INTERPERSONAL_DORMANT_EPSILON` | 0.10 | 低于此值归零 → `dormant` |
| 正向互动 | `+0.10 × (1 − affection)` | 聊天愉快、被记住、被帮助 |
| 负向互动 | `−(0.15)` 线性，下限 0 | 被冒犯、被敷衍；同时 `negative_sentiment += 1`（上限 5） |
| 覆盖 | **不存在** | 同一人可以并存"我信任她""我喜欢和她聊天""她让我有点紧张"，各自独立 id |

> `negative_sentiment` 独立累计而不是直接扣主值，是为了让"好感 0.6 但受过一次冒犯"和"好感 0.6 一路顺遂"可区分——拟人化里这两者的表达完全不同。
>
> **人际条目照样衰减**——它走的就是本节这条好感曲线，锚点是"最后一次正向互动"，不是"最后一次被确认"。它唯一不参与的是 §5.3 的"多条比强弱、弱条减半"归并：同一个人身上的多条命题（信任 / 喜欢聊天 / 有点紧张）各自独立，互不比较、互不淘汰。也**不参与 §5.4 的否决**：用户纠正的是对某件事的说法，不会让"我对这个人的感觉"因为一句话被关掉——好感只会被互动本身改变。
>
> 代价：人际条目一旦重复（历史导入或判定失误）不会自动归并，只能靠月度体检人工处理——已知缺口，见 §14。

---

## 6. 时间与结算（谁在什么时候算衰减）

| 时机 | 触发点 | 动作 | 成本 |
|---|---|---|---|
| 写库前 | `upsert_belief` 入口 | 对该行 + 候选集逐行 `settle` | O(K)=8 |
| 每轮归档 | `archiver.archive` 步骤 4 | 只结算本次触碰到的候选集 | O(K) |
| 日结 | `maintenance --daily` 末尾 | 全空间批量结算，每批 500 行 | 万级 < 1s |
| 月度 | 日结末尾判定"是否跨月" | 快照评估 + 同一性体检 | 一次性 |
| 纯查询 | `query_by_text` | **只读，不结算**（避免读放大写） | 0 |

> 查询不结算的后果是"读到的置信度可能比真实值新"，但相差一轮归档（分钟级），对排序无影响；换来的是查询零写放大。

### 6.1 月度快照规则（用户第 2 点）

- 锚点：该信念 `create_ts` 的**本地日期**（`Asia/Shanghai`），不是自然月 1 号
- 每月同日评估一次：`confidence` 或 `status` 与上一条快照**不同**才写新行（`changed=1`）；相同则只更新评估时间，不写值
- 首次创建写一条（`trigger='create'`，`base_conf` = 初始值）
- 事件型快照与月度并存，`trigger` 区分：`override / dormant / revive / refine / collision`
- 快照表**只追加**，永不修改；不参与任何计算，只用于回溯"这条信念是怎么变的"

---

## 7. 写入口的句法规则（用户第 6 点：写入时就要细致）

`belief_sentence` 是这条信念的全部正文，所以它必须自带完整语义，脱离上下文也读得懂。硬规则（`extractors.py` 校验，不合格直接丢弃该条）：

| 规则 | 判据 | 反例 |
|---|---|---|
| 必有明确主体 | 不允许"它/那个/这个"作主语 | "喜欢吃甜的" |
| 一条只承载一个命题 | 出现两个并列谓词 = 拆两条 | "喜欢橘子并且讨厌苹果" |
| 必带必要限定 | 时间/条件/程度/例外交代清楚 | "她最近很忙"（最近是哪阵？） |
| 不拼元数据 | 句子内禁止出现置信度、时间戳、关系名 | "江妍 like 橘子 confidence 0.6" |
| 长度 15~120 字 | 太短=没信息，太长=塞了多句 | — |

**`tail` 与 `belief_sentence` 的分工**：`tail` 只放规范化取值（实体名或标量，≤20 字），负责精确索引与图连通；`belief_sentence` 放完整命题，负责召回与展示。现有库的退化形态正是"整句命题躺在 `tail` 里"——落地时必须同时约束两者。

---

## 8. 已核实的现存缺陷（不改代码，但必须作为落地第 0 步）

**"重复加固"分支是死代码。**

链路：`archiver.py:274` 的 `rid = _stable_id(space, head, rel, tail)` = `md5(space|head|rel|tail)[:12]`，**同三元组必然同 rid**。而 `kg_store.upsert_relation` 里：

1. 查同 `(head, rel)` 的生效行；
2. 循环第一句就是 `if row["rid"] == relation.rid: continue` → 命中自己 → 跳过；
3. 于是 `boosted` 保持 `False`；
4. 落到 `insert ... on conflict (space_id, rid) do update set confidence = excluded.confidence` → **用模型这次给的原始值覆盖**。

后果：同一信念被反复确认时，置信度**不是**累加，而是每轮被重置为抽取值（模型给 0.7，就永远 0.7）。而第 2 分支（`tail` 相同、`rid` 不同）因为 `rid` 含 `tail`，实际不可达——整个加固逻辑从未执行过。

**实测（本次已跑，临时库副本，未碰 `data/memory.db`）**：

```
场景1 同一三元组连写两次（rid 相同，等同生产路径）
  round1 -> {'conflicts': [], 'boosted': False, 'created': True}   0.600
  round2 -> {'conflicts': [], 'boosted': False, 'created': True}   0.600   ← 没加固，被原始值覆盖

场景2 对照：tail 相同、rid 不同（生产里不可达的路径）
  write B -> {'boosted': True, 'created': False}                   0.650   ← 只有这条分支会 +0.05

场景3 同一 (head, rel) 两个取值
  like 橘子 -> like 甜食
  -> conflicts=[('橘子','甜食')]，橘子 is_active=0                  ← 多值被当成冲突
```

三条结论坐实：加固死分支、`created` 计数在命中已有行时也置真、多值无条件误杀。

**修法**：删除 `rid` 跳过判断，改为"同 `(space, head, rel, tail, belief_type)` 已存在 → 直接走加固分支"；或保留 rid 但把 `continue` 改成命中即加固。

**第二个缺陷（同一处）**：`created=True` 在"命中已有行"时也会被置真（因为走的是 insert 路径），归档报告里的 `relations` 计数因此高估，健康度统计会失真。

**第三个缺口（读路径）**：`query_by_text` 只用 `kg_entity.name` 做子串匹配，**尾节点不进匹配池**。只在关系里出现、未登记为实体的取值永远召回不了，且实体表里混入 `topic_xxx` 这类内部 id 会污染匹配。落地时把 `tail_name` 并入匹配池，并禁止内部 id 进实体表。

---

## 9. 落点清单（文件 / 函数 / 改什么）

| 文件 | 位置 | 改动 |
|---|---|---|
| `config.py` | 信念参数区 | 新增 `BELIEF_DECAY_LAMBDA` / `BELIEF_DORMANT_EPSILON` / `BELIEF_BOOST_WEIGHTS` / `INTERPERSONAL_*` / `BELIEF_MATCH_TOP_K` / `BELIEF_MONTHLY_ENABLED` / `BELIEF_SOFT_RECALL_MAX` / `BELIEF_TRIAL_CONF_CAP` |
| `cognitive/storage/database.py` | `_SCHEMA_STATEMENTS`、`_COLUMN_MIGRATIONS` | 三张新表 + 13 个新列；`topic` 增 `raw_text`（当轮原文，命中后回填 Prompt）；索引 `(space_id, status, settle_ts)`、`(space_id, belief_id)` 唯一 |
| `cognitive/models.py` | `KGRelation` | 新增对应字段；`default_factory` 保持时间戳语义 |
| `cognitive/storage/kg_store.py` | 重写核心 | 新增 `settle_decay()` / `assign_belief_id()`（按类型分块取号，块内线性探测）/ `match_candidates()`（只取事实块）/ `upsert_belief()` / `snapshot()` / `merge_duplicates()`；`upsert_relation` 收编为内部实现；`query_by_text` 排序字段与 dormant 处理；`query_overturned` 扩成三类 |
| `cognitive/cognition/archiver.py` | 步骤 3 / 4 | 步骤 3 保证**每轮必写 topic**（失败也走兜底，带 `raw_text`）；步骤 4 改为 `upsert_belief`，处理 `same/refinement/distinct` 三态；`rid` 不再作为业务 id |
| `cognitive/engine.py` | 步骤 5 / 7 | 记忆检索改为**双路并行**：KG 精确 + 向量模糊同时出，两个区块分开标注；按 `source_topic_id` 合并同一往事的"结论"与"经过" |
| `cognitive/cognition/extractors.py` | `_normalize_relations` | 校验 `belief_type` / `belief_sentence` / `evidence` / `cardinality`；实现 §7 的句法规则；**不接受模型给的 id** |
| `cognitive/cognition/prompts.py` | `ARCHIVE_SYSTEM_PROMPT` | 增补 §7 规则、候选清单注入块、三态判定字段、关系词表试用期说明 |
| `maintenance.py` | `main()` + 新增 `cmd_belief_monthly` | `--belief-monthly`；同时挂到日结末尾（跨月才执行） |
| `install_schedule.ps1` | 计划任务 | 复用现有 23:50 日任务，不新增任务（跨月逻辑在任务内部判定） |
| `tests/smoke_test.py` | 新增用例 | §11 的 8 条探针 |

### 9.1 关系词表：半封闭 + 试用期（用户第 5 点）

- **核心词表**（`is_core=1`，预置，永不试用）：`hold_belief` / `prefer_style` / `dislike` / `use_language` / `live_in` / `work_at` / `study_at` / `own` / `related_to` / `feel_about` / `trust` / `care_about` / `plan_to`
- **新关系注册**必须齐四件：`description`（≥8 字）、`domain`、`range`、`cardinality`。缺一件 → **拒写**，模型只能改造已有关系或用 `related_to` 兜底
- **试用期**：首次出现的非核心关系，其信念 `trial=1`，`confidence` 上限 `BELIEF_TRIAL_CONF_CAP=0.70`，且不参与单值覆盖（只并存）。同一 `rel` 被用到第 3 次才解除试用期、写入 `kg_rel_schema.use_count>=3`
- **单轮上限**：一次归档最多新增 1 条非核心关系；同一 space 的 `rel` 总数超过 60 时，新注册需人工确认（这个数字就是"词表爆炸"的熔断点）

### 9.2 读路径的三档展示（把"淡忘"变成可感知的拟人行为）

| 档 | 取什么 | Prompt 区块 | 上限 |
|---|---|---|---|
| 当前相信 | `is_active=1` 且 `status=active` | 【结构化信念】 | `KG_MAX_ROWS`=15 |
| 已被推翻 | `is_active=0` 且有 `superseded_by` | 【曾被推翻的信念（原值）】 | 3 |
| 记得不太清 | `status=dormant` 且 `is_active=1` | 【隐约记得的事（不敢确定）】 | `BELIEF_SOFT_RECALL_MAX`=2 |

> 第一档要按 `(head_name, rel)` 分组、只取 `confidence` 最高的一条进 Prompt。被淡化但仍 `active` 的弱条（`superseded_by` 已有值）不在这里重复出现——两条并列只会让 AI 以为自己记岔了。

第三档是拟人化的关键：没有它，AI 会对自己淡忘的事说"我完全不记得"，比"我好像记得…但不确定"更像机器。开关 `BELIEF_SOFT_RECALL_ENABLED`，默认开。

---

## 10. Prompt 契约变更（`ARCHIVE_SYSTEM_PROMPT`）

`relations` 每项新增：

```
"belief_type": "fact | interpersonal",
"belief_sentence": "一句完整命题（主体+关系+取值+必要限定，15~120字，禁止代词，禁止并列两件事）",
"evidence": "user_direct | user_correct | user_deny | restate | ai_infer",
"match": {"verdict": "same | refinement | distinct", "belief_id": "候选清单里的id或空", "reason": "20字内"}
```

候选清单在 user 消息里以独立区块注入，**只在 K>0 时出现**：

```
【可能相关的已有信念（判定同一性用；不在清单里就不能说 same）】
- 0001234567890 [fact/single, 置信0.62, 30天未提] 江妍喜欢吃果皮薄、甜度高的蜜橘
- 0000000000017 [interpersonal/multi, 好感0.71] 我信任林夏，遇事会和她说
```

配套规则补 5 条：三态定义、四问判据、`same` 必须给清单内 id、`refinement` 只在"新命题更细"时用、`cardinality` 以词表为准（模型填了也会被代码覆写）。

---

## 11. 验收探针（能跑才算落地）

| # | 场景 | 断言 |
|---|---|---|
| 1 | 同一句连写两次 | 第二次 `boosted=True`，`confidence` 递增（现状是 `False` 且不变） |
| 2 | `like 橘子` + `like 甜食` | 两条都 `is_active=1`（现状第二条会作废第一条） |
| 3 | 同槽位两条，新的更强：`live_in 上海` 0.62 → `live_in 杭州` 0.80 | 上海条 conf 减半、`decay_lambda` 翻倍、`superseded_by` 指向杭州；**两行都在**；读路径只呈现杭州 |
| 4 | 同槽位两条，新的更弱：杭州 0.45 对上海 0.80 | 杭州条减半并指向上海；上海条一分不动；行数不变 |
| 5 | 同 id 两条（模拟历史导入） | 弱条减半 + `superseded_by`；强条不动；不新建行 |
| 6 | 衰减与归零：把 `settle_ts` 回拨 200 天 | `confidence` 归零 → `status='dormant'`，**行数不变**，`pre_dormant_conf>0` |
| 7 | 复活：dormant 条目再被提及 | `status='active'`，`confidence ≥ 0.15`，快照 `trigger='revive'` |
| 8 | 月度快照：把 `create_ts` 回拨 3 个锚点日 | 产生 3 条以内 `changed=1` 的快照，值等于当时 `confidence` |
| 9 | 人际：`last_reinforce_ts` 回拨 90 天，再正向互动一次 | 先归零 dormant，互动后回升，`negative_sentiment` 不变 |
| 10 | 人际不参与归并：同一人两条命题，好感 0.8 / 0.5 | **都不减半、不互相指向**；各自按"最后一次互动"各自衰减，互不影响 |
| 11 | 召回闭合性 | `(尾节点 ∩ 匹配池) / 尾节点` 不低于 70% |
| 12 | 词表健康度 | 只出现 1 次的 `rel` 占比 < 30%；`rel` 总数 < 60 |
| 13 | 碰撞 | 人为构造同槽位不同命题，第二次应探测到下一槽并留 `trigger='collision'` 快照 |
| 14 | 类型区隔 | 同一个人既有人际条目又有同名主体的事实条目：两条编号分别为 `p…` / 13 位数字；同槽位写入时**只有事实那条进归并**，人际那条置信分毫不动 |
| 15 | 全量入库：连跑 10 轮（含寒暄、一次性提问、一次抽取失败） | `topic` 表新增 **10 条**；每条都能从 `raw_text` 取回当轮原文；抽取失败那条也要有 |

```sql
-- 探针 14：编号形态是否与类型一致（c_invalid 应为 0）
select belief_type,
       case
         when length(belief_id) = 13 and belief_id not glob '*[^0-9]*' then 'a_fact_13'
         when length(belief_id) = 14 and belief_id glob 'p????????????' then 'b_person_p12'
         else 'c_invalid'
       end as shape,
       count(1) as cnt
from kg_relation
where space_id = :space
group by belief_type, shape order by shape;

-- 错配检查：fact 却带 p 前缀、interpersonal 却不是 p 开头（应为 0）
select count(1) as mismatch_cnt
from kg_relation
where space_id = :space
  and ((belief_type = 'fact' and belief_id like 'p%')
    or (belief_type = 'interpersonal' and belief_id not like 'p%'));
```

```sql
-- 探针 1：加固是否真的发生（连写两次后 delta 应为正）
select head_name, rel, tail_name, confidence, update_ts
from kg_relation
where space_id = :space and belief_id = :belief_id;

-- 探针 15：每轮是否都进了记忆库（应等于本轮对话轮数）
select count(1) as topic_cnt,
       sum(case when raw_text is null or raw_text = '' then 1 else 0 end) as missing_raw
from topic
where space_id = :space and create_ts >= :since;

-- 探针 5：归零但不删（应为 0 行差异，且 status 变化）
select count(1) as total,
       sum(case when status = 'dormant' then 1 else 0 end) as dormant_cnt
from kg_relation where space_id = :space;

-- 探针 9：尾节点能否进匹配池
select count(1) from (
  select distinct tail_name as n from kg_relation where space_id = :space and is_active = 1
  intersect
  select distinct name as n from kg_entity where space_id = :space and is_active = 1
);

-- 探针 10：词表是否爆炸
select count(1) as rel_cnt from kg_rel_schema where space_id = :space;
```

---

## 12. 配置项（默认值即建议值）

| 变量 | 默认 | 作用 |
|---|---|---|
| `BELIEF_DECAY_LAMBDA` | `0.011` | fact 基线衰减（半衰期 63 天） |
| `BELIEF_DORMANT_EPSILON` | `0.05` | fact 归零阈值 |
| `BELIEF_BOOST_WEIGHTS` | `{"user_direct":0.20,"user_confirm":0.12,"restate":0.06,"ai_infer":0.03}` | 证据权重 |
| `BELIEF_MATCH_TOP_K` | `8` | 判定候选条数 |
| `BELIEF_TRIAL_CONF_CAP` | `0.70` | 试用期关系词置信上限 |
| `BELIEF_MONTHLY_ENABLED` | `true` | 月度快照总开关 |
| `BELIEF_SOFT_RECALL_ENABLED` | `true` | "隐约记得"档 |
| `BELIEF_RECALL_COUNT_ENABLED` | `false` | 只统计"被回忆了几次"，**绝不改置信**（防自激） |
| `BELIEF_SOFT_RECALL_MAX` | `2` | 该档条数 |
| `INTERPERSONAL_DECAY_LAMBDA` | `0.015` | 好感衰减（半衰期 46 天） |
| `INTERPERSONAL_DORMANT_EPSILON` | `0.10` | 好感归零阈值 |
| `BELIEF_REL_SCHEMA_MAX` | `60` | 词表熔断点 |

---

## 13. 迁移与回滚

**迁移（三步，可停在任意一步）**

1. **加列 + 建表**：全部 `ADD COLUMN` 可空、带默认值，旧代码忽略新列 → 向后兼容
2. **回填老行**：老库没有 `belief_type`，一律先按 `fact` 回填——`belief_id` 取 13 位指纹；`belief_sentence = head + " " + rel + " " + tail`（只能这样补，标 `provenance='ai_infer'`、`trial=1`）；`settle_ts = update_ts`；`status = 'active'`
   - 人际老行（关系网/好感类，`rel ∈ {trust, care_about, feel_about}` 且 tail 是人名）不能在 SQL 里可靠识别，留给回填后的一次模型批量改标：改标时**必须重发 `p…` 号**，旧 id 写进快照 `note` 留墓碑。这是"id 永不复用"之外唯一允许换号的场景，要在代码注释里写明。
3. **切写路径**：`archiver` 改走 `upsert_belief`；`kg_store.upsert_relation` 保留为兼容包装

**回滚**：新列全部可空，旧代码自动忽略；把 `archiver` 指回 `upsert_relation` 即可，数据无损。

**风险点（迁移前必须先确认）**：回填的 `belief_sentence` 是拼接句，不是自然命题，会拖累 §4 的判定质量。建议回填后跑一次"句子体检"，把 `belief_sentence` 长度 < 10 字或明显是拼接的行列出来人工/模型重写，再开启新写路径。

---

## 14. 已知边界（写清，不假装覆盖）

1. **语义矛盾抓不到**：`(用户, live_in, 上海)` 与 `(用户, 住杭州, ...)` 关系词不同，不会进入同一槽位归并。要真做矛盾检测得上 NLI 或 embedding，本机装不了 `sentence-transformers`（`pip` 不可用），本期只做"同维度槽位比强弱"的弱版本。
2. **判定是概率性的**：严格兜底会攒重复条目，§4.5 的月度体检是配套成本，不做就会长毛。
3. **衰减结算成本**：全空间 O(n)，万级可接受；十万级需按 `(space_id, status, settle_ts)` 分批并考虑只在日结跑。
4. **时区**：月度锚点用 `Asia/Shanghai` 本地日对齐，库里存 UTC 秒；跨时区部署时锚点会漂。
5. **id 不可读**：13 位纯数字、`p…` 都不适合人工排序，排查要靠 `belief_sentence` 与快照表。这是换取"同命题同 id"的代价。
6. **两层边界不可省**：信念库答"我现在怎么想"，向量库答"发生过什么"；把往事当结论用会让 AI 拿旧观点当现在的立场（§3.5）。
7. **本文件未改代码**：§8 的死代码缺陷、§9 的落点清单都还是待办。

---

## 15. 落地顺序（建议）

```
第 0 步：修 §8 死代码（不改算法，只让加固真的生效）→ 探针 1
第 1 步：加列建表 + 回填 + 句子体检 + 人际改标     → 探针 6/11/12/14
第 2 步：settle_decay + 归并衰减 + 候选表          → 探针 2/3/4/5
第 3 步：判定三态接入 prompt + 候选注入            → 探针 13 + 人工抽样 20 条判定
第 4 步：人际曲线                                  → 探针 9/10
第 5 步：月度快照 + 同一性体检                      → 探针 8
第 6 步：读路径三档展示                            → 实际对话体感验收
第 7 步：两库联合（信念块与往事块分离、召回不算证据）→ 同轮连跑三次置信不变
```

前两步做完，这套系统就已经"活"了；后四步是把它从"能跑"推到"像人"。
