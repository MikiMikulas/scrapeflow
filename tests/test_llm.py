import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlmodel import Session

from scrapeflow import ai, llm
from scrapeflow.crawler import FileLink
from scrapeflow.db import get_engine
from scrapeflow.llm.base import AIConfig, parse_json_object
from scrapeflow.llm.openai_compat import OpenAICompatibleProvider
from scrapeflow.web import app

BASE = "http://ollama.test/v1"


def chat_reply(obj) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(obj)}}]})


def configure(**kw):
    with Session(get_engine()) as s:
        llm.save_config(s, {"provider": "openai", "base_url": BASE, "model": "llama3", **kw})


@pytest.mark.parametrize("raw", ['{"a": 1}', 'Sure! ```json\n{"a": 1}\n```', 'Result: {"a": 1} done'])
def test_parse_json_object(raw):
    assert parse_json_object(raw) == {"a": 1}


@respx.mock
async def test_openai_compat_falls_back_when_json_schema_unsupported():
    route = respx.post(f"{BASE}/chat/completions").mock(
        side_effect=[httpx.Response(400, json={"error": {"message": "response_format not supported"}}),
                     chat_reply({"ok": True})]
    )
    provider = OpenAICompatibleProvider(AIConfig(base_url=BASE, model="llama3", api_key="k"))
    assert await provider.generate_json(system="s", text="t", schema={"type": "object"}) == {"ok": True}
    first, second = (json.loads(c.request.content) for c in route.calls)
    assert first["response_format"]["type"] == "json_schema"
    assert second["response_format"]["type"] == "json_object"
    assert route.calls[0].request.headers["authorization"] == "Bearer k"


@respx.mock
async def test_openai_compat_auth_error_does_not_retry():
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    provider = OpenAICompatibleProvider(AIConfig(base_url=BASE, model="m"))
    with pytest.raises(llm.LLMError, match="401"):
        await provider.generate_json(system="s", text="t", schema={})
    assert route.call_count == 1


def test_settings_roundtrip_and_masking():
    assert llm.get_provider() is None  # nothing configured by default
    configure(api_key="sk-secret-1234")
    cfg = llm.load_config()
    assert cfg.base_url == BASE and cfg.api_key == "sk-secret-1234"
    assert llm.get_provider() is not None

    with TestClient(app) as client:
        data = client.get("/api/settings/ai").json()
        assert data["configured"] and data["api_key_hint"] == "…1234"
        assert "sk-secret" not in client.get("/settings/models").text

        # an empty key field keeps the stored key for the same service
        client.post("/settings/models", data={"preset": "custom", "base_url": BASE, "model": "qwen"})
        assert llm.load_config().api_key == "sk-secret-1234"
        assert llm.load_config().model == "qwen"
        assert "Configured" in client.get("/settings/models").text

        # switching to another service drops the old key instead of sending it elsewhere
        client.post("/settings/models", data={"preset": "groq", "base_url": "https://api.groq.com/openai/v1",
                                              "model": "llama"})
        cfg = llm.load_config()
        assert cfg.preset == "groq" and cfg.api_key is None

        client.post("/settings/models/remove")
        assert llm.get_provider() is None


def test_ai_test_endpoint_uses_saved_key():
    configure(api_key="sk-saved")
    with TestClient(app) as client, respx.mock:
        route = respx.post(f"{BASE}/chat/completions").mock(return_value=chat_reply({"ok": True, "model": "m"}))
        r = client.post("/api/ai/test", json={"preset": "custom", "base_url": BASE, "model": "llama3"})
        assert r.status_code == 200 and r.json()["ok"]
        assert route.calls[0].request.headers["authorization"] == "Bearer sk-saved"


@respx.mock
async def test_ai_filter_keeps_matching_files():
    configure()
    respx.post(f"{BASE}/chat/completions").mock(return_value=chat_reply({"keep": [2]}))
    links = [FileLink("https://x.test/menu.pdf", "Menu", "https://x.test/"),
             FileLink("https://x.test/faktura-10.pdf", "Faktura 10/2026", "https://x.test/")]
    assert await ai.filter_files("only invoices", "X", links) == {"https://x.test/faktura-10.pdf"}

    respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(500))
    assert await ai.filter_files("only invoices", "X", links) is None  # failure → caller keeps everything
