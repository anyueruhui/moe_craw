"""Tests for kmoe.cli — CLI argument parsing and dispatch logic."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kmoe.cli import _build_parser, _dispatch, _resolve_cookies, _security_report, _show_book_info
from kmoe.config import load_config


class TestBuildParser:
    """Test argument parser construction."""

    def test_default_values(self):
        with patch("kmoe.cli.load_config", return_value={}):
            parser = _build_parser({})
        args = parser.parse_args([])
        assert args.type == "epub"
        assert args.delay == 1.0
        assert args.start == 0
        assert args.max == 0
        assert args.download is False
        assert args.login is False

    def test_search_short_flag(self):
        parser = _build_parser({})
        args = parser.parse_args(["-s", "漫画名"])
        assert args.search == "漫画名"

    def test_download_flags(self):
        parser = _build_parser({})
        args = parser.parse_args(["-d", "--download-all"])
        assert args.download is True
        assert args.download_all is True

    def test_type_choices(self):
        parser = _build_parser({})
        args_mobi = parser.parse_args(["--type", "mobi"])
        assert args_mobi.type == "mobi"
        args_epub = parser.parse_args(["--type", "epub"])
        assert args_epub.type == "epub"

    def test_output_default(self):
        with patch("kmoe.cli.load_config", return_value={}):
            parser = _build_parser({})
        args = parser.parse_args([])
        assert args.output == "~/Downloads"

    def test_custom_config_values(self):
        parser = _build_parser({
            "type": "mobi", "delay": 0.5, "start": 3, "max": 5, "output": "/tmp/books"
        })
        args = parser.parse_args([])
        assert args.type == "mobi"
        assert args.delay == 0.5
        assert args.start == 3
        assert args.max == 5
        assert args.output == "/tmp/books"


class TestResolveCookies:
    """Test cookie resolution priority."""

    def test_uses_cli_args_first(self, tmp_path: Path):
        from kmoe.auth import AccountManager
        mgr = AccountManager({}, state_file=tmp_path / "state.json")
        parser = _build_parser({})
        args = parser.parse_args([
            "--cookie-vlibsid", "cli_vs",
            "--cookie-volskey", "cli_vk",
            "--cookie-volsess", "cli_ve",
        ])
        cookies = _resolve_cookies(args, mgr, parser)
        assert cookies["VLIBSID"] == "cli_vs"

    def test_falls_back_to_state(self, tmp_path: Path):
        import json
        from kmoe.auth import AccountManager
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "accounts": [{"vlibsid": "state_vs", "volskey": "state_vk", "volsess": "state_ve"}],
            "active_account": 0,
        }))
        mgr = AccountManager({}, state_file=state_file)
        parser = _build_parser({})
        args = parser.parse_args([])
        cookies = _resolve_cookies(args, mgr, parser)
        assert cookies["VLIBSID"] == "state_vs"

    def test_auto_login_when_missing(self, tmp_path: Path):
        from kmoe.auth import AccountManager
        mgr = AccountManager(
            {"accounts": [{"email": "a@t.com", "passwd": "pw"}]},
            state_file=tmp_path / "state.json",
        )
        parser = _build_parser({})
        args = parser.parse_args([])

        mock_cookies = {"VLIBSID": "login_vs", "VOLSKEY": "login_vk", "VOLSESS": "login_ve"}
        with patch.object(mgr, "get_active_cookies", return_value=None), \
             patch.object(mgr, "login", return_value=mock_cookies), \
             patch.object(type(mgr), "active_email", new_callable=lambda: property(lambda self: "a@t.com")):
            cookies = _resolve_cookies(args, mgr, parser)
        assert cookies["VLIBSID"] == "login_vs"


class TestDispatch:
    """Test dispatch logic."""

    def test_search_without_download(self, capsys):
        parser = _build_parser({})
        args = parser.parse_args(["-s", "漫画"])
        crawler = MagicMock()
        crawler.search.return_value = [
            {"book_url": "https://example.com/b1", "name": "漫画1"},
        ]
        _dispatch(args, crawler, file_type=2)
        crawler.search.assert_called_once_with("漫画")
        crawler.batch_download_book.assert_not_called()

    def test_search_with_download_first(self):
        parser = _build_parser({})
        args = parser.parse_args(["-s", "漫画", "-d"])
        crawler = MagicMock()
        crawler.search.return_value = [
            {"book_url": "https://example.com/b1", "name": "漫画1"},
        ]
        _dispatch(args, crawler, file_type=2)
        crawler.batch_download_book.assert_called_once()

    def test_search_with_download_all(self):
        parser = _build_parser({})
        args = parser.parse_args(["-s", "漫画", "--download-all"])
        crawler = MagicMock()
        crawler.search.return_value = [
            {"book_url": "https://example.com/b1"},
            {"book_url": "https://example.com/b2"},
        ]
        _dispatch(args, crawler, file_type=2)
        assert crawler.batch_download_book.call_count == 2

    def test_book_url_with_download(self):
        parser = _build_parser({})
        args = parser.parse_args(["--book-url", "https://example.com/b1", "-d"])
        crawler = MagicMock()
        _dispatch(args, crawler, file_type=1)
        crawler.batch_download_book.assert_called_once()

    def test_book_url_without_download_shows_info(self):
        parser = _build_parser({})
        args = parser.parse_args(["--book-url", "https://example.com/b1"])
        crawler = MagicMock()
        crawler.get_book_detail.return_value = {
            "data_hash": "hash123", "title": "Test Book"
        }
        crawler.get_volumes.return_value = [
            {"pages": "200", "size_mobi": "45"},
        ]
        _dispatch(args, crawler, file_type=2)
        crawler.get_book_detail.assert_called_once()
        crawler.get_volumes.assert_called_once()

    def test_empty_search_results(self, capsys):
        parser = _build_parser({})
        args = parser.parse_args(["-s", "不存在的漫画"])
        crawler = MagicMock()
        crawler.search.return_value = []
        _dispatch(args, crawler, file_type=2)
        # Should not crash, no download attempted


class TestSecurityReport:
    """Test security report output."""

    def test_prints_report(self, capsys):
        crawler = MagicMock()
        crawler.request_count = 42
        crawler.security_notes = ["Note 1", "Note 2"]
        _security_report(crawler, 10.5)
        output = capsys.readouterr().out
        assert "42" in output
        assert "安全测试报告" in output
        assert "Note 1" in output

    def test_handles_empty_notes(self, capsys):
        crawler = MagicMock()
        crawler.request_count = 0
        crawler.security_notes = []
        _security_report(crawler, 1.0)
        output = capsys.readouterr().out
        assert "未检测到" in output


class TestShowBookInfo:
    """Test book info display."""

    def test_shows_volumes(self, capsys):
        crawler = MagicMock()
        crawler.get_book_detail.return_value = {
            "data_hash": "h", "title": "Test"
        }
        crawler.get_volumes.return_value = [
            {"pages": "200", "size_mobi": "45"},
            {"pages": "180", "size_mobi": "40"},
        ]
        _show_book_info(crawler, "https://example.com/b1")
        output = capsys.readouterr().out
        assert "共 2 卷" in output

    def test_shows_no_volumes(self, capsys):
        crawler = MagicMock()
        crawler.get_book_detail.return_value = {
            "data_hash": "h", "title": "Test"
        }
        crawler.get_volumes.return_value = []
        _show_book_info(crawler, "https://example.com/b1")
        output = capsys.readouterr().out
        assert "无卷数据" in output

    def test_shows_no_detail(self, capsys):
        crawler = MagicMock()
        crawler.get_book_detail.return_value = None
        _show_book_info(crawler, "https://example.com/b1")
        output = capsys.readouterr().out
        assert "无法获取漫画信息" in output

    def test_shows_no_hash(self, capsys):
        crawler = MagicMock()
        crawler.get_book_detail.return_value = {"data_hash": "", "title": "T"}
        _show_book_info(crawler, "https://example.com/b1")
        output = capsys.readouterr().out
        assert "无法获取漫画信息" in output
