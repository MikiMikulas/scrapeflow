"""Any server that speaks the OpenAI Chat Completions API.

Works with Ollama, LM Studio, llama.cpp server, vLLM, LocalAI, OpenRouter, Groq, Together,
Mistral, OpenAI and others – point ``base_url`` at the ``/v1`` root.
"""

import base64

import httpx

from .base import LLMError, LLMProvider, parse_json_object, schema_hint


class OpenAICompatibleProvider(LLMProvider):
    name = "openai"
    label = "OpenAI-compatible API"
    description = (
        "Ollama, LM Studio, vLLM, llama.cpp, OpenRouter, Groq, OpenAI… – any /v1/chat/completions endpoint."
    )
    needs_base_url = True

    async def generate_json(
        self, *, system: str, text: str, schema: dict, images: list[bytes] | None = None
    ) -> dict:
        content: list[dict] = []
        if images and self.config.vision:
            for img in images:
                url = "data:image/jpeg;base64," + base64.standard_b64encode(img).decode()
                content.append({"type": "image_url", "image_url": {"url": url}})
        content.append({"type": "text", "text": f"{text}\n\n{schema_hint(schema)}"})

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content if len(content) > 1 else content[0]["text"]},
            ],
            "temperature": 0,
        }
        # Servers differ in structured-output support, so degrade step by step.
        formats = [
            {"type": "json_schema", "json_schema": {"name": "result", "schema": schema, "strict": True}},
            {"type": "json_object"},
            None,
        ]
        last_error = "no response"
        for fmt in formats:
            payload = dict(body)
            if fmt:
                payload["response_format"] = fmt
            status, data = await self._post(payload)
            if status < 400:
                return parse_json_object(_message_text(data))
            last_error = _error_text(status, data)
            if status not in (400, 404, 415, 422, 500, 501):
                break  # auth / rate-limit errors won't be fixed by a simpler format
        raise LLMError(last_error)

    async def _post(self, payload: dict) -> tuple[int, dict]:
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                r = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise LLMError(f"Cannot reach {url}: {e}") from e
        try:
            data = r.json()
        except ValueError:
            data = {"error": {"message": r.text[:300]}}
        return r.status_code, data


def _message_text(data: dict) -> str:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError("Unexpected response shape from the API") from e
    if isinstance(content, list):  # some servers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def _error_text(status: int, data: dict) -> str:
    err = data.get("error") if isinstance(data, dict) else None
    msg = err.get("message") if isinstance(err, dict) else err
    return f"HTTP {status}: {msg or data}"
