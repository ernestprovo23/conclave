# conclave — Product Design Document

> **Status:** v0.1.0 shipped and working. This is the **canonical authority document** for
> conclave's product scope, design, and roadmap. When this document and any other doc
> disagree, this document wins. Code is the source of truth for *current behavior*; this
> document marks anything not yet in code as **Roadmap**.

- **Repo:** `/Users/ernestprovo/dev/conclave/`
- **License:** MIT
- **Author:** Data Science & Engineering Experts, Inc. (DSE)
- **Last updated:** 2026-06-06

---

## 1. Problem & Vision

### Problem
Single foundation models are confidently wrong in ways that are hard to detect from inside
a single model. Different models have different training data, failure modes, and blind
spots. Today, if you want a "second opinion" you either:

- paste the same prompt into 3-5 web UIs by hand and eyeball the differences, or
- adopt a heavyweight multi-agent framework (LangGraph, AutoGen) and write orchestration
  graph code, accept a runtime, and learn its abstractions, or
- route everything through a hosted aggregator that takes a margin on your tokens and sees
  your prompts.

None of these is a lightweight, scriptable, **own-your-keys** primitive for "ask N models
the same thing and reconcile the answers."

### Vision
conclave is a small, sharp tool: **a council of foundation models you can call from one
CLI command or one Python import.** Fan a prompt out to several models concurrently — each
through *your own* API keys, no markup, no middleman — and aggregate the answers. The
v0.1 aggregation is a **synthesizer** that merges raw answers into one consolidated
response. The longer arc is a small set of **council modes** (debate, adversarial, vote)
that turn a flat panel of opinions into a structured deliberation.

conclave's first real use was an **adversarial design review**: a council of Grok, Gemini,
Perplexity, and Claude critiquing a security-tool strategy and catching flaws a single
model missed. That origin is why adversarial/debate modes are first-class in the vision,
not a bolt-on. The product is opinionated about staying lightweight: a **library-first
primitive with structured results**, not an agent framework.

---

## 2. Target Users & Personas

| Persona | Who | What they want from conclave |
|---------|-----|------------------------------|
| **The skeptical engineer** | Senior dev / architect making a consequential technical call | A fast second/third opinion across models, with raw per-model answers visible so they can judge disagreement themselves. Uses the CLI ad hoc. |
| **The library integrator** | Developer building a tool that needs multi-model input at *design/eval time* | `from conclave import Council`, structured `CouncilResult` (latency, token usage, per-model errors), partial-failure resilience. The primary downstream example is **mcp-warden** (see §10). |
| **The researcher / evaluator** | Someone comparing model behavior on a prompt set | Deterministic structure around answers, JSON output (`--json`) for downstream analysis, per-model latency and token accounting. |
| **The cost-conscious power user** | Heavy LLM user who already pays each provider directly | BYO-keys with **no markup** and **no third party seeing the prompt**. conclave is a thin local orchestrator over the user's own accounts. |

Non-personas (explicitly *not* who we build for): teams wanting a hosted multi-agent SaaS,
or anyone needing a deterministic runtime adjudicator (see Non-Goals, §8, and the
mcp-warden boundary, §10).

---

## 3. BYO-keys Model & Key-Handling Security

conclave is **bring-your-own-keys** by design. This is both a positioning choice (no
markup, no middleman) and a security property.

**Key-handling invariants (enforced in code today):**

1. **Keys are referenced by env-var NAME only, never by value.** The provider registry
   (`registry.py`) maps each LiteLLM provider prefix to the env var(s) that satisfy it
   (e.g. `xai → ["XAI_API_KEY"]`, `gemini → ["GEMINI_API_KEY", "GOOGLE_API_KEY"]`). The
   functions `key_present()` and `key_source()` answer *"is a key set?"* and *"which
   variable name holds it?"* — they **never read, return, or log the value.**
2. **conclave never stores keys.** Config (`~/.conclave/config.yml`) references providers
   by friendly name and model id only. There is no field in `ConclaveConfig` that can hold
   a secret. The example config in `config.py` is keys-free by construction.
3. **LiteLLM resolves the actual key from the environment at call time** (`providers.py`).
   conclave hands LiteLLM a model id and messages; LiteLLM reads the relevant env var
   itself. The key value never transits a conclave data structure.
4. **Secrets never reach serialized output.** `CouncilResult.model_dump()` (used by
   `--json`) contains prompts, answers, model ids, latency, token usage, and error strings
   — no key material. The `providers` CLI command shows a check/cross and the env-var
   *name*, never the value.
5. **Missing keys degrade gracefully, they don't crash.** A requested member whose key is
   absent is skipped with a warning and recorded in `CouncilResult.skipped`. Unknown
   providers (no static env-var mapping) are *not* pre-emptively skipped — the live call is
   attempted and any auth error is captured as a `ModelAnswer.error`.

**Residual considerations (worth a user's awareness):** error strings captured from a
provider are surfaced verbatim. In the unlikely event a provider SDK echoes a key fragment
into an exception message, that string would appear in `ModelAnswer.error`. We consider
this low risk (LiteLLM/provider SDKs do not echo full keys) but it is the one path where
provider-originated text is passed through unfiltered. Tracked as a hardening item in §9.

---

## 4. Council Modes & Consensus Algorithms

A **council mode** is the algorithm used to turn N independent model calls into a single
useful output. v0.1 ships one true mode plus a pass-through.

| Mode | Status | What it does |
|------|--------|--------------|
| **synthesize** | **BUILT (v0.1)** | Fan out concurrently → collect each raw answer → a **synthesizer model** merges them into one consolidated answer, reconciling agreement, adjudicating disagreement, and flagging clearly-wrong answers. The synthesizer is instructed to rely only on the provided answers and not invent a model's position. |
| **raw** | **BUILT (v0.1)** | Fan out and return every member's raw answer with no synthesis. Not a deliberation mode — it is "synthesize off." Exposed as `--mode raw` / `ask(..., synthesize=False)`. |
| **debate** | **ROADMAP (v0.2+)** | N rounds where each model sees the other models' prior-round answers and may revise. Converges (or surfaces durable disagreement) over rounds. |
| **adversarial** | **ROADMAP (v0.2+)** | Structured propose → refute → verdict. One or more models propose, others attack, a verdict model adjudicates. This is the mode that conclave's origin story (the security design review) exercised by hand. |
| **vote** | **ROADMAP (v0.2+)** | Structured majority. Each model answers a constrained question; conclave tallies a structured vote and reports the majority plus the split. |

### Synthesize algorithm (as built)
1. Resolve requested friendly names to LiteLLM model ids via config.
2. Partition members into *available* (key present) and *skipped* (no key).
3. Fan out the prompt to all available members concurrently (`asyncio.gather`,
   `return_exceptions=True` as a belt-and-suspenders guard).
4. Each call returns a structured `ModelAnswer` (answer **or** error — `call_model` never
   raises for provider failures), so partial results always survive.
5. If `synthesize=True` and at least one member succeeded, build a synthesis prompt that
   embeds the original prompt and each successful answer, and call the synthesizer model.
6. If the synthesizer has no key, or no member succeeded, set `synthesis_error` and return
   raw answers only. A run with zero available members returns an empty-answer result
   rather than raising.

**Consensus note:** synthesize is a *generative* reconciliation, not a deterministic vote.
It is inherently stochastic. This matters for the mcp-warden boundary in §10.

---

## 5. Provider Support Matrix

Friendly names, default LiteLLM model ids, and the env var(s) that satisfy each. Defaults
live in `registry.DEFAULT_MODELS` / `registry.PROVIDER_ENV_VARS` and are overridable via
`~/.conclave/config.yml`.

| Provider | Friendly name | Default model id | Env var(s) (first present wins) | Status |
|----------|---------------|------------------|---------------------------------|--------|
| xAI | `grok` | `xai/grok-4.3` | `XAI_API_KEY` | BUILT |
| Google | `gemini` | `gemini/gemini-2.5-pro` | `GEMINI_API_KEY`, `GOOGLE_API_KEY` | BUILT |
| Anthropic | `claude` | `anthropic/claude-sonnet-4-6` | `ANTHROPIC_API_KEY` | BUILT |
| Perplexity | `perplexity` | `perplexity/sonar-pro` | `PERPLEXITY_API_KEY` | BUILT |
| OpenAI | `openai` | `openai/gpt-4.1` | `OPENAI_API_KEY` | BUILT |
| *(any LiteLLM-supported provider)* | *raw id as name* | *passed through verbatim* | *LiteLLM's own env handling* | SUPPORTED (untyped) |

**Default synthesizer:** `claude`. **Default council** (when none is configured): all five
known providers. Because `resolve_model_id()` passes unknown names through verbatim, a user
can already add a council member by raw LiteLLM id (e.g. `openai/gpt-4o`) without a code
change; it just won't have a static key-presence check (treated as "attempt and catch").

Expanding the *first-class* provider list (more friendly-name defaults) is Roadmap, §9.

---

## 6. Architecture

conclave is a thin, layered orchestrator over LiteLLM. Each module has one job; the data
models are the stable contract between layers and downstream consumers.

```
CLI (cli.py, typer+rich)   Library (from conclave import Council)
            \                         /
             v                       v
                 Council (council.py)
        fan-out · skip-no-key · partial-results · synthesis
                          |
              call_model (providers.py)
        one async path · latency · token usage · error capture
                          |
                  LiteLLM acompletion
                          |
        xai · gemini · anthropic · perplexity · openai · (any LiteLLM provider)
```

**Module responsibilities (ground truth):**

| Module | Responsibility |
|--------|----------------|
| `council.py` | `Council` — primary importable entry point. Resolves names, partitions available/skipped members, fans out concurrently, collects partial results, runs the synthesizer. Sync wrapper (`ask_sync`) guards against being called inside a running event loop. |
| `providers.py` | `call_model` — the single async call path over `litellm.acompletion`. Captures latency, token usage, and any error into a `ModelAnswer`; never raises for provider-side failures. Sets `litellm.drop_params = True` and `litellm.telemetry = False`. |
| `registry.py` | Single source of truth for friendly-name → model-id defaults and provider → env-var mapping. Key *presence* logic only — never key values. |
| `config.py` | Loads/merges `~/.conclave/config.yml` over built-in defaults (`CONCLAVE_CONFIG` env var overrides path). Resolves model ids and named/CSV councils. Keys-free by construction. |
| `models.py` | Pydantic contract: `TokenUsage`, `ModelAnswer`, `CouncilResult` (+ `successful_answers`/`failed_answers`/`ok` helpers). The stable importable surface for downstream consumers. |
| `cli.py` | `conclave ask` and `conclave providers`. Rich panels for humans, `--json` for machines. Never prints key values. |
| `logging.py` | One logger factory, stderr, verbosity via `CONCLAVE_LOG_LEVEL` (default `WARNING`). |

**Key design properties:**
- **Library-first.** The CLI is a thin shell over the same `Council` any consumer imports.
- **Partial-failure resilience is structural,** not optional: failures become data
  (`ModelAnswer.error`), never exceptions that abort the run.
- **Structured results.** Every run yields per-model latency, token usage, and error
  capture — the differentiator vs. text-only multi-model tools.
- **Stable contract.** `models.py` field names are intentionally stable for downstream
  consumers (e.g. mcp-warden) to depend on.

**Stack:** Python 3.11+, LiteLLM (provider abstraction), `asyncio` (concurrency),
Pydantic v2 (config + results), Typer + Rich (CLI), PyYAML (config). Packaged with
hatchling; console script `conclave = conclave.cli:app`.

---

## 7. v0.1 Scope

**In scope and shipped:**
- `synthesize` and `raw` modes (fan-out, partial results, synthesizer merge).
- 5 first-class providers + pass-through for any LiteLLM model id.
- BYO-keys via env-var name only; graceful skip of missing-key members.
- Concurrent fan-out with per-call timeout and temperature.
- Structured `CouncilResult` with latency, token usage, per-model error capture.
- CLI (`ask`, `providers`) with human and `--json` output.
- Config file: named models, named councils, default synthesizer.
- Importable library API with sync and async entry points.
- Test suite that mocks LiteLLM (no network, no keys required).

---

## 8. Non-Goals (v0.1, and some permanent)

- **Not a runtime adjudicator.** conclave is stochastic; it must not be used as a
  deterministic decision gate. (See mcp-warden boundary, §10.) This is a **permanent**
  non-goal for the synthesize/debate/adversarial modes.
- **Not an agent framework.** No tool-calling graphs, no long-running stateful agents, no
  orchestration DSL. We compete by being *small*. (Permanent.)
- **Not a key manager / secrets vault.** conclave reads env vars; it does not provision,
  rotate, store, or proxy keys. (Permanent.)
- **No hosted/proxied token path.** No conclave-operated endpoint that sees user prompts or
  takes a margin. BYO-keys, direct-to-provider, always. (Permanent.)
- **No persistence/caching of results** in v0.1 (caching is Roadmap, §9).
- **No streaming** in v0.1 (Roadmap, §9).
- **No server mode** in v0.1 (possible Roadmap, §9).
- **No debate/adversarial/vote modes** in v0.1 (Roadmap, §9 — flagged for build, not for
  removal).

---

## 9. Roadmap (v0.2+, NOT yet built)

Ordered roughly by strategic value to the origin use case and to mcp-warden.

1. **`adversarial` mode** — propose → refute → verdict. Directly productizes conclave's
   first real use (security design review). High priority.
2. **`debate` mode** — N rounds, members see each other's prior answers. Configurable
   round count and convergence/stop criteria.
3. **`vote` mode** — structured majority with reported split. Needs a constrained
   answer schema so votes are comparable.
4. **More first-class providers** — additional friendly-name defaults (e.g. more OpenAI,
   Anthropic, Google, and open-weights endpoints LiteLLM already supports).
5. **Caching** — optional result cache keyed on (prompt, council, mode, model ids) to make
   repeated/eval runs cheap. Must remain off by default and never persist keys.
6. **Streaming** — stream member answers and/or the synthesis to the terminal/library.
7. **Local HTTP/server mode (under evaluation)** — a *local* server for convenience only;
   must not become a hosted token path or violate the no-middleman non-goal.
8. **Key-leak hardening** — scrub/limit provider-originated error strings before they land
   in `ModelAnswer.error` (residual risk noted in §3).

**Roadmap discipline:** items are added and reprioritized freely; items are not *removed*
on the strength of a single data point — flag for discussion first.

---

## 10. Downstream Boundary: conclave ↔ mcp-warden

**mcp-warden** is a sibling project: an MCP-server security "integrity gateway." It will
**import conclave as a DEV-TIME dependency only** — for things like adversarial design
review of warden's own strategy and taxonomy/label brainstorming during development.

**mcp-warden will NOT use conclave as a RUNTIME dependency.** Security findings require
**determinism and reproducibility**; a stochastic council is the wrong tool for runtime
adjudication of security events. The same property that makes conclave valuable for design
review (diverse, generative, multi-model reconciliation) makes it unsuitable as a runtime
gate. This boundary is deliberate and load-bearing:

| | conclave (this project) | mcp-warden runtime |
|---|---|---|
| Nature | Stochastic, generative, multi-model | Deterministic, reproducible |
| Right use | Design review, eval, taxonomy labeling (dev time) | Runtime security adjudication |
| Dependency direction | — | imports conclave **at dev time only** |

If you find yourself wanting conclave inside mcp-warden's runtime decision path, that is a
design smell — re-read this section.

---

## 11. Licensing & Positioning

**License:** MIT (`pyproject.toml`). Permissive on purpose: conclave is meant to be a
small primitive others embed (starting with mcp-warden).

**Positioning vs. prior art:**

| | conclave | Simon Willison `llm` + `llm-consortium` | LangGraph / AutoGen |
|---|---|---|---|
| Primary surface | **Library-first** (CLI is a thin shell) | CLI/plugin first | Framework/runtime first |
| Result shape | **Structured** (`CouncilResult`: per-model latency, token usage, error capture) | Mostly text-oriented | Rich but framework-coupled |
| Failure model | **Partial-failure resilient by construction** (failures become data) | Plugin-dependent | App must handle |
| Keys | **BYO, env-var name only, never stored/logged** | BYO via `llm` key store | BYO, app-managed |
| Weight | **Intentionally lightweight** — no agent runtime | Lightweight, plugin ecosystem | Heavyweight agent frameworks |
| Modes | synthesize/raw now; **debate/adversarial/vote** planned | consortium (iterate-to-consensus) | arbitrary graphs you author |

**Where we are distinct:** conclave is the **library-first, structured-result,
partial-failure-resilient** option with a **planned deliberation-mode set**
(debate/adversarial/vote) and a strict **name-only BYO-keys** posture. We are not trying to
beat LangGraph/AutoGen at general agent orchestration — we are deliberately the small,
embeddable council primitive. Where `llm-consortium` overlaps conceptually (iterate-to-
consensus), conclave differentiates on the structured result contract, the resilience
model, and the explicit adversarial/debate roadmap rooted in the security-review origin.

---

## 12. Open Product Questions

These need a decision and are surfaced for discussion, not resolved here:

1. **Synthesizer-in-council policy.** Should the default synthesizer (`claude`) be allowed
   to also be a council member in the same run, or excluded to avoid self-reinforcement?
2. **`vote` answer schema.** Does `vote` require a constrained/structured answer format
   (and therefore a prompt contract), or do we tally free-text answers post hoc?
3. **Per-member model/temperature overrides.** Today temperature and timeout are
   council-wide. Do we want per-member overrides (and where does that live — config vs.
   call args)?
4. **Server mode scope.** If a local HTTP mode ships, how do we keep it from drifting into
   a hosted token path that violates the no-middleman non-goal?
5. **First-class provider expansion criteria.** What is the bar for promoting a raw
   pass-through model to a friendly-name default with a key-presence mapping?
