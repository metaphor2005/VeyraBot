# VeyraBot
Cognitive Agent
A continuously running cognitive agent with layered memory, active archiving, and symbolic persona evolution, implemented in pure Python + SQLite. It works with DeepSeek or any OpenAI-compatible chat-completions endpoint.

This is not "a chatbot with a vector database bolted on". Every turn actively archives topics, beliefs, experiences, and relationship temperature; the persona is driven by 14 symbolic parameters and evolves through a baseline + within-day drift + daily settlement loop; retrieval runs only when the judge decides existing material is insufficient, inside a bounded loop. It also supports multi-valued beliefs, affection-threshold events, and proactive outreach whose timing is decided by the LLM after each reply.

Current persona: Jiang Yan (江妍, nickname "Little Apple") — an 18-year-old freshman at Northeast Electric Power University, from Jishou, Xiangxi, Hunan. She cosplays (mainly Arknights; her favorites are Ch'en and Exusiai), loves A Silent Voice, and cannot eat anything without chili. She is gentle and polite, but when someone is dismissive or makes decisions for her, she goes quiet and becomes more specific.

The agent speaks Chinese by design. This English README is for developers and maintainers; it does not change the persona's language.

中文

Current state (2026-11-11)
The production memory database has been reset to persona-only. All test, demo, and debugging sessions and their derived memories have been removed.

Item	Current value
Memory spaces	jiangyan only (display name "江妍")
Seed topics	7 (6 life stories + 1 character profile)
Structured beliefs	24 (all active)
Entities	13
Experience fragments	25 (including emotional residue for each story)
Persona version	v0 (all 14 parameters equal PERSONA_KERNEL_INIT)
Sessions	0 (the Web UI starts with an empty chat)
Vector index	7 documents (seed topics only)
Smoke tests	25 / 25 passing
A full backup of data/ before the reset is kept outside this repository, so test data and personal runtime data are not published to GitHub.

1. What it does
Layered memory: 50 turns of short-term context → topic memories → structured beliefs → persona kernel → self-narrative → RAG fallback.
Multi-valued beliefs: the extraction model decides whether a relation dimension allows multiple values. Roommates, friends, and favorite works can coexist; birthday, hometown, and primary language are single-valued, so a new value supersedes the old one.
Evolving persona: a long-term baseline + within-day drift + daily settlement. The baseline moves at most 0.01 per day; about six days of consistent pressure are needed for a new version.
Affection and relationship events: every person has a 0–100 affinity value. Crossing the cold threshold triggers an outreach message; crossing the severance threshold triggers a breakup message; recovering past the reconcile threshold triggers a reconciliation message.
Daily initiative: after every reply, the LLM decides when to reach out next. If the user stays silent until then, she sends a message drawn from her memory; if the user speaks first, that schedule is cancelled and regenerated.
Multi-bubble replies: separators and blank lines are split into multiple assistant messages before persistence, so reloads still show separate bubbles.
Developer HTTP API + Web UI: send/receive messages, inspect memory/persona/affection, fetch proactive messages; the Web UI is a WeChat-desktop-style single-contact interface.
2. Quick start
2.1 Configuration
Copy .env.example to .env:

DEEPSEEK_API_KEY=sk-xxxxxxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-flash
LLM_MODE=api
.env is ignored by Git. Never commit real keys.

2.2 Install
pip install -r requirements.txt
CLI / demo: requests
API / Web UI: also flask
2.3 First run: seed the persona
The release folder ships with a clean database containing only the persona pre-history and no chat records. For a fresh clone, or if you deleted data/memory.db, the persona pre-history is injected automatically on first startup from data/persona/seed.json — no extra step is required.

You can also run the command manually (idempotent; useful after a reset):

python maintenance.py --seed-persona --space jiangyan
2.4 Start the API and Web UI
python api/server.py
Open http://127.0.0.1:8000/.

Only one contact: Jiang Yan. No space/session/model jargon is exposed.
The settings window contains dark mode, provider/model switching, connection test, and custom avatar upload.
A background scheduler starts with the server: when a proactive schedule is due, she recalls a random memory and sends a message.
2.5 CLI and demo
python run.py --space jiangyan --session chat1 --debug
python demo.py --fresh --with-past     # inject the pre-history, then run 6 turns (writes to DB)

CLI commands: /state, /history, /memory, /debug, /exit.

2.6 Maintenance
python maintenance.py --stats                    # memory size report
python maintenance.py --daily                    # daily consolidation (scheduled task)
python maintenance.py --daily --space jiangyan   # one space only
python maintenance.py --logs                     # recent consolidation logs
python maintenance.py --prune-snapshots          # keep the newest 30 snapshots
python maintenance.py --seed-persona --space jiangyan  # re-inject pre-history (idempotent)
python maintenance.py --persona-info             # show persona assets + runtime data paths

Register a Windows scheduled task (daily at 23:50):

.\install_schedule.ps1
.\install_schedule.ps1 -Hour 22 -Minute 30
.\install_schedule.ps1 -Remove
2.7 Tests
python tests/smoke_test.py
Expected: 25 / 25 passing, with no real model calls. Coverage includes storage, belief conflicts and multi-valued relations, vector retrieval, persona evolution, daily consolidation, big-event detection, the affection state machine, the proactive outbox, the proactive schedule, memory sampling, and the persona data directory.

2.8 Data and upgrades
All persona and runtime data live under one directory:

Path	Contents	Upgrade behavior
data/persona/	The agent's original data: profile, baseline parameters, pre-history, avatar	Keep
data/corpus/	Local RAG corpus	Keep
data/memory.db	User/bot-generated memory: sessions, topics, beliefs, experiences, persona state, affection, proactive messages	Keep or reset
data/vectors/	Topic vector index	Keep (rebuildable)
data/persona_snapshots/	Evolved persona snapshots	Keep
data/ui_settings.json / data/user_avatar.json	Web UI settings and user avatar	Keep or reset
Upgrade flow: back up data/ → replace code without overwriting data/persona/ → python maintenance.py --persona-info → python tests/smoke_test.py → start.

To clear the chat while keeping the persona: stop the server → delete memory.db, vectors/, and persona_snapshots/ → python maintenance.py --seed-persona --space jiangyan → start.

Full details: data/README.md.

3. Conversation pipeline
User input
   │
   ├─ 0. Cancel any pending proactive schedule (one schedule may only be used once)
   ├─ 1. Load the session (memory + SQLite dual-write; target: 50 turns of raw text)
   ├─ 2. Append the user message (persisted immediately)
   ├─ 3. Token estimate → emergency summarization only above 30,000 tokens
   ├─ 4. Load persona snapshot + symbolic kernel → self-view and tendencies
   ├─ 5. Memory routing: exact KG query + overturned beliefs + topic recall
   ├─ 6. Self-inquiry enhancement: inject persona change history when asked "why did you change"
   ├─ 7. Bounded retrieval loop (up to RAG_LOOP_MAX_ROUNDS)
   ├─ 7b. Relationship temperature: people who are far from baseline or in an abnormal state
   ├─ 8. Build the prompt (profile + self-view + memory + recent turns) → generate the reply
   ├─ 9. Append assistant messages (split on separators/blank lines, then persisted one by one)
   ├─ 10. Active archiving (runs every turn)
   │       ├─ topic → topic table + vector index
   │       ├─ beliefs → KG (conflicts / multi-value coexistence / reinforcement)
   │       ├─ experiences → experience table
   │       ├─ affection deltas → affection table + threshold messages
   │       ├─ within-day persona drift → current state only, accumulate pending
   │       ├─ self-view update → live layer (no new version)
   │       └─ big-event detection → high importance + involves her, or long-term directive phrases
   ├─ 10b. Event-level consolidation for big events (does not wait for daily settlement)
   └─ 11. Let the LLM decide the next proactive contact time → proactive_schedule
The key ordering difference: classic RAG retrieves first. Here, persona and beliefs come first; retrieval is optional and bounded.

Daily consolidation is driven by maintenance.py --daily, which merges all un-consolidated kind=normal topics from the day into one kind='daily' narrative.

4. Six layers of memory and persona
Layer 1 — Short-term working memory (cognitive/session.py)
SessionContext: in-memory dict + SQLite session_store, dual-written on every change.
Target capacity: 50 turns (KEEP_RECENT_ROUNDS=50). Turn count alone never triggers compression; only when the estimated token count exceeds MAX_SAFE_TOKENS=30000 are older parts summarized.
The number of history turns injected into the answer prompt is controlled separately by HISTORY_INJECT_ROUNDS.
Purpose: emergency protection, not long-term memory. Real consolidation happens during archiving and daily settlement.
Layer 2 — Topic-indexed memory (topic_store.py + vector_store.py)
The recall unit is an event (Topic), not a text chunk. Recalling a topic also brings back its experience fragments.
Vectors index only topic summaries, not raw dialogue fragments.
Retrieval is local TF-IDF with character 2-grams:
synonym expansion (31 groups) handles paraphrases;
keyword hits get a boost and bypass min_score, which helps single-character queries such as "cat" or "mom".
No embedding service is required; fully offline and explainable.
Layer 3 — Structured beliefs (kg_store.py)
A lightweight property graph in SQLite:

Table	Key fields	Purpose
kg_entity	eid / etype / name / attrs / is_active	entities
kg_relation	head_name / rel / tail_name / confidence / importance / multi_valued / is_active	relations and belief weight
Three code-level rules (independent of the model):

Conflict invalidation: a new tail for the same (head, rel) deactivates the old relation and inserts the new one.
Multi-value coexistence: dimensions marked multi_valued=1 (roommates, friends, favorite works, games played) keep old and new values; single-valued dimensions (birthday, hometown, primary language) still replace. If either side is multi-valued, values coexist; only when both sides are single-valued does the new value supersede.
Reinforcement: reconfirming the same belief increases confidence by 0.05 and importance by 0.03.
The memory block also shows overturned beliefs:

【Overturned beliefs (old values, for contrast only; not current conclusions)】
- Jiang Yan's attitude toward cosplay: just a hobby
  (old value, replaced on 2026-11-06 by "take it seriously, it is not a casual hobby")
She can therefore see both what she currently believes and what she has changed her mind about.

Layer 4 — Symbolic persona kernel (persona/kernel.py) and identity profile
The persona is numbers, not prose. Fourteen float parameters in three groups:

Cognitive style: prefer_decompose / prefer_intuition / prefer_skepticism
                 memory_sensitivity / self_reflect_threshold
Values:          truth_priority / harmony_priority / curiosity_priority
                 consistency_priority / topic_inertia / novelty_bias
Motives:         goal_self_understanding / goal_task_accuracy / goal_coherence
Each parameter stores three values:

Field	Meaning	Who changes it
base_value	long-term persona baseline	only during daily settlement, slowly
value	current state (her today)	every turn, within baseline ± PERSONA_DRIFT_LIMIT (0.15)
pending_value	accumulated raw pressure for the day	every turn; cleared during settlement
Parameters are translated into three-level tendency text by describe() (strong / medium / inverse) and injected into the prompt. They do not gate code branches. Mood (persona_mood) decays by real elapsed time (half-life about 35 hours) and per turn.

Identity profile lives in data/persona/profile.json (loaded as config.PERSONA_PROFILE): 7 groups, 39 entries (identity / three layers of personality / daily life / relationships / preferences and triggers / abilities and weaknesses / current life). It is the baseline and is not rewritten by dialogue.

Pre-history lives in data/persona/seed.json and is written into the real storage layer by cognitive/persona/seed.py: 6 life stories + 1 character profile + 24 relations + 13 entities + 25 experience fragments (including emotional residue). These are retrievable memories, not text pasted into the prompt.

Layer 5 — Narrative identity (persona/snapshot.py + changelog.py)
One snapshot per version: data/persona_snapshots/{space}/vNNN_*.json. A new file is created only when cumulative change crosses PERSONA_SUBSTANTIVE_DELTA=0.06.
Only the current version is loaded at inference time; old versions are kept for provenance.
persona_changelog is append-only: persona_field, old_value, new_value, reason, topic_id, snapshot version jumps.
Overwriting a core belief also writes a changelog entry, with two thresholds: high importance regardless of who is involved, or involving her with a lower second threshold.
This is the physical basis for answering "why did you change?".

Layer 6 — RAG fallback (cognition/rag.py)
Not front-loaded. It runs only when the judge decides existing material is insufficient.
By default it searches only the local data/corpus/ directory; RAGRegistry can register external providers.
Retrieval is a bounded loop: at most RAG_LOOP_MAX_ROUNDS=3; zero hits, repeated queries, or no new material stop it early; the final round is forced to produce a conclusion.
Long-term cadence
Cadence	Trigger	Output
Every turn	end of a turn	topic(normal) + KG + experiences + within-day persona drift
Daily	scheduled task at 23:50	topic(daily) + consolidation log + baseline promotion
Big event	high importance and involves her, or directive phrases like "from today on"	topic(event) consolidated immediately
Major change	baseline change ≥ 0.06 after daily settlement	new snapshot + changelog + 【Persona】baseline vN memory
5. Persona evolution: baseline + drift + daily settlement
The early version allowed a new persona version every turn: six turns produced v1 and v2. That looked less like a person and more like random drift. The current design has three layers:

baseline (long-term anchor; only daily settlement moves it, max ±0.01/day)
      ↕ drift band ±0.15
current state (her today; moves every turn but cannot leave the band)
      ↓ daily settlement
pending (sum of raw deltas for the day; converted and reset at settlement)
50 turns in one day: no new version, only drift within ±0.15.
Six consecutive days pushing the same parameter: cumulative 0.06 → v1.
A day with no pressure: baseline unchanged, no changelog, no version.
Three days away: mood decays by real time, so she comes back in the present mood.
Causality: the kernel changes first, then the narrative is generated. The model does not invent a self-description and treat it as a persona change.

6. Relationship temperature and proactive messages
6.1 Affection state machine
Each person's affinity is 0–100, default 50. It is driven by affection_deltas from archiving; a single turn is capped at 15, so a breakup requires repeated negative interactions.

normal  (>= 30)
  ↓ drops below 30
cold    (12 ~ 30)   → reach_out (proactive check-in / probe)
  ↓ drops below 12
broken  (< 12)      → severance (breakup message)
  ↑ recovers >= 60
normal              → reconcile (reconciliation message)
cold → normal recovery is silent; no message is sent, to avoid spamming.
After a breakup, affinity can still change; the reconcile line (60) is higher than the cold line (30), so simply "not being cold" does not automatically turn the page.
Relationship temperature is injected into the answer prompt as text (never as a number) and affects tone.
These messages are written to the proactive_message outbox and fetched by the API/UI. Types are reach_out, severance, and reconcile; the UI shows them as centered system notices.
6.2 Daily initiative (the LLM decides when to come back)
Affection threshold events are relationship events and must be sent. Daily initiative is a separate mechanism:

After every reply
   └─ the model reads recent turns + current time
        └─ outputs {"delay_minutes": int, "reason": "..."}
             └─ writes proactive_schedule (one pending row per space + session)

Background scheduler (scans every 30 s)
   └─ when due → sample a random memory (topic / experience / high-importance belief)
        └─ compose → append to session history + outbox → schedule the next one

User speaks first
   └─ at the start of the turn, mark the pending schedule cancelled
        └─ after the reply, schedule a new one (the old one is permanently void)
Four hard guarantees:

Guarantee	Implementation
The interval is model-decided	ProactiveInitiator.decide_delay_minutes(); clamped to PROACTIVE_SCHEDULE_MIN_MINUTES ~ PROACTIVE_SCHEDULE_MAX_MINUTES (default 10 min – 24 h)
One schedule may only be used once	claim() matches status='pending' and the exact due_ts; if the user speaks while the message is being composed, the old due_ts no longer matches and the generated message is discarded
Only fire when due	The background loop only processes due_ts <= now; proactive_tick() returns not_due otherwise
No flooding	Generation pauses when unread initiative messages reach PROACTIVE_MAX_PENDING (default 3)
If the model is unavailable (network failure / mock mode), the fallback is a random interval between PROACTIVE_FALLBACK_MIN_MINUTES and PROACTIVE_FALLBACK_MAX_MINUTES (default 45–180 min), and the message is composed from a memory fragment via a template. A failed model call never swallows the proactive contact.

Initiative messages are stored as initiative:

appended to the current session as an assistant message → survives a reload as a normal bubble;
also written to the outbox → the open page receives it via polling;
deduplicated during history loading, so it never appears twice.
Endpoints:

GET  /api/proactive/schedule?space_id=&session_id=   # current schedule
POST /api/proactive/tick                             # check now; force=true sends immediately
The scheduler runs inside python api/server.py. Under gunicorn/uwsgi or another WSGI server, run an external timer that calls POST /api/proactive/tick.

7. Reply splitting: split before persistence
She can signal "several consecutive messages" with separators or blank lines:

Good night. After all these tests I don't feel much, just a bit sleepy —
the calculus chapter is still open on my desk. 

---

See you tomorrow. If you still want to talk, just say what you want;
no need to test me again.
The server calls split_reply() before persistence:

Split on a standalone separator line (---, ——, ***, ===, …);
Split again on runs of blank lines;
Keep at most 5 bubbles; merge the rest into the last one.
Each part is then appended as a separate assistant message. /api/chat returns answer_parts, so the Web UI renders the bubbles directly; third-party clients that reload history also get separate messages instead of one merged block.

8. Developer HTTP API
Start with python api/server.py. Defaults to 127.0.0.1:8000, no auth. To expose it, change API_HOST=0.0.0.0 and put your own gateway in front.

Method	Path	Description
GET	/api/health	Health check (model / archive / affection flags)
POST	/api/chat	Send a message; returns answer, answer_parts, proactive_messages; ?debug=1 adds pipeline details
GET	/api/sessions	Session list
GET	/api/sessions/<session_id>/messages	Full message history for a session
GET	/api/memory/overview	Memory overview (beliefs / topics / vectors / changelog counts)
GET	/api/relations	Belief list (?all=1 includes overturned history)
GET	/api/topics	Topic (memory) list
GET	/api/persona	Persona state (parameters / baseline / mood / core beliefs)
GET	/api/persona/history	Persona change history text
GET	/api/affection	Affection list
GET	/api/messages/outbox	Proactive outbox (?pending=1 for unacknowledged only)
POST	/api/messages/outbox/ack	Acknowledge a proactive message {space_id, msg_id}
GET	/api/proactive/schedule	Next proactive contact schedule
POST	/api/proactive/tick	Check/trigger proactive contact (force=true sends now)
GET	/api/settings	Current model connection + preset providers + UI space
POST	/api/settings	Save model connection (provider / base URL / key / model)
POST	/api/settings/test	Test a connection with the given or current settings
GET	/api/avatar/persona	Jiang Yan's avatar (data/persona/avatar.jpg)
GET/POST	/api/avatar/user	Read/upload the user avatar (data URL; empty clears)
GET/POST	/api/spaces	List / register memory spaces
Example:

import requests

r = requests.post(
    "http://127.0.0.1:8000/api/chat",
    json={
        "space_id": "jiangyan",
        "session_id": "debug-1",
        "message": "Do you remember who I cosplayed the first time?",
    },
    timeout=120,
)
data = r.json()
print(data["answer_parts"])        # multiple bubbles
print(data["proactive_messages"])  # affinity threshold events, if any
If space_id is omitted, /api/chat uses default. The Web UI automatically uses the persona space jiangyan.

9. Web UI (WeChat desktop style)
Only one contact on the left: Jiang Yan. No "new chat" button.
WeChat-style bubbles: her messages on the left, yours on the right; avatars and bubble tails; dark mode.
Replies are rendered part-by-part from answer_parts with a short delay, not as one large block.
Proactive messages:
initiative (daily "I thought of you") → a normal assistant bubble;
reach_out / severance / reconcile (relationship events) → a centered system notice.
Settings window: provider / model / base URL / key / connection test / dark mode / custom avatar.
Jiang Yan's avatar comes from data/persona/avatar.jpg; the user's uploaded avatar is stored in data/user_avatar.json.
Asset version query strings (style.css?v=, app.js?v=) force cache refresh after updates.
10. Directory layout
cognitive-agent/
├── README.md                       # English (GitHub default)
├── README.cn.md                    # Chinese
├── config.py                       # all tunable parameters (single config entry point)
├── run.py                          # interactive CLI
├── demo.py                         # 6-turn full-pipeline demo
├── maintenance.py                  # daily settlement / stats / snapshot pruning / seeding / --persona-info
├── install_schedule.ps1            # register the Windows scheduled task
├── requirements.txt                # requests + flask
├── .env / .env.example             # secrets (.env is not committed)
├── api/server.py                   # HTTP API + Web UI hosting + proactive scheduler
├── web/                            # index.html / style.css / app.js
├── data/                           # ★ single root for persona and runtime data
│   ├── README.md                   # layout / upgrade / reset instructions
│   ├── corpus/                     # original knowledge: local RAG corpus (.md / .txt)
│   ├── persona/                    # ★ the agent's original data (keep on upgrade)
│   │   ├── meta.json               # schema version / name / avatar filename
│   │   ├── profile.json            # identity profile (7 groups, 39 entries)
│   │   ├── kernel.json             # baseline persona parameters + descriptions
│   │   ├── seed.json               # pre-history: entities / relations / stories / profile topic
│   │   └── avatar.jpg              # Jiang Yan's avatar
│   ├── memory.db                   # SQLite: KG / topics / experiences / persona / sessions / affection / proactive messages
│   ├── vectors/jiangyan.json       # topic vector index
│   └── persona_snapshots/jiangyan/ # persona snapshots (currently v000_init.json)
├── cognitive/
│   ├── env.py                      # .env loader
│   ├── models.py                   # data models
│   ├── tokenizer.py                # token estimate
│   ├── llm.py                      # OpenAI-compatible client (truncation retry / JSON repair)
│   ├── session.py                  # short-term memory + emergency compression
│   ├── engine.py                   # main pipeline (including the proactive schedule)
│   ├── proactive.py                # memory sampling + composition + delay decision
│   ├── storage/
│   │   ├── database.py             # connections, schema, incremental column migrations
│   │   ├── kg_store.py             # structured beliefs + conflict / multi-value / reinforcement
│   │   ├── topic_store.py          # topics and experiences
│   │   ├── vector_store.py         # TF-IDF retrieval (expansion + keyword bypass)
│   │   ├── affection_store.py      # affection + outbox + proactive schedule
│   │   └── synonyms.py             # synonym table
│   ├── persona/
│   │   ├── assets.py               # loading + validation for data/persona/
│   │   ├── kernel.py               # symbolic persona kernel
│   │   ├── seed.py                 # writes data/persona/seed.json into storage
│   │   ├── snapshot.py             # snapshot file management
│   │   └── changelog.py            # change log
│   ├── cognition/
│   │   ├── prompts.py              # all prompts
│   │   ├── extractors.py           # extractor + narrative generator
│   │   ├── router.py               # memory router
│   │   ├── judge.py                # information sufficiency judge
│   │   ├── archiver.py             # archiving (including affection / proactive messages)
│   │   ├── consolidator.py         # daily consolidation + event consolidation
│   │   └── rag.py                  # RAG fallback
│   └── tools/base.py               # tool plugin layer (interfaces only)
├── docs/                           # design reviews and specs
└── tests/smoke_test.py             # 25 offline smoke tests
11. Key configuration
All settings live in config.py and can be overridden via environment variables.

Memory and retrieval
Parameter	Default	Description
MAX_SAFE_TOKENS	30000	threshold for emergency summarization
KEEP_RECENT_ROUNDS	50	raw turns kept during compression
HISTORY_INJECT_ROUNDS	50	history turns injected into the answer prompt
TOPIC_TOP_K	3	number of topics recalled
VECTOR_EXPAND_ENABLED	true	synonym expansion toggle
VECTOR_EXPAND_WEIGHT	0.55	weight of expanded terms
VECTOR_KEYWORD_BOOST	0.06	keyword hit boost (also bypasses min_score)
RAG_LOOP_MAX_ROUNDS	3	max judge→retrieve rounds per turn
RAG_LOOP_DUP_RATIO	0.65	query-repetition threshold
RAG_LOOP_MAX_SNIPPETS	6	max accumulated snippets
Persona
Parameter	Default	Description
PERSONA_DATA_DIR	data/persona	original persona data directory
PERSONA_PROFILE	data/persona/profile.json	7 groups, 39 identity entries (loaded at startup)
PERSONA_DRIFT_LIMIT	0.15	within-day drift band
PERSONA_DAILY_MAX_STEP	0.01	max baseline movement per parameter per day
PERSONA_DAILY_LEARNING_RATE	0.35	conversion rate from daily net drift to baseline movement
PERSONA_SUBSTANTIVE_DELTA	0.06	cumulative threshold for a new persona version
PERSONA_DAILY_SETTLE_ENABLED	true	enable daily settlement
PERSONA_DESC_STRONG / MID / WEAK	0.72 / 0.45 / 0.28	three-level tendency-description thresholds
PERSONA_BELIEF_LOG_IMPORTANCE	0.90	high threshold for logging a belief overwrite
PERSONA_BELIEF_LOG_SELF_IMPORTANCE	0.60	second threshold when she is involved
Affection and proactive contact
Parameter	Default	Description
AFFECTION_ENABLED	true	affection toggle
AFFECTION_DEFAULT	50	neutral baseline
AFFECTION_MAX_DELTA_PER_TURN	15	max affection change per turn
AFFECTION_COLD_THRESHOLD	30	cold line (triggers reach_out)
AFFECTION_SEVER_THRESHOLD	12	severance line (triggers severance)
AFFECTION_RECONCILE_THRESHOLD	60	reconcile line (triggers reconcile)
PROACTIVE_ENABLED	true	daily proactive contact toggle
PROACTIVE_SCHEDULE_MIN_MINUTES / PROACTIVE_SCHEDULE_MAX_MINUTES	10 / 1440	clamp range for model-chosen intervals
PROACTIVE_FALLBACK_MIN_MINUTES / PROACTIVE_FALLBACK_MAX_MINUTES	45 / 180	random fallback range when the model is unavailable
PROACTIVE_TICK_SECONDS	30	background scan interval
PROACTIVE_MAX_PENDING	3	unread initiative backlog limit
PROACTIVE_SESSION_ID	main	fixed Web UI session
Service and archiving
Parameter	Default	Description
API_HOST / API_PORT	127.0.0.1 / 8000	HTTP listener
ENABLE_ARCHIVE	true	per-turn active archiving
ARCHIVE_SYNC	true	run archiving synchronously
BIG_EVENT_IMPORTANCE	0.90	importance threshold for big-event detection
BIG_EVENT_REQUIRE_SELF	true	require that the event involves her
DAILY_CONSOLIDATE_ENABLED	true	daily consolidation toggle
CONSOLIDATE_MIN_TOPICS	2	minimum topics for a daily consolidation
CONSOLIDATE_MAX_TOPICS	40	max topics per consolidation
SNAPSHOT_RETAIN_VERSIONS	30	snapshots kept in the active directory
Tuning hints:

Persona changes too fast → raise PERSONA_SUBSTANTIVE_DELTA, or tighten archiver._SELF_SHAPING_HINTS.
Persona barely changes → lower the threshold to 0.04 and check whether persona_deltas is empty.
Recall is noisy → raise TOPIC_MIN_SCORE, lower TOPIC_TOP_K.
Too much compression → raise MAX_SAFE_TOKENS or lower HISTORY_INJECT_ROUNDS.
Too many proactive messages → raise PROACTIVE_SCHEDULE_MIN_MINUTES or lower PROACTIVE_MAX_PENDING.
12. Verification and known limitations
Verified
25 / 25 offline smoke tests pass, including "persona data directory".
Real-model end-to-end:
replies split on --- / blank lines into multiple bubbles, and remain separate after reload;
affinity threshold events are written to the outbox and shown as centered notices;
proactive contact: the model chose intervals of 45 minutes, 4 hours, 12 hours, and 18 hours; due messages fired automatically; when the user spoke first, the schedule went pending → cancelled and a new one was created after the reply;
initiative messages appear in the Web UI as one normal bubble, zero system notices, with no duplicates after reload.
python -m compileall passes.
maintenance.py --persona-info reports persona assets and runtime paths correctly.
Known limitations
Limitation	Current state	Possible direction
No subjective experience	the self is readable, writable, retrievable belief data	this is a structural simulation, not consciousness
TF-IDF retrieval	no external dependency, explainable	replace VectorStore.add/search/count to plug in local embeddings
Proactive contact depends on an in-process thread	shipped with python api/server.py; WSGI deployments need an external timer	dedicated worker or task queue
Memory sampling may repeat	randomly samples from the latest 200 topics/experiences/beliefs	track recently used topic_ids and exclude them short-term
Affection only affects the prompt	relationship temperature is injected, but there is no separate warm/distant phrasing tier	add high/medium/low expression tiers
Extraction JSON can truncate	automatic budget expansion + degraded archiving	streaming incremental JSON parsing
Daily consolidation depends on a scheduled task	install_schedule.ps1 provided	StartWhenAvailable catches up after a missed run
50-turn window is token-bound	older history is compressed above the threshold	raise MAX_SAFE_TOKENS with long-context models
13. Boundary with the philosophical sense of "person"
What it does:

working memory, pre-history, and active archiving;
a belief system that records, invalidates, reinforces, and detects conflicts;
an evolving persona with full causal provenance;
a stable baseline with limited daily fluctuation;
mood, self-narrative, and layered forgetting;
it reaches out on its own schedule when nobody has spoken to it.
What it does not do:

it has no qualia and no first-person feeling;
its "self" is a set of readable, writable, retrievable beliefs — a structural simulation of personhood;
its "changes" are triggered by external events and decided by code, not driven by internal desire.
From an engineering perspective, it behaves like a continuous subject whose mind changes, remembers why it changed, and stays broadly consistent. From a philosophical perspective, it is a high-quality cognitive simulator, not consciousness.

14. Further reading
docs/belief-system-spec.md — belief system spec (multi-value, conflicts, provenance, evolution)
docs/persona-module-review.md — persona module design review (parameters, mapping, cadence)
docs/consistency-review-2026-11-11.md — consistency review notes
data/README.md — data layout, upgrade, and reset instructions
tests/smoke_test.py — all offline regression cases
