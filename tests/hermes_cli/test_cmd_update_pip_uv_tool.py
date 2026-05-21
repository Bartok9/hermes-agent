"""Tests for _cmd_update_pip uv-tool-install detection (issue #29700).

When Hermes is installed via `uv tool install hermes-agent`, the update
command must use `uv tool upgrade hermes-agent` instead of
`uv pip install --upgrade hermes-agent`, which requires an active venv.
"""
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest


# Import the function under test; guard against heavy import side-effects
# by patching print during module load.
def _get_cmd_update_pip():
    from hermes_cli.main import _cmd_update_pip
    return _cmd_update_pip


class TestCmdUpdatePipUvToolDetection:
    """_cmd_update_pip routes to the correct uv sub-command."""

    def _run(self, uv_path, tool_list_output, tool_list_rc=0, update_rc=0):
        """Helper: run _cmd_update_pip with controlled subprocess outcomes."""
        fn = _get_cmd_update_pip()

        tool_list_result = SimpleNamespace(
            returncode=tool_list_rc,
            stdout=tool_list_output,
            stderr="",
        )
        update_result = SimpleNamespace(returncode=update_rc)

        captured_cmds = []

        def fake_run(cmd, *args, **kwargs):
            captured_cmds.append(cmd)
            if kwargs.get("capture_output"):
                return tool_list_result
            return update_result

        with patch("shutil.which", return_value=uv_path), \
             patch("subprocess.run", side_effect=fake_run), \
             patch("hermes_cli.main.__version__", "0.14.0", create=True), \
             patch("builtins.print"):
            fn(SimpleNamespace())

        return captured_cmds

    def test_uv_tool_install_uses_uv_tool_upgrade(self):
        """When hermes-agent appears in `uv tool list`, use `uv tool upgrade`."""
        cmds = self._run(
            uv_path="/usr/bin/uv",
            tool_list_output="hermes-agent v0.14.0\n",
        )
        # First call: tool list probe; second call: the actual upgrade
        assert len(cmds) == 2
        assert cmds[0] == ["/usr/bin/uv", "tool", "list"]
        assert cmds[1] == ["/usr/bin/uv", "tool", "upgrade", "hermes-agent"]

    def test_no_uv_tool_install_uses_uv_pip(self):
        """When hermes-agent is NOT in `uv tool list`, fall back to uv pip."""
        cmds = self._run(
            uv_path="/usr/bin/uv",
            tool_list_output="some-other-tool v1.0\n",
        )
        assert len(cmds) == 2
        assert cmds[0] == ["/usr/bin/uv", "tool", "list"]
        assert cmds[1] == ["/usr/bin/uv", "pip", "install", "--upgrade", "hermes-agent"]

    def test_tool_list_failure_falls_back_to_uv_pip(self):
        """If `uv tool list` exits non-zero, still fall back to uv pip."""
        cmds = self._run(
            uv_path="/usr/bin/uv",
            tool_list_output="",
            tool_list_rc=1,
        )
        assert cmds[-1] == ["/usr/bin/uv", "pip", "install", "--upgrade", "hermes-agent"]

    def test_no_uv_falls_back_to_pip_module(self):
        """When uv is not installed at all, use sys.executable -m pip."""
        fn = _get_cmd_update_pip()
        update_result = SimpleNamespace(returncode=0)
        captured_cmds = []

        def fake_run(cmd, *args, **kwargs):
            captured_cmds.append(cmd)
            return update_result

        with patch("shutil.which", return_value=None), \
             patch("subprocess.run", side_effect=fake_run), \
             patch("hermes_cli.main.__version__", "0.14.0", create=True), \
             patch("builtins.print"):
            fn(SimpleNamespace())

        assert len(captured_cmds) == 1
        assert captured_cmds[0] == [sys.executable, "-m", "pip", "install", "--upgrade", "hermes-agent"]

    def test_tool_list_timeout_falls_back_to_uv_pip(self):
        """TimeoutExpired during `uv tool list` should fall back to uv pip."""
        fn = _get_cmd_update_pip()
        update_result = SimpleNamespace(returncode=0)
        captured_cmds = []
        call_count = 0

        def fake_run(cmd, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("capture_output"):
                raise subprocess.TimeoutExpired(cmd, 15)
            captured_cmds.append(cmd)
            return update_result

        with patch("shutil.which", return_value="/usr/bin/uv"), \
             patch("subprocess.run", side_effect=fake_run), \
             patch("hermes_cli.main.__version__", "0.14.0", create=True), \
             patch("builtins.print"):
            fn(SimpleNamespace())

        assert captured_cmds == [["/usr/bin/uv", "pip", "install", "--upgrade", "hermes-agent"]]
