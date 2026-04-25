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
             patch.object(crawler, "_download_volume", return_value=True):
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
             patch.object(crawler, "_download_volume", return_value=True) as mock_dl:
            crawler.batch_download_book(
                "https://koz.moe/book.php?b=1", save_dir=tmp_path,
                start_vol=2, max_vols=3,
            )
            assert mock_dl.call_count == 3


class TestDownloadVolume:
    """Test _download_volume with account rotation."""

    def test_succeeds_on_first_try(self, crawler: KmoeCrawler, tmp_path: Path):
        with patch.object(crawler, "_try_download_volume", return_value=True):
            result = crawler._download_volume(
                {"volid": "v1", "name": "卷1"}, {}, tmp_path, 2, "url"
            )
        assert result is True

    def test_switches_account_on_exhaustion(self, crawler: KmoeCrawler, tmp_path: Path):
        mock_cookies = {"VLIBSID": "new", "VOLSKEY": "new", "VOLSESS": "new"}
        with patch.object(crawler, "_try_download_volume", side_effect=[
            AccountExhaustedError("quota"),
            True,
        ]), \
             patch.object(crawler._account_manager, "switch_account", return_value=mock_cookies), \
             patch.object(crawler, "replace_session"), \
             patch.object(crawler, "get_book_detail", return_value={"bookid": "1", "data_hash": "h"}):
            result = crawler._download_volume(
                {"volid": "v1", "name": "卷1"}, {"bookid": "1"}, tmp_path, 2, "url"
            )
        assert result is True

    def test_stops_when_all_exhausted(self, crawler: KmoeCrawler, tmp_path: Path):
        with patch.object(crawler, "_try_download_volume",
                          side_effect=AccountExhaustedError("quota")), \
             patch.object(crawler._account_manager, "switch_account", return_value=None):
            result = crawler._download_volume(
                {"volid": "v1", "name": "卷1"}, {}, tmp_path, 2, "url"
            )
        assert result is False

    def test_no_account_manager_single_attempt(self, tmp_path: Path):
        c = KmoeCrawler({"VLIBSID": "x"}, delay=0, account_manager=None)
        with patch.object(c, "_try_download_volume",
                          side_effect=AccountExhaustedError("quota")):
            result = c._download_volume(
                {"volid": "v1", "name": "卷1"}, {}, tmp_path, 2, "url"
            )
        assert result is False
        c.session.close()


class TestTryDownloadVolume:
    """Test _try_download_volume individual download logic."""

    def test_epub_with_fallback_to_mobi(self, crawler: KmoeCrawler, tmp_path: Path):
        detail = {"bookid": "1", "title": "TestBook"}
        vol = {"volid": "v1", "name": "卷1"}

        with patch.object(crawler, "get_download_url", side_effect=[
            {"url": "", "name": ""},  # epub fails
            {"url": "https://cdn.example.com/book.mobi", "name": "book"},  # mobi works
        ]), \
             patch.object(crawler, "download_file", return_value=tmp_path / "book.mobi"):
            result = crawler._try_download_volume(vol, detail, tmp_path, file_type=2)
        assert result is True

    def test_mobi_directly(self, crawler: KmoeCrawler, tmp_path: Path):
        detail = {"bookid": "1", "title": "TestBook"}
        vol = {"volid": "v1", "name": "卷1"}

        with patch.object(crawler, "get_download_url", return_value={
            "url": "https://cdn.example.com/book.mobi", "name": "book",
        }), \
             patch.object(crawler, "download_file", return_value=tmp_path / "book.mobi"):
            result = crawler._try_download_volume(vol, detail, tmp_path, file_type=1)
        assert result is True

    def test_skips_when_no_url(self, crawler: KmoeCrawler, tmp_path: Path):
        detail = {"bookid": "1", "title": "TestBook"}
        vol = {"volid": "v1", "name": "卷1"}

        with patch.object(crawler, "get_download_url", return_value={
            "url": "", "name": "",
        }):
            result = crawler._try_download_volume(vol, detail, tmp_path, file_type=1)
        assert result is False

    def test_skips_when_download_returns_none(self, crawler: KmoeCrawler, tmp_path: Path):
        detail = {"bookid": "1", "title": "TestBook"}
        vol = {"volid": "v1", "name": "卷1"}

        with patch.object(crawler, "get_download_url", return_value={
            "url": "https://cdn.example.com/book.epub", "name": "book",
        }), \
             patch.object(crawler, "download_file", return_value=None):
            result = crawler._try_download_volume(vol, detail, tmp_path, file_type=2)
        assert result is False


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
