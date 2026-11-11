"""
符号化人格内核（底层 + 中层 + 上层）。

职责边界（非常重要）：
- 本模块只处理"可计算的符号"，包括浮点参数、情景状态强度；
- 不生成任何叙事文本；
- 叙事文本只在发生实质性跃迁时，由顶层分工的 snapshot 模块生成。

三层含义：
- 底层：persona_kernel 表中的浮点参数（认知风格、价值取舍）
- 中层：动机目标权重（同样是 persona_kernel 中的 goal_* 参数）
- 上层：persona_mood 表中的情景状态（带衰减，反复出现才沉淀到底层）
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

import config
from cognitive.storage.database import Database


class PersonaKernel:
    """人格参数与情景状态的读写与演化。"""

    def __init__(self, db: Database, space_id: str) -> None:
        """
        :param db: 数据库访问对象
        :param space_id: 记忆空间（人格随空间隔离）
        """
        self.db = db
        self.space_id = space_id
        self.ensure_initialized()

    # ------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------
    def ensure_initialized(self) -> None:
        """
        首次使用时写入基线参数与元信息（幂等）。

        基线参数不是硬编码在 Prompt 里的文字，而是数据库中的数值，
        后续每一轮都可能被演化。
        """
        connection = self.db.conn()

        # 读取当前已有参数数量，判断是否已初始化
        existing = connection.execute(
            """
            select count(1) as cnt
            from persona_kernel as k
            where k.space_id = ?
            """,
            (self.space_id,),
        ).fetchone()

        if existing and int(existing["cnt"]) > 0:
            return

        now = int(time.time())
        for param, value in config.PERSONA_KERNEL_INIT.items():
            connection.execute(
                """
                insert or ignore into persona_kernel (space_id, param, value, base_value, pending_value, update_ts)
                values (?, ?, ?, ?, 0, ?)
                """,
                (self.space_id, param, float(value), float(value), now),
            )

        # 元信息初始化：快照版本从 0 开始
        connection.execute(
            """
            insert or ignore into persona_meta (space_id, current_version, persona_name)
            values (?, 0, ?)
            """,
            (self.space_id, config.PERSONA_NAME),
        )
        connection.commit()
        self._backfill_baseline()

    def _backfill_baseline(self) -> None:
        """老库升级：base_value 为空的参数，用当前 value 作为基调（幂等）。"""
        connection = self.db.conn()
        connection.execute(
            """
            update persona_kernel
            set base_value = value
            where space_id = ? and (base_value is null)
            """,
            (self.space_id,),
        )
        connection.commit()

    # ------------------------------------------------------------
    # 参数读取
    # ------------------------------------------------------------
    def get_params(self) -> Dict[str, float]:
        """
        读取当前全部参数。

        :return: 参数字典
        """
        connection = self.db.conn()
        rows = connection.execute(
            """
            select k.param as param, k.value as value
            from persona_kernel as k
            where k.space_id = ?
            """,
            (self.space_id,),
        ).fetchall()

        params = {row["param"]: float(row["value"]) for row in rows}

        # 补齐缺失参数，防止历史库字段不全导致 KeyError
        for param, default in config.PERSONA_KERNEL_INIT.items():
            params.setdefault(param, float(default))
        return params

    def get_param(self, name: str, default: float = 0.0) -> float:
        """读取单个参数。"""
        return self.get_params().get(name, default)

    def get_baseline(self) -> Dict[str, float]:
        """
        读取人格基调（长期锚点）。

        基调只在日结结算时缓慢移动，日内不随对话摆动；
        `current`（get_params）可以在基调 ± PERSONA_DRIFT_LIMIT 内摆动。

        :return: 基调参数字典
        """
        connection = self.db.conn()
        rows = connection.execute(
            """
            select k.param as param, k.base_value as base_value, k.value as value
            from persona_kernel as k
            where k.space_id = ?
            """,
            (self.space_id,),
        ).fetchall()

        baseline: Dict[str, float] = {}
        for row in rows:
            # 老库没有 base_value 时以当前值为基准
            raw = row["base_value"]
            baseline[row["param"]] = (
                float(raw) if raw is not None else float(row["value"])
            )

        for param, default in config.PERSONA_KERNEL_INIT.items():
            baseline.setdefault(param, float(default))
        return baseline

    def get_pending(self) -> Dict[str, float]:
        """
        读取当日累积偏移（尚未结算到基调的部分）。

        :return: 参数 -> 带符号累计偏移
        """
        connection = self.db.conn()
        rows = connection.execute(
            """
            select k.param as param, k.pending_value as pending_value
            from persona_kernel as k
            where k.space_id = ?
            """,
            (self.space_id,),
        ).fetchall()

        pending: Dict[str, float] = {}
        for row in rows:
            raw = row["pending_value"]
            if raw:
                pending[row["param"]] = float(raw)
        return pending

    # ------------------------------------------------------------
    # 参数演化
    # ------------------------------------------------------------
    def apply_deltas(self, deltas: Dict[str, float]) -> List[Tuple[str, float, float]]:
        """
        应用一组参数增量（代码层累加，不做文本重写）。

        这一步只移动"当前状态"，不移动"人格基调"：
        - 当前值被夹在 [基调 - 摆动带, 基调 + 摆动带] 内，所以一两轮对话无法让人格跑偏
        - 增量同时记入 pending（记的是**未夹制的原始力度**，顶到摆动带后仍继续累计），日结时再由 settle_daily 折算成基调位移

        :param deltas: 参数名 -> 增量（可为负）
        :return: [(参数名, 旧值, 新值)]，仅包含真正发生变化的项
        """
        if not deltas:
            return []

        connection = self.db.conn()
        now = int(time.time())
        current = self.get_params()
        baseline = self.get_baseline()
        pending = self.get_pending()
        changes: List[Tuple[str, float, float]] = []

        for param, delta in deltas.items():
            # 只接受白名单内参数，拒绝模型凭空新增维度
            if param not in config.PERSONA_KERNEL_INIT:
                continue

            try:
                delta_value = float(delta)
            except (TypeError, ValueError):
                continue

            old_value = current.get(param, float(config.PERSONA_KERNEL_INIT[param]))
            base_value = baseline.get(param, old_value)

            # 当天累积的是"原始力度"（未夹制的增量），不是夹制后的实际差值。
            # 为什么必须这样：当前值一旦顶到摆动带边界，同方向再施压，
            # 实际差值恒为 0，pending 就再也不增长了——"今天被反复顶住"
            # 和"今天只被顶了一次"在日结时结果完全一样，与"日积月累"的设计意图冲突。
            pending_delta = max(
                -config.PERSONA_PENDING_CAP,
                min(config.PERSONA_PENDING_CAP, pending.get(param, 0.0) + delta_value),
            )

            new_value = old_value + delta_value

            # 1) 夹在人格基调的摆动带内（这是"人格是基调、不是每轮重写"的物理约束）
            limit_low = max(
                config.PERSONA_PARAM_MIN,
                base_value - config.PERSONA_DRIFT_LIMIT,
            )
            limit_high = min(
                config.PERSONA_PARAM_MAX,
                base_value + config.PERSONA_DRIFT_LIMIT,
            )
            new_value = max(limit_low, min(limit_high, new_value))

            # 变化过小视为噪声：数值不写，但力度仍要留在 pending 里
            if abs(new_value - old_value) < 1e-6:
                if abs(pending_delta - pending.get(param, 0.0)) > 1e-9:
                    connection.execute(
                        """
                        update persona_kernel
                        set pending_value = ?, update_ts = ?
                        where space_id = ? and param = ?
                        """,
                        (pending_delta, now, self.space_id, param),
                    )
                continue

            connection.execute(
                """
                insert into persona_kernel (space_id, param, value, base_value, pending_value, update_ts)
                values (?, ?, ?, ?, ?, ?)
                on conflict (space_id, param) do update set
                    value = excluded.value,
                    pending_value = excluded.pending_value,
                    update_ts = excluded.update_ts
                """,
                (
                    self.space_id,
                    param,
                    new_value,
                    base_value,
                    pending_delta,
                    now,
                ),
            )
            changes.append((param, old_value, new_value))

        connection.commit()
        return changes

    def settle_daily(self, multiplier: float = 1.0) -> List[Dict[str, float]]:
        """
        日结结算：把当天累积偏移折算成人格基调的位移。

        规则（节奏控制的关键）：
        - 净偏移 = pending（当天所有**原始**增量之和，含被摆动带夹掉的那部分力度）
        - 基调位移 = 净偏移 × PERSONA_DAILY_LEARNING_RATE，再夹到 ±PERSONA_DAILY_MAX_STEP
        - 结算后 current 复位到新基调，pending 清零（日内摆动不跨天累积）

        因此：
        - 单日最多移动 0.01，凑够 PERSONA_SUBSTANTIVE_DELTA(0.06) 需要约 6 天同向积累
        - 日内无论聊得多激烈，都不会把人格拉离基调

        :param multiplier: 倍率（大事当天可放大，允许"大变化"快一点）
        :return: [{"param","base_from","base_to"}]，仅包含真正移动的维度
        """
        if not config.PERSONA_DAILY_SETTLE_ENABLED:
            return []

        connection = self.db.conn()
        now = int(time.time())
        baseline = self.get_baseline()
        pending = self.get_pending()
        max_step = config.PERSONA_DAILY_MAX_STEP * max(0.0, float(multiplier))
        steps: List[Dict[str, float]] = []

        for param, accumulated in pending.items():
            if param not in config.PERSONA_KERNEL_INIT:
                continue

            base_from = baseline.get(param, float(config.PERSONA_KERNEL_INIT[param]))
            raw_step = accumulated * config.PERSONA_DAILY_LEARNING_RATE
            step = max(-max_step, min(max_step, raw_step))
            base_to = max(
                config.PERSONA_PARAM_MIN,
                min(config.PERSONA_PARAM_MAX, base_from + step),
            )

            if abs(base_to - base_from) < 1e-6:
                # 没移动也要清零 pending，避免跨天累积
                connection.execute(
                    """
                    update persona_kernel
                    set pending_value = 0
                    where space_id = ? and param = ?
                    """,
                    (self.space_id, param),
                )
                continue

            # 基调移动 → 当前值复位到新基调（当天摆动结束）
            connection.execute(
                """
                update persona_kernel
                set base_value = ?, value = ?, pending_value = 0, update_ts = ?
                where space_id = ? and param = ?
                """,
                (base_to, base_to, now, self.space_id, param),
            )
            steps.append({"param": param, "base_from": base_from, "base_to": base_to})

        # 没有任何 pending 的参数也统一清零，保持表干净
        connection.execute(
            """
            update persona_kernel
            set pending_value = 0
            where space_id = ? and pending_value <> 0
            """,
            (self.space_id,),
        )
        connection.commit()
        return steps

    # ------------------------------------------------------------
    # 上层情景状态
    # ------------------------------------------------------------
    def _decay_to_now(
        self, intensity: float, last_decay_ts: Optional[int], now: int
    ) -> float:
        """
        按真实流逝时间把情景强度衰减到此刻（喂给 set_mood 与 decay_moods 共用）。

        为什么需要它：轮次衰减只在"有对话"时生效。隔三天不聊天，强度一点不掉，
        一开口还在用三天前那股情绪接话——不像人。这里按 wall clock 补一次指数衰减。

        :param intensity: 上次记录的强度
        :param last_decay_ts: 上次衰减时刻（None 视为刚刚）
        :param now: 当前时刻
        :return: 衰减后的强度
        """
        if not last_decay_ts:
            return float(intensity)

        elapsed_hours = max(0.0, (now - int(last_decay_ts)) / 3600.0)

        # 间隔太短不重复折算，避免每轮都按同一小段时间重复打折
        if elapsed_hours < config.MOOD_TIME_DECAY_MIN_HOURS:
            return float(intensity)

        return float(intensity) * math.exp(
            -config.MOOD_DECAY_LAMBDA_HOUR * elapsed_hours
        )

    def set_mood(
        self, mode: str, intensity: float, decay_rate: Optional[float] = None
    ) -> None:
        """
        激活或增强一种情景倾向。

        :param mode: 模式名，必须在白名单内
        :param intensity: 强度 0~1
        :param decay_rate: 衰减率，缺省使用全局配置
        """
        # 白名单校验：拒绝模型自创的情绪维度，防止状态空间失控
        if mode not in config.MOOD_MODE_WHITELIST:
            return

        connection = self.db.conn()
        now = int(time.time())
        rate = (
            float(decay_rate) if decay_rate is not None else config.MOOD_DECAY_PER_TURN
        )
        value = max(0.0, min(1.0, float(intensity)))

        # 先把旧强度按流逝时间衰减到此刻，再和本次强度取大。
        # 不这样做的话，昨夜的余温会被当成"此刻的状态"直接参与比较。
        row = connection.execute(
            """
            select m.intensity as intensity, m.last_decay_ts as last_decay_ts
            from persona_mood as m
            where m.space_id = ? and m.mode = ?
            """,
            (self.space_id, mode),
        ).fetchone()

        if row:
            value = max(
                value,
                self._decay_to_now(float(row["intensity"]), row["last_decay_ts"], now),
            )

        connection.execute(
            """
            insert into persona_mood
                (space_id, mode, intensity, base_intensity, decay_rate, create_ts, last_decay_ts)
            values (?, ?, ?, ?, ?, ?, ?)
            on conflict (space_id, mode) do update set
                intensity = excluded.intensity,
                base_intensity = excluded.base_intensity,
                decay_rate = excluded.decay_rate,
                last_decay_ts = excluded.last_decay_ts
            """,
            (self.space_id, mode, value, value, rate, now, now),
        )
        connection.commit()

    def decay_moods(self) -> Dict[str, float]:
        """
        衰减情景强度：先按真实流逝时间，再按"这一轮"。

        为什么两段都要：
        - 时间那段处理"隔了几天没说话"（真人的情绪不会原地待机）；
        - 轮次那段处理"对话里的情绪回落"（说话当下情绪退得快）。

        :return: 衰减后的情景状态 {模式: 强度}
        """
        connection = self.db.conn()
        now = int(time.time())
        rows = connection.execute(
            """
            select m.mode as mode, m.intensity as intensity,
                   m.decay_rate as decay_rate, m.last_decay_ts as last_decay_ts
            from persona_mood as m
            where m.space_id = ?
            """,
            (self.space_id,),
        ).fetchall()

        result: Dict[str, float] = {}
        for row in rows:
            intensity = self._decay_to_now(
                float(row["intensity"]), row["last_decay_ts"], now
            )
            new_intensity = intensity * (1.0 - float(row["decay_rate"]))

            if new_intensity < config.MOOD_MIN_INTENSITY:
                # 强度过低视为消退，直接移除
                connection.execute(
                    """
                    delete from persona_mood
                    where space_id = ? and mode = ?
                    """,
                    (self.space_id, row["mode"]),
                )
            else:
                connection.execute(
                    """
                    update persona_mood
                    set intensity = ?, last_decay_ts = ?
                    where space_id = ? and mode = ?
                    """,
                    (new_intensity, now, self.space_id, row["mode"]),
                )
                result[row["mode"]] = new_intensity

        connection.commit()
        return result

    def get_moods(self) -> Dict[str, float]:
        """
        读取当前活跃的情景状态。

        注意：这里必须按流逝时间折算一次，而不是原样返回库里的数值。
        为什么：decay_moods 只在每轮归档末尾调用。用户隔三天回来时，
        本轮读 Prompt 发生在任何 decay 之前——不在这里折算，
        "隔三天还用三天前那股情绪接第一句话"的毛病就依然存在。
        这里只读不写（不落库），持久化仍由 decay_moods 负责。
        """
        connection = self.db.conn()
        now = int(time.time())
        rows = connection.execute(
            """
            select m.mode as mode, m.intensity as intensity, m.last_decay_ts as last_decay_ts
            from persona_mood as m
            where m.space_id = ?
            """,
            (self.space_id,),
        ).fetchall()

        moods: Dict[str, float] = {}
        for row in rows:
            value = self._decay_to_now(
                float(row["intensity"]), row["last_decay_ts"], now
            )
            # 已低于消退阈值的，读的时候就不该再当成"此刻的状态"
            if value >= config.MOOD_MIN_INTENSITY:
                moods[row["mode"]] = value
        return moods

    # ------------------------------------------------------------
    # 实质性跃迁判定
    # ------------------------------------------------------------
    def is_substantive_change(
        self,
        param_changes: List[Tuple[str, float, float]],
        self_view_changed: bool = False,
    ) -> Tuple[bool, List[str]]:
        """
        判断一组变化是否构成"实质性人格跃迁"。

        语义已调整：这里的 param_changes 是「上一版快照的参数 → 结算后的基调」，
        即**多天累计**的对比，而不是单轮增量。单轮增量受摆动带限制，不可能触发。

        规则：
        1. 任一参数累计变化 >= PERSONA_SUBSTANTIVE_DELTA → 算
        2. 只有累计变化达标才跃迁，**不看单轮增量**，也不给核心参数打折
           （单轮时代给核心参数半阈值的优惠，会让 3 天就产生一版，与"不要变化太快"冲突）
        3. 自我认知文本变化由调用方决定是否计入（日结结算时不计入，
           否则每天微调一句话都会产生新版本）

        :param param_changes: [(参数名, 旧值, 新值)]
        :param self_view_changed: 自我认知是否变化
        :return: (是否实质跃迁, 触发原因列表)
        """
        reasons: List[str] = []

        if self_view_changed:
            reasons.append("自我认知文本发生变化")

        for param, old_value, new_value in param_changes:
            delta = abs(new_value - old_value)
            if delta >= config.PERSONA_SUBSTANTIVE_DELTA:
                reasons.append(
                    f"参数 {param} 累计变化 {old_value:.2f} -> {new_value:.2f}"
                )

        return (len(reasons) > 0, reasons)

    # ------------------------------------------------------------
    # 快照版本号
    # ------------------------------------------------------------
    def get_current_version(self) -> int:
        """读取当前快照版本号。"""
        connection = self.db.conn()
        row = connection.execute(
            """
            select p.current_version as current_version
            from persona_meta as p
            where p.space_id = ?
            """,
            (self.space_id,),
        ).fetchone()
        return int(row["current_version"]) if row else 0

    def set_current_version(self, version: int) -> None:
        """写入当前快照版本号。"""
        connection = self.db.conn()
        connection.execute(
            """
            insert into persona_meta (space_id, current_version, persona_name)
            values (?, ?, ?)
            on conflict (space_id) do update set
                current_version = excluded.current_version
            """,
            (self.space_id, int(version), config.PERSONA_NAME),
        )
        connection.commit()

    # ------------------------------------------------------------
    # 即时自我认知（不版本化，日结时才固化进快照）
    # ------------------------------------------------------------
    def set_live_self_view(self, text: str) -> None:
        """
        写入"即时自我认知"。

        为什么单独存一层：自我认知是每轮都可能微调的一句话，
        但它不该每轮都生成一个快照版本。这里存即时值，日结时再固化。

        :param text: 自我认知一句话
        """
        value = (text or "").strip()
        if not value:
            return

        connection = self.db.conn()
        connection.execute(
            """
            insert into persona_meta (space_id, current_version, persona_name, live_self_view)
            values (?, 0, ?, ?)
            on conflict (space_id) do update set
                live_self_view = excluded.live_self_view
            """,
            (self.space_id, config.PERSONA_NAME, value),
        )
        connection.commit()

    def get_live_self_view(self) -> str:
        """
        读取即时自我认知。

        :return: 自我认知文本；从未写入过返回空字符串
        """
        connection = self.db.conn()
        row = connection.execute(
            """
            select p.live_self_view as live_self_view
            from persona_meta as p
            where p.space_id = ?
            """,
            (self.space_id,),
        ).fetchone()

        if not row or row["live_self_view"] is None:
            return ""
        return str(row["live_self_view"])

    def clear_live_self_view(self) -> None:
        """清除即时自我认知（日结固化后调用，避免重复采纳）。"""
        connection = self.db.conn()
        connection.execute(
            """
            update persona_meta
            set live_self_view = null
            where space_id = ?
            """,
            (self.space_id,),
        )
        connection.commit()

    # ------------------------------------------------------------
    # 供 Prompt 使用的紧凑描述
    # ------------------------------------------------------------
    # 参数名 -> (高值描述, 中值描述, 低值描述)
    # 低值描述不是"没有倾向"，而是"反向倾向"：prefer_decompose 低的人
    # 不等于没有风格，他的风格是"先说结论再补细节"。
    # 中值也要有话说：大部分参数常年落在 0.45~0.72，如果不描述，
    # 这个区块就只剩三四行，人格倾向等于没注入。
    _TENDENCY_TEXT: Dict[str, Tuple[str, str, str]] = {
        "prefer_decompose": (
            "遇到复杂问题，你习惯先拆开分析再回答。",
            "面对复杂问题你会先理一理再答，但不写成长篇大纲。",
            "你不爱先搭框架，更愿意先把结论说出来，再回头补细节。",
        ),
        "prefer_intuition": (
            "你多半先凭直觉给判断，再回头找理由。",
            "直觉和依据你各用一半：先说感觉，想清楚了再补。",
            "你不太信第一感觉，习惯先把依据摆出来再说。",
        ),
        "prefer_skepticism": (
            "你对自己旧有的判断保持审慎，会主动检查前后是否自相矛盾。",
            "你会回头检查自己的旧判断，但不至于为此纠缠太久。",
            "你很少怀疑自己已经认定的事，别人的反对要先过一遍才听得进。",
        ),
        "memory_sensitivity": (
            "你倾向于先回忆相关往事再作答。",
            "聊到相关话题你会想起一些旧事，但不至于句句翻旧账。",
            "你不常主动翻旧事，注意力更多放在眼前这句话上。",
        ),
        # 注意：这个参数的语义是"触发自我反思的阈值"，越低越容易反思，
        # 所以高值对应"不容易反思"的描述，方向与其它参数相反，别跟着一起读反了。
        "self_reflect_threshold": (
            "你不轻易自我怀疑，只有被说到点上才会回头想。",
            "你会自省，但需要一点由头，不会动不动就怀疑自己。",
            "你容易往自己身上想，常回头琢磨自己是不是哪里做得不对。",
        ),
        "truth_priority": (
            "事实准确性优先于表达上的圆融。",
            "你在意说得对，也照顾对方感受，两头各让一点。",
            "你更在意把话说得不伤人，准确性可以让一步。",
        ),
        "harmony_priority": (
            "你很在意对话的气氛和对方的感受，会为这个调整说法。",
            "你在意气氛，但不至于为了和气把话说假。",
            "你不怎么迁就气氛，觉得该说的直说更省事。",
        ),
        "curiosity_priority": (
            "你对未知的部分有较强好奇心，会主动追问关键信息。",
            "碰到有意思的地方你会问两句，问完就收。",
            "你不太主动追问，别人问什么就答什么。",
        ),
        "consistency_priority": (
            "你重视自身说法前后一致；发现不一致，会明确指出来。",
            "你在意前后一致，只是不强行圆场。",
            "你不太计较前后是否一致，改主意就改主意。",
        ),
        "topic_inertia": (
            "话题聊开了你会接着往下讲，不急着收尾。",
            "话题顺的时候你会往下讲，不顺就换个方向。",
            "你说话偏利落，讲完就停，不硬续话题。",
        ),
        "novelty_bias": (
            "你对新观点接受得快，愿意试着改自己。",
            "新说法你听得进，但要先看它站不站得住。",
            "你偏保守，新的说法要先掂量很久才肯认。",
        ),
        "goal_self_understanding": (
            "你常想着弄明白自己到底是个什么样的人。",
            "你偶尔会想自己是什么样的人，不当成日常课题。",
            "你不太琢磨自己，注意力多半在眼前的事上。",
        ),
        "goal_task_accuracy": (
            "做事时你很在意结果对不对，不含糊。",
            "做事你要求大致对，细节错了会补，但不苛求。",
            "你更看重把事推进下去，细节对错可以后面再补。",
        ),
        "goal_coherence": (
            "你希望自己讲得连贯、说得清。",
            "你希望讲得清楚，偶尔跳一下也无妨。",
            "你说话可以跳，想到哪说到哪。",
        ),
    }

    def describe(self) -> str:
        """
        把符号内核翻译成一段简短的行为约束描述（不是叙事，不替代快照）。

        作用：让模型知道"我现在倾向怎么思考"，而不是只知道自己叫什么。

        规则：
        - 三级判定（强 / 中 / 反向），阈值在 config 里，不再写死 0.6 / 0.8；
        - 只输出偏离中位最明显的若干条，封顶 PERSONA_DESC_MAX_LINES 行，
          避免把 Prompt 挤满；
        - 情景状态附在后面，强度够高才提。

        :return: 描述文本
        """
        params = self.get_params()
        moods = self.get_moods()

        # 情景状态先行，用来给它预留行数——否则参数攒满 8 行，最"当下"的情绪反而被挤掉
        mood_text = {
            "self_reflect": "此刻你处于较强的自省状态",
            "skeptical": "此刻你对新信息保持审慎",
            "exploratory": "此刻你更愿意发散与追问",
            "analytic": "此刻你更倾向分步分析",
            "empathetic": "此刻你更关注对方的感受",
        }
        mood_lines = [
            f"{mood_text[mode]}（强度 {intensity:.2f}）。"
            for mode, intensity in moods.items()
            if intensity >= 0.3 and mode in mood_text
        ]

        # 按"离中位多远"排序：先讲她此刻最鲜明的特征，而不是按参数字母序；
        # 同幅度时按参数名兜底，保证同样的库两次调用输出顺序一致（便于对比调试）
        ordered = sorted(
            params.items(),
            key=lambda item: (-abs(float(item[1]) - 0.5), str(item[0])),
        )

        param_budget = max(1, config.PERSONA_DESC_MAX_LINES - len(mood_lines))
        lines: List[str] = []
        for param, value in ordered:
            if len(lines) >= param_budget:
                break

            texts = self._TENDENCY_TEXT.get(param)
            if not texts:
                continue

            high_text, mid_text, low_text = texts
            value_now = float(value)
            if value_now >= config.PERSONA_DESC_STRONG:
                lines.append(high_text)
            elif value_now >= config.PERSONA_DESC_MID:
                lines.append(mid_text)
            elif value_now <= config.PERSONA_DESC_WEAK:
                lines.append(low_text)
            # (WEAK, MID) 之间视为接近中性，不描述，避免堆废话

        lines.extend(mood_lines)

        return "\n".join(lines) if lines else "当前没有特别突出的行为倾向。"
