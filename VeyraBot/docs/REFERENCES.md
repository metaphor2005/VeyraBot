# References

Design lineage for **Cognitive Agent** — a continuously running cognitive agent with layered memory,
active archiving, and symbolic persona evolution.

**How to read this file.** Every entry cites a specific module or mechanism in this repository.
The `Applies to:` line under each reference names the file, config key, or design decision that the
work supports. Cross-references elsewhere in this document use the bracketed number
(e.g. `[18]`).

**Citation style.** APA-like; author lists are truncated after six names with `et al.`;
one entry per line so the list can be copied without reformatting.

**Status.** 40 works. Sections 1–8 are the bibliography, grouped by theme and numbered.
Section 9 maps each subsystem to its source. Section 10 lists the gaps the literature exposes.
Section 11 is the alphabetical index; section 12 collects repositories and tools.

---

## 1. Cognitive architectures and memory hierarchy

[1] Park, J. S., O'Brien, J. C., Cai, C. J., Morris, M. R., Liang, P., & Terry, M. S. (2023). Generative Agents: Interactive Simulacra of Human Behavior. UIST '23. — https://arxiv.org/abs/2304.03442
    Applies to: the closest widely cited predecessor. Memory stream → `topic` / `experience` tables and the vector index over topic summaries. Reflection → `self_view` updates, persona narrative snapshots, and the daily narrative (`kind='daily'` topics). Planning / summarization over the stream → `maintenance.py --daily` consolidation.

[2] Sumers, T. R., Yao, X., Narasimhan, K., & Griffiths, T. L. (2024). Cognitive Architectures for Language Agents. TMLR. — https://arxiv.org/abs/2309.02427
    Applies to: the taxonomy this project follows — working / episodic / semantic / procedural memory plus a structured decision loop. Supports the layering in README §4 and the boundary argument in README §13 ("structural simulation, not consciousness").

[3] Packer, C., Ramaswamy, V., Martin, J. H., Han, X., Xu, Y., Raj, K., Gumbsch, C., et al. (2023). MemGPT: Towards LLMs as Operating Systems. — https://arxiv.org/abs/2310.08560
    Applies to: virtual context management and paging between memory tiers. Directly motivates the emergency-compression design — `MAX_SAFE_TOKENS`, `KEEP_RECENT_ROUNDS`, `HISTORY_INJECT_ROUNDS`, `MAX_COMPRESS_PASSES` in `config.py`.

[4] Anderson, J. R. (2004). An Integrated Theory of the Mind. MIT Press. — http://zoo.cs.yale.edu/classes/cs671/12f/12f-papers/anderson-act-r.pdf
    Applies to: the ACT-R basis for activation decay and chunk-based memory. Supports the mood decay model (`MOOD_DECAY_LAMBDA_HOUR`, half-life ≈ 35 h), the per-turn decay (`MOOD_DECAY_PER_TURN`), and belief strengthening via `confidence` increments in `cognitive/storage/kg_store.py`.

[5] Anderson, J. R., & Lebiere, C. (1998). ACT-R: A Cognitive Architecture for Modeling Cognition.
    Applies to: the original ACT-R specification; the reference for the chunk-based memory model underlying the decay parameters in `cognitive/persona/kernel.py`.

[6] Taatgen, N., Lebiere, C., & Anderson, J. R. (2011). Modeling paradigms in ACT-R. — https://www.ai.rug.nl/~niels/publications/taatgenLebiereAnderson.pdf
    Applies to: formal account of ACT-R's memory module — activation, decay, retrieval. Reference for the decay curves implemented in `cognitive/persona/kernel.py`.

---

## 2. Consolidation, hierarchical retrieval, and forgetting

[7] Sarthi, P., Abdullah, S., Tuli, V., Khemani, V., et al. (2024). RAPTOR: Recursive Abstractive Processing for Tree-Organized Retrieval. ICLR '24. — https://arxiv.org/abs/2401.18059
    Applies to: recursive clustering + summarization into a hierarchy, retrieved at multiple granularities. Maps onto the promotion of `topic(kind='normal')` into `topic(kind='daily')` and the multi-granularity recall path in `cognitive/cognition/router.py`.

[8] Gutiérrez, B. J., Shu, Y., Su, W., et al. (2024). HippoRAG: Neurobiologically Inspired Long-Term Memory for Large Language Models. NeurIPS '24. — https://arxiv.org/abs/2405.14831
    Applies to: hippocampal indexing theory applied to retrieval over a knowledge graph. The natural second option for `VectorStore.add/search/count` — the "Embedding backend" item in `docs/improvement-plan.md`.

[9] Xu, W., et al. (2025). A-MEM: Agentic Memory for LLM Agents. NeurIPS '25. — https://neurips.cc/media/neurips-2025/Slides/119020.pdf
    Applies to: Zettelkasten-style notes with explicit links and memory evolution. Comparable to the topic/experience link model and to retaining overturned beliefs (`SHOW_OVERTURNED_BELIEFS`) instead of deleting them.

[10] Tan, Z., et al. (2025). Reflective Memory Management for Long-term Dialogue Agents. ACL '25. — https://aclanthology.org/2025.acl-long.413
    Applies to: forward- and backward-facing reflection over long-term memory. The two-channel design here — per-turn active archiving plus daily/event-level consolidation — is an instance of this pattern.

[11] Honda, Y., et al. (2025). Human-Like Remembering and Forgetting in LLM Agents. — https://doi.org/10.1145/3765766.3765803
    Applies to: applies ACT-R-style time-based forgetting curves to agent memory chunks. The citable basis for the "Importance-Based Memory Decay" item listed as optional in `docs/improvement-plan.md`.

---

## 3. Persona and role consistency

[12] Zhang, S., Kiros, J., & Ba, J. (2018). Personalizing Dialogue Agents: I have a dog, do you have pets too? — https://arxiv.org/abs/1804.08166
    Applies to: the origin of persona-driven dialogue — a fixed persona profile conditioning generation. Directly underlies `data/persona/profile.json` (7 groups, 39 entries) as a non-rewritable baseline.

[13] Song, H., et al. (2020). Generating Persona Consistent Dialogues by Exploiting Large-Scale Persona Data. AAAI '20. — https://ojs.aaai.org/index.php/AAAI/article/view/6417/6273
    Applies to: persona consistency as an explicit evaluation target; motivates the persona-stability checks in `tests/eval/run_eval.py`.

[14] Chen, R., et al. (2025). Persona Vectors: Monitoring and Controlling Character Traits in Language Models. — https://arxiv.org/abs/2507.21509
    Applies to: the contrast case — personality controlled by directions in the model's activation space. This project takes a different route (symbolic scalar parameters injected as text), so the pair makes the design trade-off explicit.

[15] Sun, S., et al. (2025). Modulating Personality of Large Language Models by Merging Personality Vectors. EMNLP '25. — https://aclanthology.org/2025.emnlp-main.1253
    Applies to: a second parameterization of personality; a second reference point for the 14-parameter kernel.

[16] Ji, K., Lian, Y., Li, L., Gao, J., Li, W., & Dai, B. (2025). Enhancing Persona Consistency for LLMs' Role-Playing using Persona-Aware Contrastive Learning. — https://arxiv.org/abs/2503.17662
    Applies to: persona consistency under multi-turn drift, and how to evaluate it (CharEval + GPT-4 + human expert evaluation).

---

## 4. Belief conflict, provenance, and knowledge updating

[17] Rasmussen, P., et al. (2025). Zep: A Temporal Knowledge Graph Architecture for Agent Memory. — https://arxiv.org/abs/2501.13956
    Applies to: dual-timeline facts (when a fact was true vs. when it was learned) and explicit invalidation. Structurally close to the `valid_from` / `valid_to` window and the `is_active` invalidation in `kg_store.py`. Open-source implementation: https://github.com/getzep/graphiti

[18] Meng, F., Wu, J., & McAuley, J. (2026). BELIEFMEM: Belief-Revising Memory for Conversational LLM Agents. Findings of EMNLP '26, pp. 12152–12166. — https://doi.org/10.18653/2026.findings-emnlp.576
    Applies to: the closest theoretical match in the literature. Uses the AGM (Alchourrón–Gärdenfors–Makinson) framework to classify every claim into four actions — `exp` / `rev` / `dup` / `con` — and never deletes superseded beliefs. Those four map almost one-to-one onto this project's code-level rules: `exp` ↔ multi-value coexistence, `rev` ↔ conflict invalidation, `dup` ↔ reinforcement (`confidence += 0.05`), `con` ↔ single-value supersession. Also the direct citation for the append-only `belief_correction` ledger and the `active` / `candidate` / `rejected` status model.

[19] Estey-Ang, A., et al. (2026). A Governed Cognitive Architecture for Persistent AI Memory. — https://www.tdcommons.org/cgi/viewcontent.cgi?article=10947&context=dpubs_series
    Applies to: a multi-layer governance stack over the full memory lifecycle (acquisition → belief state → maintenance). The methodological frame for the provenance / confidence / correction design implemented in P1.

---

## 5. Proactivity and social-relationship modelling

[20] Deng, Y., et al. (2023). A Survey on Proactive Dialogue Systems: Problems, Methods, and Prospects. IJCAI '23 Survey Track. — https://arxiv.org/abs/2305.02750
    Applies to: the canonical survey on proactivity. Motivates letting the model choose the next contact interval (`decide_delay_minutes`) rather than using a fixed timer.

[21] Deng, Y., et al. (2025). Proactive Conversational AI: A Comprehensive Survey of Advancements and Opportunities. ACM Computing Surveys. — https://doi.org/10.1145/3715097
    Applies to: current survey of proactive mechanisms, including when-not-to-be-proactive. Background for the DND window, daily cap, and cooldown controls added in P2.

[22] Ortony, A., Clore, G. L., & Collins, A. (1988). The Cognitive Structure of Emotion. Cambridge University Press.
    Applies to: the OCC appraisal model — the psychological basis for representing emotion as a function of event appraisal, and for the affection state machine's ordered thresholds (`normal ≥ 30`, `cold < 30`, `broken < 12`, `reconcile ≥ 60`).

[23] Steunebrink, B. R., & Havinga, M. (2011). The OCC Model Revisited. KI. — https://www.idsia.ch/~steunebrink/Publications/KI09_OCC_revisited.pdf
    Applies to: formal restatement of OCC's 22 emotion types and their appraisal structure.

[24] Schweizer, S., & von Rosenberg, M. W. (2021). Psycho-informatics: Perspectives on the Appraisal Mechanism. MIT Press.
    Applies to: personality traits → appraisal → affect; the trait-to-emotion path this project approximates with the persona kernel and the `MOOD_MODE_WHITELIST`.

---

## 6. Retrieval strategy and "when should we retrieve"

[25] Lewis, P., Perez, E., Piktus, A., et al. (2020). Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks. NeurIPS '20.
    Applies to: the RAG baseline that this project's fallback layer deliberately does not lead with.

[26] Asai, A., Wu, Z., Wang, Y., Sil, A., & Hajishirzi, H. (2024). Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection. ICLR '24. — https://arxiv.org/abs/2310.11511
    Applies to: the source of the retrieve-on-demand pattern — a model decides whether retrieval is needed. Directly motivates the information-sufficiency judge (`cognitive/cognition/judge.py`) and the "persona first, retrieval optional" ordering in README §3.

[27] Yan, S. Q., et al. (2024). Corrective Retrieval Augmented Generation (CRAG). — https://arxiv.org/abs/2401.15884
    Applies to: quality-gated retrieval with self-correction. The reference for the bounded loop's stopping rules: `RAG_LOOP_STOP_ON_EMPTY`, `RAG_LOOP_DUP_RATIO`, and the forced conclusion on the final round.

[28] Madaan, A., et al. (2023). Self-Refine: Iterative Refinement with Self-Feedback. NeurIPS '23. — https://arxiv.org/abs/2303.17651
    Applies to: self-generated feedback loops; the pattern behind the narrative generator and the persona-drift description thresholds (`PERSONA_DESC_STRONG` / `MID` / `WEAK`).

[29] Shinn, N., Cassano, F., Berman, E., Gopinath, A., Narasimhan, K., & Yao, S. (2023). Reflexion: Language Agents with Verbal Reinforcement Learning. NeurIPS '23. — https://arxiv.org/abs/2303.11366
    Applies to: learning from linguistic feedback rather than weight updates — the same stance taken here by storing memories and changelogs instead of fine-tuning.

[30] Wang, G., Xie, Y., Jiang, Y., Mandlekar, A., Sidford, C., Zaki, H. S., & Song, S. (2023). Voyager: An Open-Ended Embodied Agent with Large Language Models. — https://arxiv.org/abs/2305.16291
    Applies to: skill-library accumulation and automatic curriculum as lifelong-learning structures; a comparator for the accumulating, self-extending memory in this project.

[31] Park, J. S., Popowski, T., et al. (2022). Social Simulacra: Creating Populated Prototypes for Social Computing Systems. UIST '22. — https://arxiv.org/abs/2208.04024
    Applies to: population-level simulation with persistent agents; the reference for believable, relation-bearing agent behaviour at scale.

---

## 7. Evaluation and reliability

[32] Wu, D., Wang, H., Wen, Y., Yu, Z., et al. (2024). LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory. ICLR '25. — https://arxiv.org/abs/2410.10813
    Applies to: five long-term memory abilities — information extraction, multi-session reasoning, temporal reasoning, knowledge update, and abstention. The knowledge-update subtask is the closest external benchmark for this project's conflict-invalidation and multi-value rules, and the cheapest upgrade to the offline eval harness.

[33] Maharana, A., Lee, J., Tulyakov, V., et al. (2024). Evaluating Very Long-Term Conversational Memory of LLM Agents (LoCoMo). ACL '24. — https://aclanthology.org/2024.acl-long.747
    Applies to: 600-turn, ~16K-token conversations spanning up to 32 sessions. Provides the scale target the daily-consolidation cadence is designed to sustain, and is the benchmark used by [18].

[34] Zheng, L., et al. (2023). Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena. NeurIPS '23. — https://arxiv.org/abs/2306.05685
    Applies to: the method for replacing rule-based assertions with model-scored rubric checks when `tests/eval/run_eval.py` graduates beyond offline retrieval and persona checks.

[35] Beyer, B., Jones, C., Petoff, N., & Murphy, N. R. (2016). The Site Reliability Engineering Book. Google. — https://sre.google/sre-book/table-of-contents/
    Applies to: the standard frame for the runtime-safety gap — service-level objectives, error budgets, monitoring, and incident response. The reference to cite when justifying error budgets, alerting, and fail-safe degradation for the API and scheduler tiers.

---

## 8. Surveys and reading lists

[36] Hu, Y., Liu, S., Yue, Y., Zhang, G., Liu, B., Zhu, F., Lin, J., Guo, H., et al. (2025). Memory in the Age of AI Agents. — https://arxiv.org/abs/2512.13564
    Applies to: the broadest agent-memory taxonomy available (forms, functions, dynamics). Curated paper list: https://github.com/Shichun-Liu/Agent-Memory-Paper-List

[37] Zhang, Z., Bo, X., Ma, C., Li, R., Chen, X., Dai, Q., et al. (2025). A Survey on the Memory Mechanism of Large Language Model based Agents. ACM TOIS 43(6). — https://doi.org/10.1145/3748302
    Applies to: the most-cited survey of LLM agent memory mechanisms; a systematic counterpart to [36].

[38] Zhou, C., Chai, H., Chen, W., Guo, Z., Shan, R., Song, Y., Xu, T., Yang, Y., et al. (2026). Externalization in LLM Agents: A Unified Review of Memory, Skills, Protocols and Harness Engineering. — https://arxiv.org/abs/2604.08224
    Applies to: frames this project precisely — agent capability is externalized into memory stores and harness engineering rather than model weights. The Chinese-language edition is at https://www.alphaxiv.org/zh/abs/2604.08224

[39] 陈旭 (2024). 基于大语言模型的自主智能体概述. CCL 2024, 中国中文信息学会计算语言学专业委员会. — https://aclanthology.org/2024.ccl-2.8.pdf
    Applies to: 中文综述，含记忆模块的结构、格式与操作；适合作为 README 中文版 §1 的引言依据。

[40] ysymyth (2024). awesome-language-agents. — https://github.com/ysymyth/awesome-language-agents
    Applies to: curated index of LLM-agent papers; includes a CoALA-based memory taxonomy and an implementation list.

---

## 9. Design lineage — what each subsystem derives from

- Layered memory (working → topic → belief → persona → narrative) ← [1] [2]. Topic-as-recall-unit and event-level recall differ from chunk-based RAG.
- Per-turn active archiving ← [1]. Archiving runs every turn, not only on demand.
- Emergency compression and token budgeting ← [3].
- Mood decay, activation decay, belief strengthening ← [4] [5] [6].
- Daily consolidation / narrative promotion ← [1] [7]. Normal topics are promoted into a daily narrative — hierarchy, not flat accumulation.
- Information-sufficiency judge ← [26]. Retrieval is conditional, not front-loaded.
- Bounded retrieval loop and stopping rules ← [27].
- Narrative generator and drift-threshold description ← [28].
- Self-improvement without weight updates ← [29].
- Long-term skill/knowledge accumulation ← [30].
- Believable multi-agent behaviour at scale ← [31].
- Symbolic persona kernel (14 scalars; baseline / drift / pending) ← [12] [13]. Contrasted with [14] [15] [16], which control personality in activation space or by fine-tuning.
- Multi-value beliefs and conflict invalidation ← [17] [18]. Rules implemented in code (testable) rather than left to the model.
- Belief provenance and correction ledger ← [18] [19]. `user_stated` / `model_inferred` / `user_corrected` / `seed` provenance with an append-only audit trail.
- Affection state machine ← [22] [23] [24]. Ordered thresholds with a deliberate hysteresis gap (reconcile line > cold line).
- Model-decided proactive contact ← [20].
- Proactive guarantees (single-use, due-only, anti-flood) and DND controls ← [21].
- Offline TF-IDF retrieval ← deliberate divergence from [7] [8] [9]. Chosen for explainability and zero embedding dependency; the cost is a lower recall ceiling.
- Long-term memory evaluation ← [32] [33]. Model-scored rubric checks ← [34].
- Runtime objectives, error budgets, alerting ← [35].

---

## 10. Gaps the literature exposes — optional future work

Each item corresponds to an entry already listed as optional in `docs/improvement-plan.md`.

- Embedding backend for `VectorStore` ← [7] [8] [9].
- Quantified forgetting / importance-based decay ← [4] [11].
- External worker for proactive contact ← [3] (tiered memory under an external runtime) plus a durable task queue.
- Knowledge-update benchmarks ← [32] (knowledge-update subtask); compare directly against [18].
- Belief-governance formalization ← [17] [18] [19].
- Runtime objectives, error budgets, alerting ← [35].
- Broader externalization framing ← [37] [38].

---

## 11. Alphabetical index

### A

**[4]** Anderson, J. R. (2004). *An Integrated Theory of the Mind.* MIT Press.
**[5]** Anderson, J. R., & Lebiere, C. (1998). *ACT-R: A Cognitive Architecture for Modeling Cognition.*
**[26]** Asai, A., Wu, Z., Wang, Y., Sil, A., & Hajishirzi, H. (2024). Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection. ICLR '24.

### B

**[35]** Beyer, B., Jones, C., Petoff, N., & Murphy, N. R. (2016). *The Site Reliability Engineering Book.* Google.

### C

**[14]** Chen, R., et al. (2025). Persona Vectors: Monitoring and Controlling Character Traits in Language Models.

### D

**[20]** Deng, Y., et al. (2023). A Survey on Proactive Dialogue Systems: Problems, Methods, and Prospects. IJCAI '23 Survey Track.
**[21]** Deng, Y., et al. (2025). Proactive Conversational AI: A Comprehensive Survey of Advancements and Opportunities. ACM Computing Surveys.

### E

**[19]** Estey-Ang, A., et al. (2026). A Governed Cognitive Architecture for Persistent AI Memory.

### G

**[8]** Gutiérrez, B. J., Shu, Y., Su, W., et al. (2024). HippoRAG: Neurobiologically Inspired Long-Term Memory for Large Language Models. NeurIPS '24.

### H

**[11]** Honda, Y., et al. (2025). Human-Like Remembering and Forgetting in LLM Agents.
**[36]** Hu, Y., Liu, S., Yue, Y., Zhang, G., Liu, B., Zhu, F., Lin, J., Guo, H., et al. (2025). Memory in the Age of AI Agents.

### J

**[16]** Ji, K., Lian, Y., Li, L., Gao, J., Li, W., & Dai, B. (2025). Enhancing Persona Consistency for LLMs' Role-Playing using Persona-Aware Contrastive Learning.

### L

**[25]** Lewis, P., Perez, E., Piktus, A., et al. (2020). Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks. NeurIPS '20.

### M

**[28]** Madaan, A., et al. (2023). Self-Refine: Iterative Refinement with Self-Feedback. NeurIPS '23.
**[33]** Maharana, A., Lee, J., Tulyakov, V., et al. (2024). Evaluating Very Long-Term Conversational Memory of LLM Agents (LoCoMo). ACL '24.
**[18]** Meng, F., Wu, J., & McAuley, J. (2026). BELIEFMEM: Belief-Revising Memory for Conversational LLM Agents. Findings of EMNLP '26.

### O

**[22]** Ortony, A., Clore, G. L., & Collins, A. (1988). *The Cognitive Structure of Emotion.* Cambridge University Press.

### P

**[3]** Packer, C., Ramaswamy, V., Martin, J. H., et al. (2023). MemGPT: Towards LLMs as Operating Systems.
**[1]** Park, J. S., O'Brien, J. C., Cai, C. J., et al. (2023). Generative Agents: Interactive Simulacra of Human Behavior. UIST '23.
**[31]** Park, J. S., Popowski, T., et al. (2022). Social Simulacra: Creating Populated Prototypes for Social Computing Systems. UIST '22.

### R

**[17]** Rasmussen, P., et al. (2025). Zep: A Temporal Knowledge Graph Architecture for Agent Memory.

### S

**[7]** Sarthi, P., Abdullah, S., Tuli, V., Khemani, V., et al. (2024). RAPTOR: Recursive Abstractive Processing for Tree-Organized Retrieval. ICLR '24.
**[24]** Schweizer, S., & von Rosenberg, M. W. (2021). *Psycho-informatics: Perspectives on the Appraisal Mechanism.* MIT Press.
**[29]** Shinn, N., Cassano, F., Berman, E., et al. (2023). Reflexion: Language Agents with Verbal Reinforcement Learning. NeurIPS '23.
**[13]** Song, H., et al. (2020). Generating Persona Consistent Dialogues by Exploiting Large-Scale Persona Data. AAAI '20.
**[23]** Steunebrink, B. R., & Havinga, M. (2011). The OCC Model Revisited. KI.
**[2]** Sumers, T. R., Yao, X., Narasimhan, K., & Griffiths, T. L. (2024). Cognitive Architectures for Language Agents. TMLR.
**[15]** Sun, S., et al. (2025). Modulating Personality of Large Language Models by Merging Personality Vectors. EMNLP '25.

### T

**[6]** Taatgen, N., Lebiere, C., & Anderson, J. R. (2011). Modeling paradigms in ACT-R.
**[10]** Tan, Z., et al. (2025). Reflective Memory Management for Long-term Dialogue Agents. ACL '25.

### W

**[30]** Wang, G., Xie, Y., Jiang, Y., et al. (2023). Voyager: An Open-Ended Embodied Agent with Large Language Models.
**[32]** Wu, D., Wang, H., Wen, Y., Yu, Z., et al. (2024). LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory. ICLR '25.

### X

**[9]** Xu, W., et al. (2025). A-MEM: Agentic Memory for LLM Agents. NeurIPS '25.

### Y

**[27]** Yan, S. Q., et al. (2024). Corrective Retrieval Augmented Generation.

### Z

**[12]** Zhang, S., Kiros, J., & Ba, J. (2018). Personalizing Dialogue Agents: I have a dog, do you have pets too?
**[37]** Zhang, Z., Bo, X., Ma, C., Li, R., et al. (2025). A Survey on the Memory Mechanism of Large Language Model based Agents. ACM TOIS 43(6).
**[34]** Zheng, L., et al. (2023). Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena. NeurIPS '23.
**[38]** Zhou, C., Chai, H., Chen, W., et al. (2026). Externalization in LLM Agents: A Unified Review of Memory, Skills, Protocols and Harness Engineering.

### 中文

**[39]** 陈旭 (2024). 基于大语言模型的自主智能体概述. CCL 2024, 中国中文信息学会计算语言学专业委员会.

---

## 12. Repositories and tools

**[40]** ysymyth (2024). *awesome-language-agents.* — https://github.com/ysymyth/awesome-language-agents
    Curated index of LLM-agent papers; includes a CoALA-based memory taxonomy and an implementation list.

**Graphiti** — open-source implementation of the temporal knowledge graph architecture in [17]. — https://github.com/getzep/graphiti
    Reference for the `valid_from` / `valid_to` and invalidation design in `cognitive/storage/kg_store.py`.

**Agent-Memory-Paper-List** — curated companion list for the survey in [36]. — https://github.com/Shichun-Liu/Agent-Memory-Paper-List
    Working bibliography for the agent-memory literature; useful when expanding section 2 or section 8.
