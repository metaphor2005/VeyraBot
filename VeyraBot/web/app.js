/* ============================================================
 * 江妍 · 认知主体 Web UI（微信电脑版风格）v20261113
 *
 * - 单联系人：只有江妍，一个固定会话（不暴露空间/会话等技术概念）
 * - 聊天：POST /api/chat
 * - 主动消息：chat 响应携带 + 定时轮询发件箱（GET /api/messages/outbox）
 *   好感度跌破阈值时，她会主动找你，或发出断交声明
 *   日常也会按模型排的时间表，从记忆库里想起一件事主动发消息（initiative）
 * - 设置窗口：深色模式 / 模型连接（供应商·模型·密钥）/ 自定义头像
 * - 黑白主题：localStorage 记忆，CSS 变量切换
 * ============================================================ */
"use strict";

const $ = (sel) => document.querySelector(sel);

const SESSION_ID = "main";

const state = {
  theme: localStorage.getItem("cg.theme") || "light",
  space: "default",
  providers: [],
  settings: null,
  sending: false,
  seenNotices: new Set(JSON.parse(localStorage.getItem("cg.seen") || "[]")),
  avatarTs: Date.now(),
};

const KIND_LABEL = {
  reach_out: "主动联系",
  severance: "断交",
  reconcile: "和解",
  initiative: "想起你",
};

/* ---------------- 基础工具 ---------------- */

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function fmtListTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const now = new Date();
  const showTime = (ts) => fmtTime(ts);
  // 聊天记录里的时间分组：
  // 今天 → 直接显示时间；昨天 → 昨天 + 时间；前天 → 前天 + 时间；更早 → 具体日期 + 时间
  if (d.toDateString() === now.toDateString()) return showTime(ts);
  const yest = new Date(now.getTime() - 24 * 3600 * 1000);
  if (d.toDateString() === yest.toDateString()) return `昨天 ${showTime(ts)}`;
  const d2 = new Date(now.getTime() - 2 * 24 * 3600 * 1000);
  if (d.toDateString() === d2.toDateString()) return `前天 ${showTime(ts)}`;
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 ${showTime(ts)}`;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

function scrollBottom() {
  const box = $("#messages");
  box.scrollTop = box.scrollHeight;
}

/* ---------------- 头像元素 ---------------- */

function makeAvatar(kind) {
  const wrap = document.createElement("div");
  wrap.className = "avatar-wrap";

  const img = document.createElement("img");
  img.className = "avatar-img";
  img.alt = kind === "persona" ? "江妍" : "我";
  if (kind === "persona") {
    img.src = "/api/avatar/persona";
  } else {
    img.src = `/api/avatar/user?ts=${state.avatarTs}`;
  }

  const fb = document.createElement("div");
  fb.className = `avatar avatar-fallback ${kind === "persona" ? "fb-persona" : "fb-user"}`;
  fb.textContent = kind === "persona" ? "妍" : "我";
  fb.hidden = true;

  img.onerror = () => {
    img.hidden = true;
    fb.hidden = false;
  };

  wrap.append(img, fb);
  return wrap;
}

/* ---------------- 渲染 ---------------- */

function makeMsg(role, content, ts) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role === "user" ? "out" : "in"}`;
  wrap.append(makeAvatar(role === "user" ? "user" : "persona"));

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.textContent = content;
  wrap.appendChild(bubble);

  if (ts) wrap.dataset.ts = ts;
  return wrap;
}

function makeTimeDivider(ts) {
  const el = document.createElement("div");
  el.className = "time-divider";
  el.textContent = fmtListTime(ts);
  el.dataset.ts = ts;
  return el;
}

function makeSysNotice(notice) {
  const el = document.createElement("div");
  el.className = "sys";
  el.dataset.msgId = notice.msg_id;
  const kind = KIND_LABEL[notice.kind] || notice.kind;
  el.innerHTML = `
    <div>
      <span class="sys-kind k-${notice.kind}">${kind}</span>
      她向「${notice.person_name}」发出了消息
    </div>
    <div class="sys-text"></div>`;
  el.querySelector(".sys-text").textContent = notice.content || "";
  return el;
}

/* 主动消息落位：
 * - initiative：她日常想起一件事来找你，按普通 assistant 气泡显示；
 * - reach_out / severance / reconcile：关系事件，按居中系统通知显示。
 */
function appendProactiveNotice(box, notice) {
  if (notice.kind === "initiative") {
    box.appendChild(
      makeMsg(
        "assistant",
        notice.content || "",
        notice.create_ts || Math.floor(Date.now() / 1000)
      )
    );
    return;
  }
  box.appendChild(makeSysNotice(notice));
}

function renderMessages(items) {
  const box = $("#messages");
  box.innerHTML = "";
  if (!items.length) {
    box.innerHTML =
      '<div class="empty-hint">和江妍说点什么吧。<br>她会记住你们聊过的每一件事。</div>';
    return;
  }
  let lastTs = 0;
  for (const it of items) {
    if (it.ts - lastTs > 300) box.appendChild(makeTimeDivider(it.ts));
    lastTs = it.ts || lastTs;
    if (it.kind) box.appendChild(makeSysNotice(it));
    else box.appendChild(makeMsg(it.role, it.content, it.ts));
  }
  scrollBottom();
}

/* ---------------- 回复拆分（分隔符 / 空行 → 多个气泡） ---------------- */

function splitReply(text) {
  const raw = String(text || "").trim();
  if (!raw) return [""];

  // 1) 纯分隔符行（--- / *** / ___ / === / —— 等）先切一刀
  let parts = raw.split(/\n[ \t]*(?:[-—–_*~=·|｜]{2,})[ \t]*\n/);
  // 2) 她换段（空行）也视为"下一条消息"
  parts = parts.flatMap((p) => p.split(/\n{2,}/));

  const cleaned = parts.map((p) => p.trim()).filter(Boolean);
  if (cleaned.length <= 1) return [raw];

  // 最多 5 个气泡，多出来的并进最后一个，避免刷屏
  if (cleaned.length > 5) {
    return [...cleaned.slice(0, 4), cleaned.slice(4).join("\n\n")];
  }
  return cleaned;
}

/* ---------------- 会话内容 ---------------- */

async function loadHistory() {
  let dialogue = [];
  let notices = [];
  try {
    const [msgData, outData] = await Promise.all([
      api(`/api/sessions/${SESSION_ID}/messages?space_id=${encodeURIComponent(state.space)}`),
      api(
        `/api/messages/outbox?space_id=${encodeURIComponent(state.space)}` +
          `&session_id=${encodeURIComponent(SESSION_ID)}&pending=0&limit=200`
      ),
    ]);
    dialogue = (msgData.messages || []).map((m) => ({
      role: m.role,
      content: m.content,
      ts: m.ts,
    }));
    notices = (outData.messages || []).map((m) => ({
      ...m,
      kind: m.kind,
      ts: m.create_ts || 0,
    }));
  } catch (err) {
    console.warn("历史加载失败（新会话属于正常情况）：", err.message);
  }

  // 日常主动消息（initiative）在生成时已写入会话历史，直接按 assistant 气泡还原；
  // 不能再从发件箱叠一遍，否则刷新后会出现两条一样的气泡
  const historyNotices = notices.filter((n) => n.kind !== "initiative");
  const items = [...dialogue, ...historyNotices].sort(
    (a, b) => (a.ts || 0) - (b.ts || 0)
  );
  renderMessages(items);
  updateEntryPreview(items);
  markSeen(notices);
  // 历史里已展示过的通知顺手确认掉，避免发件箱待处理数越积越多
  for (const n of notices) if (!n.acked) ackNotice(n).catch(() => {});
}

function updateEntryPreview(items) {
  const entryTime = $("#entryTime");
  const entryPreview = $("#entryPreview");
  if (!items.length) {
    entryTime.textContent = "";
    entryPreview.textContent = "她会记住你们聊过的每一件事";
    return;
  }
  const last = items[items.length - 1];
  entryTime.textContent = fmtListTime(last.ts);
  const text = (last.content || "").replace(/\s+/g, " ").trim();
  entryPreview.textContent = text.slice(0, 40) || "（新消息）";
}

/* ---------------- 发送 ---------------- */

async function sendMessage() {
  const input = $("#input");
  const text = input.value.trim();
  if (!text || state.sending) return;

  state.sending = true;
  $("#sendBtn").disabled = true;
  input.value = "";

  const box = $("#messages");
  if (box.querySelector(".empty-hint")) box.innerHTML = "";
  box.appendChild(makeMsg("user", text, Math.floor(Date.now() / 1000)));
  scrollBottom();
  $("#typing").classList.remove("hidden");
  scrollBottom();

  try {
    const data = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({
        space_id: state.space,
        session_id: SESSION_ID,
        message: text,
      }),
    });

    $("#typing").classList.add("hidden");

    // 服务端已按分隔符/空行切好并分条入库（answer_parts）；
    // 旧服务端没有该字段时，前端用同款规则现场切分兜底
    const bubbles =
      Array.isArray(data.answer_parts) && data.answer_parts.length
        ? data.answer_parts
        : splitReply(data.answer);
    for (let i = 0; i < bubbles.length; i += 1) {
      if (i > 0) {
        await new Promise((r) => setTimeout(r, 140));
      }
      box.appendChild(makeMsg("assistant", bubbles[i], Math.floor(Date.now() / 1000)));
      scrollBottom();
    }

    // 主动消息（好感度阈值触发 / 日常想起你）：按类型落位 + ack
    const notices = data.proactive_messages || [];
    for (const n of notices) {
      appendProactiveNotice(box, n);
      markSeen([n]);
      ackNotice(n).catch(() => {});
    }
    scrollBottom();

    updateEntryPreview([
      { ts: Math.floor(Date.now() / 1000), content: bubbles[bubbles.length - 1] },
    ]);
  } catch (err) {
    $("#typing").classList.add("hidden");
    box.appendChild(
      makeMsg("assistant", `（发送失败：${err.message}）`, Math.floor(Date.now() / 1000))
    );
    scrollBottom();
  } finally {
    state.sending = false;
    $("#sendBtn").disabled = false;
    input.focus();
  }
}

/* ---------------- 主动消息轮询 ---------------- */

function markSeen(notices) {
  for (const n of notices) {
    if (n.msg_id) state.seenNotices.add(n.msg_id);
  }
  localStorage.setItem("cg.seen", JSON.stringify([...state.seenNotices].slice(-500)));
}

async function ackNotice(n) {
  await api("/api/messages/outbox/ack", {
    method: "POST",
    body: JSON.stringify({ space_id: state.space, msg_id: n.msg_id }),
  });
}

async function pollOutbox() {
  try {
    const data = await api(
      `/api/messages/outbox?space_id=${encodeURIComponent(state.space)}` +
        `&session_id=${encodeURIComponent(SESSION_ID)}&pending=1&limit=20`
    );
    const fresh = (data.messages || []).filter((n) => !state.seenNotices.has(n.msg_id));
    if (!fresh.length) return;

    markSeen(fresh);
    const box = $("#messages");
    for (const n of fresh) {
      ackNotice(n).catch(() => {});
      appendProactiveNotice(box, n);
    }
    scrollBottom();
    updateEntryPreview([{ ts: fresh[fresh.length - 1].create_ts, content: fresh[fresh.length - 1].content }]);
  } catch (err) {
    /* 后台轮询失败静默（服务器可能暂不可用） */
  }
}


/* ---------------- 功能开关（设置页「功能」页签） ---------------- */
/* 对应 config 中的布尔开关：设置页保存后，服务端临时覆盖 config 并重建 agent */
const FEATURE_SETTINGS = [
  { key: "AFFECTION_ENABLED", label: "好感度系统", note: "关闭后不再产生好感度演化与关系事件消息" },
  { key: "PROACTIVE_ENABLED", label: "日常主动联系", note: "关闭后不再按时间表从记忆库主动发消息" },
  { key: "ENABLE_ARCHIVE", label: "主动归档", note: "关闭后每轮对话不再写入主题/信念/经验记忆" },
  { key: "ARCHIVE_SYNC", label: "归档同步执行", note: "关闭后可改为后台执行归档" },
  { key: "RAG_LOOP_ENABLED", label: "多轮检索循环", note: "关闭后检索只执行一轮" },
  { key: "ENABLE_RAG", label: "RAG 兜底", note: "关闭后不启用本地语料检索" },
  { key: "PERSONA_DAILY_SETTLE_ENABLED", label: "人格日结结算", note: "关闭后人格基调不再每日推进" },
  { key: "DAILY_CONSOLIDATE_ENABLED", label: "日结巩固", note: "关闭后不再每天归并往事" },
  { key: "BIG_EVENT_TRIGGER", label: "大事立即沉淀", note: "关闭后大事只归档，不立即生成事件记忆" },
  { key: "BIG_EVENT_REQUIRE_SELF", label: "大事需涉及本人", note: "关闭后大事按纯重要度判定（可能误判）" },
  { key: "CONSOLIDATE_MARK_CONSUMED", label: "日结标记已归并", note: "关闭后原主题不再标记为已归并" },
  { key: "VECTOR_EXPAND_ENABLED", label: "同义词检索扩展", note: "关闭后检索只用字面 TF-IDF" },
  { key: "DAILY_USE_MODEL", label: "日结使用模型摘要", note: "关闭后日结退化为机械摘要" },
];

function renderFeatureSettings() {
  const host = document.getElementById("tab-features");
  if (!host) return;
  host.innerHTML = "";
  for (const f of FEATURE_SETTINGS) {
    const row = document.createElement("div");
    row.className = "setting-row";
    row.innerHTML = `
      <div class="setting-label">${f.label}</div>
      <label class="switch">
        <input type="checkbox" data-feature="${f.key}" checked>
        <span class="slider"></span>
      </label>
      <span class="setting-note">${f.note}</span>`;
    host.appendChild(row);
  }
  // 初始值从已保存的设置读取，避免界面与磁盘不一致
  for (const f of FEATURE_SETTINGS) {
    const cb = host.querySelector(`input[data-feature="${f.key}"]`);
    if (cb && state.features && state.features[f.key] !== undefined) {
      cb.checked = state.features[f.key];
    }
  }
}

function updateFeatureSettingsUI() {
  const host = document.getElementById("tab-features");
  if (!host) return;
  state.features = state.features || {};
  // 把界面状态同步回 state，保存时一起提交
  for (const f of FEATURE_SETTINGS) {
    const cb = host.querySelector(`input[data-feature="${f.key}"]`);
    if (cb) state.features[f.key] = cb.checked;
  }
}

function toggleFeature(key, checked) {
  state.features = state.features || {};
  state.features[key] = !!checked;
}

/* ---------------- 记忆：信念列表与纠正 ---------------- */

const BELIEF_STATUS_LABEL = {
  active: "生效",
  candidate: "待验证",
  rejected: "已否决",
};

async function loadBeliefs() {
  const host = $("#beliefList");
  if (!host) return;
  host.innerHTML = `<p class="belief-empty">正在读取……</p>`;
  try {
    const data = await api(
      `/api/memory/beliefs?space_id=${encodeURIComponent(state.space)}&limit=120`
    );
    const beliefs = data.beliefs || [];
    host.innerHTML = "";
    if (!beliefs.length) {
      host.innerHTML = `<p class="belief-empty">她还没有记住需要纠正的事。</p>`;
      return;
    }
    for (const b of beliefs) {
      host.appendChild(makeBeliefItem(b));
    }
  } catch (err) {
    host.innerHTML = `<p class="belief-empty">读取失败：${err.message}</p>`;
  }
}

function makeBeliefItem(belief) {
  const item = document.createElement("div");
  item.className = "belief-item";

  const main = document.createElement("div");
  main.className = "belief-main";
  const text = document.createElement("div");
  text.className = "belief-text";
  text.textContent = `${belief.head} ${belief.rel} ${belief.tail}`;
  const meta = document.createElement("div");
  meta.className = "belief-meta";
  const sourceLabel =
    belief.source_type === "user_stated"
      ? "你说过"
      : belief.source_type === "user_corrected"
      ? "你纠正过"
      : belief.source_type === "seed"
      ? "前史"
      : "她推断";
  meta.textContent = `${sourceLabel} · 置信 ${Number(belief.confidence).toFixed(2)} · 重要 ${Number(belief.importance).toFixed(2)}`;
  main.appendChild(text);
  main.appendChild(meta);

  const status = document.createElement("span");
  status.className = `belief-status ${belief.status || "active"}`;
  status.textContent = BELIEF_STATUS_LABEL[belief.status] || belief.status || "生效";

  const actions = document.createElement("div");
  actions.className = "belief-actions";
  const mk = (label, action) => {
    const btn = document.createElement("button");
    btn.className = "btn-secondary";
    btn.textContent = label;
    btn.addEventListener("click", () => updateBelief(belief.rid, action, btn));
    return btn;
  };
  actions.appendChild(mk("确认", "confirm"));
  actions.appendChild(mk("纠正", "correct"));
  actions.appendChild(mk("否决", "reject"));

  item.appendChild(main);
  item.appendChild(status);
  item.appendChild(actions);
  return item;
}

async function updateBelief(rid, action, btn) {
  if (btn) btn.disabled = true;
  try {
    await api("/api/memory/beliefs/update", {
      method: "POST",
      body: JSON.stringify({ space_id: state.space, rid, action }),
    });
    await loadBeliefs();
  } catch (err) {
    alert(`操作失败：${err.message}`);
    if (btn) btn.disabled = false;
  }
}

/* ---------------- 人格：冻结 / 回退 / 主动联系偏好 ---------------- */

async function loadPersonaSettings() {
  const summary = $("#personaSummary");
  try {
    const data = await api(
      `/api/persona?space_id=${encodeURIComponent(state.space)}`
    );
    const frozen = !!data.frozen;
    const sw = $("#personaFrozenSwitch");
    if (sw) sw.checked = frozen;
    if (summary) {
      const version = data.version ?? 0;
      const selfView = (data.self_view || "").trim();
      summary.textContent = `当前人格版本 v${version}${selfView ? ` · ${selfView}` : ""}${frozen ? " · 已冻结" : ""}`;
    }
  } catch (err) {
    if (summary) summary.textContent = `人格状态读取失败：${err.message}`;
  }

  try {
    const policyData = await api(
      `/api/proactive/policy?space_id=${encodeURIComponent(state.space)}`
    );
    const p = policyData.policy || {};
    $("#dndStartInput").value = p.dnd_start ?? 23;
    $("#dndEndInput").value = p.dnd_end ?? 8;
    $("#dailyCapInput").value = p.daily_cap ?? 3;
    $("#cooldownInput").value = p.cooldown_minutes ?? 60;
  } catch (err) {
    /* 偏好读取失败不影响冻结/回退 */
  }
}

async function setPersonaFrozen(frozen) {
  const status = $("#personaSummary");
  try {
    const data = await api("/api/persona/freeze", {
      method: "POST",
      body: JSON.stringify({ space_id: state.space, frozen }),
    });
    if (status) status.textContent = data.frozen ? "人格已冻结" : "人格已解冻";
  } catch (err) {
    alert(`操作失败：${err.message}`);
    const sw = $("#personaFrozenSwitch");
    if (sw) sw.checked = !frozen;
  }
}

async function resetPersona() {
  if (!confirm("把她的性格回到最初吗？记忆和往事会保留。")) return;
  try {
    await api("/api/persona/reset", {
      method: "POST",
      body: JSON.stringify({ space_id: state.space }),
    });
    await loadPersonaSettings();
    const status = $("#personaSummary");
    if (status) status.textContent = "人格已回到基线";
  } catch (err) {
    alert(`操作失败：${err.message}`);
  }
}

async function saveProactivePolicy() {
  const status = $("#policyStatus");
  const toInt = (id, fallback) => {
    const v = parseInt($(id).value, 10);
    return Number.isFinite(v) ? v : fallback;
  };
  try {
    await api("/api/proactive/policy", {
      method: "POST",
      body: JSON.stringify({
        space_id: state.space,
        dnd_start: toInt("#dndStartInput", 23),
        dnd_end: toInt("#dndEndInput", 8),
        daily_cap: toInt("#dailyCapInput", 3),
        cooldown_minutes: toInt("#cooldownInput", 60),
      }),
    });
    if (status) {
      status.textContent = "已保存";
      status.className = "setting-status ok";
    }
  } catch (err) {
    if (status) {
      status.textContent = `保存失败：${err.message}`;
      status.className = "setting-status err";
    }
  }
}

async function skipNextProactive() {
  const status = $("#policyStatus");
  try {
    await api("/api/proactive/skip", {
      method: "POST",
      body: JSON.stringify({ space_id: state.space }),
    });
    if (status) {
      status.textContent = "这一轮她不会主动来找你";
      status.className = "setting-status ok";
    }
  } catch (err) {
    if (status) {
      status.textContent = `操作失败：${err.message}`;
      status.className = "setting-status err";
    }
  }
}

/* ---------------- 设置窗口 ---------------- */

function fillModelSuggestions(provider) {
  const list = $("#modelSuggestions");
  list.innerHTML = "";
  const p = provider || state.providers.find((x) => x.id === $("#providerSelect").value);
  for (const model of (p && p.models) || []) {
    const opt = document.createElement("option");
    opt.value = model;
    list.appendChild(opt);
  }
}

async function refreshSettings() {
  const data = await api("/api/settings");
  state.providers = data.providers || [];
  state.settings = data.settings || {};
  state.features = (state.settings && state.settings.features) || {};
  if (data.space) state.space = data.space;

  const sel = $("#providerSelect");
  sel.innerHTML = "";
  for (const p of state.providers) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.name;
    sel.appendChild(opt);
  }
  if (state.settings.provider) sel.value = state.settings.provider;

  $("#baseUrlInput").value = state.settings.base_url || "";
  $("#modelInput").value = state.settings.model || "";
  $("#apiKeyInput").value = "";
  $("#apiKeyInput").placeholder = state.settings.api_key_set
    ? `已保存（${state.settings.api_key_masked}），留空则不修改`
    : "未设置";
  fillModelSuggestions();
  renderFeatureSettings();
}

async function openSettings() {
  $("#settingsOverlay").classList.remove("hidden");
  const status = $("#modelStatus");
  status.textContent = "";
  status.className = "setting-status";
  try {
    await refreshSettings();
    renderFeatureSettings();
    refreshAvatarPreview();
  } catch (err) {
    status.textContent = `设置读取失败：${err.message}`;
    status.className = "setting-status err";
  }
}

function closeSettings() {
  $("#settingsOverlay").classList.add("hidden");
}

function setStatus(el, text, kind) {
  el.textContent = text;
  el.className = "setting-status" + (kind ? ` ${kind}` : "");
}

async function saveModelSettings() {
  const btn = $("#saveModelBtn");
  const status = $("#modelStatus");
  btn.disabled = true;
  setStatus(status, "正在保存…");
  try {
    // 先把功能开关从界面同步回 state，确保保存的是所有开关的当前状态
    updateFeatureSettingsUI();
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        provider: $("#providerSelect").value,
        base_url: $("#baseUrlInput").value.trim(),
        model: $("#modelInput").value.trim(),
        api_key: $("#apiKeyInput").value.trim(),
        features: JSON.stringify(state.features),
      }),
    });
    await refreshSettings();
    setStatus(status, "已保存，新的模型连接已经生效", "ok");
  } catch (err) {
    setStatus(status, `保存失败：${err.message}`, "err");
  } finally {
    btn.disabled = false;
  }
}

async function testModelConnection() {
  const btn = $("#testConnBtn");
  const status = $("#modelStatus");
  btn.disabled = true;
  setStatus(status, "正在测试连接…");
  try {
    const data = await api("/api/settings/test", {
      method: "POST",
      body: JSON.stringify({
        base_url: $("#baseUrlInput").value.trim(),
        model: $("#modelInput").value.trim(),
        api_key: $("#apiKeyInput").value.trim(),
      }),
    });
    if (data.ok) {
      setStatus(status, `连接正常（${data.latency_ms} ms）`, "ok");
    } else {
      setStatus(status, `连接失败：${data.error}`, "err");
    }
  } catch (err) {
    setStatus(status, `连接失败：${err.message}`, "err");
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- 头像 ---------------- */

function refreshAvatarPreview() {
  const img = $("#avatarPreview");
  const fb = $("#avatarPreviewFallback");
  img.hidden = false;
  fb.hidden = true;
  img.src = `/api/avatar/user?ts=${state.avatarTs}`;
}

function refreshUserAvatarBubbles() {
  document.querySelectorAll(".msg.out .avatar-img").forEach((img) => {
    img.hidden = false;
    const fb = img.nextElementSibling;
    if (fb) fb.hidden = true;
    img.src = `/api/avatar/user?ts=${state.avatarTs}`;
  });
}

async function uploadAvatar(file) {
  const status = $("#avatarStatus");
  if (file.size > 2 * 1024 * 1024) {
    setStatus(status, "图片不能超过 2MB", "err");
    return;
  }
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      setStatus(status, "正在保存…");
      await api("/api/avatar/user", {
        method: "POST",
        body: JSON.stringify({ data: reader.result }),
      });
      state.avatarTs = Date.now();
      refreshAvatarPreview();
      refreshUserAvatarBubbles();
      setStatus(status, "头像已更新", "ok");
    } catch (err) {
      setStatus(status, `上传失败：${err.message}`, "err");
    }
  };
  reader.readAsDataURL(file);
}

async function resetAvatar() {
  const status = $("#avatarStatus");
  try {
    await api("/api/avatar/user", {
      method: "POST",
      body: JSON.stringify({ data: "" }),
    });
    state.avatarTs = Date.now();
    $("#avatarPreview").hidden = true;
    $("#avatarPreviewFallback").hidden = false;
    refreshUserAvatarBubbles();
    setStatus(status, "已恢复默认头像", "ok");
  } catch (err) {
    setStatus(status, `操作失败：${err.message}`, "err");
  }
}

/* ---------------- 主题 ---------------- */

function applyTheme() {
  document.documentElement.dataset.theme = state.theme;
  const darkSwitch = $("#darkSwitch");
  if (darkSwitch) darkSwitch.checked = state.theme === "dark";
  const toggle = $("#themeToggle");
  if (toggle) toggle.title = state.theme === "light" ? "切换到深色模式" : "切换到浅色模式";
}

function setTheme(theme) {
  state.theme = theme;
  localStorage.setItem("cg.theme", theme);
  applyTheme();
}

function toggleTheme() {
  setTheme(state.theme === "light" ? "dark" : "light");
}

/* ---------------- 事件绑定 ---------------- */

function bindEvents() {
  $("#themeToggle").addEventListener("click", toggleTheme);
  $("#settingsBtn").addEventListener("click", openSettings);
  $("#settingsClose").addEventListener("click", closeSettings);
  $("#settingsOverlay").addEventListener("mousedown", (e) => {
    if (e.target === $("#settingsOverlay")) closeSettings();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSettings();
  });

  document.querySelectorAll(".modal-nav-item").forEach((item) => {
    item.addEventListener("click", () => {
      document.querySelectorAll(".modal-nav-item").forEach((x) => x.classList.remove("active"));
      item.classList.add("active");
      ["general", "model", "profile", "features", "memory", "persona"].forEach((tab) => {
        const el = $(`#tab-${tab}`);
        if (el) el.classList.toggle("hidden", tab !== item.dataset.tab);
      });
      if (item.dataset.tab === "memory") loadBeliefs();
      if (item.dataset.tab === "persona") loadPersonaSettings();
    });
  });

  $("#beliefRefreshBtn").addEventListener("click", loadBeliefs);
  $("#personaFrozenSwitch").addEventListener("change", (e) => {
    setPersonaFrozen(e.target.checked);
  });
  $("#personaResetBtn").addEventListener("click", resetPersona);
  $("#savePolicyBtn").addEventListener("click", saveProactivePolicy);
  $("#skipNextBtn").addEventListener("click", skipNextProactive);

  $("#darkSwitch").addEventListener("change", (e) => {
    setTheme(e.target.checked ? "dark" : "light");
  });

  $("#providerSelect").addEventListener("change", () => {
    const p = state.providers.find((x) => x.id === $("#providerSelect").value);
    if (!p) return;
    if (p.base_url) $("#baseUrlInput").value = p.base_url;
    fillModelSuggestions(p);
    if (p.models.length && !p.models.includes($("#modelInput").value.trim())) {
      $("#modelInput").value = p.models[0];
    }
  });

  $("#testConnBtn").addEventListener("click", testModelConnection);
  $("#saveModelBtn").addEventListener("click", saveModelSettings);

  $("#chooseAvatarBtn").addEventListener("click", () => $("#avatarFile").click());
  $("#avatarFile").addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    if (file) uploadAvatar(file);
    e.target.value = "";
  });
  $("#resetAvatarBtn").addEventListener("click", resetAvatar);

  $("#sendBtn").addEventListener("click", sendMessage);
  $("#input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendMessage();
    }
  });
}

/* ---------------- 初始化 ---------------- */

async function init() {
  applyTheme();
  bindEvents();

  // 拉一次设置：确定她所在的记忆空间（界面不暴露这个概念）
  try {
    const data = await api("/api/settings");
    state.providers = data.providers || [];
    state.settings = data.settings || {};
    if (data.space) state.space = data.space;
  } catch (err) {
    console.warn("设置读取失败：", err.message);
  }

  await loadHistory();

  // 主动消息轮询：每 12 秒（断交/主动联系的"突袭感"就靠它）
  pollOutbox();
  setInterval(pollOutbox, 12000);
  $("#input").focus();
}

init();
