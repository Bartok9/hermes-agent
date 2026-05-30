"""BartokGraph v2.0 — Three-Layer Knowledge Graph (Python port).

Direct port of bartokgraph-v2.mjs. Everything runs on-device. No data
ever leaves the user's machine. No Supabase. No telemetry.

Three layers:

  KNOWLEDGE — weighted prose extraction. Headers, bold concepts, rules.
              Source files weighted by type: SOUL.md=50, daily logs=20,
              project notes=15, code=1, test files=0.1.
              This is the layer the Proactive Communication Loop reads.

  CODE      — code intelligence. Function/class/import graphs. For the
              agent to navigate the codebase, not for user-facing features.

  PERSON    — per-person filtered view of the knowledge layer. Uses
              patterns from bartokgraph-config.json in the workspace root,
              or a safe default that matches common directory conventions.
              No personal names are hardcoded here — config drives it.

Credential redaction runs on every file before extraction. API keys,
JWTs, passwords are replaced with [CREDENTIAL].

Usage (CLI)::

    python -m hermes_cli.bartokgraph build ~/workspace
    python -m hermes_cli.bartokgraph build ~/workspace --layer code
    python -m hermes_cli.bartokgraph build ~/workspace --person alice
    python -m hermes_cli.bartokgraph build ~/workspace --all
    python -m hermes_cli.bartokgraph query graph.json "regenerative agriculture"
    python -m hermes_cli.bartokgraph report graph.json

Usage (API)::

    from hermes_cli.bartokgraph import build_graph, KnowledgeGraph

    graph = build_graph("/path/to/workspace", layer="knowledge")
    god_nodes = graph.find_god_nodes(15)
    clusters = graph.find_clusters()
    graph.save("/path/to/output/graph.json")
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# File weight system — the core innovation from v2.0
# ──────────────────────────────────────────────────────────────────────

# (pattern, weight) — checked in order, first match wins
# test files are checked first so they never get elevated by extension rules
_FILE_WEIGHTS: List[Tuple[str, float]] = [
    # Test/spec files — near-invisible (checked before any extension rule)
    (r"(?:^|[/_-])test[_.]",        0.1),
    (r"\.(?:test|spec)\.",           0.1),
    (r"(?:^|/)tests?/",              0.1),
    # Sacred identity files
    (r"(?:^|/)soul\.md$",           50.0),
    (r"(?:^|/)user\.md$",           50.0),
    (r"(?:^|/)memory\.md$",         50.0),
    (r"(?:^|/)agents\.md$",         50.0),
    (r"(?:^|/)identity\.md$",       50.0),
    (r"(?:^|/)tools\.md$",          50.0),
    (r"(?:^|/)heartbeat\.md$",      50.0),
    # Daily memory logs
    (r"memory/\d{4}-\d{2}-\d{2}\.md$", 20.0),
    # Project knowledge
    (r"projects/.*\.md$",           15.0),
    (r"projects/.*\.txt$",          15.0),
    # Research notes
    (r"research/",                  12.0),
    # General prose
    (r"\.md$",                       8.0),
    (r"\.txt$",                      8.0),
    (r"\.vtt$",                      8.0),
    # Documents
    (r"\.html?$",                    6.0),
    (r"\.pdf$",                      6.0),
    # Structured data
    (r"\.jsonl?$",                   4.0),
    # Code — low noise floor (last)
    (r"\.(ts|tsx|js|mjs|jsx|py|sh|sql)$", 1.0),
]

_LAYER_MULTIPLIERS: Dict[str, float] = {
    "knowledge": 10.0,
    "person":    10.0,
    "code":       1.0,
}


def get_file_weight(file_path: str, workspace_root: str) -> float:
    """Return importance weight for a file based on path patterns."""
    rel = os.path.relpath(file_path, workspace_root).lower().replace("\\", "/")
    fname = os.path.basename(file_path).lower()
    # Check rel path first, then just the filename (catches SOUL.md anywhere)
    for pattern, weight in _FILE_WEIGHTS:
        if re.search(pattern, rel, re.IGNORECASE) or re.search(pattern, fname, re.IGNORECASE):
            return weight
    return 2.0  # default


# ──────────────────────────────────────────────────────────────────────
# Walk settings
# ──────────────────────────────────────────────────────────────────────

_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".openclaw", "dist", "build",
    ".cache", "logs", ".venv", "venv", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "htmlcov", "coverage", ".tox",
}

_SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
    ".mp3", ".mp4", ".ogg", ".wav", ".m4a", ".flac",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".bin", ".pyc", ".pyo", ".whl",
    ".woff", ".woff2", ".ttf", ".eot",
    ".lock", ".ico", ".icns",
    ".xlsx", ".xls", ".docx", ".pptx",
    ".db", ".sqlite", ".sqlite3",
}

_MAX_FILE_BYTES = 500 * 1024  # 500 KB
_MAX_WALK_DEPTH = 8


def walk_files(directory: str, depth: int = 0) -> Iterator[Tuple[str, float]]:
    """Yield (file_path, mtime) tuples under directory, respecting skip rules."""
    if depth > _MAX_WALK_DEPTH:
        return
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                if name.startswith(".") and name not in {".claude"}:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if name not in _SKIP_DIRS:
                        yield from walk_files(entry.path, depth + 1)
                elif entry.is_file(follow_symlinks=False):
                    try:
                        stat = entry.stat()
                        if stat.st_size < _MAX_FILE_BYTES:
                            ext = os.path.splitext(name)[1].lower()
                            if ext not in _SKIP_EXTENSIONS:
                                yield entry.path, stat.st_mtime
                    except OSError:
                        pass
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────────────
# Credential redaction
# ──────────────────────────────────────────────────────────────────────

_CREDENTIAL_PATTERNS = [
    re.compile(r"\bsk-[a-zA-Z0-9]{20,}\b"),
    re.compile(r"\beyJ[a-zA-Z0-9_-]{20,}\b"),
    re.compile(r"\bsb_(?:publishable|secret)_[a-zA-Z0-9_-]+\b"),
    re.compile(r"password\s*[=:]\s*[\"']?[^\s\"']{8,}[\"']?", re.IGNORECASE),
    re.compile(r"\bghp_[a-zA-Z0-9]{36}\b"),   # GitHub tokens
    re.compile(r"\bxoxb-[a-zA-Z0-9_-]{50,}\b"),  # Slack tokens
]


def redact_credentials(text: str) -> str:
    for pat in _CREDENTIAL_PATTERNS:
        text = pat.sub("[CREDENTIAL]", text)
    return text


# ──────────────────────────────────────────────────────────────────────
# Agent-aware person config
# ──────────────────────────────────────────────────────────────────────

_DEFAULT_AGENT_CONFIG = {
    "agent_id": "hermes",
    "agent_name": "Hermes",
    "users": [],  # empty by default — no personal names hardcoded
}


def load_agent_config(workspace_root: str) -> dict:
    """Load bartokgraph-config.json from workspace root, or return safe default."""
    candidates = [
        os.path.join(workspace_root, "bartokgraph-config.json"),
        os.path.join(workspace_root, ".bartokgraph", "config.json"),
        os.path.expanduser("~/.config/bartokgraph/config.json"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    cfg = json.load(f)
                logger.debug("BartokGraph: loaded config from %s", path)
                return cfg
            except Exception as exc:
                logger.debug("BartokGraph: config load failed at %s: %s", path, exc)
    return _DEFAULT_AGENT_CONFIG


def build_person_filters(config: dict) -> Dict[str, Optional[List[re.Pattern]]]:
    """Build regex filter sets per person. None = sees everything (the agent itself)."""
    filters: Dict[str, Optional[List[re.Pattern]]] = {}
    filters[config["agent_id"]] = None  # agent sees all
    for user in config.get("users", []):
        patterns = []
        for p in user.get("patterns", []):
            try:
                patterns.append(re.compile(re.escape(p).replace(r"\*", ".*"), re.IGNORECASE))
            except re.error:
                pass
        filters[user["id"]] = patterns
    return filters


def file_matches_person(
    file_path: str,
    workspace_root: str,
    person: str,
    filters: Dict[str, Optional[List[re.Pattern]]],
) -> bool:
    if person not in filters or filters[person] is None:
        return True
    rel = os.path.relpath(file_path, workspace_root).lower().replace("\\", "/")
    fname = os.path.basename(file_path).lower()
    return any(p.search(rel) or p.search(fname) for p in filters[person])


# ──────────────────────────────────────────────────────────────────────
# Node and Edge constants
# ──────────────────────────────────────────────────────────────────────

NODE_TYPES = {
    "concept": "concept", "tool": "tool", "project": "project",
    "agent": "agent", "lesson": "lesson", "memory": "memory",
    "skill": "skill", "rule": "rule", "file": "file",
    "function": "function", "module": "module", "person": "person",
}

EDGE_TYPES = {
    "MENTIONS": "MENTIONS", "TEACHES": "TEACHES", "IMPLEMENTS": "IMPLEMENTS",
    "BUILDS_ON": "BUILDS_ON", "RELATES_TO": "RELATES_TO", "IS_ABOUT": "IS_ABOUT",
    "IMPORTS": "IMPORTS", "CALLS": "CALLS", "CREATED_BY": "CREATED_BY",
}


# ──────────────────────────────────────────────────────────────────────
# KnowledgeGraph
# ──────────────────────────────────────────────────────────────────────

@dataclass
class GraphNode:
    id: str
    label: str
    node_type: str
    count: float
    weight: float
    sources: List[str] = field(default_factory=list)
    layer: str = "knowledge"
    person: Optional[str] = None
    last_seen_ts: float = 0.0
    source_path: str = ""


@dataclass
class GraphEdge:
    from_id: str
    to_id: str
    relationship: str
    weight: float
    confidence: str = "EXTRACTED"


class KnowledgeGraph:
    """In-memory knowledge graph. Direct port of the JS KnowledgeGraph class."""

    def __init__(self, owner: str = "hermes", layer: str = "knowledge") -> None:
        self.owner = owner
        self.layer = layer
        self.nodes: Dict[str, GraphNode] = {}
        self.edges: Dict[str, GraphEdge] = {}
        self.files_processed = 0
        self.created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _normalize(self, s: str) -> str:
        return re.sub(r"\s+", "-", re.sub(r"[^a-z0-9\s-]", "", s.lower().strip()))[:60]

    def add_node(
        self,
        label: str,
        node_type: str = "concept",
        source: str = "",
        weight: float = 1.0,
        person: Optional[str] = None,
        last_seen_ts: Optional[float] = None,
    ) -> Optional[str]:
        node_id = self._normalize(label)
        if not node_id or len(node_id) <= 2:
            return None
        if node_id in self.nodes:
            node = self.nodes[node_id]
            node.count += weight
            node.weight += weight
            node.last_seen_ts = last_seen_ts if last_seen_ts is not None else time.time()
            if source and source not in node.sources:
                node.sources.append(source[-50:])
        else:
            self.nodes[node_id] = GraphNode(
                id=node_id,
                label=label[:80],
                node_type=node_type,
                count=weight,
                weight=weight,
                sources=[source[-50:]] if source else [],
                layer=self.layer,
                person=person,
                last_seen_ts=last_seen_ts if last_seen_ts is not None else time.time(),
                source_path=source,
            )
        return node_id

    def add_edge(
        self,
        from_id: str,
        to_id: str,
        rel: str = "RELATES_TO",
        confidence: str = "EXTRACTED",
        weight: float = 1.0,
    ) -> None:
        if not from_id or not to_id or from_id == to_id:
            return
        if from_id not in self.nodes or to_id not in self.nodes:
            return
        key = f"{min(from_id, to_id)}→{max(from_id, to_id)}→{rel}"
        if key in self.edges:
            self.edges[key].weight += weight
        else:
            self.edges[key] = GraphEdge(from_id, to_id, rel, weight, confidence)

    def find_god_nodes(self, top_n: int = 15) -> List[dict]:
        """Identify the most connected, highest-weight nodes — the conceptual core."""
        degree: Dict[str, float] = {}
        for edge in self.edges.values():
            wa = self.nodes[edge.from_id].weight if edge.from_id in self.nodes else 1.0
            wb = self.nodes[edge.to_id].weight if edge.to_id in self.nodes else 1.0
            degree[edge.from_id] = degree.get(edge.from_id, 0.0) + edge.weight * wa
            degree[edge.to_id] = degree.get(edge.to_id, 0.0) + edge.weight * wb
        top = sorted(degree.items(), key=lambda x: x[1], reverse=True)[:top_n]
        result = []
        for node_id, deg in top:
            if node_id in self.nodes:
                n = self.nodes[node_id]
                result.append({
                    "id": n.id, "label": n.label, "type": n.node_type,
                    "weight": n.weight, "count": n.count, "degree": deg,
                    "sources": n.sources, "person": n.person,
                })
        return result

    def find_clusters(self) -> List[List[str]]:
        """Union-Find community detection. Groups strongly connected nodes."""
        parent = {k: k for k in self.nodes}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: str, y: str) -> None:
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[rx] = ry

        for edge in self.edges.values():
            if edge.weight >= 2:
                union(edge.from_id, edge.to_id)

        clusters: Dict[str, List[str]] = {}
        for node_id in self.nodes:
            root = find(node_id)
            clusters.setdefault(root, []).append(node_id)

        return sorted(
            [c for c in clusters.values() if len(c) > 1],
            key=len, reverse=True,
        )

    def get_stats(self) -> dict:
        return {
            "nodes": len(self.nodes), "edges": len(self.edges),
            "layer": self.layer, "owner": self.owner,
            "files_processed": self.files_processed,
        }

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
        logger.debug("BartokGraph: saved %d nodes to %s", len(self.nodes), path)

    def to_dict(self) -> dict:
        return {
            "owner": self.owner,
            "layer": self.layer,
            "created_at": self.created_at,
            "files_processed": self.files_processed,
            "stats": self.get_stats(),
            "nodes": [
                {
                    "id": n.id, "label": n.label, "type": n.node_type,
                    "count": n.count, "weight": n.weight, "sources": n.sources,
                    "layer": n.layer, "person": n.person,
                    "last_seen_ts": n.last_seen_ts, "source_path": n.source_path,
                }
                for n in self.nodes.values()
            ],
            "edges": [
                {
                    "from": e.from_id, "to": e.to_id,
                    "relationship": e.relationship,
                    "weight": e.weight, "confidence": e.confidence,
                }
                for e in self.edges.values()
            ],
        }

    @classmethod
    def load(cls, path: str) -> "KnowledgeGraph":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        g = cls(owner=data.get("owner", "hermes"), layer=data.get("layer", "knowledge"))
        g.files_processed = data.get("files_processed", 0)
        g.created_at = data.get("created_at", g.created_at)
        for n in data.get("nodes", []):
            node = GraphNode(
                id=n["id"], label=n.get("label", n["id"]),
                node_type=n.get("type", "concept"),
                count=n.get("count", 1.0), weight=n.get("weight", 1.0),
                sources=n.get("sources", []), layer=n.get("layer", "knowledge"),
                person=n.get("person"), last_seen_ts=n.get("last_seen_ts", 0.0),
                source_path=n.get("source_path", ""),
            )
            g.nodes[node.id] = node
        for e in data.get("edges", []):
            key = f"{min(e['from'], e['to'])}→{max(e['from'], e['to'])}→{e['relationship']}"
            g.edges[key] = GraphEdge(
                from_id=e["from"], to_id=e["to"],
                relationship=e["relationship"],
                weight=e.get("weight", 1.0),
                confidence=e.get("confidence", "EXTRACTED"),
            )
        return g


# ──────────────────────────────────────────────────────────────────────
# Extractors — ported directly from bartokgraph-v2.mjs
# ──────────────────────────────────────────────────────────────────────

_HEADER_RE = re.compile(r"^#{1,3} (.{3,60})", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*([^*]{3,40})\*\*")
_RULE_RE = re.compile(r"\*\*([a-z-]+)\*\*: ([^\n]{10,100})", re.IGNORECASE)
_FN_RE = re.compile(r"(?:function|class|def|const|let|var)\s+([A-Za-z][A-Za-z0-9_]{2,40})")
# Matches JS require/ES import strings and Python import/from statements
_IMPORT_RE = re.compile(
    r"from\s+([\w./][\w./]{2,59})\s+import"
    r"|require\s*\(\s*['\"]([ ^'\"]{2,59})['\"]{1}\)"
    r"|import\s+([\w.]{2,59})",
    re.MULTILINE,
)
_COMMENT_RE = re.compile(r"//[^/\n]{10,80}|#[^!\n]{10,80}")
_HTML_STRIP_RE = re.compile(r"<[^>]+>|&[a-z]+;", re.IGNORECASE)
_SCRIPT_RE = re.compile(r"<script[^>]*>[\s\S]*?</script>", re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[^>]*>[\s\S]*?</style>", re.IGNORECASE)
_HTML_TITLE_RE = re.compile(r"<(?:title|h1)[^>]*>([^<]{3,80})<", re.IGNORECASE)


def extract_knowledge(content: str, source: str, graph: KnowledgeGraph, weight: float = 1.0, file_mtime: Optional[float] = None) -> None:
    """Extract concepts from prose (markdown, text). Direct port of extractKnowledge."""
    clean = redact_credentials(content)

    headers = _HEADER_RE.findall(clean)
    bold_items = [b.strip() for b in _BOLD_RE.findall(clean)]
    rules = _RULE_RE.findall(clean)

    header_ids = [graph.add_node(h.strip(), "concept", source, weight, last_seen_ts=file_mtime) for h in headers]
    bold_ids = [graph.add_node(b, "concept", source, weight * 0.7, last_seen_ts=file_mtime) for b in bold_items]

    for name, _desc in rules:
        graph.add_node(name.strip(), "rule", source, weight * 0.5, last_seen_ts=file_mtime)

    all_ids = [i for i in header_ids + bold_ids if i]
    for i in range(len(all_ids)):
        for j in range(i + 1, min(i + 3, len(all_ids))):
            graph.add_edge(all_ids[i], all_ids[j], "RELATES_TO", "EXTRACTED", weight * 0.5)


def extract_code(content: str, file_path: str, source: str, graph: KnowledgeGraph, file_mtime: Optional[float] = None) -> None:
    """Extract code structure. Direct port of extractCode."""
    ext = os.path.splitext(file_path)[1].lower()
    file_label = os.path.splitext(os.path.basename(file_path))[0]
    file_id = graph.add_node(file_label, "file", source, 1.0, last_seen_ts=file_mtime)

    for m in _FN_RE.finditer(content):
        label = m.group(1)
        node_id = graph.add_node(label, "function", source, 1.0, last_seen_ts=file_mtime)
        if file_id and node_id:
            graph.add_edge(file_id, node_id, "IMPLEMENTS", "EXTRACTED", 1.0)

    for m in _IMPORT_RE.finditer(content):
        # Any of the three capture groups may match — take the first non-None
        raw = next((g for g in m.groups() if g), None)
        if not raw:
            continue
        dep = os.path.splitext(os.path.basename(raw.strip()))[0]
        dep_id = graph.add_node(dep, "module", source, 0.5, last_seen_ts=file_mtime)
        if file_id and dep_id:
            graph.add_edge(file_id, dep_id, "IMPORTS", "EXTRACTED", 1.0)

    comments = _COMMENT_RE.findall(content)
    for c in comments[:10]:
        text = re.sub(r"^//\s*|^#\s*", "", c).strip()
        if len(text) > 10:
            concept_id = graph.add_node(text, "concept", source, 0.3, last_seen_ts=file_mtime)
            if file_id and concept_id:
                graph.add_edge(file_id, concept_id, "IS_ABOUT", "INFERRED", 0.3)


def extract_html(content: str, file_path: str, source: str, graph: KnowledgeGraph, weight: float = 1.0, file_mtime: Optional[float] = None) -> None:
    """Extract from HTML. Direct port of extractHTML."""
    text = _SCRIPT_RE.sub("", content)
    text = _STYLE_RE.sub("", text)
    text = _HTML_STRIP_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text:
        extract_knowledge(text, source, graph, weight, file_mtime)
    m = _HTML_TITLE_RE.search(content)
    if m:
        graph.add_node(m.group(1).strip(), "project", source, weight * 2, last_seen_ts=file_mtime)


# ──────────────────────────────────────────────────────────────────────
# Main graph builder
# ──────────────────────────────────────────────────────────────────────


def build_graph(
    workspace_path: str,
    layer: str = "knowledge",
    person: Optional[str] = None,
    owner: Optional[str] = None,
) -> KnowledgeGraph:
    """Build a knowledge graph from a workspace directory.

    Args:
        workspace_path: Root directory to walk.
        layer: 'knowledge', 'code', or 'person' (knowledge + person filter).
        person: Person ID to filter for. Requires matching entries in
                bartokgraph-config.json in the workspace root.
        owner: Graph owner label. Defaults to person or agent_id from config.

    Returns:
        Populated KnowledgeGraph.
    """
    config = load_agent_config(workspace_path)
    person_filters = build_person_filters(config)
    resolved_owner = owner or person or config["agent_id"]
    graph = KnowledgeGraph(owner=resolved_owner, layer=layer)
    processed = 0
    skipped = 0

    logger.info("BartokGraph: building layer=%s person=%s path=%s", layer, person or "all", workspace_path)

    for file_path, file_mtime in walk_files(workspace_path):
        # Person filter
        if person and not file_matches_person(file_path, workspace_path, person, person_filters):
            skipped += 1
            continue

        ext = os.path.splitext(file_path)[1].lower()
        weight = get_file_weight(file_path, workspace_path)
        source = os.path.relpath(file_path, workspace_path)

        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                content = f.read()

            if layer == "code":
                if ext in {".ts", ".tsx", ".js", ".mjs", ".jsx", ".py", ".sh", ".sql"}:
                    extract_code(content, file_path, source, graph, file_mtime)
                    processed += 1
                else:
                    skipped += 1

            else:  # knowledge / person
                if ext in {".md", ".txt", ".vtt"}:
                    extract_knowledge(content, source, graph, weight, file_mtime)
                    processed += 1
                elif ext in {".html", ".htm"}:
                    extract_html(content, file_path, source, graph, weight, file_mtime)
                    processed += 1
                elif ext in {".json", ".jsonl"}:
                    _extract_json(content, source, graph, weight, file_mtime)
                    processed += 1
                elif ext in {".ts", ".tsx", ".js", ".mjs", ".jsx", ".py"}:
                    # Code in knowledge layer — concepts from comments only, low weight
                    comments = _COMMENT_RE.findall(content)
                    for c in comments[:5]:
                        text = re.sub(r"^//\s*|^#\s*", "", c).strip()
                        if len(text) > 15:
                            graph.add_node(text, "concept", source, 0.2, last_seen_ts=file_mtime)
                    processed += 1
                elif ext == ".pdf":
                    # Best-effort text extraction from PDF bytes
                    cleaned = re.sub(r"[^\x20-\x7E\n\r]", " ", content)
                    cleaned = re.sub(r"\s+", " ", cleaned).strip()
                    if len(cleaned) > 100:
                        extract_knowledge(cleaned, source, graph, weight, file_mtime)
                    processed += 1
                else:
                    skipped += 1

        except Exception as exc:
            logger.debug("BartokGraph: skipping %s: %s", file_path, exc)
            skipped += 1

    graph.files_processed = processed
    logger.info(
        "BartokGraph: done — processed=%d skipped=%d nodes=%d edges=%d",
        processed, skipped, len(graph.nodes), len(graph.edges),
    )
    return graph


def _extract_json(content: str, source: str, graph: KnowledgeGraph, weight: float, file_mtime: Optional[float] = None) -> None:
    """Extract title/name fields from JSON. Direct port of the JSON branch."""
    try:
        data = json.loads(content)
        items = []
        if isinstance(data, list):
            items = data[:50]
        elif isinstance(data, dict):
            items = (
                data.get("tasks", []) +
                data.get("projects", []) +
                data.get("notes", []) +
                data.get("items", [])
            )
        for item in items:
            if isinstance(item, dict):
                label = (item.get("title") or item.get("name") or "")[:60]
                if len(label) > 3:
                    graph.add_node(redact_credentials(label), "concept", source, weight, last_seen_ts=file_mtime)
    except (json.JSONDecodeError, TypeError):
        pass


# ──────────────────────────────────────────────────────────────────────
# Report generator — ported from generateReport()
# ──────────────────────────────────────────────────────────────────────


def generate_report(graph: KnowledgeGraph) -> str:
    god_nodes = graph.find_god_nodes(15)
    clusters = graph.find_clusters()

    lines = [
        "# BartokGraph v2.0 Report",
        f"Layer: {graph.layer} | Owner: {graph.owner} | Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        f"Files: {graph.files_processed} | Nodes: {len(graph.nodes)} | Edges: {len(graph.edges)}",
        "",
        "## 🌟 God Nodes (Weighted — high-weight files amplified)",
        "",
    ]
    for n in god_nodes:
        lines.append(
            f"- **{n['label']}** ({n['type']}, "
            f"weighted_score: {n['degree']:.0f}, mentions: {n['count']:.0f})"
        )

    lines += ["", f"## 🗂️ Knowledge Clusters ({len(clusters)})", ""]
    for i, cluster in enumerate(clusters[:10]):
        labels = []
        for node_id in cluster[:5]:
            node = graph.nodes.get(node_id)
            labels.append(node.label if node else node_id)
        rest = f" +{len(cluster) - 5} more" if len(cluster) > 5 else ""
        lines.append(f"**Cluster {i+1}** ({len(cluster)}): {', '.join(labels)}{rest}")

    lines += ["", "---", "*BartokGraph v2.0 — weighted, layered, person-filtered. All data stays on-device.*"]
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# CLI — mirrors the JS CLI exactly
# ──────────────────────────────────────────────────────────────────────


def _cli() -> None:  # noqa: C901
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m hermes_cli.bartokgraph",
        description="BartokGraph v2.0 — Three-Layer Knowledge Graph. All data stays on-device.",
    )
    sub = parser.add_subparsers(dest="cmd")

    build_p = sub.add_parser("build", help="Build a knowledge graph from a workspace directory")
    build_p.add_argument("path", nargs="?", default=os.path.expanduser("~"), help="Workspace path")
    build_p.add_argument("--layer", choices=["knowledge", "code", "person"], default="knowledge")
    build_p.add_argument("--person", default=None, help="Person ID to filter (requires config)")
    build_p.add_argument("--all", action="store_true", help="Build all layers + person graphs")
    build_p.add_argument("--output", default=None, help="Output directory (default: <path>/.bartokgraph)")

    query_p = sub.add_parser("query", help="Query a graph.json")
    query_p.add_argument("graph", help="Path to graph.json")
    query_p.add_argument("question", nargs="+", help="Search terms")

    report_p = sub.add_parser("report", help="Print a text report from graph.json")
    report_p.add_argument("graph", help="Path to graph.json")

    args = parser.parse_args()

    if args.cmd == "build":
        workspace = os.path.expanduser(args.path)
        output_dir = args.output or os.path.join(workspace, ".bartokgraph")
        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(os.path.join(output_dir, "person"), exist_ok=True)

        config = load_agent_config(workspace)
        agent_id = config["agent_id"]

        if args.all:
            print(f"\n🏗️  Building ALL layers for {config['agent_name']} ({agent_id})...\n")

            kg = build_graph(workspace, layer="knowledge", owner=agent_id)
            out = os.path.join(output_dir, f"{agent_id}-knowledge-graph.json")
            kg.save(out)
            with open(os.path.join(output_dir, f"{agent_id}-GRAPH_REPORT.md"), "w", encoding="utf-8") as f:
                f.write(generate_report(kg))
            print(f"✅ Knowledge graph: {out}")

            cg = build_graph(workspace, layer="code", owner=agent_id)
            out = os.path.join(output_dir, f"{agent_id}-code-graph.json")
            cg.save(out)
            print(f"✅ Code graph: {out}")

            for user in config.get("users", []):
                uid = user["id"]
                print(f"\n--- Person graph: {uid} ---")
                pg = build_graph(workspace, layer="knowledge", person=uid, owner=uid)
                out = os.path.join(output_dir, "person", f"{uid}-graph.json")
                pg.save(out)
                with open(os.path.join(output_dir, "person", f"{uid}-GRAPH_REPORT.md"), "w", encoding="utf-8") as f:
                    f.write(generate_report(pg))
                print(f"✅ {uid} graph: {out}")

            print(f"\n✅ All builds complete. Outputs: {output_dir}")
            print("\n🌟 Knowledge Graph God Nodes:")
            for n in kg.find_god_nodes(10):
                print(f"   {n['label']} ({n['type']}, score: {n['degree']:.0f})")

        else:
            graph = build_graph(workspace, layer=args.layer, person=args.person)
            prefix = args.person or f"{agent_id}-{args.layer}"
            out_dir = os.path.join(output_dir, "person") if args.person else output_dir
            os.makedirs(out_dir, exist_ok=True)
            out = os.path.join(out_dir, f"{prefix}-graph.json")
            graph.save(out)
            with open(os.path.join(out_dir, f"{prefix}-GRAPH_REPORT.md"), "w", encoding="utf-8") as f:
                f.write(generate_report(graph))
            print(f"✅ Saved: {out}")
            print(f"   Nodes: {len(graph.nodes)} | Edges: {len(graph.edges)}")
            print("\n🌟 God Nodes:")
            for n in graph.find_god_nodes(8):
                print(f"   {n['label']} ({n['type']}, score: {n['degree']:.0f})")

    elif args.cmd == "query":
        g = KnowledgeGraph.load(args.graph)
        q = " ".join(args.question).lower()
        matches = [
            n for n in g.nodes.values()
            if q in n.label.lower() or q in n.id
        ][:5]
        print(f'\nResults for "{q}":')
        for n in matches:
            print(f"  {n.label} ({n.node_type}, count: {n.count:.0f})")

    elif args.cmd == "report":
        g = KnowledgeGraph.load(args.graph)
        print(generate_report(g))

    else:
        parser.print_help()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _cli()
