"""
长期运行的巩固器：把"一件件往事"压缩成"一段时间的主线"，并把大事单独沉淀。

三种更新节奏（对应 config 里的三组参数）：
1. 每轮：归档（archiver 负责，与 token 无关）
2. 每天一次：日结巩固（本模块 consolidate_daily，由 maintenance.py --daily 或计划任务驱动）
   并在结尾结算"人格基调"——这才是一天一变的那一步
3. 事件一次：
   - 大事：importance 超阈值或用户明确表态 → consolidate_event（不等日结）
   - 人格跃迁：日结结算时累计差跨过阈值 → 新版本快照 + event 记忆（本模块 _settle_persona）

注意节奏的分层：
- 每轮只改"当日状态"（受基调 ± DRIFT_LIMIT 约束），不会生成人格版本
- 基调只在日结移动，单参数单日最多 PERSONA_DAILY_MAX_STEP
- 所以"人格跃迁"从"可能每轮发生"变成"多天同向积累才发生"

为什么必须单独有这一层：
主题（topic）是事件级记忆，一年下来会有上千条。若不归并，
检索时永远只能召回"某一天说过的某句话"，而无法回答"这半年我在这件事上走到了哪"。
日结把若干 topic 归并成一条"主线"，原 topic 标记 consolidated 后不再参与召回，
既压缩了检索面，又保住了细节（原 topic 仍在库里，可被溯源查询）。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import config
from cognitive.cognition import prompts
from cognitive.llm import DeepSeekClient, LLMError, get_client
from cognitive.models import Experience, Topic
from cognitive.storage.database import Database
from cognitive.storage.topic_store import TopicStore
from cognitive.storage.vector_store import VectorStore


# 日结与大事沉淀的类型标识
KIND_DAILY = "daily"
KIND_EVENT = "event"


def _new_id(prefix: str) -> str:
    """生成短 id（时间戳 + 微秒，足够避免同进程冲突）。"""
    return f"{prefix}_{int(time.time() * 1000)}_{int(time.time_ns() % 1000000):06d}"


def _fmt_ts(ts: int) -> str:
    """把时间戳格式化为人类可读日期。"""
    if not ts:
        return "（起始未知）"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


class Consolidator:
    """日结巩固与大事沉淀。"""

    def __init__(self, db: Database, client: Optional[DeepSeekClient] = None) -> None:
        """
        :param db: 数据库访问对象
        :param client: 模型客户端，缺省走全局单例；显式传 None 表示"允许降级为机械摘要"
        """
        self.db = db
        self._client = client
        self._client_resolved = client is not None

    def _get_client(self) -> DeepSeekClient:
        """惰性获取模型客户端。"""
        if self._client is None:
            self._client = get_client()
        return self._client

    # ------------------------------------------------------------
    # 巩固日志
    # ------------------------------------------------------------
    def last_consolidated_ts(self, space_id: str, kind: str = KIND_DAILY) -> int:
        """
        取上次巩固覆盖到的结束时间，作为本次的起点。

        :param space_id: 记忆空间
        :param kind: 巩固类型（daily / event）
        :return: 时间戳；从未巩固过返回 0
        """
        connection = self.db.conn()
        row = connection.execute(
            """
            select c.range_to as range_to
            from consolidation_log as c
            where c.space_id = ? and c.kind = ?
            order by c.range_to desc
            limit 1
            """,
            (space_id, kind),
        ).fetchone()

        return int(row["range_to"] or 0) if row else 0

    def _append_log(
        self,
        space_id: str,
        kind: str,
        range_from: int,
        range_to: int,
        topic_ids: List[str],
        summary: str,
    ) -> str:
        """
        写一条巩固日志（append-only，用于"多久没巩固"的审计与防重复）。

        :param space_id: 记忆空间
        :param kind: 巩固类型
        :param range_from: 覆盖起始
        :param range_to: 覆盖结束
        :param topic_ids: 被归并的主题 id
        :param summary: 归纳正文
        :return: 日志 id
        """
        import json

        log_id = _new_id("clog")
        connection = self.db.conn()
        connection.execute(
            """
            insert into consolidation_log
                (space_id, log_id, kind, range_from, range_to, topic_ids, summary, create_ts)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                space_id,
                log_id,
                kind,
                int(range_from),
                int(range_to),
                json.dumps(topic_ids, ensure_ascii=False),
                summary,
                int(time.time()),
            ),
        )
        connection.commit()
        return log_id

    def list_logs(self, space_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        列出最近的巩固记录。

        :param space_id: 记忆空间
        :param limit: 返回上限
        :return: 记录列表
        """
        connection = self.db.conn()
        rows = connection.execute(
            """
            select c.log_id as log_id, c.kind as kind, c.range_from as range_from,
                   c.range_to as range_to, c.topic_ids as topic_ids,
                   c.summary as summary, c.create_ts as create_ts
            from consolidation_log as c
            where c.space_id = ?
            order by c.create_ts desc
            limit ?
            """,
            (space_id, limit),
        ).fetchall()

        return [
            {
                "log_id": row["log_id"],
                "kind": row["kind"],
                "range_from": int(row["range_from"] or 0),
                "range_to": int(row["range_to"] or 0),
                "summary": row["summary"] or "",
                "create_ts": int(row["create_ts"] or 0),
            }
            for row in rows
        ]

    # ------------------------------------------------------------
    # 日结巩固（每天一次）
    # ------------------------------------------------------------
    def consolidate_daily(
        self,
        space_id: str,
        force: bool = False,
        now_ts: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        执行一次日结巩固：把上次日结以来的普通主题归并成一条"日结"。

        :param space_id: 记忆空间
        :param force: 少于 CONSOLIDATE_MIN_TOPICS 时是否强制执行
        :param now_ts: 当前时间（测试可注入）
        :return: 执行报告
        """
        now = int(now_ts or time.time())
        report: Dict[str, Any] = {
            "ok": False,
            "kind": KIND_DAILY,
            "topic_count": 0,
            "summary_chars": 0,
            "lessons": 0,
            "degraded": False,
            "reason": "",
        }

        if not config.DAILY_CONSOLIDATE_ENABLED and not force:
            report["reason"] = "日结开关已关闭（DAILY_CONSOLIDATE_ENABLED=false）"
            return report

        since = self.last_consolidated_ts(space_id, KIND_DAILY)
        topics_store = TopicStore(self.db)
        pending = topics_store.list_pending_consolidation(
            space_id=space_id,
            since_ts=since,
            limit=config.CONSOLIDATE_MAX_TOPICS,
        )

        report["topic_count"] = len(pending)

        # 没有新内容：不产生日志，避免日志表被空记录塞满
        if not pending:
            report["reason"] = "上次日结之后没有新的往事，无需巩固"
            return report

        # 内容太少：默认攒到下次（force 可绕过）
        if len(pending) < config.CONSOLIDATE_MIN_TOPICS and not force:
            report["reason"] = (
                f"待归并往事只有 {len(pending)} 条，低于阈值 "
                f"{config.CONSOLIDATE_MIN_TOPICS}，留到下次"
            )
            return report

        range_from = min(topic.create_ts for topic in pending) or since
        range_to = max(topic.create_ts for topic in pending)

        # 当日窗口内发生过大事沉淀（event 巩固日志与本窗口有重叠）时，
        # 人格结算步长按 PERSONA_EVENT_STEP_MULTIPLIER 放大：
        # 大事当天因果明确，允许基调走得比平常快一点
        step_multiplier = 1.0
        event_hit = self.db.conn().execute(
            """
            select 1 from consolidation_log as c
            where c.space_id = ? and c.kind = ?
              and c.range_from <= ? and c.range_to >= ?
            limit 1
            """,
            (space_id, KIND_EVENT, range_to, range_from),
        ).fetchone()
        if event_hit:
            step_multiplier = config.PERSONA_EVENT_STEP_MULTIPLIER

        # 渲染待归并清单
        topics_text = "\n".join(
            f"- [{item.title}] {item.abstract}（关键词：{'、'.join(item.keywords)}）"
            for item in pending
        )

        self_view = self._current_self_view(space_id)
        messages = prompts.build_consolidate_daily_messages(
            topics_text=topics_text,
            count=len(pending),
            range_text=f"{_fmt_ts(range_from)} ~ {_fmt_ts(range_to)}",
            self_view=self_view,
        )

        payload = self._call_json(messages, report)

        title = str(payload.get("title") or "").strip() or "日结"
        summary = str(payload.get("summary") or "").strip()
        lessons = self._normalize_lessons(payload.get("lessons"))
        drift = str(payload.get("drift") or "").strip()

        # 降级：模型不可用时，用清单本身充当摘要，保证记忆链不断
        if not summary:
            summary = "【机械摘要】\n" + "\n".join(
                f"- {item.title}：{item.abstract[:80]}" for item in pending
            )
            report["degraded"] = True

        summary = summary[: config.CONSOLIDATE_SUMMARY_MAX_CHARS]

        # 日结本身写成一条 kind='daily' 的主题，可被后续检索召回
        daily_topic = Topic(
            topic_id=_new_id("daily"),
            space_id=space_id,
            title=title[:20],
            abstract=summary,
            keywords=self._collect_keywords(pending),
            session_id="",
            snapshot_version=self._current_version(space_id),
            kind=KIND_DAILY,
            create_ts=now,
        )
        topics_store.save_topic(daily_topic)

        # 日结也进向量索引，否则"最近这段时间在做什么"召回不到
        VectorStore(space_id).add(
            doc_id=daily_topic.topic_id,
            text=f"{daily_topic.title}。{summary}",
            meta={
                "title": daily_topic.title,
                "kind": KIND_DAILY,
                "range_from": range_from,
                "range_to": range_to,
            },
        )

        # 日结里提炼的结论挂成经验，挂在日结主题下
        for index, lesson in enumerate(lessons):
            topics_store.add_experience(
                Experience(
                    exp_id=_new_id(f"dexp{index + 1}"),
                    space_id=space_id,
                    topic_id=daily_topic.topic_id,
                    content=lesson,
                )
            )

        report["lessons"] = len(lessons)
        report["summary_chars"] = len(summary)

        # 「这一天我发生了什么变化」单独留痕，便于日后回答"你那阵子在变吗"
        if drift:
            topics_store.add_experience(
                Experience(
                    exp_id=_new_id("ddrift"),
                    space_id=space_id,
                    topic_id=daily_topic.topic_id,
                    content=f"【当日自我变化】{drift}"[: config.EXPERIENCE_MAX_CHARS],
                )
            )

        # 写巩固日志：range_from/range_to 是下一次的起点
        self._append_log(
            space_id=space_id,
            kind=KIND_DAILY,
            range_from=range_from,
            range_to=range_to,
            topic_ids=[item.topic_id for item in pending],
            summary=f"{title}：{summary}",
        )

        # 原主题标记已归并：不再参与主题召回，但仍在库里可溯源
        if config.CONSOLIDATE_MARK_CONSUMED:
            topics_store.mark_consolidated(space_id, [item.topic_id for item in pending])

        # ---------- 日结的最后一步：结算人格基调 ----------
        # 这才是真正的"一天一变"：日内只在基调附近摆动，基调在日结时移动。
        # 每天最多移动 PERSONA_DAILY_MAX_STEP（大事当天 × PERSONA_EVENT_STEP_MULTIPLIER），
        # 所以人格不会一两天就变个人。
        report["persona"] = self._settle_persona(
            space_id=space_id,
            drift_text=drift,
            daily_title=title,
            anchor_topic_id=pending[-1].topic_id if pending else "",
            step_multiplier=step_multiplier,
        )

        report["ok"] = True
        report["title"] = title
        report["topic_id"] = daily_topic.topic_id
        report["reason"] = "日结完成"
        return report

    # ------------------------------------------------------------
    # 人格基调结算（日结的最后一步）
    # ------------------------------------------------------------
    def _settle_persona(
        self,
        space_id: str,
        drift_text: str,
        daily_title: str,
        anchor_topic_id: str,
        step_multiplier: float = 1.0,
    ) -> Dict[str, Any]:
        """
        把当天累积的人格偏移结算进"人格基调"，必要时生成新的人格版本。

        设计要点：
        - 基调位移 = 当天净偏移 × 学习率，再夹到单日上限（默认 0.01，
          大事当天 × step_multiplier）
        - 是否构成"实质跃迁"看的是**相对上一版快照的累计差**，不是当日步长
          → 同一方向连续积累约 6 天才够一次版本，节奏天然与日结同步
        - 跃迁时：写变迁日志 + 生成叙事 + 落一个独立快照文件 + 落一条 event 记忆
        - 没跃迁时：仍然写变迁日志（记录基调正在缓慢移动），但不新增版本

        :param space_id: 记忆空间
        :param drift_text: 日结模型给出的"这一天我怎么变了"
        :param daily_title: 日结主题名（作为叙事上下文）
        :param anchor_topic_id: 归因用的原始主题 id（当天最后一件往事）
        :param step_multiplier: 单日步长上限的放大倍率（大事当天 > 1）
        :return: 结算报告
        """
        from cognitive.cognition.extractors import NarrativeGenerator
        from cognitive.models import PersonaChange, Topic
        from cognitive.persona.changelog import PersonaChangelogStore
        from cognitive.persona.kernel import PersonaKernel
        from cognitive.persona.snapshot import PersonaSnapshotStore
        from cognitive.storage.kg_store import KGStore

        report: Dict[str, Any] = {
            "stepped": 0,
            "steps": [],
            "substantive": False,
            "version": 0,
            "changelog_entries": 0,
            "step_multiplier": step_multiplier,
            "message": "",
        }

        kernel = PersonaKernel(self.db, space_id)
        kg = KGStore(self.db)
        snapshots = PersonaSnapshotStore(self.db, space_id, kernel, kg)
        changelog = PersonaChangelogStore(self.db, space_id)

        previous = snapshots.load_current()
        params_before = dict(previous.kernel)
        old_self_view = previous.self_view
        version_before = kernel.get_current_version()

        steps = kernel.settle_daily(multiplier=step_multiplier)
        report["steps"] = [
            {"param": item["param"], "base_from": round(item["base_from"], 3), "base_to": round(item["base_to"], 3)}
            for item in steps
        ]
        report["stepped"] = len(steps)

        if not steps:
            report["message"] = "当天没有值得推进基调的偏移"
            return report

        # 累计差 = 上一版快照的参数 vs 结算后的基调
        settled = kernel.get_params()
        cumulative_changes = [
            (item["param"], float(params_before.get(item["param"], item["base_from"])), float(settled[item["param"]]))
            for item in steps
        ]

        # 自我认知：即时层优先，日结时固化
        live_self_view = kernel.get_live_self_view()
        new_self_view = live_self_view or old_self_view
        self_view_changed = bool(new_self_view.strip()) and new_self_view.strip() != old_self_view.strip()

        # 跃迁判定只看参数累计差；自我认知的措辞微调不单独触发新版本，
        # 否则"每天微调一句自我描述"就等于每天一版人格，与"不要变化太快"冲突
        substantive, reasons = kernel.is_substantive_change(cumulative_changes, False)
        report["substantive"] = substantive
        version_after = version_before + 1 if substantive else version_before

        # 归因文本：优先用日结模型给出的 drift，其次用变化差值描述
        reason_text = drift_text.strip()
        if not reason_text:
            reason_text = "；".join(
                f"{param} {old_value:.3f} -> {new_value:.3f}"
                for param, old_value, new_value in cumulative_changes
            )
        if substantive and reasons:
            reason_text = f"{reason_text}（跃迁判定：{'；'.join(reasons)}）"

        # 写变迁日志：记录的是"累计变化"，所以跨过 CHANGELOG_MIN_DELTA 才记
        log_count = 0
        for param, old_value, new_value in cumulative_changes:
            if abs(new_value - old_value) < config.PERSONA_CHANGELOG_MIN_DELTA:
                continue

            changelog.append(
                PersonaChange(
                    log_id=_new_id("plog"),
                    space_id=space_id,
                    topic_id=anchor_topic_id,
                    persona_field=f"kernel:{param}",
                    old_value=f"{old_value:.3f}",
                    new_value=f"{new_value:.3f}",
                    reason=reason_text[:400],
                    source_type="daily_settle",
                    snapshot_from=version_before,
                    snapshot_to=version_after,
                )
            )
            log_count += 1

        if self_view_changed:
            changelog.append(
                PersonaChange(
                    log_id=_new_id("plog"),
                    space_id=space_id,
                    topic_id=anchor_topic_id,
                    persona_field="self_view",
                    old_value=old_self_view,
                    new_value=new_self_view,
                    reason=reason_text[:400],
                    source_type="daily_settle",
                    snapshot_from=version_before,
                    snapshot_to=version_after,
                )
            )
            log_count += 1

        report["changelog_entries"] = log_count

        # 只有累计跨过阈值，才生成新的人格版本（独立文件，只加载最新一份）
        if substantive:
            change_lines = [
                f"{param}：{old_value:.3f} -> {new_value:.3f}"
                for param, old_value, new_value in cumulative_changes
            ]
            narrative = ""
            try:
                narrative = NarrativeGenerator(self._get_client()).generate(
                    topic_title=daily_title,
                    topic_abstract=drift_text or "一天结束，我回顾了这段时间的经历。",
                    old_self_view=old_self_view,
                    old_params=params_before,
                    new_self_view=new_self_view,
                    new_params=settled,
                    change_lines=change_lines,
                    reason=reason_text,
                )
            except Exception:
                narrative = f"这一天里，因为「{daily_title}」，我的基调整体移动了：{'；'.join(change_lines)}"

            snapshot = snapshots.create(
                self_view=new_self_view,
                narrative=narrative,
                topic_id=anchor_topic_id or "daily",
            )
            report["version"] = snapshot.version

            # 「大的变化也单独留一条记忆」：落一条 kind='event' 的主题，不等下一轮对话
            self._save_promotion_topic(
                space_id=space_id,
                version=snapshot.version,
                daily_title=daily_title,
                drift_text=drift_text,
                change_lines=change_lines,
            )

            # 即时自我认知已固化进快照，清空避免重复采纳。
            # 注意只在跃迁（快照真的落盘）时清：未跃迁时快照没动，
            # 若也清空，等于把当天累积的自我认知微调直接丢掉
            if self_view_changed:
                kernel.clear_live_self_view()
        else:
            report["version"] = version_before

        report["message"] = "基调已推进" + ("（构成实质跃迁）" if substantive else "")
        return report

    def _save_promotion_topic(
        self,
        space_id: str,
        version: int,
        daily_title: str,
        drift_text: str,
        change_lines: List[str],
    ) -> Optional[str]:
        """
        把一次人格跃迁单独沉淀成 kind='event' 的长期记忆。

        为什么需要：用户要求"每经历一次大的变化更新一次"，
        跃迁本身也要能被日后检索到，而不是只留在快照文件里。

        :param space_id: 记忆空间
        :param version: 新的人格版本号
        :param daily_title: 触发这次跃迁的日结主题
        :param drift_text: 当天的自我变化描述
        :param change_lines: 参数累计变化行
        :return: 主题 id；失败返回 None
        """
        try:
            topics_store = TopicStore(self.db)
            abstract_parts = [f"人格基调推进到第 {version} 版。"]
            if drift_text:
                abstract_parts.append(f"原因：{drift_text}")
            if change_lines:
                abstract_parts.append("累计变化：" + "；".join(change_lines[:6]))

            promotion = Topic(
                topic_id=_new_id("promotion"),
                space_id=space_id,
                title=f"【人格】基调 v{version}"[:20],
                abstract="".join(abstract_parts)[: config.CONSOLIDATE_SUMMARY_MAX_CHARS],
                keywords=["人格", "跃迁", f"v{version}", daily_title[:8]],
                session_id="",
                snapshot_version=version,
                kind=KIND_EVENT,
                is_big_event=True,
                create_ts=int(time.time()),
            )
            topics_store.save_topic(promotion)

            VectorStore(space_id).add(
                doc_id=promotion.topic_id,
                text=f"{promotion.title}。{promotion.abstract}",
                meta={"title": promotion.title, "kind": KIND_EVENT, "persona_version": version},
            )
            topics_store.add_experience(
                Experience(
                    exp_id=_new_id("pexp"),
                    space_id=space_id,
                    topic_id=promotion.topic_id,
                    content=f"【人格跃迁·v{version}】{drift_text or '基调移动'}"[: config.EXPERIENCE_MAX_CHARS],
                )
            )
            return promotion.topic_id
        except Exception:
            # 沉淀失败不影响快照与日志已经落盘
            return None

    # ------------------------------------------------------------
    # 大事沉淀（事件一次）
    # ------------------------------------------------------------
    def consolidate_event(self, space_id: str, topic: Topic) -> Dict[str, Any]:
        """
        把一件"大事"单独沉淀成 kind='event' 的长期记忆。

        :param space_id: 记忆空间
        :param topic: 被判定为大事的原始主题
        :return: 执行报告
        """
        report: Dict[str, Any] = {
            "ok": False,
            "kind": KIND_EVENT,
            "source_topic_id": topic.topic_id,
            "degraded": False,
            "reason": "",
        }

        # 幂等：同一个主题只沉淀一次（既是大事又触发人格跃迁时不会重复落库）
        if self._already_consolidated(space_id, topic.topic_id):
            report["reason"] = "该主题已做过事件沉淀，跳过"
            return report

        topics_store = TopicStore(self.db)
        experiences = topics_store.get_experiences_by_topic(space_id, topic.topic_id)
        self_view = self._current_self_view(space_id)

        messages = prompts.build_consolidate_event_messages(
            title=topic.title,
            abstract=topic.abstract,
            keywords=topic.keywords,
            experiences=experiences,
            self_view=self_view,
        )

        payload = self._call_json(messages, report)

        title = str(payload.get("title") or "").strip() or topic.title
        summary = str(payload.get("summary") or "").strip()
        lessons = self._normalize_lessons(payload.get("lessons"))
        impact = str(payload.get("impact") or "").strip()

        if not summary:
            summary = f"{topic.title}：{topic.abstract[:200]}"
            report["degraded"] = True

        summary = summary[: config.CONSOLIDATE_SUMMARY_MAX_CHARS]

        event_topic = Topic(
            topic_id=_new_id("event"),
            space_id=space_id,
            title=f"【大事】{title}"[:20],
            abstract=summary,
            keywords=topic.keywords[:6],
            session_id=topic.session_id,
            snapshot_version=topic.snapshot_version,
            kind=KIND_EVENT,
            is_big_event=True,
            create_ts=int(time.time()),
        )
        topics_store.save_topic(event_topic)
        VectorStore(space_id).add(
            doc_id=event_topic.topic_id,
            text=f"{event_topic.title}。{summary}",
            meta={"title": event_topic.title, "kind": KIND_EVENT, "source_topic_id": topic.topic_id},
        )

        for index, lesson in enumerate(lessons):
            topics_store.add_experience(
                Experience(
                    exp_id=_new_id(f"eexp{index + 1}"),
                    space_id=space_id,
                    topic_id=event_topic.topic_id,
                    content=lesson,
                )
            )

        if impact:
            topics_store.add_experience(
                Experience(
                    exp_id=_new_id("eimpact"),
                    space_id=space_id,
                    topic_id=event_topic.topic_id,
                    content=f"【长期影响】{impact}"[: config.EXPERIENCE_MAX_CHARS],
                )
            )

        self._append_log(
            space_id=space_id,
            kind=KIND_EVENT,
            range_from=topic.create_ts,
            range_to=topic.create_ts,
            topic_ids=[topic.topic_id],
            summary=f"{title}：{summary}",
        )

        topics_store.mark_big_event(space_id, topic.topic_id)

        report["ok"] = True
        report["title"] = title
        report["topic_id"] = event_topic.topic_id
        report["impact"] = impact
        report["reason"] = "大事沉淀完成"
        return report

    # ------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------
    def _already_consolidated(self, space_id: str, topic_id: str) -> bool:
        """
        判断某个主题是否已经做过事件沉淀（幂等保护）。

        :param space_id: 记忆空间
        :param topic_id: 主题 id
        :return: 是否已沉淀
        """
        connection = self.db.conn()
        row = connection.execute(
            """
            select count(1) as cnt
            from consolidation_log as c
            where c.space_id = ? and c.kind = 'event' and c.topic_ids like ?
            """,
            (space_id, f'%"{topic_id}"%'),
        ).fetchone()

        return int(row["cnt"] or 0) > 0

    def _call_json(self, messages: List[Dict[str, str]], report: Dict[str, Any]) -> Dict[str, Any]:
        """
        调用模型并解析 JSON；失败返回空字典，由调用方降级。

        :param messages: 消息列表
        :param report: 报告字典（用于记录错误原因）
        :return: 解析后的字典
        """
        if not config.DAILY_USE_MODEL:
            report["degraded"] = True
            return {}

        try:
            return self._get_client().chat_json(
                messages,
                temperature=0.3,
                max_tokens=config.CONSOLIDATE_MAX_TOKENS,
            )
        except LLMError as exc:
            report["degraded"] = True
            report["error"] = f"巩固模型调用失败：{exc}"
            return {}
        except Exception as exc:  # 客户端未配置等
            report["degraded"] = True
            report["error"] = f"{type(exc).__name__}: {exc}"
            return {}

    @staticmethod
    def _normalize_lessons(value: Any) -> List[str]:
        """
        规范化 lessons 字段（必须是非空字符串列表，且有长度上限）。

        :param value: 模型原始值
        :return: 经验列表
        """
        if not isinstance(value, list):
            return []

        lessons: List[str] = []
        for item in value:
            text = str(item or "").strip()
            if not text:
                continue
            lessons.append(text[: config.EXPERIENCE_MAX_CHARS])
            if len(lessons) >= config.ARCHIVE_MAX_EXPERIENCES:
                break
        return lessons

    @staticmethod
    def _collect_keywords(topics: List[Topic]) -> List[str]:
        """
        汇总关键词，取出现频次最高的若干个。

        :param topics: 主题列表
        :return: 关键词列表（最多 8 个）
        """
        counter: Dict[str, int] = {}
        for topic in topics:
            for keyword in topic.keywords:
                key = str(keyword).strip()
                if key:
                    counter[key] = counter.get(key, 0) + 1

        ordered = sorted(counter.items(), key=lambda pair: (-pair[1], pair[0]))
        return [item[0] for item in ordered[:8]]

    def _current_self_view(self, space_id: str) -> str:
        """取当前自我认知（即时层优先），供巩固 Prompt 使用。"""
        try:
            from cognitive.persona.kernel import PersonaKernel
            from cognitive.persona.snapshot import PersonaSnapshotStore
            from cognitive.storage.kg_store import KGStore

            kernel = PersonaKernel(self.db, space_id)
            store = PersonaSnapshotStore(self.db, space_id, kernel, KGStore(self.db))
            return store.current_self_view()
        except Exception:
            return ""

    def _current_version(self, space_id: str) -> int:
        """取当前人格快照版本号。"""
        try:
            from cognitive.persona.kernel import PersonaKernel

            return PersonaKernel(self.db, space_id).get_current_version()
        except Exception:
            return 0
