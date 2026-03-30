<!-- AUTO-GENERATED — do not edit by hand. Updated weekly by GitHub Actions. -->
<!-- Source: src/llm_router/data/benchmarks.json · Generator: scripts/update_benchmarks.py -->

# Model Benchmarks

> 🔄 **Auto-updated every Monday** by [GitHub Actions](../.github/workflows/benchmarks.yml)
> 📅 Last updated: **2026-03-30** &nbsp;·&nbsp; Version **1**
> 📊 Sources: Arena Hard Auto · Aider Edit Leaderboard · HuggingFace Open LLM v2 · LiteLLM Pricing

The router uses these benchmarks to dynamically reorder model chains so the best-performing
model for each task type is tried first. Updated data is distributed automatically to all
users on the next `pip install --upgrade claude-code-llm-router` + server restart.

---

## Tier System

| Tier | Badge | Description | Routing Profile |
|------|-------|-------------|-----------------|
| Premium | 🥇 | Top 3 models per task — highest quality, higher cost | `QUALITY_MODE=best` |
| Balanced | 🥈 | Next 4 models — strong quality/cost ratio | `QUALITY_MODE=balanced` *(default)* |
| Budget | 🥉 | Remaining models — maximum cost savings | `QUALITY_MODE=conserve` |

---

## Rankings by Task Type

### 💻 Code Generation & Refactoring

*Weights: Aider pass rate 50% · Arena Hard 30% · Cost 20%*

| Rank | Tier | Model | Score | Aider Pass Rate | Cost Input/1M |
|------|------|-------|-------|-----------------|---------------|
| 1 | 🥇 | openai/o3 | 0.94 | 79% | $10.00 |
| 2 | 🥇 | anthropic/claude-opus-4-6 | 0.88 | 72% | $15.00 |
| 3 | 🥈 | anthropic/claude-sonnet-4-6 | 0.79 | 64% | $3.00 |
| 4 | 🥈 | openai/gpt-4o | 0.72 | 57% | $2.50 |
| 5 | 🥈 | gemini/gemini-2.5-pro | 0.75 | 61% | $1.25 |
| 6 | 🥉 | deepseek/deepseek-reasoner | 0.65 | 58% | $0.55 |
| 7 | 🥉 | deepseek/deepseek-chat | 0.55 | 49% | $0.14 |
| 8 | 🥉 | gemini/gemini-2.5-flash | 0.48 | 41% | $0.075 |
| 9 | 🥉 | groq/llama-3.3-70b-versatile | 0.42 | 38% | $0.59 |
| 10 | 🥉 | anthropic/claude-haiku-4-5-20251001 | 0.38 | 33% | $0.80 |
| 11 | 🥉 | openai/gpt-4o-mini | 0.35 | 30% | $0.15 |

---

### 🔍 Analysis & Debugging

*Weights: Arena Hard 40% · HuggingFace 40% · Cost 20%*

| Rank | Tier | Model | Score | Arena Hard | HF Score | Cost Input/1M |
|------|------|-------|-------|------------|----------|---------------|
| 1 | 🥇 | openai/o3 | 0.93 | — | 0.91 | $10.00 |
| 2 | 🥇 | anthropic/claude-opus-4-6 | 0.89 | — | 0.88 | $15.00 |
| 3 | 🥇 | deepseek/deepseek-reasoner | 0.72 | — | 0.83 | $0.55 |
| 4 | 🥈 | gemini/gemini-2.5-pro | 0.85 | — | 0.85 | $1.25 |
| 5 | 🥈 | anthropic/claude-sonnet-4-6 | 0.81 | — | 0.82 | $3.00 |
| 6 | 🥈 | openai/gpt-4o | 0.78 | — | 0.80 | $2.50 |
| 7 | 🥉 | xai/grok-3 | 0.83 | — | 0.84 | $3.00 |
| 8 | 🥉 | mistral/mistral-large-latest | 0.60 | — | 0.74 | $2.00 |
| 9 | 🥉 | deepseek/deepseek-chat | 0.58 | — | 0.75 | $0.14 |
| 10 | 🥉 | gemini/gemini-2.5-flash | 0.50 | — | 0.72 | $0.075 |
| 11 | 🥉 | groq/llama-3.3-70b-versatile | 0.45 | — | 0.71 | $0.59 |
| 12 | 🥉 | anthropic/claude-haiku-4-5-20251001 | 0.38 | — | 0.65 | $0.80 |
| 13 | 🥉 | openai/gpt-4o-mini | 0.36 | — | 0.68 | $0.15 |

---

### ❓ Query & Factual Lookup

*Weights: Arena Hard 40% · HuggingFace 40% · Cost 20%*

| Rank | Tier | Model | Score | Cost Input/1M |
|------|------|-------|-------|---------------|
| 1 | 🥇 | openai/o3 | 0.90 | $10.00 |
| 2 | 🥇 | gemini/gemini-2.5-pro | 0.87 | $1.25 |
| 3 | 🥇 | xai/grok-3 | 0.84 | $3.00 |
| 4 | 🥈 | anthropic/claude-opus-4-6 | 0.83 | $15.00 |
| 5 | 🥈 | anthropic/claude-sonnet-4-6 | 0.80 | $3.00 |
| 6 | 🥈 | openai/gpt-4o | 0.77 | $2.50 |
| 7 | 🥉 | deepseek/deepseek-reasoner | 0.68 | $0.55 |
| 8 | 🥉 | deepseek/deepseek-chat | 0.60 | $0.14 |
| 9 | 🥉 | gemini/gemini-2.5-flash | 0.55 | $0.075 |
| 10 | 🥉 | groq/llama-3.3-70b-versatile | 0.48 | $0.59 |
| 11 | 🥉 | anthropic/claude-haiku-4-5-20251001 | 0.40 | $0.80 |
| 12 | 🥉 | openai/gpt-4o-mini | 0.38 | $0.15 |

---

### ✍️ Content Generation & Writing

*Weights: Arena Hard 50% · HuggingFace 30% · Cost 20%*

| Rank | Tier | Model | Score | Cost Input/1M |
|------|------|-------|-------|---------------|
| 1 | 🥇 | anthropic/claude-opus-4-6 | 0.91 | $15.00 |
| 2 | 🥇 | gemini/gemini-2.5-pro | 0.86 | $1.25 |
| 3 | 🥇 | openai/o3 | 0.80 | $10.00 |
| 4 | 🥈 | anthropic/claude-sonnet-4-6 | 0.83 | $3.00 |
| 5 | 🥈 | openai/gpt-4o | 0.78 | $2.50 |
| 6 | 🥈 | cohere/command-r-plus | 0.65 | $2.50 |
| 7 | 🥉 | xai/grok-3 | 0.75 | $3.00 |
| 8 | 🥉 | deepseek/deepseek-chat | 0.58 | $0.14 |
| 9 | 🥉 | gemini/gemini-2.5-flash | 0.52 | $0.075 |
| 10 | 🥉 | mistral/mistral-large-latest | 0.50 | $2.00 |
| 11 | 🥉 | groq/llama-3.3-70b-versatile | 0.44 | $0.59 |
| 12 | 🥉 | anthropic/claude-haiku-4-5-20251001 | 0.38 | $0.80 |
| 13 | 🥉 | openai/gpt-4o-mini | 0.36 | $0.15 |

---

### 🔎 Research & Web Search

*Weights: Arena Hard 60% · Cost 40%*

| Rank | Tier | Model | Score | Cost Input/1M |
|------|------|-------|-------|---------------|
| 1 | 🥇 | perplexity/sonar-pro | 0.95 | $3.00 |
| 2 | 🥇 | perplexity/sonar | 0.82 | $1.00 |
| 3 | 🥇 | openai/o3 | 0.72 | $10.00 |
| 4 | 🥈 | gemini/gemini-2.5-pro | 0.75 | $1.25 |
| 5 | 🥈 | openai/gpt-4o | 0.68 | $2.50 |
| 6 | 🥉 | gemini/gemini-2.5-flash | — | $0.075 |
| 7 | 🥉 | anthropic/claude-haiku-4-5-20251001 | 0.35 | $0.80 |
| 8 | 🥉 | openai/gpt-4o-mini | 0.32 | $0.15 |

---

## Raw Scores

Full data behind the composite rankings above.

| Model | Arena Hard Score | Aider Pass Rate | HF Avg | Cost In/1M | Cost Out/1M |
|-------|-----------------|-----------------|--------|------------|-------------|
| anthropic/claude-opus-4-6 | 1350 | 72% | 0.88 | $15.00 | $75.00 |
| anthropic/claude-sonnet-4-6 | 1290 | 64% | 0.82 | $3.00 | $15.00 |
| anthropic/claude-haiku-4-5-20251001 | 1150 | 33% | 0.65 | $0.80 | $4.00 |
| openai/o3 | 1380 | 79% | 0.91 | $10.00 | $40.00 |
| openai/gpt-4o | 1260 | 57% | 0.80 | $2.50 | $10.00 |
| openai/gpt-4o-mini | 1160 | 30% | 0.68 | $0.15 | $0.60 |
| gemini/gemini-2.5-pro | 1300 | 61% | 0.85 | $1.25 | $5.00 |
| gemini/gemini-2.5-flash | 1180 | 41% | 0.72 | $0.075 | $0.30 |
| deepseek/deepseek-chat | 1220 | 49% | 0.75 | $0.14 | $0.28 |
| deepseek/deepseek-reasoner | 1280 | 58% | 0.83 | $0.55 | $2.19 |
| groq/llama-3.3-70b-versatile | 1200 | 38% | 0.71 | $0.59 | $0.79 |
| xai/grok-3 | 1310 | 60% | 0.84 | $3.00 | $15.00 |
| mistral/mistral-large-latest | 1210 | 42% | 0.74 | $2.00 | $6.00 |
| cohere/command-r-plus | 1190 | 35% | 0.70 | $2.50 | $10.00 |
| perplexity/sonar-pro | 1230 | — | — | $3.00 | $15.00 |
| perplexity/sonar | 1170 | — | — | $1.00 | $1.00 |

*Arena Hard scores are win-rates (0–100) against GPT-4. HF Avg is mean of IFEval + MMLU-Pro + MATH Lvl 5 + BBH (0–1). Aider pass rate is the whole-file edit benchmark (0–1).*

---

## Scoring Weights

Each task type uses a different weighting formula to compute composite scores:

| Task Type | Arena Hard | Aider | HF Score | Cost (inverted) |
|-----------|-----------|-------|----------|-----------------|
| 💻 code | 30% | **50%** | 0% | 20% |
| 🔍 analyze | 40% | 0% | **40%** | 20% |
| ❓ query | 40% | 0% | **40%** | 20% |
| ✍️ generate | **50%** | 0% | 30% | 20% |
| 🔎 research | **60%** | 0% | 0% | **40%** |

*Cost inverted* means cheaper models score higher on the cost dimension — it's a savings signal, not a quality one. When sources are missing for a model, weights are redistributed proportionally across available signals.

---

## Data Sources

| Source | URL | What it measures | Update frequency |
|--------|-----|-----------------|------------------|
| **Arena Hard Auto** | [lm-sys/arena-hard-auto](https://github.com/lm-sys/arena-hard-auto) | Win-rate vs GPT-4 (general quality) | Irregular |
| **Aider Edit Leaderboard** | [Aider-AI/aider](https://github.com/Aider-AI/aider/blob/main/aider/website/_data/edit_leaderboard.yml) | Code editing pass rate | Continuous |
| **HuggingFace Open LLM v2** | [open-llm-leaderboard](https://huggingface.co/datasets/open-llm-leaderboard/contents) | IFEval, MMLU-Pro, MATH, BBH | Irregular |
| **LiteLLM Pricing** | Bundled in `litellm` package | Input/output cost per 1M tokens | Per `pip upgrade` |

---

## Update Mechanism

```
Every Monday 06:00 UTC
    GitHub Actions runs scripts/update_benchmarks.py
        ├── Fetches Arena Hard CSV
        ├── Fetches Aider YAML
        ├── Fetches HuggingFace JSON (5×100 rows)
        ├── Reads LiteLLM pricing table
        ├── Computes weighted composite scores per task type
        ├── Assigns premium / balanced / budget tiers
        ├── Writes src/llm_router/data/benchmarks.json  ← used by router
        └── Writes docs/BENCHMARKS.md                   ← this file
    Opens PR with both files
    Merged PR → next pip upgrade distributes to all users
```

On MCP server startup, `check_and_update_benchmarks()` compares the bundled JSON version
with `~/.llm-router/benchmarks.json` and overwrites if the bundled copy is newer.
