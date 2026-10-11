"""Optional AI filter: let a model decide which newly found files are worth storing."""

import logging

from .config import get_settings
from .crawler import FileLink
from .llm import LLMError, get_provider

log = logging.getLogger(__name__)

FILTER_SYSTEM = """You help a user who mirrors files from a website into their own storage. \
You get the user's instructions (which files they want) and a numbered list of files newly \
found on the website, each with its file name, the text of the link and the page it was \
linked from.

Return the numbers of the files the user wants to keep. Follow the instructions strictly, but \
judge by meaning – names and link texts can be in any language, abbreviated or inconsistent. \
When it is unclear whether a file matches, keep it."""

FILTER_SCHEMA = {
    "type": "object",
    "properties": {"keep": {"type": "array", "items": {"type": "integer"}}},
    "required": ["keep"],
    "additionalProperties": False,
}

_BATCH = 150


def ai_available() -> bool:
    return get_provider() is not None


async def filter_files(instructions: str, site_name: str, links: list[FileLink]) -> set[str] | None:
    """URLs of the files that match ``instructions``, or None if the model could not be used."""
    provider = get_provider()
    if provider is None or not links:
        return None
    keep: set[str] = set()
    limit = get_settings().ai_max_chars
    for start in range(0, len(links), _BATCH):
        batch = links[start : start + _BATCH]
        lines = [f"{i}. {link.filename} | link text: {link.text or '-'} | page: {link.page_url}"
                 for i, link in enumerate(batch, 1)]
        text = (f"Website: {site_name}\nUser instructions: {instructions.strip()}\n\n"
                "New files:\n" + "\n".join(lines))[:limit]
        try:
            data = await provider.generate_json(system=FILTER_SYSTEM, text=text, schema=FILTER_SCHEMA)
            numbers = {int(n) for n in data["keep"]}
        except (LLMError, KeyError, TypeError, ValueError) as e:
            log.warning("AI filter failed for %s: %s", site_name, e)
            return None
        keep |= {link.url for i, link in enumerate(batch, 1) if i in numbers}
    return keep
