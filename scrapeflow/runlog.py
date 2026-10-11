"""Activity log of a check: what it is doing right now, saved with the run when it ends."""

import json
import time
from dataclasses import dataclass, field

MAX_LINES = 5000
LEVELS = ("info", "ok", "warn", "error", "muted")


@dataclass
class RunLog:
    run_id: int
    lines: list[list] = field(default_factory=list)  # [unix time, level, message]
    dropped: int = 0  # oldest lines removed to stay under MAX_LINES

    def __call__(self, message: str, level: str = "info") -> None:
        self.lines.append([round(time.time(), 1), level, message])
        if len(self.lines) > MAX_LINES:
            del self.lines[: len(self.lines) - MAX_LINES]
            self.dropped += 1

    def since(self, after: int) -> tuple[list[list], int]:
        """Lines after the absolute position ``after`` and the position to ask from next time."""
        start = max(after - self.dropped, 0)
        return self.lines[start:], self.dropped + len(self.lines)

    def dump(self) -> str:
        return json.dumps(self.lines, ensure_ascii=False)


_live: dict[int, RunLog] = {}  # site id → log of its running check


def start(site_id: int, run_id: int) -> RunLog:
    _live[site_id] = RunLog(run_id)
    return _live[site_id]


def live(site_id: int) -> RunLog | None:
    return _live.get(site_id)


def finish(site_id: int) -> None:
    _live.pop(site_id, None)


def load(raw: str | None) -> list[list]:
    try:
        return json.loads(raw) if raw else []
    except ValueError:
        return []


def filesize(n: int | None) -> str:
    if n is None:
        return "? B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"
