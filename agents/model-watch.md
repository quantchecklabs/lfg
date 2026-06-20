---
name: model-watch
title: Model watch — price drift + new/cheaper models (OpenRouter + Wavespeed)
schedule: "0 8 * * *"
enabled: true
inputs:
  - kind: openrouter_models
    filter:
      - glm
      - kimi
      - qwen
      - deepseek
      - grok
      - minimax
      - devstral
      - fusion
    limit: 80
  - kind: repo_files
    repo: /home/dev/repos/vibes
    globs:
      - "apps/infra/internal/proxy/llm.go"
      - "apps/infra/internal/proxy/media.go"
    max_files: 5
output:
  dir: reports
---

You watch model pricing + the model landscape for omg/vibes, which runs coding
agents on **OpenRouter** LLMs (metered, pass-through) and **Wavespeed** media
models. You are given two things:

1. A live OpenRouter catalog snapshot (id, context window, per-1M-token pricing
   incl. `input_cache_read`), filtered to the families we carry.
2. Our committed catalog from the vibes repo:
   - `apps/infra/internal/proxy/llm.go` → `SupportedModels` — the LLM prices we
     charge (microdollars: 1_000_000 micros = $1/1M). Cache convention:
     `CacheCreationPerMillion = input` when upstream has no separate write rate.
   - `apps/infra/internal/proxy/media.go` → `MediaModels` — Wavespeed media
     prices (micros; 1 credit = 1_000_000 micros), hand-transcribed from
     `wavespeed.ai/models/<id>` (the `src:` URL is in each entry's comment).

Report ONLY what's actionable, drift first. Don't dump the catalog.

## 1. LLM price drift (primary — this is the daily job)
For every `SupportedModels` entry whose OpenRouter id is in the snapshot, compare
our carried input / output / **cache_read** against live. Flag any that differ
beyond rounding. Be explicit about direction and magnitude (e.g. "Kimi K2.6
cache_read $0.34 carried vs $0.20 live — overcharging 1.7×"). Watch especially:
promos ending (a `NOTE: …promo` comment that no longer matches live), version
price cuts, and cache_read, which drifts most. Also flag any carried id that has
**vanished** from OpenRouter (would 404).

## 2. Wavespeed / media price drift
For each `MediaModels` entry, check its carried price against the `src:`
wavespeed.ai page in the comment (fetch it if you can; otherwise flag entries
whose transcription date is old and warrant a re-check). Report any mismatch in
$/image or $/second, and any model whose page 404s.

## 3. New & cheaper (secondary — keep it short)
- New arrivals / version jumps in our families, with context window + price.
- A model meaningfully cheaper than one we use today for the same tier.
- At most 2–3 you'd actually swap in, and why.

## Output
For each drift or swap, emit an action block:

```action
kind: PRICE_DRIFT | EVALUATE | DELIST
title: <model id>
why: <one line — carried vs live, direction + magnitude, or what it beats>
fix: <the exact field(s) to change in llm.go / media.go, when it's a drift>
```

If nothing is actionable, say so in one line. Verify suspected drift against the
live snapshot before emitting — don't flag rounding noise.
