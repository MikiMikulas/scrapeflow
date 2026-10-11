"""Storage backends for downloaded files."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path


class StorageError(RuntimeError):
    pass


@dataclass
class ConfigField:
    """A setting a backend needs; rendered as a form field on the Settings page."""

    name: str
    label: str
    placeholder: str = ""
    help: str = ""
    secret: bool = False
    required: bool = False
    kind: str = "text"  # text | password | checkbox


class StorageBackend:
    """Base class for storage backends.

    To add one as a plugin, subclass this and expose it through the ``scrapeflow.storage``
    entry-point group::

        [project.entry-points."scrapeflow.storage"]
        gdrive = "my_package.storage:GoogleDriveStorage"
    """

    name: str = ""
    label: str = ""
    description: str = ""
    fields: list[ConfigField] = []

    def __init__(self, options: dict[str, str]):
        self.options = options

    def opt(self, key: str, default: str = "") -> str:
        return (self.options.get(key) or default).strip()

    def validate(self) -> None:
        missing = [f.label for f in self.fields if f.required and not self.opt(f.name)]
        if missing:
            raise StorageError("Missing settings: " + ", ".join(missing))

    async def put(self, path: str, local_file: Path, content_type: str | None = None) -> None:
        """Store ``local_file`` under ``path`` (forward slashes, relative)."""
        raise NotImplementedError

    async def open(self, path: str) -> AsyncIterator[bytes]:
        """Stream a stored file back (used by the dashboard's download links)."""
        raise NotImplementedError
        yield b""  # pragma: no cover

    async def delete(self, path: str) -> None:
        raise NotImplementedError

    async def test(self) -> str:
        """Write, read and delete a small file. Returns a short description of the target."""
        import tempfile

        with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False) as f:
            f.write(b"scrapeflow storage test\n")
            tmp = Path(f.name)
        path = ".scrapeflow-test.txt"
        try:
            await self.put(path, tmp, "text/plain")
            data = b"".join([chunk async for chunk in self.open(path)])
            if data != b"scrapeflow storage test\n":
                raise StorageError("Read back different content than was written")
            await self.delete(path)
        finally:
            tmp.unlink(missing_ok=True)
        return self.describe()

    def describe(self) -> str:
        return self.label
