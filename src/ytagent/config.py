"""Configuration: secrets from .env, behaviour from config/settings.yaml."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    database_url: str = "postgresql+psycopg://ytagent:change-me@127.0.0.1:5432/ytagent"
    ollama_api_base: str = "http://127.0.0.1:11434"
    searxng_url: str = "http://127.0.0.1:8888"
    # Full path when ffmpeg is not on PATH (MoviePy reads the same FFMPEG_BINARY variable).
    ffmpeg_binary: str = "ffmpeg"
    youtube_api_key: str = ""
    youtube_client_secrets: str = "secrets/youtube_client_secret.json"
    pexels_api_key: str = ""
    gemini_api_key: str = ""
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""
    pixabay_api_key: str = ""
    ntfy_topic: str = ""


class ChannelCfg(BaseModel):
    name: str = "TBD"
    tagline: str = ""
    handle: str = ""
    youtube_channel_id: str = ""
    language: str = "en"
    geo: str = "IN"
    niches: list[str] = Field(default_factory=list)
    blocked_topics: list[str] = Field(default_factory=list)


class LlmCfg(BaseModel):
    fast: str
    writer: str
    # Tried in order when a role's model fails (rate limit, overload, outage, missing key).
    fallbacks: dict[str, list[str]] = Field(default_factory=dict)
    embed: str
    embed_dims: int = 1024
    timeout_s: int = 600
    hosted_timeout_s: int = 120


class ScoringCfg(BaseModel):
    w_traffic: float = 0.30
    w_corroboration: float = 0.25
    w_freshness: float = 0.15
    w_llm_fit: float = 0.30
    freshness_half_life_h: float = 18


class TrendsCfg(BaseModel):
    sources: dict[str, dict[str, Any]] = Field(default_factory=dict)
    scoring: ScoringCfg = Field(default_factory=ScoringCfg)
    keep_days: int = 14


class PathsCfg(BaseModel):
    data_dir: str = "data"


class VoiceCfg(BaseModel):
    host_voice: str = "Puck"
    host_style: str = "the channel's cheerful, curious cartoon bee host"
    require_host_voice: bool = True
    gemini_tts_models: list[str] = Field(default_factory=list)
    tts_plain_models: list[str] = Field(default_factory=list)
    engine: str = "kokoro"
    voice: str = "af_heart"
    speed: float = 1.08
    line_gap_s: float = 0.16


class CaptionsCfg(BaseModel):
    whisper_model: str = "base.en"


class VideoCfg(BaseModel):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    target_seconds: int = 36        # narration is nudged (max 8% faster) towards this
    max_short_seconds: int = 90     # QA limit (YouTube Shorts allow up to 3 minutes)


class VisualsCfg(BaseModel):
    min_match: float = 0.25          # CLIP bar for real footage (clear matches score ~0.27-0.31)
    ai_style: str = ("vibrant 3D claymation stop-motion style, soft warm studio lighting, playful, cinematic "
                     "composition, highly detailed, vertical 9:16, no text, no letters, no watermark")
    ai_daily_cap: int = 120          # stay inside Cloudflare's free daily allocation


class AutopilotCfg(BaseModel):
    ideas_every_hours: int = 3
    produce_at: list[str] = Field(default_factory=lambda: ["01:00", "03:30"])
    cleanup_at: str = "05:30"
    score_limit: int = 12
    require_gemini_writer: bool = True


class PublishingCfg(BaseModel):
    mode: str = "manual"
    contains_synthetic_media: bool = False
    publish_mode: str = "slot"       # slot: schedule approved Shorts for the next slot | now: publish at once
    slots: list[str] = Field(default_factory=lambda: ["13:00", "19:30"])
    timezone: str = "Asia/Kolkata"
    min_gap_hours: float = 4.0


class FormatCfg(BaseModel):
    share: float
    voice: str
    speed: float = 1.05
    music: str = ""
    gemini_voice: str = "Puck"
    style: str = "a friendly, engaging host"


class Settings(BaseModel):
    channel: ChannelCfg
    llm: LlmCfg
    formats: dict[str, FormatCfg] = Field(default_factory=dict)
    trends: TrendsCfg
    voice: VoiceCfg = Field(default_factory=VoiceCfg)
    captions: CaptionsCfg = Field(default_factory=CaptionsCfg)
    video: VideoCfg = Field(default_factory=VideoCfg)
    publishing: PublishingCfg = Field(default_factory=PublishingCfg)
    autopilot: AutopilotCfg = Field(default_factory=AutopilotCfg)
    visuals: VisualsCfg = Field(default_factory=VisualsCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)
    env: Env = Field(default_factory=Env)

    @property
    def data_dir(self) -> Path:
        path = ROOT / self.paths.data_dir
        path.mkdir(parents=True, exist_ok=True)
        return path


def load_settings(path: Path | None = None) -> Settings:
    raw = yaml.safe_load((path or ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    return Settings(**raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
