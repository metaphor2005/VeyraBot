# Improvement plan (P0–P3)

This document records the external code-review roadmap and how each item was implemented. It is the companion to `docs/consistency-review-2026-11-11.md` (the review that produced it).

Guiding rule: fix **correctness** before **reliability**, then **UX**, then **runtime**. Every item below is implemented, covered by an offline test where possible, and verified with `python -m compileall`, `tests/smoke_test.py`, and `tests/eval/run_eval.py`.

---

## Scope at a glance

| Priority | Theme | Items | Status |
|---|---|---|---|
| P0 | Archiving correctness | turn-aware transcript, idempotency, explicit big-event input | Done |
| P1 | Memory reliability | belief provenance, user correction, correction ledger | Done |
| P2 | UX and control | persona freeze/reset, proactive policy (DND / cap / cooldown / skip) | Done |
| P3 | Runtime safety | optional API token, export/backup/reset maintenance commands | Done |
| — | Evaluation | offline retrieval + persona-prompt eval harness | Done |

---

## P0 — Archiving correctness

### Problem

The archiver reconstructed "this turn" from the tail of session history. With multi-bubble replies that is ambiguous: a user message could be labelled `assistant`, and re-running archiving on the same turn duplicated topics, beliefs and experiences.

### Changes

| File | Change |
|---|---|
| `cognitive/models.py` | `Message.turn_id`, `new_id("turn")`; `SessionContext.append(..., turn_id=...)` |
| `cognitive/session.py` | `SessionManager.append(..., turn_id=...)` persists the turn id |
| `cognitive/storage/topic_store.py` | `Topic.turn_id` column; `get_topic_by_turn(space_id, turn_id)` |
| `cognitive/cognition/archiver.py` | `archive(..., user_message, answer_parts, turn_id)`; `_build_transcript` uses explicit inputs; duplicate check returns `ok=True, duplicate=True, skipped=True` |
| `cognitive/cognition/judge.py` | `judge_big_event(relations, user_message="", user_text="")` uses the explicit user message |
| `cognitive/engine.py` | generates one `turn_id` per chat turn; passes it to user message, every assistant bubble, and archiving |

### Guarantees

- One `turn_id` per user turn; every assistant bubble shares it.
- Re-archiving the same turn returns a duplicate report instead of writing twice.
- Big-event detection reads the actual user message, not a slice of the transcript that may start with an assistant bubble.
- `ARCHIVE_SYNC=false` runs archiving in a daemon thread and returns an immediate placeholder report (`{"ok": true, "async": true, "turn_id": ...}`) so callers never mistake `None` for failure.

### Tests

`test_p0_turn_aware_transcript`, `test_p0_archiver_idempotent`, `test_p0_judge_uses_explicit_message`.

---

## P1 — Memory reliability

### Problem

Beliefs had no provenance and no user-facing correction path. A model-inferred claim looked identical to something the user explicitly said, and a wrong belief could not be confirmed, corrected, or rejected.

### Data model

`kg_relation` gains additive columns (via `_COLUMN_MIGRATIONS`; existing DBs migrate on open):

| Column | Meaning |
|---|---|
| `source_turn_id` | which turn produced the belief |
| `source_type` | `user_stated` / `model_inferred` / `user_corrected` / `seed` |
| `status` | `active` / `candidate` / `rejected` |
| `valid_from` / `valid_to` | validity window (0 = open) |
| `user_confirmed` | user pressed "confirm" |

New table `belief_correction` is an append-only audit ledger: `space_id, correction_id, rid, head_name, rel, old_tail, new_tail, action, note, create_ts`.

### Changes

| File | Change |
|---|---|
| `cognitive/storage/kg_store.py` | provenance-aware `upsert_relation`; `list_beliefs`, `get_belief`, `update_belief`, `confirm_belief`, `correct_belief`, `reject_belief`, `_append_correction`, `list_corrections` |
| `cognitive/cognition/archiver.py` | persists provenance from extraction; freeze gate; writes correction audit entries; `_stable_id` restored |
| `api/server.py` | `GET /api/memory/beliefs`, `POST /api/memory/beliefs/update`, `GET /api/memory/corrections` |

### Semantics

- **confirm** → `status='active'`, `user_confirmed=1`.
- **correct** → `status='candidate'`, `user_confirmed=0` (kept for re-validation, not deleted).
- **reject** → `status='rejected'`, `is_active=0` (kept for history).
- Every action appends to `belief_correction`.
- `list_beliefs` filters to `status='active'` by default.

### Tests

`test_p1_belief_provenance`, `test_p1_user_correction_api_shape`.

---

## P2 — UX and control

### Persona freeze / reset

| File | Change |
|---|---|
| `cognitive/persona/kernel.py` | `is_frozen`, `set_frozen`, `reset_to_baseline` |
| `cognitive/cognition/archiver.py` | when frozen, clears `persona_deltas`, `persona_mood`, `self_view_update` |
| `api/server.py` | `POST /api/persona/freeze`, `POST /api/persona/reset` |
| `cognitive/engine.py` | `persona_state` exposes `frozen` |

Semantics: freezing keeps **memory growing** (topics, beliefs, experiences) but stops personality, mood and self-view drift. Reset restores `config.PERSONA_KERNEL_INIT` into `value` and `base_value`, clears pending drift and moods, and **keeps all memories**.

Note: `persona_kernel` columns are `space_id, param, value, base_value, pending_value, update_ts`.

### Proactive policy

| File | Change |
|---|---|
| `cognitive/storage/affection_store.py` | `ProactivePolicyStore` (DND / daily cap / cooldown / skip), `ProactiveScheduleStore.skip` |
| `cognitive/engine.py` | `proactive_tick` checks policy before firing; `force=true` skips policy except `skip_next` |
| `api/server.py` | `GET/POST /api/proactive/policy`, `POST /api/proactive/skip` |
| `config.py` | `PROACTIVE_DND_START/END`, `PROACTIVE_DAILY_CAP`, `PROACTIVE_COOLDOWN_MINUTES` |

Block reasons are explicit strings: `dnd`, `daily_cap`, `cooldown`, `skip_next`. Supported DND windows cross midnight (e.g. 23 → 8).

### Web UI

New settings tabs:

- **记忆** — belief list with 确认 / 纠正 / 否决 and refresh.
- **人格** — freeze switch, "回到最初" reset, DND / daily cap / cooldown fields, "这轮先别发" button.

### Tests

`test_p2_persona_freeze_reset`, `test_p2_proactive_policy`.

---

## P3 — Runtime safety

### Optional API token

`config.API_TOKEN` (empty = no auth; default binds `127.0.0.1`). When set, `/api/*` requires `X-API-Token` header or `?token=`. `/api/health` and static files are exempt so health checks and page loads still work.

### Maintenance commands

| Command | Purpose |
|---|---|
| `python maintenance.py --export data/export.json` | dump persona, beliefs, corrections, topics, experiences as JSON |
| `python maintenance.py --backup` | back up DB + vectors + snapshots to `data/backups/` (keeps recent 10) |
| `python maintenance.py --reset --yes` | clear runtime memory for the space, re-seed persona baseline (destructive; requires `--yes`) |

`--export` and `--backup` are non-destructive and recommended before `--reset`.

---

## Evaluation harness

`tests/eval/run_eval.py` (offline, no model calls):

- 17 colloquial retrieval queries over 8 topics;
- asserts `Recall@1 >= 0.55`, `Recall@3 >= 0.85`, false-recall = 0 on adversarial technical queries;
- persona prompt checks: name present, identity, rules, non-empty, no `sk-` key, no local paths.

Current result: Recall@1 = 1.000, Recall@3 = 1.000, false recall = 0, persona checks all pass.

---

## File map

```
cognitive/models.py                     # turn_id on messages
cognitive/session.py                    # append(..., turn_id)
cognitive/engine.py                     # turn pipeline, async archiving, policy-aware tick
cognitive/cognition/archiver.py         # turn-aware, idempotent, provenance, freeze, ledger
cognitive/cognition/judge.py            # explicit user-message big-event decision
cognitive/persona/kernel.py             # freeze / reset
cognitive/storage/kg_store.py           # provenance + correction API
cognitive/storage/topic_store.py        # turn_id, get_topic_by_turn
cognitive/storage/affection_store.py    # ProactivePolicyStore, schedule.skip
cognitive/storage/database.py           # belief_correction, proactive_policy, migrations
api/server.py                           # belief / persona / policy endpoints, API_TOKEN
maintenance.py                          # --export / --backup / --reset
web/index.html, web/app.js, web/style.css  # 记忆 / 人格 tabs
tests/smoke_test.py                     # 32 offline tests
tests/eval/run_eval.py                  # offline retrieval + persona eval
docs/improvement-plan.md                # this document
```

---

## Verification commands

```bash
python -X utf8 -m compileall -q api cognitive config.py maintenance.py tests/smoke_test.py tests/eval/run_eval.py
python -X utf8 tests/smoke_test.py      # expect 32/32
python -X utf8 tests/eval/run_eval.py   # expect all checks pass
```

---

## Remaining optional work

| Item | Why it is optional | Sketch |
|---|---|---|
| Fixed-dialogue replay harness | nice-to-have regression net; smoke tests already cover storage/persona | replay scripted turns with a mock client and diff archive reports |
| Optional severance storyline | affected only by narrative polish, not correctness | scripted multi-turn arc that walks affection through cold → severance → reconcile |
| Embedding backend | TF-IDF is explainable and dependency-free | swap `VectorStore.add/search/count` for a local embedding model |
| External worker for proactive contact | in-process thread is fine for a single-user local app | move scheduling to a queue/worker for multi-instance deployments |
| Affection phrasing tiers | affection already enters the prompt | add warm / neutral / distant expression tiers |
