"""Cards shown on the Model providers and Storage pages.

Each card is a shortcut to one of the provider / storage plugins with sensible defaults, so
"Ollama" and "OpenRouter" both use the OpenAI-compatible provider with a different base URL.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelPreset:
    key: str
    label: str
    provider: str  # LLM provider plugin name
    description: str
    base_url: str | None = None  # None = the provider doesn't take a base URL
    needs_key: bool = True
    model_hint: str = ""
    tags: tuple[str, ...] = ("LLM",)
    color: str = "#475467"


MODEL_PRESETS = [
    ModelPreset("openai", "OpenAI", "openai", "GPT models through the OpenAI API.",
                "https://api.openai.com/v1", model_hint="gpt-4.1-mini", color="#10a37f"),
    ModelPreset("anthropic", "Anthropic", "anthropic", "Claude models through the Anthropic API.",
                model_hint="claude-opus-5-5", color="#d97757"),
    ModelPreset("openrouter", "OpenRouter", "openai", "Hundreds of models behind one API key.",
                "https://openrouter.ai/api/v1", model_hint="meta-llama/llama-3.3-70b-instruct",
                color="#6467f2"),
    ModelPreset("ollama", "Ollama", "openai", "Local models on your own machine or server.",
                "http://host.docker.internal:11434/v1", needs_key=False, model_hint="qwen2.5:7b",
                tags=("LLM", "Local"), color="#101828"),
    ModelPreset("lmstudio", "LM Studio", "openai", "Local models served by LM Studio.",
                "http://host.docker.internal:1234/v1", needs_key=False, model_hint="qwen2.5-7b-instruct",
                tags=("LLM", "Local"), color="#4f46e5"),
    ModelPreset("groq", "Groq", "openai", "Very fast inference of open models.",
                "https://api.groq.com/openai/v1", model_hint="llama-3.3-70b-versatile", color="#f55036"),
    ModelPreset("deepseek", "DeepSeek", "openai", "DeepSeek chat models.",
                "https://api.deepseek.com/v1", model_hint="deepseek-chat", color="#4d6bfe"),
    ModelPreset("mistral", "Mistral", "openai", "Mistral AI models.",
                "https://api.mistral.ai/v1", model_hint="mistral-small-latest", color="#fa520f"),
    ModelPreset("custom", "OpenAI-compatible", "openai",
                "vLLM, llama.cpp, LocalAI, Together… – any /v1/chat/completions endpoint.",
                "", needs_key=False, tags=("LLM", "Custom"), color="#1570ef"),
]


@dataclass(frozen=True)
class StoragePreset:
    key: str
    label: str
    backend: str  # storage plugin name
    description: str
    defaults: dict[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    color: str = "#475467"


STORAGE_PRESETS = [
    StoragePreset("local", "Local folder", "local", "A folder on this server or a mounted volume.",
                  tags=("Default",), color="#475467"),
    StoragePreset("s3", "Amazon S3", "s3", "Buckets in Amazon Web Services.",
                  {"region": "eu-central-1"}, tags=("S3",), color="#ff9900"),
    StoragePreset("r2", "Cloudflare R2", "s3", "S3-compatible storage without egress fees.",
                  {"endpoint_url": "https://<account-id>.r2.cloudflarestorage.com", "region": "auto"},
                  tags=("S3",), color="#f38020"),
    StoragePreset("b2", "Backblaze B2", "s3", "Low-cost S3-compatible storage.",
                  {"endpoint_url": "https://s3.eu-central-003.backblazeb2.com", "region": "eu-central-003"},
                  tags=("S3",), color="#e21e29"),
    StoragePreset("minio", "MinIO", "s3", "Self-hosted S3-compatible object storage.",
                  {"endpoint_url": "http://minio:9000", "region": "us-east-1"}, tags=("S3", "Self-hosted"),
                  color="#c72c48"),
    StoragePreset("nextcloud", "Nextcloud", "webdav", "Your Nextcloud or ownCloud through WebDAV.",
                  {"url": "https://cloud.example.com/remote.php/dav/files/USERNAME/", "folder": "ScrapeFlow"},
                  tags=("WebDAV",), color="#0082c9"),
    StoragePreset("webdav", "WebDAV", "webdav", "Synology, QNAP, Box or any other WebDAV server.",
                  {"folder": "ScrapeFlow"}, tags=("WebDAV",), color="#1570ef"),
]
