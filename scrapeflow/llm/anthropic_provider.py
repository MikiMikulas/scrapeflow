"""Claude via the official Anthropic SDK."""

import base64
import json
import os

import anthropic

from .base import LLMError, LLMProvider


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    label = "Anthropic (Claude)"
    description = "Claude models through the Anthropic API."
    default_model = "claude-opus-5-5"

    def is_configured(self) -> bool:
        # Without an explicit key the SDK can also use a token or an `ant auth login` profile.
        return bool(
            self.config.api_key
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_PROFILE")
        )

    async def generate_json(
        self, *, system: str, text: str, schema: dict, images: list[bytes] | None = None
    ) -> dict:
        content: list[dict] = []
        if images and self.config.vision:
            for img in images:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(img).decode(),
                    },
                })
        content.append({"type": "text", "text": text})

        client = anthropic.AsyncAnthropic(
            api_key=self.config.api_key or None,
            base_url=self.config.base_url or None,
            timeout=self.config.timeout,
        )
        try:
            response = await client.beta.messages.create(
                model=self.model,
                max_tokens=4000,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_config={
                    "effort": self.config.effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                # On a safety decline, the API retries on a fallback model automatically.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise LLMError("Invalid Anthropic API key") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Anthropic rate limit reached") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Cannot reach the Anthropic API: {e}") from e

        if response.stop_reason == "refusal":
            raise LLMError("The model declined this request")
        raw = "".join(b.text for b in response.content if b.type == "text")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMError(f"Unparseable model output (stop_reason={response.stop_reason})") from e
