"""Tests for kmoe.crawler batch download logic and missing coverage."""

from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
import requests

from kmoe.auth import AccountManager
from kmoe.crawler import AccountExhaustedError, KmoeCrawler


@pytest.fixture
def crawler(tmp_path: Path) -> KmoeCrawler:
    mgr = AccountManager(
        {"accounts": [
            {"email": "a@t.com", "passwd": "pw1"},
            {"email": "b@t.com", "passwd": "pw2"},
        ]},
        state_file=tmp_path / "state.json",
    )
    c = KmoeCrawler({"VLIBSID": "vs"}, delay=0, account_manager=mgr)
    yield c
    c.session.close()


class TestBatchDownloadBook:
    """Test batch_download_book orchestration."""

    def test_downloads_all_volumes(self, crawler: KmoeCrawler, tmp_path: Path):
        detail = {
            "url": "https://koz.moe/book.php?b=1",
            "title": "TestBook",
            "bookid": "1",
            "data_hash": "abc123",
            "uin": "0",
        }
        volumes = [
            {"volid": "v1", "name": "第1卷"},
            {"volid": "v2", "name": "第2卷"},
        ]

        with patch.object(crawler, "get_book_detail", return_value=detail), \
             patch.object(crawler, "get_volumes", return_value=volumes), \
             patch.object(crawler, "_resolve_download_info", side_effect=[
                 {"url": "https://cdn/1.epub", "ext": "epub"},
                 {"url": "https://cdn/2.epub", "ext": "epub"},
             ]), \
             patch.object(crawler, "download_file", return_value=tmp_path / "f.epub"):
            crawler.batch_download_book("https://koz.moe/book.php?b=1", save_dir=tmp_path)

    def test_stops_on_no_detail(self, crawler: KmoeCrawler, tmp_path: Path):
        with patch.object(crawler, "get_book_detail", return_value=None):
            crawler.batch_download_book("https://koz.moe/book.php?b=1", save_dir=tmp_path)

    def test_stops_on_no_hash(self, crawler: KmoeCrawler, tmp_path: Path):
        with patch.object(crawler, "get_book_detail", return_value={
            "url": "x", "title": "T", "bookid": "1", "data_hash": "", "uin": "0",
        }):
            crawler.batch_download_book("https://koz.moe/book.php?b=1", save_dir=tmp_path)

    def test_stops_on_no_volumes(self, crawler: KmoeCrawler, tmp_path: Path):
        detail = {"url": "x", "title": "T", "bookid": "1", "data_hash": "h", "uin": "0"}
        with patch.object(crawler, "get_book_detail", return_value=detail), \
             patch.object(crawler, "get_volumes", return_value=[]):
            crawler.batch_download_book("https://koz.moe/book.php?b=1", save_dir=tmp_path)

    def test_respects_start_and_max(self, crawler: KmoeCrawler, tmp_path: Path):
        detail = {
            "url": "x", "title": "T", "bookid": "1", "data_hash": "h", "uin": "0",
        }
        volumes = [{"volid": f"v{i}", "name": f"卷{i}"} for i in range(10)]
        with patch.object(crawler, "get_book_detail", return_value=detail), \
             patch.object(crawler, "get_volumes", return_value=volumes), \
             patch.object(crawler, "_resolve_download_info", return_value=None):
            crawler.batch_download_book(
                "https://koz.moe/book.php?b=1", save_dir=tmp_path,
                start_vol=2, max_vols=3,
            )


class TestResolveDownloadInfo:
    """Test _resolve_download_info with account rotation."""

    def test_succeeds_on_first_try(self, crawler: KmoeCrawler):
        vol = {"volid": "v1", "name": "卷1"}
        with patch.object(crawler, "_try_resolve", return_value={"url": "u", "ext": "epub"}):
            result = crawler._resolve_download_info(
                vol, {"bookid": "1"}, 2, "url"
            )
        assert result == {"url": "u", "ext": "epub"}

    def test_switches_account_on_exhaustion(self, crawler: KmoeCrawler):
        mock_cookies = {"VLIBSID": "new", "VOLSKEY": "new", "VOLSESS": "new"}
        vol = {"volid": "v1", "name": "卷1"}
        with patch.object(crawler, "_try_resolve", side_effect=[
            AccountExhaustedError("quota"),
            {"url": "u", "ext": "epub"},
        ]), \
             patch.object(crawler._account_manager, "switch_account", return_value=mock_cookies), \
             patch.object(crawler, "replace_session"), \
             patch.object(crawler, "get_book_detail", return_value={"bookid": "1", "data_hash": "h"}):
            result = crawler._resolve_download_info(
                vol, {"bookid": "1"}, 2, "url"
            )
        assert result == {"url": "u", "ext": "epub"}

    def test_stops_when_all_exhausted(self, crawler: KmoeCrawler):
        vol = {"volid": "v1", "name": "卷1"}
        with patch.object(crawler, "_try_resolve",
                          side_effect=AccountExhaustedError("quota")), \
             patch.object(crawler._account_manager, "switch_account", return_value=None):
            result = crawler._resolve_download_info(
                vol, {}, 2, "url"
            )
        assert result is None

    def test_no_account_manager_single_attempt(self):
        c = KmoeCrawler({"VLIBSID": "x"}, delay=0, account_manager=None)
        with patch.object(c, "_try_resolve",
                          side_effect=AccountExhaustedError("quota")):
            result = c._resolve_download_info(
                {"volid": "v1", "name": "卷1"}, {}, 2, "url"
            )
        assert result is None
        c.session.close()


class TestTryResolve:
    """Test _try_resolve individual download URL resolution."""

    def test_epub_with_fallback_to_mobi(self, crawler: KmoeCrawler):
        detail = {"bookid": "1", "title": "TestBook"}
        vol = {"volid": "v1", "name": "卷1"}

        with patch.object(crawler, "get_download_url", side_effect=[
            {"url": "", "name": ""},  # epub fails
            {"url": "https://cdn.example.com/book.mobi", "name": "book"},  # mobi works
        ]):
            result = crawler._try_resolve(vol, detail, file_type=2)
        assert result == {"url": "https://cdn.example.com/book.mobi", "ext": "mobi"}

    def test_mobi_directly(self, crawler: KmoeCrawler):
        detail = {"bookid": "1", "title": "TestBook"}
        vol = {"volid": "v1", "name": "卷1"}

        with patch.object(crawler, "get_download_url", return_value={
            "url": "https://cdn.example.com/book.mobi", "name": "book",
        }):
            result = crawler._try_resolve(vol, detail, file_type=1)
        assert result == {"url": "https://cdn.example.com/book.mobi", "ext": "mobi"}

    def test_skips_when_no_url(self, crawler: KmoeCrawler):
        detail = {"bookid": "1", "title": "TestBook"}
        vol = {"volid": "v1", "name": "卷1"}

        with patch.object(crawler, "get_download_url", return_value={
            "url": "", "name": "",
        }):
            result = crawler._try_resolve(vol, detail, file_type=1)
        assert result is None

    def test_epub_returns_url(self, crawler: KmoeCrawler):
        detail = {"bookid": "1", "title": "TestBook"}
        vol = {"volid": "v1", "name": "卷1"}

        with patch.object(crawler, "get_download_url", return_value={
            "url": "https://cdn.example.com/book.epub", "name": "book",
        }):
            result = crawler._try_resolve(vol, detail, file_type=2)
        assert result == {"url": "https://cdn.example.com/book.epub", "ext": "epub"}


class TestGetDownloadUrlQuotaKeywords:
    """Test that all quota-related keywords trigger AccountExhaustedError."""

    @pytest.mark.parametrize("msg", [
        "额度不足", "權限不足", "limit exceeded", "quota exceeded",
        "等級不够", "验证失败",
    ])
    def test_quota_keywords(self, crawler: KmoeCrawler, msg: str):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 403, "msg": msg}
        mock_resp.cookies = {}

        with patch.object(crawler, "session") as mock_session:
            mock_session.get.return_value = mock_resp
            mock_session.cookies = []
            with patch.object(crawler, "_sync_cookies"):
                with pytest.raises(AccountExhaustedError):
                    crawler.get_download_url("b1", "v1")


class TestBatchDownloadCategoryFilter:
    """Test --category filter in batch_download_book."""

    def test_filters_volumes_by_category(self, crawler: KmoeCrawler, tmp_path: Path):
        detail = {
            "url": "x", "title": "T", "bookid": "1", "data_hash": "h", "uin": "0",
        }
        volumes = [
            {"volid": "v1", "name": "第1卷", "category": "單行本"},
            {"volid": "v2", "name": "第2卷", "category": "話"},
            {"volid": "v3", "name": "第3卷", "category": "單行本"},
        ]
        with patch.object(crawler, "get_book_detail", return_value=detail), \
             patch.object(crawler, "get_volumes", return_value=volumes), \
             patch.object(crawler, "_resolve_download_info", return_value=None) as mock_resolve:
            crawler.batch_download_book(
                "https://koz.moe/book.php?b=1", save_dir=tmp_path,
                category="單行本",
            )
        # Should only process v1 and v3 (category 單行本)
        assert mock_resolve.call_count == 2

    def test_shows_available_categories_when_no_match(self, crawler: KmoeCrawler, tmp_path: Path, capsys):
        detail = {
            "url": "x", "title": "T", "bookid": "1", "data_hash": "h", "uin": "0",
        }
        volumes = [
            {"volid": "v1", "name": "第1卷", "category": "單行本"},
            {"volid": "v2", "name": "SP", "category": "番外篇"},
        ]
        with patch.object(crawler, "get_book_detail", return_value=detail), \
             patch.object(crawler, "get_volumes", return_value=volumes):
            crawler.batch_download_book(
                "https://koz.moe/book.php?b=1", save_dir=tmp_path,
                category="話",
            )
        output = capsys.readouterr().out
        assert "單行本" in output
        assert "番外篇" in output

    def test_no_category_passes_all_volumes(self, crawler: KmoeCrawler, tmp_path: Path):
        detail = {
            "url": "x", "title": "T", "bookid": "1", "data_hash": "h", "uin": "0",
        }
        volumes = [
            {"volid": "v1", "name": "卷1", "category": "單行本"},
            {"volid": "v2", "name": "卷2", "category": "話"},
        ]
        with patch.object(crawler, "get_book_detail", return_value=detail), \
             patch.object(crawler, "get_volumes", return_value=volumes), \
             patch.object(crawler, "_resolve_download_info", return_value=None) as mock_resolve:
            crawler.batch_download_book(
                "https://koz.moe/book.php?b=1", save_dir=tmp_path,
                category=None,
            )
        assert mock_resolve.call_count == 2
