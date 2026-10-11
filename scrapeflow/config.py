from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (or a .env file)."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="SCRAPEFLOW_", extra="ignore")

    data_dir: Path = Path("data")
    database_url: str | None = None  # defaults to sqlite in data_dir
    host: str = "0.0.0.0"
    port: int = 8000
    scheduler_enabled: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36 ScrapeFlow/0.3"
    )
    request_timeout: float = 30.0
    # Crawling
    crawl_delay: float = 0.5  # seconds between page requests, to be polite
    respect_robots: bool = True
    max_parallel_checks: int = 2
    # Files
    files_dir: Path | None = None  # default folder for the local storage backend
    max_file_mb: int = 500
    max_downloads_per_check: int = 1000  # new + changed files downloaded in one check
    # Sign-in with local accounts. Only disable it behind your own authenticating proxy.
    auth_disabled: bool = False
    secure_cookies: bool | None = None  # None = only when the request came over HTTPS

    # AI defaults. Everything here can be overridden on the Model providers page.
    ai_provider: str = "openai"
    ai_base_url: str | None = None  # e.g. http://host.docker.internal:11434/v1 for Ollama
    ai_api_key: str | None = None  # falls back to OPENAI_API_KEY / ANTHROPIC_API_KEY
    ai_model: str | None = None
    ai_effort: str = "low"  # Anthropic only
    ai_max_chars: int = 20_000
    ai_timeout: float = 120.0

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'scrapeflow.db'}"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
