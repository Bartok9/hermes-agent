"""BartokGraph — Knowledge graph builder for the Proactive Communication Loop.

BartokGraph is an optional bundled plugin that maps concepts, projects, people,
and ideas from the user's files and conversation history into a weighted knowledge
graph with typed edges.

Standalone usage::

    hermes bartokgraph build ~/my-notes
    hermes bartokgraph query ~/my-notes "what connects my AI work to my health?"
    hermes bartokgraph report ~/my-notes

Local model support::

    # Default: Ollama with qwen3:8b (zero API cost)
    hermes bartokgraph build ~/my-notes

    # Specify a different local model
    BARTOKGRAPH_LLM_MODEL=gemma2:27b hermes bartokgraph build ~/my-notes

    # Use any OpenAI-compatible API
    BARTOKGRAPH_API_BASE=https://api.openai.com/v1 \\
    BARTOKGRAPH_API_KEY=$OPENAI_API_KEY \\
    BARTOKGRAPH_LLM_MODEL=gpt-4o-mini \\
    hermes bartokgraph build ~/my-notes

Integration with the Proactive Communication Loop::

    # In hermes config:
    proactive_communication:
      enabled: true
      bartokgraph:
        enabled: true          # use graph augmentation (default: true)
        workspace: "~"         # what to graph
        local_model: qwen3:8b  # model for graph building
        rebuild_interval_days: 7
"""

from hermes_cli.bartokgraph_adapter import BartokGraphAdapter, _resolve_local_model_provider

__all__ = ["BartokGraphAdapter", "_resolve_local_model_provider"]

PLUGIN_NAME = "bartokgraph"
PLUGIN_VERSION = "1.0.0"
PLUGIN_DESCRIPTION = (
    "BartokGraph knowledge graph builder — surfaces cross-temporal and cross-domain "
    "connections for the Proactive Communication Loop. Runs locally with zero API cost."
)
