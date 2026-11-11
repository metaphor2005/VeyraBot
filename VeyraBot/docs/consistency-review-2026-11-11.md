# 代码与说明书一致性审查（2026-11-11）

审查对象：`cognitive-agent/` 全量源码、`README.md`、`docs/belief-system-spec.md`、`docs/persona-module-review.md`、`data/` 真实运行库。
方法：逐条读代码 → 对 `data/memory.db` 与 `data/vectors/*.json` 做只读探针（在临时副本上，不碰原库）→ 跑 `tests/smoke_test.py` → 用 mock 模式跑一轮 `CognitiveAgent.chat` 做端到端复现。

---

## 一、结论

说明书主体（六层架构、三档节奏、基调+摆动+日结、参数翻译规则）与代码是**对得上**的：14 个参数、三档倾向文本、配置表 50 项数值逐一相符、计划任务确实注册且 `LastTaskResult=0`。

但有 **3 处"文档说能用、代码实际不能用"**，其中第 1 条会让整个对话在真实库上直接崩：

| 级别 | 问题 | 影响 |
|---|---|---|
| P0-1 | `VectorStore.search` 必然抛 `TypeError`（`self._idf` 属性遮蔽了 `_idf()` 方法） | 主题召回整层失效；有历史主题的空间**每一轮对话都失败** |
| P0-2 | 已归并主题只改 DB 标记，向量索引不删、召回不过滤 | 日结"不再参与召回"没做到，重复命中照旧 |
| P0-3 | 未跃迁的日结会把即时自我认知清掉且不落快照 | 昨天学到的"我是谁"今天丢，与 README"未跃迁则保留"相反 |

另外 README 自身有两处**同文档内互相打脸**（第 10 步说 KG"权重恢复"，第 132 行说加固是坏的；§九又说"会加固"），以及若干可核验数字偏差。

---

## 二、P0：文档声称可用，实际不可用

### P0-1 向量检索必然崩溃

- 文档：README §三"检索算法三层叠加"、`实测 4/6 → 6/6`、§十"已加同义扩展补换种说法"。
- 代码：`vector_store.py:73` 在 `__init__` 里写 `self._idf: Dict[str, float] = {}`，而 `vector_store.py:137` 又定义同名方法 `def _idf(self)`。实例属性把方法覆盖成 dict，`search()` 第 269 行 `idf = self._idf()` 直接 `TypeError: 'dict' object is not callable`。
- 实证：
  - `python tests/smoke_test.py` → `[异常] 向量检索`、`[异常] 改写式召回`，**共 16 项，失败 2 项**（README §六 写"预期输出 16 项全部通过"）。
  - `CognitiveAgent.chat`（mock 模式）：
    - `demo_space` → CRASH `TypeError`，栈顶 `hits = vector.search(query)`（router.py:55）
    - `jiangyan` → 同上
    - 空索引的新空间 `fresh_space` → 正常返回（因为 `search` 在 `not self._docs` 时提前 return，压根没走到 `_idf()`）
- 影响面：`engine.py:176 memory = self.router.route(...)` **没有 try/except**，异常直接穿出 `chat()`。也就是说：一旦某个空间有过任意一条主题，这个空间就再也答不了话。README 里所有"召回实测"（前史召回、曾被推翻 3 条、6/6 命中）在当前代码上都不可复现。
- 修法：
  ```python
  # __init__
  self._idf_cache: Dict[str, float] = {}
  self._idf_cache_ts = 0
  # _idf() 内改用 self._idf_cache；add() 末尾把 self._idf_cache_ts = 0 使缓存失效
  ```
  另建议给 `engine.py:176` 包一层 try/except，检索失败降级为"无主题召回"而不是整轮失败。

### P0-2 已归并主题仍在召回池里

- 文档：README §三之六"原主题 mark_consolidated → consolidated=1 …**只是不再参与日常主题召回**"；§九"日结后原始事件退居可溯源层"。
- 代码：`topic_store.mark_consolidated` 只 `update topic set consolidated=1`；`VectorStore` 没有 remove/更新接口；`router.route` 拿到 `hits` 后只 `get_topic`，**不看 `consolidated`**；`get_topic` 也不过滤。
- 实证（只读统计）：
  - `demo_space`：主题 7 条、全部 `consolidated=1`；向量索引 7 篇，**7 篇仍是已归并主题**。
  - `jiangyan`：已归并普通主题 13 条；向量索引 16 篇，其中 **13 篇是已归并主题**。
  - 换句话说，标记只写进了 SQLite，索引侧与读路径都不认这个标记。
- 修法（二选一）：
  1. `router.route` 里 `topic = topics_store.get_topic(...)` 后 `if topic.consolidated: continue`；
  2. 或 `mark_consolidated` 时同步 `VectorStore.remove(topic_ids)` + `search` 按 meta 过滤。
  建议 1（读路径过滤最稳，索引仍可用于溯源检索）。

### P0-3 未跃迁时"即时自我认知"被丢弃

- 文档：README §四"即时层自我认知已固化进快照（**或本次未跃迁则保留**），下次继续"。
- 代码 `consolidator._settle_persona`：
  - `if not steps: return` —— 当天没有基调位移时**直接返回**，`live_self_view` 既不固化也不清空（这一支至少没丢）；
  - 有 steps 但未跃迁时：写了 `self_view` 变迁日志 → `kernel.clear_live_self_view()`，**但只有 `substantive` 分支才 `snapshots.create()`**。于是即时层被清空、快照仍是旧 self_view，第二天 Prompt 回到旧表述。
- 实证（`jiangyan`）：
  ```
  快照版本 v0；live_self_view 为空；
  persona_changelog: self_view / source_type=daily_settle / 1 条；
  v000_init.json 的 self_view 仍是初始值
  ```
  正好对应 README §六 里那句"变迁日志 +1（自我认知表述变了）"——日志记了，人格没吃到。
- 修法：把 `clear_live_self_view()` 收进 `if substantive:` 分支；未跃迁时保留 live（或把新 self_view 写进当版快照的 `self_view` 字段，不动版本号）。

---

## 三、P1：文档与实现不一致（能跑，但描述不对）

| # | 文档说法 | 代码事实 / 证据 |
|---|---|---|
| 1 | §二 第 10 步"信念抽取 → KG（含冲突检测与**权重恢复**）"；§九"会记录、会失效、**会加固**、会冲突检测" | 加固分支永不可达：`archiver` 的 rid = `_stable_id(space, head, rel, tail)`，同一信念必然同 rid；`kg_store.py:131 if row["rid"] == relation.rid: continue` 跳过的正是自己。实测同 rid 连写两次：`boosted=false`，置信度仍 `0.60`。README 第 132 行已自曝此缺陷，但第 62 行与第 831 行没改，属**同文档自相矛盾** |
| 2 | 冒烟项"KG 权重加固 [通过]" | 通过≠生效：`smoke_test.py:76-99` 用 `r1`/`r2` 两个不同 rid 造出了"同 tail 不同 rid"，而生产链路永远生成同一 rid。测试与实现**不同构**，是假绿 |
| 3 | §三"冲突失效：同一 (head,rel) 下不同 tail → 旧值 is_active=0"（未提副作用） | 实测：`江妍 室友 陈雨` + `江妍 室友 赵小雨` → 只留赵小雨 active，陈雨被无条件关掉。多值属性被误杀，README 只在第 132 行小字标注，规则段仍写得很正面 |
| 4 | §三"50 轮以内永不压缩，一条不删"；配置表"50 轮以内不压缩" | `session.maybe_compress` 只有 `count_tokens > MAX_SAFE_TOKENS` 一个触发条件，**没有 50 轮触发**；反过来 20 轮长回答也能触发压缩。"超过 50 轮，或超 30000"里的"或前半句"在代码里不存在。§六 第 860 行自己也承认"实际长度受 token 预算约束"，与第 83 行绝对表述冲突 |
| 5 | §二 第 4 步"自我认知（即时层优先）" | 只有回答 Prompt（engine:229）用 live；自我溯源区块（engine:192）是 `snapshot.narrative or live_self_view`，narrative 通常非空 → live 永远用不上；判决器（engine:456）直接用 `snapshot.self_view`。**两条路径绕过即时层** |
| 6 | §二 第 7 步"四条硬性收束条件" | 代码有 5 条：多一条 `new_hits < RAG_LOOP_MIN_NEW_HITS`；默认值 1 时与"零命中"重复，README 也未收录该配置 |
| 7 | §三"同义词表 36 组" | 实际 `SYNONYM_GROUPS` 为 **31 组** |
| 8 | §一/§三/配置表"档案 7 组、40 余条" | 实际 7 组共 **39 条**（10/4/6/5/6/4/4） |
| 9 | §三"按每轮用户 100 字 + 回答 400 字，100 条消息约 21750 token" | 用项目自带 `tokenizer` 复算为 **19300 token**（`estimate_messages_tokens`，含每条 +4 结构开销）。阈值 30000 的余量是 ~10700，不是"约 8000 余量" |
| 10 | §六 收尾统计"经验片段 56" | 当前库 `jiangyan` 为 **63** 条（seed 情绪片段补写后未回写文档） |
| 11 | `PERSONA_PENDING_CAP=3.0` 说明"防止极端对话无限累加" | 该上限在当前参数下**永不生效**：日结位移 = pending×0.35 再夹 ±0.01，pending 只要 ≥0.0286 就顶格。cap=3.0 要生效需 pending×0.35>3，即 pending>8.57，先被 cap 截在 3.0 |

---

## 四、P2：无调用点 / 死代码 / 未接线

| # | 位置 | 事实 |
|---|---|---|
| 1 | `kernel.py:791-863` | `batch_apply_deltas` 被误缩进到模块级占位函数 `_unused(connection)` 内部，**永远不是 `PersonaKernel` 的方法**；它自称"添加批量操作方法"，但整段不可达。属粘贴缩进事故 |
| 2 | `database.py:211` | `close_all_connections()` 遍历 `self._connection_pool`，而该类只有 `self._local`（threading.local），**一旦调用必 AttributeError**；全项目无调用点 |
| 3 | `config.py:482` | `validate_config()` 定义了大量断言（参数区间、阈值顺序等），但**导入与启动都不调用**，grep 无任何调用点 → 配置校验形同虚设 |
| 4 | `config.py:365` + `kernel.settle_daily(multiplier)` | `PERSONA_EVENT_STEP_MULTIPLIER=2.0` 注释为"大事当天可放大"，但 `consolidator` 调 `settle_daily()` 从不传 multiplier → 该能力未接线，注释承诺落空 |
| 5 | `config.py:200` 注释 | `DAILY_CONSOLIDATE_HOUR=23` 仅文档用途（README 已注明），属可接受但易误解的"配置项不生效" |

---

## 五、可核验且已复核为正确的部分（避免误改）

- 配置表 50 项：与 `config.py` **逐一相符**（含 0.35 学习率、0.15 摆动带、0.01 日步、0.06 跃迁阈值、0.02 日志阈值、0.18/0.02 情绪衰减、0.65 判重比、30 版冷存）。
- 14 个参数 × 3 套倾向文本齐全；`describe()` 默认输出正好 **8 行**（封顶生效，情绪行优先预留预算，按 `|v-0.5|` 排序）。
- `settle_daily` 的"pending×0.35、夹 ±0.01、base 前移、current 复位、pending 清零"与 README §四 完全一致；`steps` 为空时仍清 pending 的细节也一致。
- 变迁日志 append-only、belief 维度两档门槛（0.90 / 涉己 0.60）、`【信念更替】`经验留痕：**均已实现**。
- `query_core_beliefs` 按 `confidence × importance` 排序取 12 条：**与文档一致**。
- `query_overturned` 会补"现为"，渲染成 `{head} {rel}：{旧值}（原值，{日期}被替换为「{新值}」）`：实测 3 条，无空值。
- 大事判定 `importance ≥ 0.9 且涉及本人，或命中长期指令词`：代码与 README §三之六一致。
- 计划任务 `CognitiveAgentDailyConsolidate` **确实存在**：`State=Ready`、`LastTaskResult=0`、`LastRunTime=2026-11-10 08:36`，README"已在本机注册"属实。
- `docs/` 交叉引用有效：`belief-system-spec.md` 确有 §8 与 §15；`persona-module-review.md` 确有 §3.1/§3.3/§3.4/§5.3。

---

## 六、修复顺序建议

1. **改 `_idf` 命名冲突**（1 行属性改名 + 1 行引用），并在 `engine.py:176` 加降级 try —— 这是当前唯一"阻断对话"的问题。
2. **召回过滤 `consolidated`**（router 读路径），否则修好 1 之后 P0-2 会立刻显形（重复命中）。
3. **`clear_live_self_view` 只在 substantive 分支执行**，未跃迁时保留 live。
4. **KG rid 去掉 tail**（改为 `_stable_id(space, head, rel)`），让加固/冲突分支真正可达；同时给"多值属性"建例外：单一值 rel 白名单之外不做失效。修完补一条"用 archiver 同款 rid 连写两次"的冒烟项，避免假绿。
5. 文档收口：删掉第 62 行"权重恢复"、第 831 行"会加固"（或改成"加固分支未接线"）；修正 36→31、40 余→39、21750→19300、56→63；"16 项全部通过"改为"14 项通过，2 项失败（向量检索待修）"；统一"50 轮"表述为"仅按 token 阈值触发"。
6. 清理 P2：把 `batch_apply_deltas` 移回类内或删除；`close_all_connections` 改用 `self._local`；`validate_config` 在 `engine` 构造时调用；删除或接线 `PERSONA_EVENT_STEP_MULTIPLIER`。

---

## 七、本次未覆盖

- 未用真实 DeepSeek API 复跑 6 轮（需密钥与额度），README §六 的逐轮数字（主题/关系/冲突数）只核对了落库结果与描述的一致性，未重放。
- `docs/belief-system-spec.md` 是**未落地**的设计规格（文件自述"未改动任何代码"），未按规格逐条判"已实现/未实现"，只核对了 README 对它的引用是否指向存在的章节。
- `verify_run.log` 只抽读，未逐行与当前库对账。
