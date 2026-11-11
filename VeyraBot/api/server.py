"""
开发者 HTTP API 服务器（Flask）。

给其他开发者用的收发消息接口 + 记忆/人格/好感度观测接口 + 主动消息发件箱。
Web UI（web/index.html）由本服务直接托管，开箱即用。

启动：
    python api/server.py            # 或 python -m api.server

端点总览（JSON，除静态资源外统一 UTF-8）：
    GET  /                              # Web UI
    GET  /api/health                    # 健康检查
    GET  /api/settings                  # 当前模型连接设置 + 可选供应商列表
    POST /api/settings                  # 保存模型连接设置（供应商/地址/密钥/模型）
    POST /api/settings/test             # 用给定（或当前）设置试连一次模型
    GET  /api/avatar/persona            # 江妍的头像（data/persona/avatar.jpg）
    GET  /api/avatar/user               # 用户自定义头像（未设置返回 404）
    POST /api/avatar/user               # 上传用户头像 {data: dataURL}；data 为空串则清除
    GET  /api/spaces                    # 记忆空间列表
    POST /api/spaces                    # 注册空间 {space_id, name}
    POST /api/chat                      # 发消息给 AI（核心收发接口）
    GET  /api/sessions                  # 会话列表（?space_id=）
    GET  /api/sessions/<sid>/messages   # 某会话完整消息历史
    GET  /api/memory/overview           # 记忆总览
    GET  /api/relations                 # 信念（KG 关系）列表
    GET  /api/topics                    # 主题列表
    GET  /api/persona                   # 人格状态
    GET  /api/persona/history           # 人格变迁史文本
    GET  /api/affection                 # 好感度列表
    GET  /api/messages/outbox           # 主动消息发件箱（?pending=1）
    POST /api/messages/outbox/ack      # 确认主动消息 {space_id, msg_id}
    GET  /api/proactive/schedule        # 下一次主动联系的时间表
    POST /api/proactive/tick            # 立即检查/触发一次主动联系（body 可带 force）

说明：
- 全局单例 CognitiveAgent：Database 连接按线程隔离（threading.local），
  多线程请求安全；模型客户端为共享连接池。
- 模型连接设置持久化在 DB_PATH 同目录的 ui_settings.json，
  保存后立即重建 agent（同一记忆库，只换模型客户端）。
- 无鉴权：默认只监听 127.0.0.1。对外暴露请改 API_HOST=0.0.0.0 并自行加网关。
"""
from __future__ import annotations

import base64
import io
import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

# 允许 python api/server.py 直接运行（与 tests/smoke_test.py 同款处理）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify, request, send_file, send_from_directory

import config
from cognitive.engine import CognitiveAgent
from cognitive.llm import DeepSeekClient, LLMError
from cognitive.storage.affection_store import ProactiveScheduleStore
from cognitive.storage.database import Database
from cognitive.storage.kg_store import KGStore
from cognitive.storage.topic_store import TopicStore

# Web 静态资源目录（与 api/ 平级）
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# 江妍的头像：人格原始数据的一部分，随 data/persona/ 一起备份/迁移
PERSONA_AVATAR_FILE = config.PERSONA_AVATAR_FILE

# 模型连接设置持久化位置：与记忆库同目录（换库即换配置，不污染真实库）
SETTINGS_FILE = config.DB_PATH.parent / "ui_settings.json"

# 用户自定义头像（JSON 包装：mime + base64，避免扩展名纠缠）
USER_AVATAR_FILE = config.DB_PATH.parent / "user_avatar.json"

# 预置供应商：OpenAI 兼容协议均可接入；models 只是输入建议，不限制填写
LLM_PROVIDERS = [
    {
        "id": "deepseek",
        "name": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-flash", "deepseek-chat", "deepseek-reasoner"],
    },
    {
        "id": "siliconflow",
        "name": "SiliconFlow 硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": [
            "deepseek-ai/DeepSeek-V3",
            "deepseek-ai/DeepSeek-V3.1",
            "Qwen/Qwen2.5-72B-Instruct",
        ],
    },
    {
        "id": "moonshot",
        "name": "Moonshot 月之暗面",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "kimi-latest"],
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [
            "deepseek/deepseek-chat",
            "openai/gpt-4o-mini",
            "anthropic/claude-3.5-sonnet",
        ],
    },
    {
        "id": "custom",
        "name": "自定义（OpenAI 兼容）",
        "base_url": "",
        "models": [],
    },
]

# 用户头像大小上限（解码后的字节数）
USER_AVATAR_MAX_BYTES = 2 * 1024 * 1024

# data URL: data:image/png;base64,xxxx
_DATA_URL_PATTERN = re.compile(
    r"^data:(image/(?:png|jpe?g|webp|gif));base64,(.+)$", re.S
)

app = Flask(__name__, static_folder=None)

# 认知主体全局单例：进程内共享，连接层线程安全
_agent: Optional[CognitiveAgent] = None
_agent_lock = threading.Lock()
_seed_lock = threading.Lock()
_persona_seed_checked = False

# 人格空间固定为 jiangyan（Web UI 的默认空间）
PERSONA_SPACE_ID = "jiangyan"


def _ensure_persona_seeded(db: Optional[Database] = None) -> None:
    """
    首次运行自动注入人格前史（幂等）。

    全新克隆 / 删除 memory.db 后，用户不需要先手工执行 --seed-persona：
    这里检查 jiangyan 空间是否存在，不存在就从 data/persona/seed.json 注入。
    不构造 CognitiveAgent，也不调用模型，所以即使还没配置 API Key 也能完成。

    :param db: 可选的数据库对象（复用当前线程连接）；缺省新建
    """
    global _persona_seed_checked
    with _seed_lock:
        if _persona_seed_checked:
            return
        database = db or Database(config.DB_PATH)
        exists = database.conn().execute(
            "select 1 from memory_space_meta where space_id = ?",
            (PERSONA_SPACE_ID,),
        ).fetchone()
        if not exists:
            from cognitive.persona.kernel import PersonaKernel
            from cognitive.persona.seed import seed_emotions, seed_persona
            from cognitive.persona.snapshot import PersonaSnapshotStore
            from cognitive.storage.kg_store import KGStore

            seed_persona(database, PERSONA_SPACE_ID, "江妍")
            seed_emotions(database, PERSONA_SPACE_ID)
            kernel = PersonaKernel(database, PERSONA_SPACE_ID)
            PersonaSnapshotStore(
                database, PERSONA_SPACE_ID, kernel, KGStore(database)
            ).load_current()
            print(f"[人格] 空库已自动注入前史：{PERSONA_SPACE_ID}")
        _persona_seed_checked = True


# ============================================================
# 模型连接设置（读写 + 应用）
# ============================================================
def _load_saved_settings() -> Dict[str, str]:
    """
    读取持久化的模型连接设置。

    :return: 设置字典（文件不存在返回空字典）
    """
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _effective_settings() -> Dict[str, str]:
    """
    合并出当前生效的模型连接设置（保存的设置优先，缺省回落 config / .env）。

    :return: {"provider", "base_url", "model", "api_key"}
    """
    saved = _load_saved_settings()
    return {
        "provider": str(saved.get("provider") or "deepseek").strip() or "deepseek",
        "base_url": str(
            saved.get("base_url") or config.DEEPSEEK_BASE_URL
        ).strip().rstrip("/"),
        "model": str(saved.get("model") or config.MODEL_NAME).strip(),
        "api_key": str(saved.get("api_key") or config.DEEPSEEK_API_KEY).strip(),
    }


def _mask_key(key: str) -> str:
    """
    密钥打码（界面上只展示掩码，不回传明文）。

    :param key: 明文密钥
    :return: 掩码字符串
    """
    if not key:
        return ""
    if len(key) <= 10:
        return "已设置"
    return f"{key[:5]}…{key[-4:]}"


def _settings_view(s: Dict[str, str]) -> Dict[str, Any]:
    """把生效设置转成对外的安全视图（密钥只给掩码与是否存在）。"""
    return {
        "provider": s["provider"],
        "base_url": s["base_url"],
        "model": s["model"],
        "api_key_set": bool(s.get("api_key")),
        "api_key_masked": _mask_key(s.get("api_key", "")),
    }


def _rebuild_agent(s: Dict[str, str]) -> None:
    """
    按给定设置重建认知主体（同一记忆库，只换模型客户端）。

    所有子组件（会话压缩 / 判决 / 归档 / 巩固）都从 CognitiveAgent 拿同一个
    客户端实例，重建 agent 即完成整体切换，无需逐个替换。

    :param s: 生效设置
    """
    global _agent
    # 同步到 config：配置校验（validate_config 要求密钥存在）与全局兜底都读这里
    config.DEEPSEEK_API_KEY = s["api_key"]
    config.DEEPSEEK_BASE_URL = s["base_url"]
    config.MODEL_NAME = s["model"]
    client = DeepSeekClient(
        api_key=s["api_key"], base_url=s["base_url"], model=s["model"]
    )
    with _agent_lock:
        _agent = CognitiveAgent(client=client)


def get_agent() -> CognitiveAgent:
    """
    懒加载认知主体单例（有保存的设置则按设置构造，否则走 .env 默认）。

    :return: CognitiveAgent
    """
    global _agent
    if _agent is not None:
        return _agent
    s = _effective_settings()
    client = None
    if s.get("api_key"):
        # 有自定义连接时按设置构造；密钥同步进 config 以便配置校验通过
        config.DEEPSEEK_API_KEY = s["api_key"]
        config.DEEPSEEK_BASE_URL = s["base_url"]
        config.MODEL_NAME = s["model"]
        client = DeepSeekClient(
            api_key=s["api_key"], base_url=s["base_url"], model=s["model"]
        )
    with _agent_lock:
        if _agent is None:
            _agent = CognitiveAgent(client=client)
    # 全新库：第一次真正用到 agent 时，自动注入人格前史（不需要模型）
    try:
        _ensure_persona_seeded(_agent.db)
    except Exception as exc:
        print(f"[人格] 自动注入前史失败：{type(exc).__name__}: {exc}")
    return _agent


def _ui_space() -> str:
    """
    Web UI 使用的记忆空间：优先有前史的人格空间，其次 default。

    界面不暴露空间概念（用户不该看到），由服务端一次性决定。

    :return: 空间 id
    """
    try:
        # /api/settings 可能在还没配置密钥时就被调用，因此这里不能构造 agent；
        # 直接用存储层完成首次注入，保证界面第一次打开就能选到 jiangyan。
        db = Database(config.DB_PATH)
        _ensure_persona_seeded(db)
        rows = db.conn().execute(
            "select space_id from memory_space_meta"
        ).fetchall()
        ids = {row["space_id"] for row in rows}
        if PERSONA_SPACE_ID in ids:
            return PERSONA_SPACE_ID
        if "default" in ids:
            return "default"
        return sorted(ids)[0] if ids else "default"
    except Exception:
        return "default"


def _fail(message: str, status: int = 400) -> Any:
    """
    统一错误响应。

    :param message: 错误说明
    :param status: HTTP 状态码
    """
    return jsonify({"ok": False, "error": message}), status


def _body() -> Dict[str, Any]:
    """
    解析请求体 JSON（空 body 返回空字典）。

    :return: 请求体字典
    """
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@app.after_request
def _add_cors(response):
    """宽松 CORS：这是本地开发 API，方便第三方开发者直接调用。"""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ============================================================
# 静态资源（Web UI）
# ============================================================
@app.get("/")
def index():
    """Web UI 首页。"""
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/<path:filename>")
def static_files(filename: str):
    """Web UI 静态资源（css/js）。"""
    if ".." in filename or filename.startswith("/"):
        return _fail("非法路径", 404)
    target = WEB_DIR / filename
    if not target.is_file():
        return _fail("资源不存在", 404)
    return send_from_directory(WEB_DIR, filename)


# ============================================================
# 设置（模型连接）
# ============================================================
@app.get("/api/settings")
def get_settings():
    """当前模型连接设置 + 预置供应商列表 + UI 使用的记忆空间。"""
    s = _effective_settings()
    return jsonify(
        {
            "ok": True,
            "settings": _settings_view(s),
            "providers": LLM_PROVIDERS,
            "space": _ui_space(),
        }
    )


@app.post("/api/settings")
def save_settings():
    """
    保存模型连接设置并立即生效（重建 agent，记忆库不变）。

    body: {"provider", "base_url", "model", "api_key"}
    api_key 留空 = 沿用已保存（或 .env）的密钥，不覆盖。
    """
    body = _body()
    cur = _effective_settings()

    provider = str(body.get("provider") or cur["provider"]).strip() or "deepseek"
    base_url = str(body.get("base_url") or "").strip().rstrip("/")
    model = str(body.get("model") or "").strip()
    api_key = str(body.get("api_key") or "").strip()

    if not base_url.startswith(("http://", "https://")):
        return _fail("接口地址必须以 http:// 或 https:// 开头")
    if not model:
        return _fail("请填写模型名称")
    key = api_key or cur.get("api_key", "")
    if not key:
        return _fail("请填写 API 密钥（可在所选服务商的官网获取）")

    s = {
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "api_key": key,
    }
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _rebuild_agent(s)
    except LLMError as exc:
        return _fail(f"设置无法应用：{exc}")
    except OSError as exc:
        return _fail(f"设置保存失败：{exc}")

    return jsonify({"ok": True, "settings": _settings_view(s)})


@app.post("/api/settings/test")
def test_settings():
    """
    试连一次模型（不保存设置）。

    body 可选：{"base_url", "model", "api_key"}，缺项沿用当前生效值。
    api_key 留空 = 沿用已保存密钥。
    """
    body = _body()
    cur = _effective_settings()
    base_url = (str(body.get("base_url") or "").strip() or cur["base_url"]).rstrip("/")
    model = str(body.get("model") or "").strip() or cur["model"]
    key = str(body.get("api_key") or "").strip() or cur.get("api_key", "")

    if not key:
        return jsonify({"ok": False, "error": "尚未配置 API 密钥"})

    try:
        # max_retry=0：测试要快速失败，不要走长退避
        client = DeepSeekClient(
            api_key=key, base_url=base_url, model=model, max_retry=0
        )
        started = time.time()
        reply = client.chat(
            [{"role": "user", "content": "请只回复两个字母：ok"}], max_tokens=64
        )
        latency = int((time.time() - started) * 1000)
        return jsonify(
            {
                "ok": True,
                "latency_ms": latency,
                "reply": (reply or "").strip()[:60],
            }
        )
    except Exception as exc:  # 网络错误 / 鉴权失败 / 模型名错误
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


# ============================================================
# 头像
# ============================================================
@app.get("/api/avatar/persona")
def persona_avatar():
    """江妍的头像（data/persona/avatar.jpg）。"""
    if not PERSONA_AVATAR_FILE.is_file():
        return _fail("头像不存在", 404)
    return send_file(PERSONA_AVATAR_FILE, mimetype="image/jpeg")


@app.get("/api/avatar/user")
def user_avatar():
    """用户自定义头像（未设置返回 404，界面回退到默认样式）。"""
    if not USER_AVATAR_FILE.is_file():
        return _fail("未设置头像", 404)
    try:
        data = json.loads(USER_AVATAR_FILE.read_text(encoding="utf-8"))
        raw = base64.b64decode(data.get("data", ""))
        return send_file(io.BytesIO(raw), mimetype=str(data.get("mime", "image/png")))
    except (OSError, ValueError, KeyError) as exc:
        return _fail(f"头像读取失败：{exc}", 500)


@app.post("/api/avatar/user")
def set_user_avatar():
    """
    上传（或清除）用户头像。

    body: {"data": "data:image/png;base64,..."}；data 为空串则清除头像。
    """
    data_url = str(_body().get("data") or "").strip()

    if not data_url:
        # 清除：恢复默认样式（GET 返回 404）
        try:
            USER_AVATAR_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return jsonify({"ok": True, "cleared": True})

    match = _DATA_URL_PATTERN.match(data_url)
    if not match:
        return _fail("头像格式不正确，请选择图片文件")
    mime, payload = match.group(1), match.group(2)
    try:
        raw = base64.b64decode(payload)
    except (ValueError, TypeError):
        return _fail("头像数据无法解析")
    if len(raw) > USER_AVATAR_MAX_BYTES:
        return _fail("头像图片不能超过 2MB")

    try:
        USER_AVATAR_FILE.parent.mkdir(parents=True, exist_ok=True)
        USER_AVATAR_FILE.write_text(
            json.dumps({"mime": mime, "data": base64.b64encode(raw).decode("ascii")}),
            encoding="utf-8",
        )
    except OSError as exc:
        return _fail(f"头像保存失败：{exc}")

    return jsonify({"ok": True, "cleared": False})


# ============================================================
# 基础信息
# ============================================================
@app.get("/api/health")
def health():
    """健康检查：配置可用、主体可构造。"""
    try:
        agent = get_agent()
    except Exception as exc:  # 配置缺失 / 密钥缺失等
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
    return jsonify(
        {
            "ok": True,
            "persona": config.PERSONA_NAME,
            "model": config.MODEL_NAME,
            "archive": config.ENABLE_ARCHIVE,
            "affection": config.AFFECTION_ENABLED,
        }
    )


@app.get("/api/spaces")
def list_spaces():
    """列出全部记忆空间。"""
    rows = get_agent().db.conn().execute(
        "select space_id, name, create_ts from memory_space_meta order by create_ts"
    ).fetchall()
    return jsonify(
        {
            "ok": True,
            "spaces": [
                {
                    "space_id": row["space_id"],
                    "name": row["name"] or row["space_id"],
                    "create_ts": int(row["create_ts"] or 0),
                }
                for row in rows
            ],
        }
    )


@app.post("/api/spaces")
def create_space():
    """注册记忆空间（幂等）。body: {space_id, name}"""
    body = _body()
    space_id = str(body.get("space_id") or "").strip()
    if not space_id:
        return _fail("缺少 space_id")
    name = str(body.get("name") or "").strip()
    get_agent().ensure_space(space_id, name)
    return jsonify({"ok": True, "space_id": space_id})


# ============================================================
# 核心收发接口
# ============================================================
@app.post("/api/chat")
def chat():
    """
    发送一条用户消息，返回 AI 回答。

    body: {"space_id": str, "session_id": str, "message": str}
    query: ?debug=1 附带完整链路调试信息

    return: {"ok", "answer", "answer_parts", "session_id",
             "proactive_messages"[, "debug"]}
    answer_parts 是按分隔符/空行切好的多条消息（与入库一致），UI 直接按它渲染气泡
    """
    body = _body()
    space_id = str(body.get("space_id") or "default").strip() or "default"
    session_id = str(body.get("session_id") or "").strip()
    message = str(body.get("message") or "").strip()

    if not session_id:
        return _fail("缺少 session_id")
    if not message:
        return _fail("缺少 message（正文不能为空）")

    try:
        result = get_agent().chat(message, session_id=session_id, space_id=space_id)
    except Exception as exc:
        return _fail(f"对话处理失败：{type(exc).__name__}: {exc}", 500)

    payload = {
        "ok": True,
        "answer": result.get("answer", ""),
        "answer_parts": result.get("answer_parts", []),
        "session_id": session_id,
        "proactive_messages": result.get("proactive_messages", []),
    }
    if request.args.get("debug") in ("1", "true", "True"):
        payload["debug"] = result.get("debug", {})
    return jsonify(payload)


# ============================================================
# 会话
# ============================================================
@app.get("/api/sessions")
def list_sessions():
    """列出某空间的会话（含消息数与最近一条预览）。"""
    space_id = request.args.get("space_id") or "default"
    agent = get_agent()
    agent.ensure_space(space_id)

    rows = agent.db.conn().execute(
        """
        select session_id, payload, update_ts
        from session_store
        where space_id = ?
        order by update_ts desc
        limit 200
        """,
        (space_id,),
    ).fetchall()

    sessions = []
    for row in rows:
        try:
            data = json.loads(row["payload"] or "{}")
            messages = data.get("messages", [])
            last_user = next(
                (
                    m.get("content", "")
                    for m in reversed(messages)
                    if m.get("role") == "user"
                ),
                "",
            )
            last_reply = next(
                (
                    m.get("content", "")
                    for m in reversed(messages)
                    if m.get("role") == "assistant"
                ),
                "",
            )
            preview = (last_reply or last_user)[:60]
        except (ValueError, TypeError):
            messages, preview = [], ""
        sessions.append(
            {
                "session_id": row["session_id"],
                "space_id": space_id,
                "messages": len(messages),
                "preview": preview,
                "update_ts": int(row["update_ts"] or 0),
            }
        )
    return jsonify({"ok": True, "sessions": sessions})


@app.get("/api/sessions/<path:session_id>/messages")
def session_messages(session_id: str):
    """取某会话的完整消息历史（UI 恢复用）。"""
    space_id = request.args.get("space_id") or "default"
    agent = get_agent()
    agent.ensure_space(space_id)

    row = agent.db.conn().execute(
        "select payload from session_store where session_id = ? and space_id = ?",
        (session_id, space_id),
    ).fetchone()
    if row is None:
        return _fail("会话不存在", 404)

    try:
        data = json.loads(row["payload"] or "{}")
    except (ValueError, TypeError):
        data = {}
    return jsonify(
        {
            "ok": True,
            "session_id": session_id,
            "space_id": space_id,
            "compressed_summary": data.get("compressed_summary", ""),
            "messages": data.get("messages", []),
        }
    )


# ============================================================
# 记忆 / 人格观测
# ============================================================
@app.get("/api/memory/overview")
def memory_overview():
    """记忆库总览（KG/主题/向量/日志计数）。"""
    space_id = request.args.get("space_id") or "default"
    return jsonify({"ok": True, **get_agent().memory_overview(space_id)})


@app.get("/api/relations")
def relations():
    """信念（KG 关系）列表。query: ?all=1 含被推翻历史行"""
    space_id = request.args.get("space_id") or "default"
    limit = int(request.args.get("limit") or 200)
    agent = get_agent()
    agent.ensure_space(space_id)
    return jsonify(
        {
            "ok": True,
            "relations": KGStore(agent.db).list_relations(
                space_id,
                only_active=request.args.get("all") not in ("1", "true"),
                limit=limit,
            ),
        }
    )


@app.get("/api/topics")
def topics():
    """主题（往事）列表。"""
    space_id = request.args.get("space_id") or "default"
    limit = int(request.args.get("limit") or 50)
    agent = get_agent()
    agent.ensure_space(space_id)
    items = TopicStore(agent.db).list_topics(space_id, limit=limit)
    return jsonify(
        {
            "ok": True,
            "topics": [item.to_dict() for item in items],
        }
    )


@app.get("/api/persona")
def persona():
    """人格状态（参数 / 基线 / 情景 / 核心信念）。"""
    space_id = request.args.get("space_id") or "default"
    return jsonify({"ok": True, **get_agent().persona_state(space_id)})


@app.get("/api/persona/history")
def persona_history():
    """人格变迁史文本。"""
    space_id = request.args.get("space_id") or "default"
    limit = int(request.args.get("limit") or 20)
    return jsonify(
        {
            "ok": True,
            "history": get_agent().persona_history(space_id, limit=limit),
        }
    )


# ============================================================
# 好感度与主动消息
# ============================================================
@app.get("/api/affection")
def affection():
    """好感度列表（按好感度升序，关系最紧张的排前面）。"""
    space_id = request.args.get("space_id") or "default"
    return jsonify({"ok": True, "affection": get_agent().affection_state(space_id)})


@app.get("/api/messages/outbox")
def outbox():
    """主动消息发件箱。query: ?session_id=&pending=0"""
    space_id = request.args.get("space_id") or "default"
    session_id = request.args.get("session_id") or None
    only_pending = request.args.get("pending") not in ("0", "false")
    limit = int(request.args.get("limit") or 50)
    return jsonify(
        {
            "ok": True,
            "messages": get_agent().proactive_outbox(
                space_id,
                session_id=session_id,
                only_pending=only_pending,
                limit=limit,
            ),
        }
    )


@app.post("/api/messages/outbox/ack")
def outbox_ack():
    """确认主动消息（body: {space_id, msg_id}）。"""
    body = _body()
    space_id = str(body.get("space_id") or "default").strip() or "default"
    msg_id = str(body.get("msg_id") or "").strip()
    if not msg_id:
        return _fail("缺少 msg_id")
    ok = get_agent().ack_proactive_message(space_id, msg_id)
    if not ok:
        return _fail("消息不存在或已确认", 404)
    return jsonify({"ok": True, "msg_id": msg_id})


# ============================================================
# 主动联系时间表（模型决定下次什么时候来）
# ============================================================
@app.get("/api/proactive/schedule")
def proactive_schedule():
    """当前主动联系时间表（未排期返回 null）。"""
    space_id = request.args.get("space_id") or _ui_space()
    session_id = request.args.get("session_id") or config.PROACTIVE_SESSION_ID
    return jsonify(
        {
            "ok": True,
            "schedule": get_agent().proactive_schedule_state(space_id, session_id),
        }
    )


@app.post("/api/proactive/tick")
def proactive_tick():
    """
    立即检查/触发一次主动联系。

    body: {"space_id"?, "session_id"?, "force"?}
    force=true 跳过时间表直接发一条（调试用；发完会重新排期）。
    """
    body = _body()
    space_id = str(body.get("space_id") or _ui_space()).strip() or _ui_space()
    session_id = (
        str(body.get("session_id") or config.PROACTIVE_SESSION_ID).strip()
        or config.PROACTIVE_SESSION_ID
    )
    force = bool(body.get("force"))
    return jsonify(get_agent().proactive_tick(space_id, session_id, force=force))


# ============================================================
# 后台调度
# ============================================================
_scheduler_lock = threading.Lock()
_scheduler_started = False


def _proactive_loop() -> None:
    """后台循环：定期扫描全部到期时间表，到点就让她主动发一条。"""
    # 给 Web 服务一点启动时间，避免第一个请求还没进来就抢模型
    time.sleep(3)
    while True:
        try:
            if config.PROACTIVE_ENABLED:
                agent = get_agent()
                # 不只服务 Web UI：开发者用 /api/chat 产生的排期同样会到点触发
                due = ProactiveScheduleStore(agent.db).list_due(limit=10)
                for row in due:
                    report = agent.proactive_tick(
                        row["space_id"], row["session_id"]
                    )
                    if report.get("sent"):
                        content = (report.get("message") or {}).get("content", "")
                        print(f"[主动消息] {content[:60]}")
        except Exception as exc:  # 调度失败不能拖垮 Web 服务
            print(f"[主动消息] 调度失败：{type(exc).__name__}: {exc}")
        time.sleep(max(5, int(config.PROACTIVE_TICK_SECONDS)))


def _start_proactive_scheduler() -> None:
    """启动后台调度线程（幂等；PROACTIVE_ENABLED=false 时不启动）。"""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started or not config.PROACTIVE_ENABLED:
            return
        _scheduler_started = True
    threading.Thread(
        target=_proactive_loop, name="proactive-scheduler", daemon=True
    ).start()
    print("[主动消息] 后台调度已启动")


# ============================================================
# 启动
# ============================================================
def main() -> None:
    """启动开发服务器。"""
    host = config.API_HOST
    port = int(config.API_PORT)
    print(f"认知主体 API 启动：http://{host}:{port}")
    print(f"Web UI：http://{host}:{port}/")
    try:
        _ensure_persona_seeded()
    except Exception as exc:
        print(f"[人格] 自动注入前史失败：{type(exc).__name__}: {exc}")
    _start_proactive_scheduler()
    # threaded=True：Werkzeug 每请求一线程；DB 连接层已按线程隔离
    app.run(host=host, port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
