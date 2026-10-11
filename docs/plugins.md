# Plugins and extending ScrapeFlow

ScrapeFlow discovers storage backends and model providers through Python entry points, so you can add
your own without forking. Install the package into the image:

```dockerfile
FROM ghcr.io/mikimikulas/scrapeflow:latest
USER root
RUN pip install my-scrapeflow-plugin
USER scrapeflow
```

## Storage backends

Subclass `scrapeflow.storage.StorageBackend`, declare the settings as `ConfigField`s – they appear on
the Storage page as a card automatically – and implement `put`, `open` and `delete`:

```python
from scrapeflow.storage import ConfigField, StorageBackend, StorageError


class SFTPStorage(StorageBackend):
    name = "sftp"
    label = "SFTP"
    description = "Any SSH server."
    fields = [
        ConfigField("host", "Host", required=True),
        ConfigField("username", "Username"),
        ConfigField("password", "Password", secret=True, kind="password"),
    ]

    async def put(self, path, local_file, content_type=None):
        ...  # upload local_file to `path` (forward slashes, relative); raise StorageError on failure

    async def open(self, path):
        ...  # async generator yielding the file's bytes

    async def delete(self, path):
        ...

    def describe(self):
        return f"SFTP {self.opt('host')}"
```

```toml
[project.entry-points."scrapeflow.storage"]
sftp = "my_scrapeflow_sftp:SFTPStorage"
```

**Test** on the Storage page calls `put`, `open` and `delete` with a small file.

## Model providers

Subclass `scrapeflow.llm.LLMProvider` and return a JSON object that matches the given schema:

```python
from scrapeflow.llm import LLMError, LLMProvider


class GeminiProvider(LLMProvider):
    name = "gemini"
    label = "Google Gemini"
    description = "Gemini models via the Google AI API."
    default_model = "gemini-2.5-flash"

    async def generate_json(self, *, system, text, schema, images=None) -> dict:
        ...  # call the API; raise LLMError on failure
```

```toml
[project.entry-points."scrapeflow.providers"]
gemini = "my_scrapeflow_gemini.provider:GeminiProvider"
```

`self.config` carries the base URL, API key and model from the Model providers page. The plugin gets
its own card there.

## Notification channels

Subclass `Notifier` in [`scrapeflow/notifiers.py`](../scrapeflow/notifiers.py) and add an instance to
`NOTIFIERS`. `notify_all` runs after every check that stored new or changed files:

```python
import httpx

from scrapeflow.notifiers import NOTIFIERS, Notifier


class NtfyNotifier(Notifier):
    name = "ntfy"

    async def send(self, site, run):
        async with httpx.AsyncClient() as client:
            await client.post("https://ntfy.sh/my-topic",
                              content=f"{site.name}: {run.new_files} new, {run.updated_files} updated")


NOTIFIERS.append(NtfyNotifier())
```

## Flow nodes

Node types live in [`scrapeflow/flow.py`](../scrapeflow/flow.py): a `NodeType` entry in `NODE_TYPES`
(label, colour, outputs and fields – the editor builds its form from them) plus a branch in
`FlowRunner._run`. Nodes that only need names and links (`metadata_only=True`) run before the download.
Pull requests with new nodes are welcome – see [CONTRIBUTING.md](../CONTRIBUTING.md).
