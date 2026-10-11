"""Flows: what happens to every new or changed file, as a graph of nodes edited on a canvas.

A flow is JSON: ``{"nodes": [{"id", "type", "x", "y", "config"}], "edges": [{"from", "out", "to"}]}``.
It always starts at the single ``source`` node (the website). Each file found on the website
travels along the edges; filters send it to their ``match`` or ``else`` output, converters and
code turn it into new files, and ``storage`` nodes save whatever reaches them.

Filters only look at names and links, so they run before the file is downloaded – a file no
storage node can reach is never downloaded.
"""

import asyncio
import json
import logging
import re
import shutil
import sys
import tempfile
import textwrap
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from .files import parse_extensions, safe_filename, url_extension

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- node types


@dataclass(frozen=True)
class NodeField:
    name: str
    label: str
    kind: str = "text"  # text | textarea | code | number | select
    default: str | int | None = None
    placeholder: str = ""
    help: str = ""
    options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class NodeType:
    type: str
    label: str
    description: str
    color: str
    icon: str
    inputs: int = 1
    outputs: tuple[str, ...] = ("out",)
    fields: tuple[NodeField, ...] = ()
    metadata_only: bool = False  # decides from names and links alone, before downloading
    singleton: bool = False

    def as_dict(self) -> dict:
        return {"type": self.type, "label": self.label, "description": self.description, "color": self.color,
                "icon": self.icon, "inputs": self.inputs, "outputs": list(self.outputs),
                "singleton": self.singleton,
                "fields": [f.__dict__ | {"options": [list(o) for o in f.options]} for f in self.fields]}


CODE_TEMPLATE = textwrap.dedent('''\
    # Runs for every file that reaches this node.
    #   input_path  – pathlib.Path of the file
    #   meta        – dict: filename, url, link_text, page_url, content_type, site
    #   output_dir  – pathlib.Path; every file you write here continues to the next node
    # Return a string to save it as "<name>.md" instead. Write nothing to drop the file.
    # print() output appears in the check's log.

    def process(input_path, meta, output_dir):
        text = input_path.read_text(encoding="utf-8", errors="replace")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return f"# {meta['filename']}\\n\\nSource: {meta['url']}\\n\\n" + "\\n".join(lines)
''')

NODE_TYPES: dict[str, NodeType] = {t.type: t for t in [
    NodeType("source", "Website", "Every new or changed file found on the website starts here.",
             "#1570ef", "globe", inputs=0, outputs=("files",), singleton=True),
    NodeType("filter", "Filter", "Sends files to “match” or “else” by type or name – before downloading.",
             "#7a5af8", "filter", outputs=("match", "else"), metadata_only=True, fields=(
                 NodeField("extensions", "File types", placeholder="pdf, docx",
                           help="Comma separated. Empty = any type."),
                 NodeField("pattern", "Name contains (regex)", placeholder="faktura|smlouva",
                           help="Matched against the file name, link text and URL, ignoring case."),
             )),
    NodeType("ai_filter", "AI filter", "Your model decides from the name and link text whether a file matches.",
             "#dd2590", "sparkles", outputs=("match", "else"), metadata_only=True, fields=(
                 NodeField("instructions", "Keep files that are…", "textarea",
                           placeholder="invoices, contracts and price lists"),
             )),
    NodeType("markdown", "To Markdown", "Converts PDF, Word, Excel, PowerPoint, CSV, HTML… to Markdown.",
             "#0e9384", "text", outputs=("markdown",), fields=(
                 NodeField("on_error", "If a file can't be converted", "select", "drop",
                           options=(("drop", "Skip it"), ("pass", "Pass the original on"))),
             )),
    NodeType("code", "Python code", "Your own Python function transforms each file.",
             "#e04f16", "code", fields=(
                 NodeField("code", "Code", "code", CODE_TEMPLATE),
                 NodeField("timeout", "Time limit (seconds)", "number", 60),
             )),
    NodeType("storage", "Save to storage", "Saves what reaches it into the storage chosen on the Storage page.",
             "#079455", "database", outputs=(), fields=(
                 NodeField("folder", "Folder", placeholder="{site}",
                           help="{site} = the website's folder, {ext} = file type. Empty = {site}."),
             )),
]}


def node_types_json() -> str:
    return json.dumps([t.as_dict() for t in NODE_TYPES.values()], ensure_ascii=False)


def default_flow(ai_filter: str | None = None) -> dict:
    """What a site does without a custom flow: store every file (optionally after an AI filter)."""
    nodes = [{"id": "source", "type": "source", "x": 60, "y": 160, "config": {}},
             {"id": "storage", "type": "storage", "x": 700 if ai_filter else 420, "y": 160, "config": {}}]
    edges = [{"from": "source", "out": "files", "to": "storage"}]
    if ai_filter:
        nodes.insert(1, {"id": "ai", "type": "ai_filter", "x": 380, "y": 140,
                         "config": {"instructions": ai_filter}})
        edges = [{"from": "source", "out": "files", "to": "ai"}, {"from": "ai", "out": "match", "to": "storage"}]
    return {"nodes": nodes, "edges": edges}


def load(raw: str | None, ai_filter: str | None = None) -> dict:
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            log.warning("Stored flow is not valid JSON – using the default flow")
    return default_flow(ai_filter)


def validate(flow: dict) -> list[str]:
    """Problems that make a flow unusable (empty list = fine)."""
    if not isinstance(flow, dict) or not all(isinstance(flow.get(k), list) for k in ("nodes", "edges")):
        return ["A flow needs a list of nodes and a list of edges"]
    errors = []
    nodes = {}
    for n in flow["nodes"]:
        if not isinstance(n, dict) or not n.get("id") or n.get("type") not in NODE_TYPES:
            errors.append(f"Unknown node: {n!r}"[:200])
            continue
        if n["id"] in nodes:
            errors.append(f"Duplicate node id {n['id']!r}")
        nodes[n["id"]] = n
    sources = [n for n in nodes.values() if n["type"] == "source"]
    if len(sources) != 1:
        errors.append("A flow needs exactly one Website node")
    for e in flow["edges"]:
        src, dst = nodes.get(e.get("from")), nodes.get(e.get("to"))
        if src is None or dst is None:
            errors.append("A connection points to a missing node")
        elif e.get("out") not in NODE_TYPES[src["type"]].outputs:
            errors.append(f"{NODE_TYPES[src['type']].label} has no output {e.get('out')!r}")
        elif NODE_TYPES[dst["type"]].inputs == 0:
            errors.append("Nothing can connect into the Website node")
    for n in nodes.values():
        if n["type"] == "code":
            try:
                compile(n.get("config", {}).get("code") or "", f"<{n['id']}>", "exec")
            except SyntaxError as e:
                errors.append(f"Python code node: line {e.lineno}: {e.msg}")
        if n["type"] == "filter" and (pattern := n.get("config", {}).get("pattern")):
            try:
                re.compile(pattern)
            except re.error as e:
                errors.append(f"Filter: invalid pattern – {e}")
    if not errors and _has_cycle(nodes, flow["edges"]):
        errors.append("The flow contains a loop")
    return errors


def warnings(flow: dict) -> list[str]:
    nodes = {n["id"]: n for n in flow["nodes"]}
    reachable = _reachable(flow, "source")
    notes = []
    if not any(nodes[n]["type"] == "storage" for n in reachable if n in nodes):
        notes.append("No Save to storage node is connected – files would be found but not saved")
    unused = [NODE_TYPES[n["type"]].label for n in nodes.values() if n["id"] not in reachable]
    if unused:
        notes.append("Not connected to the Website node: " + ", ".join(unused))
    return notes


def _children(flow: dict, node_id: str, out: str | None = None) -> list[str]:
    return [e["to"] for e in flow["edges"] if e["from"] == node_id and (out is None or e["out"] == out)]


def _reachable(flow: dict, start: str) -> set[str]:
    seen, stack = set(), [start]
    while stack:
        n = stack.pop()
        if n not in seen:
            seen.add(n)
            stack.extend(_children(flow, n))
    return seen


def _has_cycle(nodes: dict, edges: list) -> bool:
    graph = {n: [e["to"] for e in edges if e["from"] == n] for n in nodes}
    state: dict[str, int] = {}

    def visit(n: str) -> bool:
        state[n] = 1
        for m in graph.get(n, []):
            if state.get(m) == 1 or (state.get(m) is None and visit(m)):
                return True
        state[n] = 2
        return False

    return any(state.get(n) is None and visit(n) for n in nodes)


# --------------------------------------------------------------------------- running


@dataclass
class Item:
    """A file travelling through the flow."""

    filename: str
    meta: dict  # url, link_text, page_url, content_type, site
    path: Path | None = None  # None until downloaded


@dataclass
class Output:
    node_id: str
    filename: str
    path: Path


@dataclass
class Result:
    outputs: list[Output] = field(default_factory=list)  # what reached storage nodes
    errors: list[str] = field(default_factory=list)
    dropped_by: str | None = None  # label of the filter that stopped the file


AIDecider = Callable[[str, list[Item]], Awaitable[set[int] | None]]


class FlowRunner:
    def __init__(self, flow: dict, workdir: Path, say: Callable[..., None], ai_decide: AIDecider | None = None):
        self.flow = flow
        self.nodes = {n["id"]: n for n in flow["nodes"]}
        self.workdir = workdir
        self.say = say
        self.ai_decide = ai_decide
        self._ai_cache: dict[tuple[str, str], bool] = {}

    def label(self, node_id: str) -> str:
        return NODE_TYPES[self.nodes[node_id]["type"]].label

    def cfg(self, node_id: str) -> dict:
        return self.nodes[node_id].get("config") or {}

    # ---- planning: run the metadata-only prefix before anything is downloaded

    async def prefetch_ai(self, items: list[Item]) -> None:
        """Ask each AI filter about many files at once instead of one call per file."""
        if not self.ai_decide:
            return
        for node_id, node in self.nodes.items():
            if node["type"] != "ai_filter" or not items:
                continue
            instructions = self.cfg(node_id).get("instructions") or ""
            keep = await self.ai_decide(instructions, items)
            if keep is not None:
                for i, item in enumerate(items):
                    self._ai_cache[(node_id, item.meta.get("url", item.filename))] = i in keep

    async def plan(self, item: Item) -> tuple[list[str], str | None]:
        """Nodes after the filters that need the file's content – and, if there are none, which
        filter stopped the file."""
        frontier: list[str] = []
        stops: list[str] = []

        async def follow(node_id: str, out: str | None) -> None:
            for child in _children(self.flow, node_id, out):
                if NODE_TYPES[self.nodes[child]["type"]].metadata_only:
                    child_out = "match" if await self._decide(child, item) else "else"
                    if not _children(self.flow, child, child_out):
                        stops.append(self.label(child))
                    await follow(child, child_out)
                elif child not in frontier:
                    frontier.append(child)

        await follow("source", None)
        return frontier, None if frontier else (stops[0] if stops else "the flow (nothing is connected)")

    async def _decide(self, node_id: str, item: Item) -> bool:
        node, cfg = self.nodes[node_id], self.cfg(node_id)
        if node["type"] == "filter":
            exts = cfg.get("extensions")
            if exts and (item.filename.rpartition(".")[2].lower() if "." in item.filename else
                         url_extension(item.meta.get("url", ""))) not in parse_extensions(exts):
                return False
            pattern = cfg.get("pattern")
            if pattern:
                haystack = " ".join([item.filename, item.meta.get("link_text") or "", item.meta.get("url") or ""])
                return re.search(pattern, haystack, re.I) is not None
            return True
        # ai_filter
        key = (node_id, item.meta.get("url", item.filename))
        if key not in self._ai_cache:
            keep = await self.ai_decide(cfg.get("instructions") or "", [item]) if self.ai_decide else None
            self._ai_cache[key] = True if keep is None else 0 in keep
        return self._ai_cache[key]

    # ---- processing the downloaded file

    async def process(self, item: Item, frontier: list[str],
                      store: Callable[[str, Item], Awaitable[None]]) -> Result:
        result = Result()
        for node_id in frontier:
            await self._run(node_id, item, store, result, depth=0)
        return result

    async def _run(self, node_id: str, item: Item, store, result: Result, depth: int) -> None:
        if depth > 50:
            result.errors.append("The flow is too deep")
            return
        node = self.nodes[node_id]
        ntype = NODE_TYPES[node["type"]]
        try:
            if ntype.metadata_only:
                out = "match" if await self._decide(node_id, item) else "else"
                for child in _children(self.flow, node_id, out):
                    await self._run(child, item, store, result, depth + 1)
                if not _children(self.flow, node_id, out):
                    self.say(f"  {ntype.label}: {item.filename} → {out} (not connected, stops here)", "muted")
                return
            if node["type"] == "storage":
                await store(node_id, item)
                result.outputs.append(Output(node_id, item.filename, item.path))
                return
            produced = await (self._markdown(node_id, item) if node["type"] == "markdown"
                              else self._code(node_id, item))
        except FlowError as e:
            result.errors.append(f"{ntype.label}: {e}")
            self.say(f"  ✗ {ntype.label} failed for {item.filename}: {e}", "error")
            return
        for new_item in produced:
            for child in _children(self.flow, node_id):
                await self._run(child, new_item, store, result, depth + 1)

    async def _markdown(self, node_id: str, item: Item) -> list[Item]:
        if item.filename.lower().endswith((".md", ".markdown")):
            return [item]
        try:
            from markitdown import MarkItDown
        except ImportError as e:
            raise FlowError("MarkItDown is not installed: pip install 'markitdown[pdf,docx,xlsx,pptx]'") from e
        try:
            converted = await asyncio.to_thread(MarkItDown().convert, str(item.path))
            text = converted.text_content or ""
        except Exception as e:  # unsupported or broken file
            if self.cfg(node_id).get("on_error") == "pass":
                self.say(f"  To Markdown: {item.filename} can't be converted ({type(e).__name__}) – passing it on",
                         "warn")
                return [item]
            raise FlowError(f"can't convert {item.filename}: {type(e).__name__}: {e}"[:300]) from e
        if not text.strip():
            if self.cfg(node_id).get("on_error") == "pass":
                return [item]
            raise FlowError(f"{item.filename} has no text (a scanned PDF?)")
        name = Path(item.filename).stem + ".md"
        path = self._new_dir() / name
        path.write_text(text, encoding="utf-8")
        self.say(f"  To Markdown: {item.filename} → {name} ({len(text):,} characters)", "ok")
        return [Item(name, item.meta | {"content_type": "text/markdown", "source_file": item.filename}, path)]

    async def _code(self, node_id: str, item: Item) -> list[Item]:
        cfg = self.cfg(node_id)
        code = cfg.get("code") or CODE_TEMPLATE
        try:
            timeout = float(cfg.get("timeout") or 60)
        except (TypeError, ValueError):
            timeout = 60.0
        job = self._new_dir()
        out_dir = job / "output"
        out_dir.mkdir()
        (job / "node.py").write_text(code, encoding="utf-8")
        (job / "meta.json").write_text(json.dumps({**item.meta, "filename": item.filename}, ensure_ascii=False),
                                       encoding="utf-8")
        (job / "runner.py").write_text(_RUNNER, encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(job / "runner.py"), str(job / "node.py"), str(item.path),
            str(job / "meta.json"), str(out_dir), item.filename,
            cwd=job, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise FlowError(f"took longer than {timeout:.0f} s") from None
        for line in stdout.decode(errors="replace").splitlines()[-50:]:
            self.say(f"  [code] {line}", "muted")
        if proc.returncode != 0:
            lines = [line for line in stderr.decode(errors="replace").splitlines() if line.strip()]
            for line in lines[-20:]:
                self.say(f"  [code] {line.replace(str(job) + '/', '')}", "muted")
            where = [m.group(1) for line in lines if (m := re.search(r'node\.py", line (\d+)', line))]
            message = lines[-1].strip() if lines else f"exit code {proc.returncode}"
            raise FlowError(message[:300] + (f" (line {where[-1]})" if where else ""))
        produced = []
        for path in sorted(p for p in out_dir.rglob("*") if p.is_file()):
            name = safe_filename(path.name)
            produced.append(Item(name, item.meta | {"content_type": None, "source_file": item.filename}, path))
        names = ", ".join(i.filename for i in produced) or "nothing (dropped)"
        self.say(f"  Python code: {item.filename} → {names}", "ok" if produced else "muted")
        return produced

    def _new_dir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="node-", dir=self.workdir))


class FlowError(RuntimeError):
    pass


_RUNNER = '''\
import json, pathlib, runpy, sys
code_file, input_path, meta_file, out_dir, filename = sys.argv[1:6]
module = runpy.run_path(code_file)
process = module.get("process")
if not callable(process):
    sys.exit("Define a function: def process(input_path, meta, output_dir)")
out = pathlib.Path(out_dir)
result = process(pathlib.Path(input_path), json.loads(pathlib.Path(meta_file).read_text("utf-8")), out)
if isinstance(result, bytes):
    (out / (pathlib.Path(filename).stem + ".md")).write_bytes(result)
elif isinstance(result, str):
    (out / (pathlib.Path(filename).stem + ".md")).write_text(result, "utf-8")
'''


def workdir(base: Path) -> Path:
    path = Path(tempfile.mkdtemp(prefix="flow-", dir=base))
    return path


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
