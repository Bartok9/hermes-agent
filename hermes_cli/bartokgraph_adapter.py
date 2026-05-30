"""BartokGraph adapter — bridges BartokGraph to the Proactive Communication Loop.

This adapter owns two responsibilities:

1. GRAPH AVAILABILITY — load or build the knowledge graph.
   - Looks for an existing graph.json in the configured workspace.
   - If none found (or stale), triggers a fresh build via bartokgraph.build_graph().
   - All data stays on-device. Zero network calls.

2. CONNECTION TRAVERSAL — given today's active topics, find the connections
   the user cannot see themselves.
   - Ranks by surprise score: semantic_strength × node_importance × temporal_decay
   - God nodes (highest weighted-degree) are boosted — they represent the
     conceptual core of the user's knowledge, and connections to them matter most.
   - Cluster membership is checked: topics in the same cluster as a dormant god
     node score highest — they're structurally important, not just coincidentally similar.

All weighting logic lives in bartokgraph.py (ported from bartokgraph-v2.mjs).
This adapter only orchestrates traversal and scoring.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from hermes_cli.proactive_communication_loop import BartokGraphConnection, BartokGraphContext

logger = logging.getLogger(__name__)

# How many days before a graph.json is considered stale and rebuilt
_GRAPH_STALE_DAYS = 7

# Minimum surprise score to include a connection (low — importance weighting filters noise)
_MIN_SURPRISE_SCORE = 0.05


class BartokGraphAdapter:
    """Adapter between BartokGraph and ProactiveCommunicationLoop.

    Constructed once per ProactiveCommunicationLoop instance. Loads or
    builds the knowledge graph, then traverses it to find cross-temporal
    connections for each synthesis pass.
    """

    def __init__(self, config: Any) -> None:
        self._cfg = config
        self._graph = self._load_or_build_graph()
        self._god_node_ids: Set[str] = set()
        self._cluster_map: Dict[str, int] = {}  # node_id → cluster_index
        if self._graph is not None:
            self._precompute_topology()

    @property
    def is_available(self) -> bool:
        return self._graph is not None

    async def get_connections(
        self,
        active_topics: List[str],
        top_k: int = 10,
        exclude_recent_hours: int = 24,
    ) -> Optional[BartokGraphContext]:
        """Find cross-temporal connections between today's topics and past knowledge.

        Returns None only if the graph is unavailable (not built).
        Returns BartokGraphContext with empty connections if nothing scores high enough.
        """
        if self._graph is None:
            return None

        t0 = time.monotonic()
        try:
            connections = self._find_connections(active_topics, top_k, exclude_recent_hours)
            return BartokGraphContext(
                connections=connections,
                provider_name="bartokgraph_v2",
                traversal_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("BartokGraphAdapter: traversal failed: %s", exc)
            return BartokGraphContext(connections=[], provider_name="error")

    # ──────────────────────────────────────────────────────────────────
    # Graph loading / building
    # ──────────────────────────────────────────────────────────────────

    def _load_or_build_graph(self):
        """Load an existing graph or build a fresh one. Never raises."""
        try:
            from hermes_cli.bartokgraph import KnowledgeGraph, build_graph
        except ImportError as exc:
            logger.debug("BartokGraphAdapter: bartokgraph module unavailable: %s", exc)
            return None

        workspace = os.path.expanduser(
            self._cfg.get("proactive_communication.bartokgraph.workspace", "~")
        )

        # Candidate graph paths (BartokGraph v2.0 output locations)
        candidates = [
            os.path.join(workspace, ".bartokgraph", "bartok-knowledge-graph.json"),
            os.path.join(workspace, ".bartokgraph", "hermes-knowledge-graph.json"),
            os.path.join(workspace, ".bartokgraph", "graph.json"),
            os.path.join(workspace, "bartokgraph-output", "bartok-knowledge-graph.json"),
        ]

        # Check if a fresh-enough graph exists
        for path in candidates:
            if os.path.exists(path):
                age_days = (time.time() - os.path.getmtime(path)) / 86400
                stale_days = float(self._cfg.get(
                    "proactive_communication.bartokgraph.rebuild_interval_days",
                    _GRAPH_STALE_DAYS,
                ))
                if age_days < stale_days:
                    try:
                        graph = KnowledgeGraph.load(path)
                        logger.debug(
                            "BartokGraphAdapter: loaded %d nodes from %s (%.1f days old)",
                            len(graph.nodes), path, age_days,
                        )
                        return graph
                    except Exception as exc:
                        logger.debug("BartokGraphAdapter: load failed, will rebuild: %s", exc)
                else:
                    logger.debug(
                        "BartokGraphAdapter: graph at %s is %.1f days old (stale > %.1f) — rebuilding",
                        path, age_days, stale_days,
                    )

        # Build a fresh graph
        should_build = self._cfg.get("proactive_communication.bartokgraph.auto_build", True)
        if not should_build:
            logger.debug("BartokGraphAdapter: auto_build disabled, no graph available")
            return None

        try:
            logger.info("BartokGraphAdapter: building knowledge graph for %s...", workspace)
            graph = build_graph(workspace, layer="knowledge")
            # Save for next time
            out_dir = os.path.join(workspace, ".bartokgraph")
            os.makedirs(out_dir, exist_ok=True)
            graph.save(os.path.join(out_dir, "hermes-knowledge-graph.json"))
            logger.info(
                "BartokGraphAdapter: built graph — %d nodes, %d edges",
                len(graph.nodes), len(graph.edges),
            )
            return graph
        except Exception as exc:
            logger.debug("BartokGraphAdapter: build failed: %s", exc)
            return None

    def _precompute_topology(self) -> None:
        """Pre-compute god nodes and cluster membership for fast traversal."""
        try:
            god_nodes = self._graph.find_god_nodes(top_n=20)
            self._god_node_ids = {n["id"] for n in god_nodes}

            clusters = self._graph.find_clusters()
            for i, cluster in enumerate(clusters):
                for node_id in cluster:
                    self._cluster_map[node_id] = i

            logger.debug(
                "BartokGraphAdapter: topology precomputed — %d god nodes, %d clusters",
                len(self._god_node_ids), len(clusters),
            )
        except Exception as exc:
            logger.debug("BartokGraphAdapter: topology precompute failed: %s", exc)

    # ──────────────────────────────────────────────────────────────────
    # Traversal and scoring
    # ──────────────────────────────────────────────────────────────────

    def _find_connections(
        self,
        active_topics: List[str],
        top_k: int,
        exclude_recent_hours: int,
    ) -> List[BartokGraphConnection]:
        cutoff_ts = time.time() - exclude_recent_hours * 3600

        # Dormant nodes: not active recently, non-trivial weight
        dormant = [
            n for n in self._graph.nodes.values()
            if n.last_seen_ts < cutoff_ts and n.weight > 0.1
        ]

        # Normalize active topics
        topic_tokens = [_tokenize(t) for t in active_topics[:8]]

        scored: List[Tuple[float, BartokGraphConnection]] = []

        for node in dormant:
            node_tokens = _tokenize(node.label)
            if not node_tokens:
                continue

            best_semantic = 0.0
            best_topic = ""
            for topic, tokens in zip(active_topics[:8], topic_tokens):
                sem = _jaccard(tokens, node_tokens)
                if sem > best_semantic:
                    best_semantic = sem
                    best_topic = topic

            if best_semantic < 0.15:  # pre-filter before expensive scoring
                continue

            # Node importance from the graph's own weight (accumulated during build)
            raw_importance = _node_importance(node)

            # Boost for god nodes — they're the conceptual core
            is_god = node.id in self._god_node_ids
            god_boost = 1.5 if is_god else 1.0

            # Boost for cluster alignment: if today's topic is in the same cluster
            # as this dormant node, that's structurally significant
            cluster_boost = 1.0
            for tokens in topic_tokens:
                topic_id = _to_node_id(best_topic)
                if topic_id in self._cluster_map and node.id in self._cluster_map:
                    if self._cluster_map[topic_id] == self._cluster_map[node.id]:
                        cluster_boost = 1.3
                        break

            days_apart = max(0, int((time.time() - node.last_seen_ts) / 86400))
            temporal = _temporal_decay(days_apart)

            surprise = best_semantic * raw_importance * temporal * god_boost * cluster_boost

            if surprise < _MIN_SURPRISE_SCORE:
                continue

            conn_type = _classify(node, days_apart, is_god)
            explanation = _explain(best_topic, node, conn_type, days_apart, raw_importance, is_god)

            scored.append((surprise, BartokGraphConnection(
                node_a_content=best_topic,
                node_b_content=node.label,
                connection_type=conn_type,
                strength=surprise,
                days_apart=days_apart,
                explanation=explanation,
            )))

        # Sort by surprise, deduplicate by node label
        scored.sort(key=lambda x: x[0], reverse=True)
        seen: Set[str] = set()
        result: List[BartokGraphConnection] = []
        for _, conn in scored:
            key = conn.node_b_content[:80]
            if key not in seen:
                seen.add(key)
                result.append(conn)
            if len(result) >= top_k:
                break

        logger.debug(
            "BartokGraphAdapter: %d candidates → %d unique connections (top surprise: %.3f)",
            len(scored), len(result), scored[0][0] if scored else 0.0,
        )
        return result


# ──────────────────────────────────────────────────────────────────────
# Scoring helpers
# ──────────────────────────────────────────────────────────────────────

_MAX_WEIGHT = 500.0  # SOUL.md (50) × knowledge layer (10×) = 500

_STOPWORDS = {
    "the", "a", "an", "in", "on", "at", "to", "for", "of", "and", "or",
    "is", "was", "are", "were", "i", "you", "me", "my", "your", "it", "its",
    "this", "that", "with", "from", "have", "had", "not", "but", "be", "by",
    "as", "we", "they", "do", "did", "has", "all", "can", "will", "just",
}


def _tokenize(s: str) -> frozenset:
    words = {
        w.strip(".,!?;:\"'()[]")
        for w in s.lower().split()
    }
    return frozenset(w for w in words if len(w) > 2 and w not in _STOPWORDS)


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _to_node_id(s: str) -> str:
    return re.sub(r"\s+", "-", re.sub(r"[^a-z0-9\s-]", "", s.lower().strip()))[:60]


def _node_importance(node) -> float:
    """Normalize node weight to 0–1 against the maximum possible."""
    return min(1.0, node.weight / _MAX_WEIGHT)


def _temporal_decay(days_apart: int) -> float:
    """Older dormant connections score higher — more likely forgotten."""
    return 1.0 + math.log1p(days_apart / 7.0)


def _classify(node, days_apart: int, is_god: bool) -> str:
    if node.person:
        return "person_knowledge"
    if node.layer == "code":
        return "cross_domain"
    if is_god and days_apart >= 7:
        return "temporal_bridge"
    if days_apart >= 7:
        return "temporal_bridge"
    return "temporal_bridge"


def _explain(topic: str, node, conn_type: str, days_apart: int, importance: float, is_god: bool) -> str:
    importance_label = (
        "core concept" if importance > 0.6 else
        "important" if importance > 0.3 else
        "notable"
    )
    god_note = " [god node — conceptual core]" if is_god else ""
    weeks = days_apart // 7

    if conn_type == "person_knowledge":
        time_str = f"{weeks}w" if weeks >= 2 else f"{days_apart}d"
        return (
            f"'{node.person}' mentioned '{node.label}' {time_str} ago "
            f"({importance_label}{god_note}) — connects to today's '{topic}'"
        )
    if conn_type == "temporal_bridge":
        time_str = f"{weeks} weeks" if weeks >= 2 else f"{days_apart} days"
        return (
            f"'{node.label}' appeared {time_str} ago "
            f"({importance_label}{god_note}) — same concept as today's '{topic}'"
        )
    return (
        f"'{topic}' structurally mirrors '{node.label}' "
        f"from a different domain ({days_apart}d dormant, {importance_label}{god_note})"
    )
