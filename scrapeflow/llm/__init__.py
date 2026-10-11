"""Model providers, loaded as plugins.

Built-in: ``openai`` (any OpenAI-compatible API) and ``anthropic`` (Claude).
Extra providers are discovered from the ``scrapeflow.providers`` entry-point group.
"""

import logging
import os
from functools import lru_cache
from importlib.metadata import entry_points

from sqlmodel import Session, select

from ..config import get_settings
from ..db import get_engine
from ..models import Setting
from .anthropic_provider import AnthropicProvider
from .base import AIConfig, LLMError, LLMProvider
from .openai_compat import OpenAICompatibleProvider

__all__ = ["AIConfig", "LLMError", "LLMProvider", "get_provider", "load_config", "providers",
           "save_config"]

log = logging.getLogger(__name__)

_FIELDS = ("provider", "preset", "base_url", "api_key", "model", "effort", "vision")


@lru_cache
def providers() -> dict[str, type[LLMProvider]]:
    found: dict[str, type[LLMProvider]] = {
        OpenAICompatibleProvider.name: OpenAICompatibleProvider,
        AnthropicProvider.name: AnthropicProvider,
    }
    for ep in entry_points(group="scrapeflow.providers"):
        try:
            cls = ep.load()
            if isinstance(cls, type) and issubclass(cls, LLMProvider):
                found[cls.name or ep.name] = cls
            else:
                log.warning("Plugin %s is not an LLMProvider subclass", ep.name)
        except Exception:
            log.exception("Failed to load provider plugin %s", ep.name)
    return found


def load_config(session: Session | None = None) -> AIConfig:
    """Environment defaults, overridden by whatever was saved on the Settings page."""
    s = get_settings()
    cfg = AIConfig(
        provider=s.ai_provider,
        base_url=s.ai_base_url,
        api_key=s.ai_api_key,
        model=s.ai_model,
        effort=s.ai_effort,
        timeout=s.ai_timeout,
    )
    own = session is None
    session = session or Session(get_engine())
    try:
        for row in session.exec(select(Setting).where(Setting.key.startswith("ai_"))):
            name = row.key.removeprefix("ai_")
            if name == "vision":
                cfg.vision = row.value == "1"
            elif name in _FIELDS:
                setattr(cfg, name, row.value or None)
    finally:
        if own:
            session.close()
    cfg.effort = cfg.effort or "low"
    cfg.provider = cfg.provider or "openai"
    if not cfg.api_key:
        env = "ANTHROPIC_API_KEY" if cfg.provider == "anthropic" else "OPENAI_API_KEY"
        cfg.api_key = os.environ.get(env) or None
    return cfg


def save_config(session: Session, values: dict) -> None:
    for name in _FIELDS:
        if name not in values:
            continue
        value = values[name]
        if name == "vision":
            value = "1" if value else "0"
        row = session.get(Setting, f"ai_{name}") or Setting(key=f"ai_{name}")
        row.value = (value or "").strip() if isinstance(value, str) else str(value or "")
        session.add(row)
    session.commit()


def get_provider(config: AIConfig | None = None) -> LLMProvider | None:
    """The configured provider, or None if AI isn't set up."""
    config = config or load_config()
    cls = providers().get(config.provider)
    if cls is None:
        log.warning("Unknown AI provider %r", config.provider)
        return None
    provider = cls(config)
    return provider if provider.is_configured() else None
