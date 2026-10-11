"""Provider-independent interface for language models."""

import json
import re
from dataclasses import dataclass, field


class LLMError(RuntimeError):
    pass


@dataclass
class AIConfig:
    provider: str = "openai"
    preset: str | None = None  # which card on the Model providers page was used
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    effort: str = "low"
    vision: bool = False
    timeout: float = 120.0
    extra: dict = field(default_factory=dict)

    def masked(self) -> dict:
        key = self.api_key or ""
        return {
            "provider": self.provider,
            "preset": self.preset,
            "base_url": self.base_url,
            "model": self.model,
            "effort": self.effort,
            "vision": self.vision,
            "api_key_set": bool(key),
            "api_key_hint": f"…{key[-4:]}" if len(key) > 8 else ("set" if key else ""),
        }


class LLMProvider:
    """Base class for model providers.

    To add a provider as a plugin, subclass this and expose it through the
    ``scrapeflow.providers`` entry-point group in your package's pyproject.toml::

        [project.entry-points."scrapeflow.providers"]
        myprovider = "my_package.module:MyProvider"
    """

    #: identifier stored in settings
    name: str = ""
    #: human-readable name shown on the Settings page
    label: str = ""
    #: shown under the provider picker
    description: str = ""
    #: whether the provider needs a base URL
    needs_base_url: bool = False
    default_model: str | None = None

    def __init__(self, config: AIConfig):
        self.config = config

    @property
    def model(self) -> str:
        model = self.config.model or self.default_model
        if not model:
            raise LLMError("No model configured – set one on the Settings page")
        return model

    def is_configured(self) -> bool:
        if self.needs_base_url and not self.config.base_url:
            return False
        return bool(self.config.model or self.default_model)

    async def generate_json(
        self, *, system: str, text: str, schema: dict, images: list[bytes] | None = None
    ) -> dict:
        """Return a JSON object that should follow ``schema``. Raise LLMError on failure."""
        raise NotImplementedError


def parse_json_object(raw: str) -> dict:
    """Parse a JSON object from model output, tolerating code fences and surrounding prose."""
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("Model did not return JSON") from None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            raise LLMError("Model returned invalid JSON") from e
    if not isinstance(data, dict):
        raise LLMError("Model returned JSON that is not an object")
    return data


def schema_hint(schema: dict) -> str:
    """Plain-text description of the expected JSON for models without structured output."""
    return (
        "Respond with a single JSON object only, no prose, matching this JSON schema:\n"
        + json.dumps(schema, ensure_ascii=False)
    )
