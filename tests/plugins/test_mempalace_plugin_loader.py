from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "memory" / "mempalace"
INIT_FILE = PLUGIN_DIR / "__init__.py"
MODULE_NAME = "plugins.memory.mempalace"


def _ensure_package(name: str, package_dir: Path) -> None:
    if name in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        name,
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    if spec and spec.loader and (package_dir / "__init__.py").exists():
        spec.loader.exec_module(module)


def load_plugin_module():
    _ensure_package("plugins", REPO_ROOT / "plugins")
    _ensure_package("plugins.memory", REPO_ROOT / "plugins" / "memory")
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        INIT_FILE,
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def plugin_module():
    return load_plugin_module()


def test_plugin_loads_via_importlib(plugin_module):
    assert hasattr(plugin_module, "MemPalaceMemoryProvider")
    assert hasattr(plugin_module, "register")


def test_register_works_when_loaded_like_hermes(plugin_module):
    ctx = MagicMock()
    plugin_module.register(ctx)
    ctx.register_memory_provider.assert_called_once()


def test_initialize_uses_current_hermes_config_shape(plugin_module, tmp_path):
    provider = plugin_module.MemPalaceMemoryProvider()
    hermes_home = tmp_path / ".hermes"
    config = {
        "memory": {"provider": "mempalace"},
        "mempalace": {
            "palace_path": str(tmp_path / "palace"),
            "wing": "conversations",
            "n_results": 5,
            "tool_max_results": 7,
            "enable_kg": True,
            "collection_template": "hermes-{platform}-{user_id}",
            "room_strategy": "platform_session",
            "fixed_room": "memory",
        },
    }

    provider.initialize(
        session_id="sess-test",
        hermes_home=str(hermes_home),
        config=config,
        user_id="jessica",
        agent_id="hermes",
        platform="telegram",
    )

    try:
        assert provider.name == "mempalace"
        assert provider.is_available() is True
        assert provider._collection is not None
        assert provider._queue is not None
        assert provider._kg is not None
        assert provider._palace_path == str(tmp_path / "palace")
        assert provider._wing == "conversations"
        assert provider._n_results == 5
        assert provider._tool_max_results == 7
        assert provider._runtime_ctx["platform"] == "telegram"
        assert provider._collection_name == "hermes-telegram-jessica"
        result = provider.handle_tool_call("mempalace_status", {})
        payload = json.loads(result)
        assert isinstance(payload, dict)
        assert payload["collection_name"] == "hermes-telegram-jessica"
        assert payload["room_strategy"] == "platform_session"
    finally:
        provider.shutdown()


def test_initialize_respects_disable_kg(plugin_module, tmp_path):
    provider = plugin_module.MemPalaceMemoryProvider()
    provider.initialize(
        session_id="sess-no-kg",
        hermes_home=str(tmp_path / ".hermes"),
        config={
            "mempalace": {
                "palace_path": str(tmp_path / "palace-no-kg"),
                "enable_kg": False,
            }
        },
        user_id="u1",
        agent_id="hermes",
    )
    try:
        assert provider._kg is None
    finally:
        provider.shutdown()


def test_tool_schemas_exposed(plugin_module):
    provider = plugin_module.MemPalaceMemoryProvider()
    names = {schema["name"] for schema in provider.get_tool_schemas()}
    assert names == {
        "mempalace_memorize",
        "mempalace_search",
        "mempalace_recall",
        "mempalace_forget",
        "mempalace_status",
    }
