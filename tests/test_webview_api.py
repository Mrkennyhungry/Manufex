"""Tests for webview_api module."""
import sys
from unittest.mock import MagicMock

# Mock webview module before importing webview_api
mock_webview = MagicMock()
sys.modules["webview"] = mock_webview

from enikk.webview_api import WebviewAPI  # noqa: E402


class TestPickFile:
    """Test pick_file method."""

    def setup_method(self):
        """Reset mock before each test."""
        mock_webview.reset_mock()
        mock_webview.windows = [MagicMock()]

    def test_empty_string_all_files(self):
        """Empty string should result in 'All Files (*.*)' filter."""
        mock_webview.windows[0].create_file_dialog.return_value = ("/path/to/file.txt",)

        api = WebviewAPI()
        api.pick_file("")

        mock_webview.windows[0].create_file_dialog.assert_called_once()
        call_kwargs = mock_webview.windows[0].create_file_dialog.call_args.kwargs
        assert call_kwargs["file_types"] == ("All Files (*.*)",)

    def test_extension_only(self):
        """Extension like 'exe' should result in 'Executable (*.exe)' filter."""
        mock_webview.windows[0].create_file_dialog.return_value = ("/path/to/app.exe",)

        api = WebviewAPI()
        api.pick_file("exe")

        call_kwargs = mock_webview.windows[0].create_file_dialog.call_args.kwargs
        assert call_kwargs["file_types"] == ("Executable (*.exe)",)

    def test_pattern_with_asterisk(self):
        """Pattern like '*.exe' should result in 'Executable (*.exe)' filter."""
        mock_webview.windows[0].create_file_dialog.return_value = ("/path/to/app.exe",)

        api = WebviewAPI()
        api.pick_file("*.exe")

        call_kwargs = mock_webview.windows[0].create_file_dialog.call_args.kwargs
        assert call_kwargs["file_types"] == ("Executable (*.exe)",)

    def test_file_types_is_tuple(self):
        """file_types parameter must be a tuple, not a string."""
        mock_webview.windows[0].create_file_dialog.return_value = ("/path/to/file.txt",)

        api = WebviewAPI()
        api.pick_file("exe")

        call_kwargs = mock_webview.windows[0].create_file_dialog.call_args.kwargs
        assert isinstance(call_kwargs["file_types"], tuple)
        assert len(call_kwargs["file_types"]) == 1

    def test_returns_selected_file(self):
        """pick_file should return the selected file path."""
        mock_webview.windows[0].create_file_dialog.return_value = ("/selected/file.exe",)

        api = WebviewAPI()
        result = api.pick_file("exe")

        assert result == "/selected/file.exe"

    def test_returns_none_on_cancel(self):
        """pick_file should return None when dialog is cancelled."""
        mock_webview.windows[0].create_file_dialog.return_value = None

        api = WebviewAPI()
        result = api.pick_file("exe")

        assert result is None

    def test_returns_none_on_empty_result(self):
        """pick_file should return None when result is empty tuple."""
        mock_webview.windows[0].create_file_dialog.return_value = ()

        api = WebviewAPI()
        result = api.pick_file("exe")

        assert result is None


class TestPickDir:
    """Test pick_dir method."""

    def setup_method(self):
        """Reset mock before each test."""
        mock_webview.reset_mock()
        mock_webview.windows = [MagicMock()]

    def test_returns_selected_dir(self):
        """pick_dir should return the selected directory."""
        mock_webview.windows[0].create_file_dialog.return_value = ("/selected/dir",)

        api = WebviewAPI()
        result = api.pick_dir()

        assert result == "/selected/dir"

    def test_returns_none_on_cancel(self):
        """pick_dir should return None when dialog is cancelled."""
        mock_webview.windows[0].create_file_dialog.return_value = None

        api = WebviewAPI()
        result = api.pick_dir()

        assert result is None

    def test_uses_enikk_home_as_default(self):
        """pick_dir should use enikk_home when no initial_dir provided."""
        mock_webview.windows[0].create_file_dialog.return_value = ("/some/dir",)

        api = WebviewAPI()
        api.pick_dir()

        call_kwargs = mock_webview.windows[0].create_file_dialog.call_args.kwargs
        from enikk.config import enikk_home
        assert call_kwargs["directory"] == str(enikk_home())
