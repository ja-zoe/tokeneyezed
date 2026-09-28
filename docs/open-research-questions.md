# Open research questions

Architecture and evaluation questions we can't answer from the code, the plan, or one day's runs. They're here so we can scope what the demo claims, answer judge questions honestly, and pick what to measure next.

This is separate from [`open-questions.md`](open-questions.md), which tracks implementation decisions that need an owner before the contract lock. When a question here gets an answer (a measurement, or a decision to leave it alone), record it under the question with the date and the evidence, and move any resulting decision into `master-plan.md` or `contracts.md`.

Each question states what the system does **today** (on `main` as of 2026-09-26), what's open, and how we'd find out.

**Contents**
1. [Does memory actually improve outcomes?](#1-does-memory-actually-improve-outcomes)
2. [Is vector similarity the right memory primitive?](#2-is-vector-similarity-the-right-memory-primitive)
3. [When should the controller replan?](#3-when-should-the-controller-replan)
4. [Is "intent" a useful control abstraction?](#4-is-intent-a-useful-control-abstraction)
5. [Can the observer become the bottleneck?](#5-can-the-observer-become-the-bottleneck)
6. [Can learned rules create a self-reinforcing failure loop?](#6-can-learned-rules-create-a-self-reinforcing-failure-loop)
7. [Does excluding bad attempts from memory hide useful information?](#7-does-excluding-bad-attempts-from-memory-hide-useful-information)
8. [Is the agent learning, or getting lucky?](#8-is-the-agent-learning-or-getting-lucky)
9. [Does the system optimize the metric or solve the task?](#9-does-the-system-optimize-the-metric-or-solve-the-task)
10. [Does CommonMark stress the long-horizon architecture enough?](#10-does-commonmark-stress-the-long-horizon-architecture-enough)
11. [What information actually transfers in an agent handoff?](#11-what-information-actually-transfers-in-an-agent-handoff)
12. [Ambiguities found while building](#12-ambiguities-found-while-building)
- [Priorities](#priorities)
- [Proposal: make strategy a first-class object](#proposal-make-strategy-a-first-class-object)

---

## 1. Does memory actually improve outcomes?

Probably the most important question. The harness has three layers of accumulated context, all funnelled into one bounded brief:

```text
past attempts
     │
     ├── structured attempt ledger     (attempts)
     ├── vector + keyword retrieval    (attempts_vector, attempts_text, $rankFusion)
     └── compacted memory              (memory, written by the compactor)
              │
              ▼
        bounded brief                  (best attempt, nearest failed attempts,
              │                         memory summaries, skills, active rules)
              ▼
            agent
```

**Today.** The brief is capped by item count and characters, so its size stays flat. The retrieval eval (`tokeneyezed eval retrieval`) measures recall@5: whether retrieval surfaces earlier attempts on the same spec section. H-mem is the ablation that removes ledger retrieval, memory, and skills.

More memory is not better memory. Open questions:
- Does retrieved history actually change the agent's next action?
- Does semantic similarity retrieve *useful* failures, or merely similar-looking ones?
- How stale does memory become, and when is an old failure irrelevant (e.g. after the code it failed on has been rewritten)?
- Does the compactor drop the information that explains *why* an approach failed? Its summaries are 2 to 4 sentences.
- Is `attempts` + `memory` redundant, given the brief already shows the nearest failed attempts?
- What's the right context budget? The caps (3 failed attempts, 3 memories, 300 characters per item) were chosen, not measured.

**How we'd find out.** Recall@5 says whether we retrieved something relevant, not whether it caused useful behaviour. Add a causal metric per attempt:

```text
retrieved attempt ──► did the next intent change strategy? ──► did the next attempt improve?
```

Concretely: log which attempt ids and memory ids each brief showed; have a cheap judge (or an embedding distance) decide whether the planner's next intent diverged from the retrieved failed intents; correlate that with the improvement rate. Compare with H-mem at equal attempt counts. The master plan's "repeat-a-failed-idea rate" is the first half of this.

## 2. Is vector similarity the right memory primitive?

**Today.** Embeddings sit on `attempts`, `memory`, `skills`, and `interventions`. The brief's failed-attempt search is `$rankFusion` over vector and keyword search, filtered to the session and goal. There's no reranker.

Similar intent is not the same as similar failure:

```text
A: "Fix nested emphasis parsing"          B: "Fix nested list parsing"
   → close in embedding space, but need completely different strategies

A: "Regex-based parser failed on nested constructs"
B: "Regex-based parser failed on link nesting"
   → different surface intents, but the same lesson
```

**Open.** Should retrieval be failure-oriented rather than intent-oriented?

```text
query
 ├── semantic similarity
 ├── same spec section
 ├── same failure mode        (needs a recorded failure mode, which we don't have)
 ├── same files touched        (derivable from the diff)
 ├── same strategy             (see the proposal at the end)
 └── outcome
       ▼
   reranker
```

**How we'd find out.** Extend the retrieval eval with a second ground truth, "shares a failure mode or strategy", alongside "same spec section", and add a reranked arm (Voyage `rerank-2.5`, or Atlas `$rerank` on MongoDB 8.3+). Today's recall numbers come from 30 hand-written attempts, not real run data, and prove nothing yet.

## 3. When should the controller replan?

Potentially the biggest weakness in the control loop.

**Today.** An attempt "improves" if its section validation score beats the best so far by *any* amount (`val > best` in `controller/graph.py`). `failure_threshold` (3) consecutive non-improving attempts trigger one replan. So:

```text
72.0% → 72.1% → 72.2% → 72.3%     every attempt resets the streak: never replans,
                                   even if the strategy has plateaued
72%   → 68%   → 81%               two misses, then a jump: looks chaotic, may be promising
```

**Open.** Can the controller detect *strategy-level stagnation*, not just failure? Candidate signals:
- slope of improvement and its variance;
- marginal improvement per token or dollar;
- repeated strategy (intents clustering together);
- newly covered spec sections, and the regression rate on other sections;
- remaining budget.

Eventually, instead of `failure_count > N`, something like `P(useful improvement | current strategy, remaining budget)`.

**How we'd find out.** The master plan's replan metric ("attempts from a plateau to the next validation improvement, after replans vs. plateaus without one") answers whether replans help. A cheap first step: a minimum improvement (e.g. +1 percentage point) before an attempt counts as "improved", and measuring how often each rule fires.

## 4. Is "intent" a useful control abstraction?

**Today.** The planner writes a one- or two-sentence intent. The observer's post-checks (`observer/postcheck.py`) compare what the agent touched with that intent and add a corrective note on mismatch.

**Open.** Who decides whether the intent was good? Suppose:

```text
Intent:          fix emphasis
Agent discovers: the real bug is shared tokenizer state
Agent modifies:  tokenizer + emphasis parser
```

A literal intent check flags that as drift, even though it was exactly the right move. Should intent be structured?

```text
intent
  ├── objective
  ├── constraints
  ├── allowed dependencies
  └── forbidden actions
```

rather than "the files and sections I expect to touch".

**How we'd find out.** Count intent-mismatch notes on attempts that went on to improve: those are likely false positives. See question 5.

## 5. Can the observer become the bottleneck?

**Today.** Every tool call goes through the pre-gate, and every completed call through the post-checks. Deterministic checks fail closed, and model-based checks fail open.

The observer now serves three competing objectives: **security**, **correctness**, and **velocity**. Open:
- How much latency does it add per tool call?
- What's its false-positive rate, and how often does it interrupt useful work?
- Does the agent learn to route around it? (Seen live: when Codex's edit tool was blocked, it wrote files through the shell instead.)
- Does it become more complex than the agent loop it guards?

**How we'd find out.** Classify every intervention:

```text
observer intervention
   ├── prevented bad behaviour
   ├── prevented useful behaviour      ← the one that matters most
   └── irrelevant
```

The replay eval over run B's events gives the first category. Measuring the second needs judgement on a sample of blocks and notes from H, plus the per-call latency the shim already sees.

## 6. Can learned rules create a self-reinforcing failure loop?

Perhaps the most interesting research question in the system.

**Today.** The rule learner (`observer/learner.py`) turns repeated flags into deterministic `input_contains` candidate rules, replays them over recorded events, and promotes them if the replay supports them. Promoted rules reach the pre-gate.

That's online policy learning, with a possible loop:

```text
observer makes a mistake → it's logged → similar mistakes cluster → a rule is learned
   → the rule blocks future behaviour → the new blocks reinforce the pattern
```

**Open.** How strong must the promotion criterion be? Separate *observed violation* from *validated invariant*. Before a rule reaches the live gate, consider requiring:

```text
candidate rule
  ├── replay over recorded events (have it)
  ├── counterexample search on attempts that improved validation
  ├── measured false-positive rate below a threshold
  └── explicit human approval above some blast radius
```

Also: should rules learned from blocks the observer itself caused count as evidence at all?

## 7. Does excluding bad attempts from memory hide useful information?

**Today.** Flagged (gamed) attempts stay in `attempts` for audit, but never reach embeddings, memory, skills, the brief, or metric history (invariant I8).

**Open.** A bad attempt may carry valuable negative knowledge ("this looks promising but causes a 20% regression elsewhere"). "Don't use this as evidence of success" is not the same as "forget it happened". Consider two memories with different trust levels:

```text
            ┌── positive memory       (evidence of what works)
attempt ────┤
            └── negative memory       (what not to do, and why)
```

The tension: surfacing a gamed attempt, even as a warning, could teach the agent the gaming technique. Any negative memory would need to describe the failure without reproducing the shortcut.

## 8. Is the agent learning, or getting lucky?

The biggest evaluation question.

**Today.** Visible (~30%) / validation (~35%) / held-out (~35%) splits. Three runs: B (naive retry), H (full harness), H-mem (no memory), compared at equal attempt counts. One run each. The master plan already says this is a demonstration, not a significant result.

**Open.** Which architectural idea is responsible for any improvement? With enough compute:

```text
B        baseline (retry loop)
B+M      + retrieval and memory
B+R      + replanning
B+O      + observer
B+M+R
B+M+R+O  full harness (H)
```

each with several seeds, reported as held-out score vs. attempt number. Without them, "the harness helped" can't be separated from "more structured prompting helped" or from seed noise. Also compare on cost (tokens and dollars per point of held-out score), not just attempts, since the harness spends more per attempt than B.

## 9. Does the system optimize the metric or solve the task?

**Today.** The validation pass rate drives both replanning and goal completion. The gaming review flags visible-up/validation-flat attempts.

**Open.** Goodhart pressure beyond the visible/validation gap:
- special-casing known test structures;
- exploiting scorer behaviour;
- improving one section while degrading another;
- architectural complexity exploding;
- brittle implementations that happen to pass the examples.

**Adaptive overfitting** too: the validation split is consulted after every attempt for hours, so the harness can overfit it even without gaming. The held-out split protects the reported number, but not the decisions made along the way.

Consider scoring `pass rate + generalization gap + cross-section regression + code complexity + behavioural diversity`, rather than pass rate alone.

## 10. Does CommonMark stress the long-horizon architecture enough?

**Today.** One task: a CommonMark renderer, about 650 examples, naturally decomposed by spec section.

**Open.** The system aims to demonstrate persistent memory, planning, an observer, replanning, and agent handoff. A Markdown parser may be decomposable enough that `goal → fix section → run tests → repeat` works without much long-horizon reasoning. Stronger evidence would come from tasks with:
- architectural dependencies between subsystems;
- ambiguous requirements;
- delayed or expensive feedback;
- state that must survive many attempts;
- several viable strategies.

A cheap probe: goals whose sections share code (emphasis and links both depend on the inline delimiter machinery), to see whether memory helps across goals.

## 11. What information actually transfers in an agent handoff?

**Today.** Resume continues a session from its LangGraph checkpoint in Atlas, optionally on a different agent (Claude ↔ Codex). Code state lives in git, and the last clean commit is restored on resume.

**Open.** What's the minimum sufficient handoff state?

```text
checkpoint + git + attempt ledger + memory     or     git + database, no LangGraph state?
```

Is agent identity genuinely irrelevant? Test the full matrix at the same point in a run:

```text
Claude → Claude     Claude → Codex     Codex → Claude     Codex → Codex
```

If performance drops sharply after a cross-agent handoff, some state is living implicitly in the agent's context rather than in the architecture. Caveat: Claude and Codex run different models, so the cross-agent cells compare models as well as state transfer.

## 12. Ambiguities found while building

Smaller questions that came up while building the data layer and wiring Codex. Each is currently answered by a choice in the code, not by evidence.

- **Outcome vocabulary.** The controller writes `improved` / `no_improvement` / `flagged` / `killed`. The brief treats anything but `improved` and `flagged` as a failed attempt, so a no-improvement attempt that scored well is shown as a failure. Is "didn't beat the best" the same as "failed"?
- **What H-mem keeps.** H-mem drops ledger retrieval, memory summaries, and skills, but keeps the best attempt so far and active rules. The master plan says "no ledger retrieval or memory summaries". Is the best attempt ledger retrieval?
- **Skills are per session.** Skills are only visible within the session that distilled them, so H and H-mem (running at the same time) and old test runs can't leak into each other. The SOW frames the skills library as growing "from the session's own experience", but cross-run reuse is arguably the point of a skills library. Which do we claim?
- **When to compact.** The compactor runs whenever a goal neither completes nor replans, and waits until 3 new attempts pile up. The work split says "on goal completion or when the ledger gets long". The last attempts of a completed goal are never compacted.
- **Replans don't change priority.** A replanned goal keeps its priority and is worked next with the new strategy. The master plan says "update goal strategy / priority". Should a stalled goal ever be deprioritized in favour of others?
- **Replan notes and retrieval.** The strategy is shown to the planner but kept out of every search query, so retrieval keeps matching the section. After a real strategy change, should retrieval look for attempts like the new strategy instead?
- **The context-size metric** is `characters / 4`, not a tokenizer count.
- **Retrieval eval validity.** The four-arm recall numbers so far come from 30 hand-written attempts. The Automated Embedding arm stores int8-quantized vectors while ours are float32, so "direct vs. automated" also compares precision. The query is the attempt's intent, while the brief queries with the section name.
- **Codex on OpenRouter.** Through OpenRouter, Codex's edits arrive as `Bash` running `apply_patch`, not the edit tool, so the observer checks them as shell commands (it still blocks protected targets and forbidden imports). Codex also has no model metadata for `openai/gpt-5.3-codex` and falls back to defaults, which may affect long attempts. Does a Codex handoff via OpenRouter count as the same experiment as one via a ChatGPT login?
- **The observer token is in the agent's environment.** The hook shim needs it, and hooks inherit the agent's environment, so an agent could post its own events to the observer. They couldn't approve its own tool calls, but they could pollute the audit log, the replay eval, and the rule learner's input. Is that acceptable?
- **Is `last_checkpoint_at` a heartbeat yet?** Sessions are now recorded (`data/sessions.py`, called from the CLI): run name, agent, status, progress. Nothing watches `last_checkpoint_at` yet, so a hung attempt is only caught by the per-attempt timebox. Do we need the heartbeat watchdog from the SOW?
- **Skill distillation is a stretch goal** and first in the cut order, but it's now built. Do we show it in the demo or leave it off?

---

## Priorities

If time is short, these five matter most:

1. **Does memory cause better decisions?** Not "does retrieval find similar things", but "does retrieved information measurably change strategy and improve the next attempt?" (Q1)
2. **Can replanning detect strategy failure?** Move from `N failures → replan` towards `diminishing expected return → replan`. (Q3)
3. **Can learned observer rules evolve safely?** The rule learner is potentially the most novel component, and the most dangerous feedback loop. (Q6)
4. **Does the harness beat a retry loop for the right reason?** Without ablations the conclusion could just be "more attempts made it better". The equal-attempt baseline comparison is the minimum. (Q8)
5. **Does the architecture generalize beyond CommonMark?** If long-horizon machinery is the thesis, the strongest evidence is more than one task type. (Q10)

## Proposal: make strategy a first-class object

The architecture has excellent attempt-level observability. The next conceptual step is making the **strategy** a durable, measurable object, rather than an implicit property of a run of intents:

```text
                    GOAL
                     │
                     ▼
                 STRATEGY
                     │
             ┌───────┴───────┐
             ▼               ▼
         attempt 1        attempt 2
             │               │
             └───────┬───────┘
                     ▼
               STRATEGY REVIEW
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
       continue    modify     abandon
          └──────────┴──────────┘
                     │
                     ▼
                   GOAL
```

A `strategies` collection (hypothesis, goal, attempts, evidence, verdict, reason) would let MongoDB remember not just what happened, but **which hypotheses we tried, what evidence they produced, and why the controller changed its mind**. It would give questions 2, 3, and 7 a natural home: retrieval by strategy, replanning on strategy-level evidence, and negative memory as "strategies abandoned, and why". That's substantially more than a persistent agent plus a vector database.

The seed exists already: `goals.strategy_notes` holds the current strategy after a replan, and the planner follows it.
