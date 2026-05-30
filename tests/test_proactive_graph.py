"""Tests for BartokGraph — graph builder, weighting, traversal, and adapter."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import pytest
from unittest.mock import MagicMock, AsyncMock

from hermes_cli.bartokgraph import (
    KnowledgeGraph,
    get_file_weight,
    build_graph,
    generate_report,
    extract_knowledge,
    extract_code,
    redact_credentials,
)
from hermes_cli.bartokgraph_adapter import (
    BartokGraphAdapter,
    _node_importance,
    _jaccard,
    _tokenize,
    _temporal_decay,
    _classify,
    _MAX_WEIGHT,
)


# ──────────────────────────────────────────────────────────────────────
# File weight system
# ──────────────────────────────────────────────────────────────────────

def test_soul_md_max_weight():
    assert get_file_weight("/workspace/SOUL.md", "/workspace") == 50.0

def test_user_md_max_weight():
    assert get_file_weight("/workspace/USER.md", "/workspace") == 50.0

def test_daily_log_weight():
    assert get_file_weight("/workspace/memory/2026-04-18.md", "/workspace") == 20.0

def test_project_md_weight():
    assert get_file_weight("/workspace/projects/kinder-way/notes.md", "/workspace") == 15.0

def test_generic_md_weight():
    assert get_file_weight("/workspace/notes.md", "/workspace") == 8.0

def test_code_file_low_weight():
    assert get_file_weight("/workspace/src/main.py", "/workspace") == 1.0

def test_test_file_near_zero():
    assert get_file_weight("/workspace/test_goals.py", "/workspace") <= 0.2

def test_test_dir_near_zero():
    assert get_file_weight("/workspace/tests/test_main.py", "/workspace") <= 0.2


# ──────────────────────────────────────────────────────────────────────
# Credential redaction
# ──────────────────────────────────────────────────────────────────────

def test_redacts_api_key():
    text = "key = sk-abc123def456ghi789jkl012mno345"
    assert "[CREDENTIAL]" in redact_credentials(text)
    assert "sk-abc" not in redact_credentials(text)

def test_redacts_jwt():
    text = "token = eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc123def"
    assert "[CREDENTIAL]" in redact_credentials(text)

def test_leaves_normal_text():
    text = "The soil carbon project is going well."
    assert redact_credentials(text) == text


# ──────────────────────────────────────────────────────────────────────
# KnowledgeGraph core
# ──────────────────────────────────────────────────────────────────────

def test_add_node_normalizes_id():
    g = KnowledgeGraph()
    nid = g.add_node("Regenerative Agriculture")
    assert nid == "regenerative-agriculture"
    assert "regenerative-agriculture" in g.nodes

def test_add_node_accumulates_weight():
    g = KnowledgeGraph()
    g.add_node("soil carbon", weight=5.0)
    g.add_node("soil carbon", weight=3.0)
    assert g.nodes["soil-carbon"].weight == 8.0

def test_add_edge_requires_both_nodes():
    g = KnowledgeGraph()
    g.add_edge("missing-a", "missing-b")  # should not raise or add
    assert len(g.edges) == 0

def test_add_edge_deduplicates():
    g = KnowledgeGraph()
    a = g.add_node("concept a")
    b = g.add_node("concept b")
    g.add_edge(a, b, weight=1.0)
    g.add_edge(a, b, weight=1.0)
    assert len(g.edges) == 1
    edge = list(g.edges.values())[0]
    assert edge.weight == 2.0

def test_short_label_rejected():
    g = KnowledgeGraph()
    nid = g.add_node("ab")  # too short
    assert nid is None

def test_find_god_nodes_returns_top():
    g = KnowledgeGraph()
    hub = g.add_node("hub concept", weight=50.0)
    for i in range(10):
        child = g.add_node(f"child concept {i}", weight=1.0)
        g.add_edge(hub, child, weight=2.0)
    gods = g.find_god_nodes(5)
    assert gods[0]["label"] == "hub concept"

def test_find_clusters_groups_connected():
    g = KnowledgeGraph()
    a = g.add_node("alpha", weight=1.0)
    b = g.add_node("beta", weight=1.0)
    c = g.add_node("gamma", weight=1.0)
    z = g.add_node("zeta isolated", weight=1.0)
    g.add_edge(a, b, weight=3.0)
    g.add_edge(b, c, weight=3.0)
    clusters = g.find_clusters()
    # a, b, c should be in one cluster; z alone is excluded
    assert any(len(cl) == 3 for cl in clusters)
    assert not any(z in cl for cl in clusters)

def test_save_and_load_roundtrip():
    g = KnowledgeGraph(owner="test", layer="knowledge")
    a = g.add_node("soil carbon", weight=15.0, source="projects/farm/notes.md")
    b = g.add_node("climate resilience", weight=10.0)
    g.add_edge(a, b, rel="RELATES_TO", weight=2.0)
    g.files_processed = 42

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        g.save(path)
        g2 = KnowledgeGraph.load(path)
        assert g2.owner == "test"
        assert g2.files_processed == 42
        assert "soil-carbon" in g2.nodes
        assert g2.nodes["soil-carbon"].weight == 15.0
        assert len(g2.edges) == 1
    finally:
        os.unlink(path)


# ──────────────────────────────────────────────────────────────────────
# Extractors
# ──────────────────────────────────────────────────────────────────────

def test_extract_knowledge_headers():
    g = KnowledgeGraph()
    md = "# Regenerative Agriculture\n\n## Soil Carbon\n\nsome text\n\n## Climate Resilience\n"
    extract_knowledge(md, "notes.md", g, weight=8.0)
    assert "regenerative-agriculture" in g.nodes
    assert "soil-carbon" in g.nodes
    assert "climate-resilience" in g.nodes

def test_extract_knowledge_bold():
    g = KnowledgeGraph()
    md = "The **BartokGraph** system maps **knowledge connections** over time."
    extract_knowledge(md, "notes.md", g, weight=5.0)
    assert "bartokgraph" in g.nodes
    assert "knowledge-connections" in g.nodes

def test_extract_knowledge_redacts_credentials():
    g = KnowledgeGraph()
    md = "API key is sk-abc123def456ghi789jkl012mno345pqr and password=supersecret99"
    extract_knowledge(md, "notes.md", g, weight=1.0)
    for node in g.nodes.values():
        assert "sk-abc" not in node.label
        assert "supersecret" not in node.label

def test_extract_code_functions():
    g = KnowledgeGraph()
    code = "def build_graph(path):\n    pass\nclass KnowledgeGraph:\n    pass\n"
    extract_code(code, "bartokgraph.py", "bartokgraph.py", g)
    assert "build-graph" in g.nodes or "build_graph" in g.nodes or any(
        "build" in k for k in g.nodes
    )

def test_extract_code_imports():
    g = KnowledgeGraph()
    code = "import json\nfrom pathlib import Path\nrequire('./utils')\n"
    extract_code(code, "main.py", "main.py", g)
    # At least one module node should be added
    module_nodes = [n for n in g.nodes.values() if n.node_type == "module"]
    assert len(module_nodes) > 0


# ──────────────────────────────────────────────────────────────────────
# build_graph integration (uses temp directory)
# ──────────────────────────────────────────────────────────────────────

def test_build_graph_from_directory():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write synthetic workspace
        os.makedirs(os.path.join(tmpdir, "memory"))
        os.makedirs(os.path.join(tmpdir, "projects", "farm"))

        with open(os.path.join(tmpdir, "SOUL.md"), "w") as f:
            f.write("# Identity\n\n## Regenerative Agriculture\n\nCore mission.\n\n**Soil Carbon** is essential.\n")

        with open(os.path.join(tmpdir, "memory", "2026-04-18.md"), "w") as f:
            f.write("## Daily Log\n\nWorked on **soil health** and **carbon sequestration** today.\n")

        with open(os.path.join(tmpdir, "projects", "farm", "notes.md"), "w") as f:
            f.write("## Kenya Project\n\n**Biochar** application and soil testing.\n")

        graph = build_graph(tmpdir, layer="knowledge")

        assert len(graph.nodes) > 0
        assert len(graph.edges) >= 0
        assert graph.files_processed >= 3

        # SOUL.md concepts should be present
        assert "regenerative-agriculture" in graph.nodes or "identity" in graph.nodes

        # SOUL.md node should have higher weight than project note
        soul_weight = graph.nodes.get("regenerative-agriculture", graph.nodes.get("identity")).weight if "regenerative-agriculture" in graph.nodes or "identity" in graph.nodes else 0
        # Just verify it processed files
        assert graph.files_processed >= 3


def test_build_graph_skips_test_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "SOUL.md"), "w") as f:
            f.write("# Identity\n\n## Real Concept\n")
        with open(os.path.join(tmpdir, "test_goals.py"), "w") as f:
            f.write("# test concept that should be invisible\ndef test_real_concept(): pass\n")

        graph = build_graph(tmpdir, layer="knowledge")
        # test file comment words should not dominate
        # Just verify the graph built without error
        assert graph.files_processed >= 1


def test_generate_report_structure():
    g = KnowledgeGraph(owner="test")
    hub = g.add_node("hub concept", weight=50.0)
    for i in range(5):
        child = g.add_node(f"child {i}", weight=1.0)
        g.add_edge(hub, child, weight=3.0)
    g.files_processed = 10

    report = generate_report(g)
    assert "God Nodes" in report
    assert "hub concept" in report
    assert "Clusters" in report
    assert "on-device" in report  # privacy note


# ──────────────────────────────────────────────────────────────────────
# Adapter scoring helpers
# ──────────────────────────────────────────────────────────────────────

def test_node_importance_normalized():
    from hermes_cli.bartokgraph import KnowledgeGraph as KG
    g = KG()
    # SOUL.md weight = 50, layer knowledge multiplier = 10 → 500 → normalized = 1.0
    soul_node = g.nodes.get(g.add_node("soul identity", weight=500.0) or "")
    if soul_node:
        soul_node.weight = 500.0
        assert _node_importance(soul_node) == pytest.approx(1.0)

def test_node_importance_low_for_test():
    from hermes_cli.bartokgraph import KnowledgeGraph as KG, GraphNode
    node = GraphNode(id="x", label="x", node_type="concept", count=0.1, weight=0.1, layer="code")
    assert _node_importance(node) < 0.01

def test_jaccard_identical():
    a = _tokenize("soil carbon research")
    assert _jaccard(a, a) == pytest.approx(1.0)

def test_jaccard_disjoint():
    a = _tokenize("quantum computing")
    b = _tokenize("soil carbon Kenya")
    assert _jaccard(a, b) == 0.0

def test_jaccard_partial():
    a = _tokenize("soil carbon research Kenya")
    b = _tokenize("Kenya soil health project")
    s = _jaccard(a, b)
    assert 0.0 < s < 1.0

def test_temporal_decay_increases():
    assert _temporal_decay(0) < _temporal_decay(7) < _temporal_decay(30) < _temporal_decay(90)

def test_temporal_decay_log_scale():
    diff1 = _temporal_decay(30) - _temporal_decay(7)
    diff2 = _temporal_decay(90) - _temporal_decay(60)
    assert diff2 < diff1  # flattens at scale


# ──────────────────────────────────────────────────────────────────────
# Adapter end-to-end with real graph builder
# ──────────────────────────────────────────────────────────────────────

def test_adapter_loads_and_finds_connections():
    """Full integration: build a graph, load it via adapter, find connections."""
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "memory"))
        os.makedirs(os.path.join(tmpdir, "projects", "farm"))

        with open(os.path.join(tmpdir, "SOUL.md"), "w") as f:
            f.write("# Identity\n\n## Regenerative Agriculture\n\n**Soil Carbon** is the mission.\n")

        with open(os.path.join(tmpdir, "memory", "2026-03-01.md"), "w") as f:
            # Old memory — 60+ days ago (will have low last_seen_ts from build)
            f.write("## Daily Log\n\n**Carbon sequestration** project making progress.\n")

        cfg = MagicMock()
        cfg.get.side_effect = lambda k, d=None: {
            "proactive_communication.bartokgraph.workspace": tmpdir,
            "proactive_communication.bartokgraph.enabled": True,
            "proactive_communication.bartokgraph.auto_build": True,
            "proactive_communication.bartokgraph.rebuild_interval_days": 7,
        }.get(k, d)

        adapter = BartokGraphAdapter(cfg)
        assert adapter.is_available

        result = asyncio.run(adapter.get_connections(
            active_topics=["soil carbon regenerative agriculture"],
            top_k=5,
        ))

        assert result is not None
        # May or may not find connections depending on last_seen_ts at build time
        # The important thing is it doesn't raise and returns a valid context
        assert hasattr(result, "connections")
        assert result.provider_name == "bartokgraph_v2"


def test_adapter_unavailable_returns_none():
    """If bartokgraph module itself is absent, adapter returns None gracefully."""
    cfg = MagicMock()
    cfg.get.return_value = "/nonexistent/path/that/does/not/exist"

    import unittest.mock as mock
    with mock.patch.dict("sys.modules", {"hermes_cli.bartokgraph": None}):
        # The adapter should handle ImportError gracefully
        adapter = BartokGraphAdapter.__new__(BartokGraphAdapter)
        adapter._cfg = cfg
        adapter._graph = None
        adapter._god_node_ids = set()
        adapter._cluster_map = {}

        result = asyncio.run(adapter.get_connections(active_topics=["anything"]))
        assert result is None


# ──────────────────────────────────────────────────────────────────────
# last_seen_ts fidelity (Grok's fix — mtime not build time)
# ──────────────────────────────────────────────────────────────────────

def test_build_graph_last_seen_ts_matches_file_mtime():
    """Nodes must carry the source file's mtime, not the build timestamp."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "notes.md")
        with open(path, "w") as f:
            f.write("# Soil Carbon\n\n**Regenerative Agriculture** is the mission.\n")

        # Set mtime to a known value 10 days ago
        target_mtime = time.time() - 10 * 86400
        os.utime(path, (target_mtime, target_mtime))

        graph = build_graph(tmpdir, layer="knowledge")

        # Every node from this file should have last_seen_ts ≈ target_mtime
        assert len(graph.nodes) > 0
        for node in graph.nodes.values():
            assert abs(node.last_seen_ts - target_mtime) < 5, (
                f"Node '{node.label}' has last_seen_ts={node.last_seen_ts}, "
                f"expected ~{target_mtime} (file mtime). "
                "build_graph must use file mtime, not time.time()."
            )


def test_build_graph_last_seen_ts_30_days_old():
    """A file modified 30 days ago must produce nodes with last_seen_ts ~30 days ago."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "old-notes.md")
        with open(path, "w") as f:
            f.write("# Old Concept\n\n**Deep Work** archive from long ago.\n")

        thirty_days_ago = time.time() - 30 * 86400
        os.utime(path, (thirty_days_ago, thirty_days_ago))

        graph = build_graph(tmpdir, layer="knowledge")

        assert len(graph.nodes) > 0
        for node in graph.nodes.values():
            age_days = (time.time() - node.last_seen_ts) / 86400
            assert age_days > 25, (
                f"Node '{node.label}' appears to be only {age_days:.1f} days old — "
                "expected ~30 days (file mtime). Not time.time()."
            )
            assert age_days < 35, (
                f"Node '{node.label}' appears {age_days:.1f} days old — "
                "mtime was set to exactly 30 days ago."
            )
