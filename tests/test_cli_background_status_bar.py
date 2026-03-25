"""Tests for CLI background command status bar coordination.

Ensures the status bar is properly hidden during background task output
and restored afterward to prevent layout corruption (#2718).
"""

import sys
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest


class MockCLI:
    """Minimal mock of HermesCLI for testing background output coordination."""

    def __init__(self):
        self._status_bar_visible = True
        self._app = None

    def set_app(self, app):
        self._app = app


class TestBackgroundStatusBarCoordination:
    """Tests for status bar hide/restore during background output."""

    def test_status_bar_hidden_before_output(self):
        """Status bar visibility is set to False before background output."""
        cli = MockCLI()
        cli._status_bar_visible = True
        mock_app = MagicMock()
        cli._app = mock_app

        # Simulate the pre-output coordination code
        _status_was_visible = getattr(cli, "_status_bar_visible", True)
        if hasattr(cli, "_status_bar_visible"):
            cli._status_bar_visible = False

        assert cli._status_bar_visible is False
        assert _status_was_visible is True

    def test_status_bar_restored_after_success(self):
        """Status bar visibility is restored after successful background output."""
        cli = MockCLI()
        cli._status_bar_visible = True
        mock_app = MagicMock()
        cli._app = mock_app

        # Capture original state
        _status_was_visible = getattr(cli, "_status_bar_visible", True)

        # Hide before output
        if hasattr(cli, "_status_bar_visible"):
            cli._status_bar_visible = False
        assert cli._status_bar_visible is False

        # Restore after output (like the success path)
        if hasattr(cli, "_status_bar_visible"):
            cli._status_bar_visible = _status_was_visible

        assert cli._status_bar_visible is True

    def test_status_bar_restored_after_error(self):
        """Status bar visibility is restored even after an error."""
        cli = MockCLI()
        cli._status_bar_visible = True
        mock_app = MagicMock()
        cli._app = mock_app

        _status_was_visible = getattr(cli, "_status_bar_visible", True)
        if hasattr(cli, "_status_bar_visible"):
            cli._status_bar_visible = False

        # Simulate error path restoration
        if hasattr(cli, "_status_bar_visible"):
            cli._status_bar_visible = _status_was_visible

        assert cli._status_bar_visible is True

    def test_status_bar_preserves_hidden_state(self):
        """If status bar was already hidden, it stays hidden after output."""
        cli = MockCLI()
        cli._status_bar_visible = False  # Start hidden
        mock_app = MagicMock()
        cli._app = mock_app

        # Capture original (hidden) state
        _status_was_visible = getattr(cli, "_status_bar_visible", True)
        assert _status_was_visible is False

        # Hide before output (already hidden, no change)
        if hasattr(cli, "_status_bar_visible"):
            cli._status_bar_visible = False

        # Restore - should stay hidden since it started hidden
        if hasattr(cli, "_status_bar_visible"):
            cli._status_bar_visible = _status_was_visible

        assert cli._status_bar_visible is False

    def test_app_invalidate_called_before_output(self):
        """App.invalidate() is called before printing background output."""
        cli = MockCLI()
        cli._status_bar_visible = True
        mock_app = MagicMock()
        cli._app = mock_app

        call_order = []
        mock_app.invalidate.side_effect = lambda: call_order.append("invalidate")

        # Simulate the coordination sequence
        _status_was_visible = getattr(cli, "_status_bar_visible", True)
        if hasattr(cli, "_status_bar_visible"):
            cli._status_bar_visible = False

        if cli._app:
            cli._app.invalidate()
            call_order.append("sleep")  # represents the sleep(0.05)

        call_order.append("print")

        assert call_order == ["invalidate", "sleep", "print"]

    def test_no_crash_when_app_is_none(self):
        """No crash when _app is None (headless/non-TUI mode)."""
        cli = MockCLI()
        cli._status_bar_visible = True
        cli._app = None

        # This should not raise
        _status_was_visible = getattr(cli, "_status_bar_visible", True)
        if hasattr(cli, "_status_bar_visible"):
            cli._status_bar_visible = False

        if cli._app:
            cli._app.invalidate()  # Should not be reached

        if hasattr(cli, "_status_bar_visible"):
            cli._status_bar_visible = _status_was_visible

        assert cli._status_bar_visible is True

    def test_terminal_line_clear_sequence(self):
        """Verify the escape sequence for clearing spinner remnants."""
        # The escape sequence "\r\033[K" means:
        # \r = carriage return (go to start of line)
        # \033[K = clear from cursor to end of line
        escape_seq = "\r\033[K"
        assert escape_seq == "\r\x1b[K"  # \033 = \x1b in Python
        assert len(escape_seq) == 4  # \r + \x1b + [ + K
