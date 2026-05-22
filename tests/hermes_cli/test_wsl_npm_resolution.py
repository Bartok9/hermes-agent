"""Tests for WSL-aware npm resolution and _update_node_dependencies() error reporting.

Regression test for: [Bug] WSL update path resolves Windows npm, breaks
ui-tui dependency refresh, but still reports "Update complete!" (issue #30271).
"""

import sys
from unittest.mock import MagicMock, patch, mock_open
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Import helpers (avoid importing the full hermes_cli.main at module level
# since it has side effects and may not have all optional dependencies)
# ---------------------------------------------------------------------------

def _import_helpers():
    from hermes_cli.main import _is_wsl, _resolve_linux_npm, _update_node_dependencies
    return _is_wsl, _resolve_linux_npm, _update_node_dependencies


# ---------------------------------------------------------------------------
# _is_wsl
# ---------------------------------------------------------------------------

class TestIsWsl:
    def test_returns_false_on_non_linux(self):
        _is_wsl, _, _ = _import_helpers()
        with patch.object(sys, "platform", "darwin"):
            assert _is_wsl() is False

    def test_returns_false_on_windows(self):
        _is_wsl, _, _ = _import_helpers()
        with patch.object(sys, "platform", "win32"):
            assert _is_wsl() is False

    def test_returns_true_when_proc_version_contains_microsoft(self):
        _is_wsl, _, _ = _import_helpers()
        with patch.object(sys, "platform", "linux"):
            with patch("builtins.open", mock_open(read_data="Linux version 5.15.0 Microsoft WSL2")):
                assert _is_wsl() is True

    def test_returns_false_when_proc_version_is_plain_linux(self):
        _is_wsl, _, _ = _import_helpers()
        with patch.object(sys, "platform", "linux"):
            with patch("builtins.open", mock_open(read_data="Linux version 6.8.0-generic #40-Ubuntu")):
                assert _is_wsl() is False

    def test_returns_false_when_proc_version_unreadable(self):
        _is_wsl, _, _ = _import_helpers()
        with patch.object(sys, "platform", "linux"):
            with patch("builtins.open", side_effect=OSError("no such file")):
                assert _is_wsl() is False


# ---------------------------------------------------------------------------
# _resolve_linux_npm
# ---------------------------------------------------------------------------

class TestResolveLinuxNpm:
    def _setup(self, which_a_output, shutil_which_result=None):
        import subprocess as _sp
        completed = MagicMock()
        completed.stdout = which_a_output
        completed.returncode = 0
        return completed

    def test_prefers_usr_npm_over_mnt_npm(self):
        _, _resolve_linux_npm, _ = _import_helpers()
        result = MagicMock()
        result.stdout = "/mnt/c/Program Files/nodejs/npm\n/usr/bin/npm\n"
        result.returncode = 0
        with patch("subprocess.run", return_value=result):
            npm = _resolve_linux_npm()
        assert npm == "/usr/bin/npm"

    def test_prefers_home_npm_over_mnt_npm(self):
        _, _resolve_linux_npm, _ = _import_helpers()
        result = MagicMock()
        result.stdout = "/mnt/c/Windows/npm\n/home/user/.nvm/versions/node/v20/bin/npm\n"
        result.returncode = 0
        with patch("subprocess.run", return_value=result):
            npm = _resolve_linux_npm()
        assert npm == "/home/user/.nvm/versions/node/v20/bin/npm"

    def test_returns_first_candidate_when_no_linux_native(self):
        _, _resolve_linux_npm, _ = _import_helpers()
        result = MagicMock()
        result.stdout = "/mnt/c/Windows/npm\n/mnt/d/tools/npm\n"
        result.returncode = 0
        with patch("subprocess.run", return_value=result):
            npm = _resolve_linux_npm()
        assert npm == "/mnt/c/Windows/npm"

    def test_falls_back_to_shutil_which_on_subprocess_error(self):
        _, _resolve_linux_npm, _ = _import_helpers()
        with patch("subprocess.run", side_effect=Exception("which not found")):
            with patch("shutil.which", return_value="/usr/bin/npm") as mock_which:
                npm = _resolve_linux_npm()
        assert npm == "/usr/bin/npm"


# ---------------------------------------------------------------------------
# _update_node_dependencies — WSL guard
# ---------------------------------------------------------------------------

class TestUpdateNodeDependenciesWslGuard:
    def test_skips_windows_npm_in_wsl_and_returns_false(self, capsys):
        """When WSL resolves a /mnt/ npm, the update should be skipped and return False."""
        _, _, _update_node_dependencies = _import_helpers()
        with patch("hermes_cli.main._is_wsl", return_value=True):
            with patch("hermes_cli.main._resolve_linux_npm", return_value="/mnt/c/npm"):
                result = _update_node_dependencies()
        assert result is False
        captured = capsys.readouterr()
        assert "Windows path" in captured.out or "Windows" in captured.out

    def test_uses_resolve_linux_npm_in_wsl(self):
        """In WSL, _resolve_linux_npm is called instead of shutil.which."""
        _, _, _update_node_dependencies = _import_helpers()
        with patch("hermes_cli.main._is_wsl", return_value=True):
            with patch("hermes_cli.main._resolve_linux_npm", return_value=None) as mock_resolve:
                with patch("shutil.which") as mock_which:
                    _update_node_dependencies()
        mock_resolve.assert_called_once()
        mock_which.assert_not_called()

    def test_non_wsl_uses_shutil_which(self):
        """Outside WSL, shutil.which is used as before."""
        _, _, _update_node_dependencies = _import_helpers()
        with patch("hermes_cli.main._is_wsl", return_value=False):
            with patch("shutil.which", return_value=None) as mock_which:
                _update_node_dependencies()
        mock_which.assert_called_once_with("npm")

    def test_returns_false_on_npm_install_failure(self, tmp_path):
        """Returns False (not True) when npm install fails, so callers can report partial update."""
        _, _, _update_node_dependencies = _import_helpers()
        (tmp_path / "package.json").write_text("{}")
        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stderr = "npm error EISDIR"
        with patch("hermes_cli.main._is_wsl", return_value=False):
            with patch("shutil.which", return_value="/usr/bin/npm"):
                with patch("hermes_cli.main.PROJECT_ROOT", tmp_path):
                    with patch("hermes_cli.main._run_npm_install_deterministic", return_value=fake_result):
                        result = _update_node_dependencies()
        assert result is False

    def test_returns_true_on_success(self, tmp_path):
        """Returns True when all npm installs succeed."""
        _, _, _update_node_dependencies = _import_helpers()
        (tmp_path / "package.json").write_text("{}")
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = ""
        with patch("hermes_cli.main._is_wsl", return_value=False):
            with patch("shutil.which", return_value="/usr/bin/npm"):
                with patch("hermes_cli.main.PROJECT_ROOT", tmp_path):
                    with patch("hermes_cli.main._run_npm_install_deterministic", return_value=fake_result):
                        result = _update_node_dependencies()
        assert result is True
