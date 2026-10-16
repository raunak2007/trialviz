"""Runtime settings, read from environment variables (and a local .env if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from trialviz.ctgov.client import API_BASE
from trialviz.interpret.llm import DEFAULT_MODEL


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments, existing variables win."""
    env_path = path or Path.cwd() / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None = None
    model: str = DEFAULT_MODEL
    ctgov_base_url: str = API_BASE
    http_timeout_seconds: float = 60.0
    max_concurrency: int = 4
    cache_ttl_seconds: float = 3600.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        return cls(
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            model=os.environ.get("TRIALVIZ_MODEL", DEFAULT_MODEL),
            ctgov_base_url=os.environ.get("TRIALVIZ_CTGOV_BASE_URL", API_BASE),
            http_timeout_seconds=float(os.environ.get("TRIALVIZ_HTTP_TIMEOUT", "60")),
            max_concurrency=int(os.environ.get("TRIALVIZ_MAX_CONCURRENCY", "4")),
            cache_ttl_seconds=float(os.environ.get("TRIALVIZ_CACHE_TTL", "3600")),
            log_level=os.environ.get("TRIALVIZ_LOG_LEVEL", "INFO"),
        )
