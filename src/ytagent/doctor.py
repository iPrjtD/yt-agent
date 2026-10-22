"""`ytagent doctor`: check every prerequisite and say what is missing and when it is needed."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy import text

from ytagent.config import ROOT, Settings


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    needed_for: str = "now"


def _run(cmd: list[str]) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else ""


def _ollama_name(model: str) -> str:
    name = model.split("/", 1)[1]
    return name if ":" in name else f"{name}:latest"


def run_checks(settings: Settings) -> list[Check]:
    env = settings.env
    checks = [Check("python", sys.version_info[:2] == (3, 12), sys.version.split()[0])]

    ffmpeg = shutil.which(env.ffmpeg_binary)
    version = _run([ffmpeg, "-version"]).split(" Copyright")[0] if ffmpeg else "not found: set FFMPEG_BINARY in .env"
    checks.append(Check("ffmpeg", bool(ffmpeg), version))

    docker = shutil.which("docker")
    engine = _run([docker, "info", "--format", "{{.ServerVersion}}"]) if docker else ""
    checks.append(Check("docker", bool(engine), f"engine {engine}" if engine else "not installed or not running"))

    try:
        from ytagent.db import get_engine
        with get_engine().connect() as conn:
            ver = conn.execute(text("select current_setting('server_version')")).scalar()
            vec = conn.execute(text("select extversion from pg_extension where extname = 'vector'")).scalar()
            rev = conn.execute(text("select version_num from alembic_version")).scalar() if conn.execute(
                text("select to_regclass('alembic_version')")).scalar() else None
        checks.append(Check("postgres", True, f"server {ver}"))
        checks.append(Check("pgvector", vec is not None, f"vector {vec}" if vec else "run: ytagent db-upgrade"))
        checks.append(Check("db schema", rev is not None, f"revision {rev}" if rev else "run: ytagent db-upgrade"))
    except Exception as exc:
        checks.append(Check("postgres", False, f"{type(exc).__name__}: run `docker compose up -d db`"))

    try:
        tags = httpx.get(f"{env.ollama_api_base}/api/tags", timeout=10).json()
        have = {m["name"] for m in tags.get("models", [])}
        checks.append(Check("ollama", True, f"{len(have)} models installed"))
        for role in ("fast", "writer", "embed"):
            model = getattr(settings.llm, role)
            if model.startswith(("ollama/", "ollama_chat/")):
                name = _ollama_name(model)
                checks.append(Check(f"model:{role}", name in have, name if name in have else f"ollama pull {name}"))
    except Exception:
        checks.append(Check("ollama", False, f"not reachable at {env.ollama_api_base}"))

    try:
        r = httpx.get(f"{env.searxng_url}/search", params={"q": "test", "format": "json"}, timeout=15)
        checks.append(Check("searxng", r.status_code == 200, f"HTTP {r.status_code}", "Phase 1 research"))
    except Exception:
        checks.append(Check("searxng", False, "run `docker compose up -d searxng`", "Phase 1 research"))

    for drive_path, min_gb in ((ROOT, 30), (Path("C:/"), 10)):
        if drive_path.exists():
            free = shutil.disk_usage(drive_path).free / 1e9
            checks.append(Check(f"disk {drive_path.drive or drive_path}", free >= min_gb,
                                f"{free:.0f} GB free (want >= {min_gb})"))

    checks.append(Check(".env", (ROOT / ".env").exists(), "copy .env.example to .env"))
    checks.append(Check("YOUTUBE_API_KEY", bool(env.youtube_api_key), "Google Cloud API key", "trending + search"))
    secrets = ROOT / env.youtube_client_secrets
    checks.append(Check("YouTube OAuth client", secrets.exists(), str(secrets.relative_to(ROOT)), "Phase 1 upload"))
    token = ROOT / "secrets" / "youtube_token.json"
    checks.append(Check("YouTube login", token.exists(),
                        "connected" if token.exists() else "run: ytagent youtube login", "Phase 1 upload"))
    # Pexels paused new API keys (Oct 2026); Pixabay is the primary stock source until it reopens.
    checks.append(Check("PEXELS_API_KEY", bool(env.pexels_api_key), "optional: issuance paused", "optional"))
    checks.append(Check("PIXABAY_API_KEY", bool(env.pixabay_api_key), "pixabay.com/api/docs", "Phase 1 visuals"))
    return checks
