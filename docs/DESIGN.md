# yt-agent design

## Goal

An agent that runs a YouTube channel end to end: finds trending topics, produces Shorts and long videos, publishes
what a human approves, measures performance, and improves. Target: YouTube Partner Program
(1,000 subscribers plus 4,000 watch hours in 12 months, or 10M Shorts views in 90 days), then revenue growth.

## Platform constraints the design enforces

| Constraint | Design response |
|---|---|
| Inauthentic / mass-produced content policy | Every video needs an added-value angle; similarity check against past uploads; varied formats; human review |
| Altered or synthetic content disclosure | Publisher sets the synthetic-media flag on every upload with AI voice or visuals |
| Copyright | Only licensed stock (Pexels, Pixabay), generated charts/text/images, YouTube Audio Library (Epidemic Sound later) |
| API quota: 10,000 units/day, upload = 1,600 | At most ~5 uploads a day; quality gate filters before upload |
| Unverified API projects upload as locked-private | Submit the YouTube API compliance audit in Phase 0 |
| Breaking-news accuracy, ad suitability | Fact-check with sources; LLM flags sensitive / blocked topics (score 0) |

## Architecture

```
 Orchestrator (scheduler + job_runs table)
   ├─ Trend research agent ── sources → topics (pgvector merge) → LLM assessment → scored backlog
   ├─ Platform knowledge agent ── YouTube policy pages → policy_docs (versioned, embedded) → rule checks
   ├─ Content factory ── brief → script → voice → visuals → captions → edit plan → render → thumbnail
   ├─ Compliance + QA gate ── policy, copyright sources, similarity, loudness/resolution checks
   ├─ Publisher ── private upload + "Pending review" playlist → waits for approval → schedules
   └─ Analytics + learning ── metrics at 1h/24h/7d/28d → Playbook entries → feeds scoring and prompts
```

### Approval flow (YouTube Studio)

1. Agent uploads the finished video as **private** with all metadata, then adds it to the private playlist
   **Pending review** (urgent trend videos go to **Urgent review**).
2. You review in YouTube Studio (YouTube's own copyright and ad-suitability checks run on private uploads too).
3. You signal the decision:
   - add to the **Approved** playlist: agent schedules at the best time and does all post-publish work;
   - set it Public or Scheduled yourself: agent keeps your timing;
   - edit title/description/thumbnail first: agent keeps your version and records the edit as a lesson;
   - add to the **Rejected** playlist: agent never publishes it and records why. The agent never deletes videos.
4. The agent polls the playlists every ~10 minutes (1 quota unit per call).
5. Urgent videos expire if not reviewed in time. Optional ping via ntfy or email.

## Stack (free now, upgrade later)

Every provider sits behind an adapter; LLMs go through LiteLLM, so upgrades are config changes.

| Layer | Now | Later |
|---|---|---|
| LLM | Ollama: `qwen3.5:4b` (fast role), `qwen3.5:9b` (writer role) | Claude Sonnet/Opus for scripts and hooks |
| Embeddings | `bge-m3` (multilingual, 1024-d) | Any embedding API (re-embed migration) |
| Workflow | APScheduler + `job_runs` table | Prefect / Temporal |
| Web research | SearXNG (Docker) + trafilatura | Brave / Tavily search API |
| Trend sources | Google Trends RSS, Google News RSS, Wikipedia pageviews, Hacker News, YouTube chart, RSS feeds | X API, paid trend tools |
| Voice | Kokoro-82M (English), Indic Parler-TTS / Piper (Indian languages), Chatterbox | ElevenLabs |
| Captions | faster-whisper + WhisperX | Same |
| Visuals | Pexels + Pixabay, charts, maps, kinetic text | Premium image/video generation |
| Rendering | FFmpeg + MoviePy from a JSON edit plan | Motion Canvas / Remotion |
| Music | YouTube Audio Library | Epidemic Sound (channel linked) |
| Thumbnails | Pillow + rembg templates, LLM-scored variants | AI images + Studio Test & Compare |
| Storage | Postgres 18 + pgvector (Docker), media on D: | Same on a VPS / managed |
| Approvals | YouTube Studio playlists (+ optional ntfy/email) | Auto-approve trusted categories |
| Dashboard | FastAPI + React (Vite, TypeScript), local only | Same, hosted |

Licences to avoid for a monetized channel: XTTS-v2, F5-TTS weights, MusicGen, FLUX.1-dev.

### Hardware notes (current PC)

i5-1235U, 32 GB RAM, no NVIDIA GPU, so everything runs on the CPU.
Measured: `qwen3.5:4b` generates about 4 tokens/s and processes prompts at about 15-20 tokens/s.
So the agent works in batches (a topic assessment takes about 45-60 s).
Thinking mode is off for JSON tasks, and schemas go through constrained decoding instead of the prompt.
C: has only ~11 GB free; all models, Docker data and media live on D:.

## Data model (Postgres)

`topics` (merged story + score + LLM assessment + embedding), `trend_signals` (one row per source observation),
`videos` (script, edit plan, YouTube id, status), `video_metrics`, `policy_docs`, `playbook_entries`, `job_runs`.

Topic score (0-100) = weighted traffic, cross-source corroboration, freshness, and LLM fit
(niche fit × ad safety). Weights are set in `config/settings.yaml`. Sensitive or blocked topics score 0.

## Roadmap

| Phase | Deliverable | Done when |
|---|---|---|
| 0 | Setup, DB, local models, trend research agent, Google Cloud project + API audit submitted | `ytagent doctor` all green; scored topic backlog fills itself |
| 1 | Brief → script → TTS → Shorts render → private upload → playlist approval → scheduled publish | 1-2 Shorts/day for ~5 min of your time |
| 2 | Analytics pulls, Playbook learning, FastAPI + React dashboard | Measurable week-over-week improvement |
| 3 | Long videos + Shorts cut-downs, platform knowledge agent, community replies, auto-approve | A week of operation without intervention |
| 4 | Upgrades funded by revenue (Claude API → ElevenLabs → GPU → video gen), more languages | Revenue growth |

## Open decisions

- Channel niche(s) and language (drive topic fit, voice model, script language).
- Whether to allow free hosted API tiers (Gemini / Groq) as an optional script booster.
