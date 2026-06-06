# conclave — System Context Diagram

This is one of conclave's three core docs (per the 3-Core Documentation Rule). It shows
the system context: how a user (or a downstream consumer) drives conclave, how config and
environment-variable keys feed in, how requests reach the five providers through LiteLLM,
and where the sibling **mcp-warden** project sits as a **dev-time** consumer.

> Authority note: behavioral details here are descriptive. The canonical spec is
> [`docs/PRODUCT_DESIGN_DOCUMENT.md`](docs/PRODUCT_DESIGN_DOCUMENT.md).

---

## System context

```mermaid
flowchart TB
    %% ---- Actors / entry points ----
    user["Engineer / power user"]
    warden["mcp-warden<br/>(sibling project)<br/>DEV-TIME consumer only"]

    %% ---- Inputs ----
    subgraph inputs["Inputs (never contain key VALUES)"]
        cfg["~/.conclave/config.yml<br/>models · councils · synthesizer<br/>(provider names only)"]
        env["Environment variables<br/>XAI_API_KEY · GEMINI_API_KEY / GOOGLE_API_KEY<br/>ANTHROPIC_API_KEY · PERPLEXITY_API_KEY · OPENAI_API_KEY"]
    end

    %% ---- conclave ----
    subgraph conclave["conclave (MIT, Python 3.11+)"]
        cli["CLI<br/>conclave ask / providers<br/>(cli.py · typer + rich)"]
        lib["Library API<br/>from conclave import Council<br/>(__init__.py)"]
        council["Council orchestrator<br/>fan_out · synthesize_blocks · skip-no-key<br/>synthesize / raw (council.py)"]
        modes["Deliberation modes<br/>debate (multi-round, anonymized peers)<br/>adversarial (propose to refute to verdict)<br/>(modes.py + prompts.py)"]
        registry["Registry<br/>name to model-id<br/>key PRESENCE only, never values<br/>(registry.py)"]
        config["Config loader<br/>(config.py)"]
        models["Result contract<br/>CouncilResult (mode · rounds · adversarial)<br/>ModelAnswer · TokenUsage · DebateRound<br/>AdversarialResult (models.py)"]
        provider["call_model<br/>latency · token usage · error capture<br/>never raises (providers.py)"]
    end

    %% ---- External ----
    litellm["LiteLLM<br/>acompletion<br/>resolves key from env per provider"]

    subgraph providers["Foundation model providers (BYO keys, no markup, no middleman)"]
        grok["xAI<br/>xai/grok-4.3"]
        gemini["Google<br/>gemini/gemini-2.5-pro"]
        claude["Anthropic<br/>anthropic/claude-sonnet-4-6"]
        perplexity["Perplexity<br/>perplexity/sonar-pro"]
        openai["OpenAI<br/>openai/gpt-4.1"]
    end

    %% ---- Edges: drivers ----
    user -->|"prompt + council + mode"| cli
    user -->|"import"| lib
    warden -.->|"imports at DEV time only<br/>(design review · taxonomy labeling)<br/>NOT a runtime dependency"| lib

    %% ---- Edges: inputs ----
    cfg --> config
    config --> council
    registry --> council
    env -.->|"key NAME presence check<br/>(value never read here)"| registry

    %% ---- Edges: internal ----
    cli --> council
    lib --> council
    council --> modes
    modes -->|"reuse fan_out + synthesize_blocks"| council
    council --> provider
    provider --> models
    models --> council
    council -->|"CouncilResult<br/>(no secrets)"| cli
    council -->|"CouncilResult<br/>(no secrets)"| lib

    %% ---- Edges: outbound calls ----
    provider --> litellm
    env -.->|"actual key VALUE read here<br/>by LiteLLM at call time"| litellm
    litellm --> grok
    litellm --> gemini
    litellm --> claude
    litellm --> perplexity
    litellm --> openai

    %% ---- Outputs back to actors ----
    cli -->|"rich panels or --json"| user
    lib -->|"CouncilResult"| warden
```

---

## Reading the diagram

- **Two entry points, one core.** The CLI (`cli.py`) and the library API
  (`from conclave import Council`) are both thin drivers over the same `Council`
  orchestrator. There is no behavior in the CLI that the library can't reach.
- **mcp-warden is dashed and dev-time.** The dotted edge from `mcp-warden` to the library
  is deliberate: warden imports conclave **only at design/eval time**. conclave is
  stochastic and must never sit in warden's deterministic runtime decision path. See PDD
  §10.
- **Two distinct env-var edges (the key-handling boundary).**
  - The **dotted edge from env to the registry** is a *presence check by name* — conclave
    asks "is `XAI_API_KEY` set and non-empty?" and never reads the value.
  - The **dotted edge from env to LiteLLM** is where the *actual key value* is read — by
    LiteLLM, at call time, never passing through a conclave data structure.
  This split is the core of conclave's "name-only" key posture (PDD §3).
- **Config carries no secrets.** `~/.conclave/config.yml` references providers by friendly
  name and model id only; it feeds names into the loader, never keys.
- **Results carry no secrets.** `CouncilResult` (prompt, answers, model ids, latency, token
  usage, errors) flows back to both the CLI and library consumers; it contains no key
  material, so `--json` and downstream serialization are safe.
- **Partial-failure is structural.** `call_model` converts any provider error into a
  `ModelAnswer.error` rather than raising, so one failing provider never aborts the run.
