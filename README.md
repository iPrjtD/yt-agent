# yt-agent

An autonomous YouTube content agent: it researches trending topics, writes and produces Shorts and long videos,
uploads them as **private** drafts for human review in YouTube Studio, publishes what you approve, and learns
from performance data. It starts on a free, open-source stack and upgrades provider by provider later.

See [docs/DESIGN.md](docs/DESIGN.md) for the architecture, stack and roadmap.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Prerequisites, database, local LLM, trend research agent | in progress |
| 1 | Script → voice → Shorts render → private upload → Studio approval | planned |
| 2 | Analytics pulls, Playbook learning loop, FastAPI + React dashboard | planned |
| 3 | Long videos, platform-policy agent, auto-approve trusted categories | planned |

## Requirements (Windows)

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Docker Desktop (WSL2 backend) for Postgres + pgvector and SearXNG
- [Ollama](https://ollama.com) with the models in `config/settings.yaml`
- FFmpeg on `PATH`

## Setup

```bash
cp .env.example .env            # then fill in values
docker compose up -d db searxng
uv sync
uv run ytagent db-upgrade
ollama pull qwen3.5:4b
ollama pull qwen3.5:9b
ollama pull bge-m3
uv run ytagent doctor           # checks every prerequisite
```

## Usage

```bash
uv run ytagent trends run       # collect signals from all sources + score topics
uv run ytagent trends top       # best topics with suggested format and angle
```

## Configuration

- `config/settings.yaml`: channel language, country, niches, blocked topics, model per role, trend sources and weights.
- `.env`: secrets and service URLs (never committed).
- `prompts/`: prompt templates, versioned with the code.

Changing a model is a one-line edit to `llm:` in `settings.yaml`. Any [LiteLLM](https://docs.litellm.ai) model string works.

## Tests

```bash
uv run pytest
```

## License

The code is released under the [MIT License](LICENSE). Stock footage, music, fonts and voice models the agent downloads keep
their own licenses.
