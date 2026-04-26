"""Tests for kmoe.crawler — search parsing, download, retry, context manager."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from kmoe.crawler import (
    AccountExhaustedError,
    KmoeCrawler,
)
from kmoe.downloader import extract_filename


@pytest.fixture
def crawler() -> KmoeCrawler:
    c = KmoeCrawler({"VLIBSID": "test"}, delay=0)
    yield c
    c.session.close()


class TestMakeFilename:
    """Test static filename generation."""

    def test_basic(self):
        assert KmoeCrawler._make_filename("漫画名", "第1卷", "epub") == "漫画名_第01卷.epub"

    def test_strips_colon_prefix(self):
        assert KmoeCrawler._make_filename("漫画名：副标题", "第1卷", "epub") == "漫画名_第01卷.epub"

    def test_sanitizes_special_chars(self):
        # First splits on ':', then removes special chars from the first part
        name = KmoeCrawler._make_filename('A/B\\C:D*E?F"G<H>I|J', "第1卷", "mobi")
        assert name == "ABC_第01卷.mobi"

    def test_zero_pads_numbers(self):
        assert KmoeCrawler._make_filename("Book", "Vol.12", "epub") == "Book_Vol.12.epub"

    def test_strips_spaces_in_vol(self):
        assert KmoeCrawler._make_filename("Book", " 第 3 卷 ", "epub") == "Book_第03卷.epub"

    def test_no_number(self):
        result = KmoeCrawler._make_filename("Book", "Extra", "epub")
        assert result == "Book_Extra.epub"


class TestExtractFilename:
    """Test _extract_filename helper."""

    def test_from_content_disposition_utf8(self):
        resp = MagicMock()
        resp.headers = {"Content-Disposition": "attachment; filename*=UTF-8''test%20file.epub"}
        assert extract_filename(resp, "https://example.com/") == "test file.epub"

    def test_from_content_disposition_ascii(self):
        resp = MagicMock()
        resp.headers = {"Content-Disposition": 'attachment; filename="book.epub"'}
        assert extract_filename(resp, "https://example.com/") == "book.epub"

    def test_from_url_path(self):
        resp = MagicMock()
        resp.headers = {}
        assert extract_filename(resp, "https://cdn.example.com/files/book.epub?token=abc") == "book.epub"

    def test_fallback_to_download(self):
        resp = MagicMock()
        resp.headers = {}
        assert extract_filename(resp, "https://example.com/") == "download"


class TestContextManager:
    """Test context manager protocol."""

    def test_enter_returns_self(self):
        with KmoeCrawler({"VLIBSID": "x"}, delay=0) as c:
            assert isinstance(c, KmoeCrawler)

    def test_exit_closes_session(self):
        c = KmoeCrawler({"VLIBSID": "x"}, delay=0)
        with patch.object(c.session, "close") as mock_close:
            c.__exit__(None, None, None)
            mock_close.assert_called_once()


class TestReplaceSession:
    """Test cookie replacement."""

    def test_clears_old_and_sets_new(self, crawler: KmoeCrawler):
        crawler.replace_session({"VLIBSID": "new_vs", "VOLSKEY": "new_vk"})
        cookie_dict = {c.name: c.value for c in crawler.session.cookies}
        assert cookie_dict["VLIBSID"] == "new_vs"
        assert cookie_dict["VOLSKEY"] == "new_vk"
        assert "VOLSESS" not in cookie_dict


class TestSearchParsing:
    """Test search result parsing with mock HTML."""

    SEARCH_HTML = '''
    <html><body>
    <script>
    disp_divinfo("div_info_" + "123",
        "https://koz.moe/book.php?b=1",
        "https://img.koz.moe/cover1.jpg",
        "",
        "",
        "",
        "",
        "",
        "9.5",
        "<b>烙印战士</b>",
        "三浦建太郎",
        "连载中",
        "2024-01");
    disp_divinfo("div_info_" + "456",
        "https://koz.moe/book.php?b=2",
        "https://img.koz.moe/cover2.jpg",
        "",
        "",
        "",
        "",
        "",
        "8.0",
        "进击的巨人",
        "谏山创",
        "已完结",
        "2023-06");
    </script>
    </body></html>
    '''

    def test_parses_search_results(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = self.SEARCH_HTML
        mock_resp.cookies = {}

        with patch.object(crawler, "session") as mock_session:
            mock_session.get.return_value = mock_resp
            mock_session.cookies = []
            with patch.object(crawler, "_sync_cookies"):
                results = crawler.search("烙印")

        assert len(results) == 2
        assert results[0]["name"] == "烙印战士"
        assert results[0]["author"] == "三浦建太郎"
        assert results[0]["score"] == "9.5"
        assert results[0]["book_url"] == "https://koz.moe/book.php?b=1"
        assert results[1]["name"] == "进击的巨人"
        assert results[1]["author"] == "谏山创"

    def test_strips_html_tags_from_name(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = self.SEARCH_HTML
        mock_resp.cookies = {}

        with patch.object(crawler, "session") as mock_session:
            mock_session.get.return_value = mock_resp
            mock_session.cookies = []
            with patch.object(crawler, "_sync_cookies"):
                results = crawler.search("test")

        assert "<b>" not in results[0]["name"]
        assert results[0]["name"] == "烙印战士"

    def test_returns_empty_on_http_error(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.cookies = {}

        with patch.object(crawler, "session") as mock_session:
            mock_session.get.return_value = mock_resp
            mock_session.cookies = []
            with patch.object(crawler, "_sync_cookies"):
                results = crawler.search("test")

        assert results == []

    def test_handles_multiline_disp_divinfo(self, crawler: KmoeCrawler):
        html = '''
        disp_divinfo("div_info_" + "789",
            "https://koz.moe/book.php?b=3",
            "https://img.koz.moe/cover3.jpg",
            "",
            "",
            "",
            "",
            "",
            "7.0",
            "Test Book",
            "Author",
            "Status",
            "Date");
        '''
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        mock_resp.cookies = {}

        with patch.object(crawler, "session") as mock_session:
            mock_session.get.return_value = mock_resp
            mock_session.cookies = []
            with patch.object(crawler, "_sync_cookies"):
                results = crawler.search("test")

        assert len(results) == 1
        assert results[0]["name"] == "Test Book"


class TestGetBookDetail:
    """Test book detail parsing with mock HTML."""

    DETAIL_HTML = '''
    <html><head><title>烙印战士 - Kmoe</title></head><body>
    <script>
    var bookid = 12345;
    var uin = 67890;
    var is_vip = 1;
    var ulevel = 3;
    var quota_now = 100;
    var quota_used = 23;
    </script>
    <script src="book_data.php?h=abc123def456"></script>
    </body></html>
    '''

    def test_extracts_detail(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = self.DETAIL_HTML
        mock_resp.cookies = {}

        with patch.object(crawler, "session") as mock_session:
            mock_session.get.return_value = mock_resp
            mock_session.cookies = []
            with patch.object(crawler, "_sync_cookies"):
                detail = crawler.get_book_detail("https://koz.moe/book.php?b=12345")

        assert detail is not None
        assert detail["bookid"] == "12345"
        assert detail["uin"] == "67890"
        assert detail["is_vip"] == "1"
        assert detail["data_hash"] == "abc123def456"
        assert "烙印战士" in detail["title"]

    def test_returns_none_on_http_error(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.cookies = {}

        with patch.object(crawler, "session") as mock_session:
            mock_session.get.return_value = mock_resp
            mock_session.cookies = []
            with patch.object(crawler, "_sync_cookies"):
                result = crawler.get_book_detail("https://koz.moe/book.php?b=1")

        assert result is None


class TestGetVolumes:
    """Test volume list parsing."""

    VOL_DATA = (
        '"volinfo=v001,1,0,漫画,1,第1卷,200,0,0,45.2,38.1,42.0,0,0,0"\n'
        '"volinfo=v002,1,0,漫画,2,第2卷,180,0,0,40.1,35.0,38.5,0,0,0"\n'
        '"volinfo=v003,0,0,漫画,3,第3卷,220,0,0,50.3,42.0,45.8,0,0,0"'
    )

    def test_parses_volumes(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = self.VOL_DATA
        mock_resp.cookies = {}

        with patch.object(crawler, "session") as mock_session:
            mock_session.get.return_value = mock_resp
            mock_session.cookies = []
            with patch.object(crawler, "_sync_cookies"):
                volumes = crawler.get_volumes("somehash")

        assert len(volumes) == 3
        assert volumes[0]["volid"] == "v001"
        assert volumes[0]["name"] == "第1卷"
        assert volumes[0]["pages"] == "200"
        assert volumes[0]["size_mobi"] == "45.2"
        assert volumes[2]["status"] == "0"

    def test_returns_empty_on_http_error(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.cookies = {}

        with patch.object(crawler, "session") as mock_session:
            mock_session.get.return_value = mock_resp
            mock_session.cookies = []
            with patch.object(crawler, "_sync_cookies"):
                volumes = crawler.get_volumes("hash")

        assert volumes == []


class TestGetDownloadUrl:
    """Test download URL retrieval."""

    def test_403_raises_account_exhausted(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.cookies = {}

        with patch.object(crawler, "session") as mock_session:
            mock_session.get.return_value = mock_resp
            mock_session.cookies = []
            with patch.object(crawler, "_sync_cookies"):
                with pytest.raises(AccountExhaustedError):
                    crawler.get_download_url("b1", "v1")

    def test_quota_error_raises_account_exhausted(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 403, "msg": "额度不足"}
        mock_resp.cookies = {}

        with patch.object(crawler, "session") as mock_session:
            mock_session.get.return_value = mock_resp
            mock_session.cookies = []
            with patch.object(crawler, "_sync_cookies"):
                with pytest.raises(AccountExhaustedError, match="额度不足"):
                    crawler.get_download_url("b1", "v1")

    def test_success_returns_url(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 200,
            "url": "https://cdn.koz.moe/dl/book.epub?token=abc",
            "name": "book",
            "disp": "Book Title",
        }
        mock_resp.cookies = {}

        with patch.object(crawler, "session") as mock_session:
            mock_session.get.return_value = mock_resp
            mock_session.cookies = []
            with patch.object(crawler, "_sync_cookies"):
                result = crawler.get_download_url("b1", "v1")

        assert result is not None
        assert result["url"] == "https://cdn.koz.moe/dl/book.epub?token=abc"

    def test_returns_none_on_non_json(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = json.JSONDecodeError("err", "doc", 0)
        mock_resp.text = "not json"
        mock_resp.cookies = {}

        with patch.object(crawler, "session") as mock_session:
            mock_session.get.return_value = mock_resp
            mock_session.cookies = []
            with patch.object(crawler, "_sync_cookies"):
                result = crawler.get_download_url("b1", "v1")

        assert result is None

    def test_non_quota_error_returns_none(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 500, "msg": "server error"}
        mock_resp.cookies = {}

        with patch.object(crawler, "session") as mock_session:
            mock_session.get.return_value = mock_resp
            mock_session.cookies = []
            with patch.object(crawler, "_sync_cookies"):
                result = crawler.get_download_url("b1", "v1")

        assert result is None

    def test_detects_user_id_in_url(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 200,
            "url": "https://cdn.koz.moe/dl/book.epub?u=12345&token=abc",
        }
        mock_resp.cookies = {}

        with patch.object(crawler, "session") as mock_session:
            mock_session.get.return_value = mock_resp
            mock_session.cookies = []
            with patch.object(crawler, "_sync_cookies"):
                crawler.get_download_url("b1", "v1")

        assert any("u=xxx" in note for note in crawler.security_notes)


class TestRetryMechanism:
    """Test retry behavior in _get."""

    def test_retries_on_connection_error(self, crawler: KmoeCrawler):
        call_count = 0

        def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise requests.exceptions.ConnectionError("refused")
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.cookies = {}
            return mock_resp

        with patch.object(crawler.session, "get", side_effect=mock_get):
            with patch.object(crawler, "_sync_cookies"):
                resp = crawler._get("https://example.com/test")

        assert resp.status_code == 200
        assert call_count == 3

    def test_raises_after_max_retries(self, crawler: KmoeCrawler):
        with patch.object(
            crawler.session, "get",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            with patch.object(crawler, "_sync_cookies"):
                with pytest.raises(requests.exceptions.ConnectionError):
                    crawler._get("https://example.com/test", max_retries=2)

    def test_timeout_passed_to_request(self, crawler: KmoeCrawler):
        crawler.timeout = 30
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.cookies = {}

        with patch.object(crawler.session, "get", return_value=mock_resp) as mock_get:
            with patch.object(crawler, "_sync_cookies"):
                crawler._get("https://example.com/test")

        _, kwargs = mock_get.call_args
        assert kwargs["timeout"] == 30


class TestDownloadFile:
    """Test file download with progress bar."""

    def test_downloads_file_atomically(self, crawler: KmoeCrawler, tmp_path: Path):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-length": "12"}
        mock_resp.iter_content.return_value = [b"hello world!"]

        with patch.object(crawler.session, "get", return_value=mock_resp):
            result = crawler.download_file("https://cdn.example.com/book.epub", tmp_path, filename="book.epub")

        assert result is not None
        assert result.name == "book.epub"
        assert result.read_text() == "hello world!"
        # No leftover .tmp
        assert not (tmp_path / "book.epub.tmp").exists()

    def test_403_raises_account_exhausted(self, crawler: KmoeCrawler, tmp_path: Path):
        mock_resp = MagicMock()
        mock_resp.status_code = 403

        with patch.object(crawler.session, "get", return_value=mock_resp):
            with pytest.raises(AccountExhaustedError):
                crawler.download_file("https://cdn.example.com/book.epub", tmp_path)

    def test_returns_none_on_http_error(self, crawler: KmoeCrawler, tmp_path: Path):
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch.object(crawler.session, "get", return_value=mock_resp):
            result = crawler.download_file("https://cdn.example.com/book.epub", tmp_path)

        assert result is None

    def test_sanitizes_filename(self, crawler: KmoeCrawler, tmp_path: Path):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-length": "5"}
        mock_resp.iter_content.return_value = [b"hello"]

        with patch.object(crawler.session, "get", return_value=mock_resp):
            result = crawler.download_file("https://cdn.example.com/", tmp_path, filename='a/b:c.epub')

        assert result is not None
        assert result.name == "a_b_c.epub"

    def test_creates_directory(self, crawler: KmoeCrawler, tmp_path: Path):
        target = tmp_path / "sub" / "dir"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-length": "5"}
        mock_resp.iter_content.return_value = [b"hello"]

        with patch.object(crawler.session, "get", return_value=mock_resp):
            result = crawler.download_file("https://cdn.example.com/book.epub", target, filename="book.epub")

        assert result is not None
        assert target.exists()

    def test_cleans_up_tmp_on_write_error(self, crawler: KmoeCrawler, tmp_path: Path):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-length": "5"}
        mock_resp.iter_content.return_value = [b"hello"]

        original_open = open

        def broken_open(path, mode="r", *args, **kwargs):
            if isinstance(path, Path) and path.suffix == ".tmp":
                raise OSError("disk full")
            return original_open(path, mode, *args, **kwargs)

        with patch.object(crawler.session, "get", return_value=mock_resp):
            with patch("builtins.open", side_effect=broken_open):
                result = crawler.download_file("https://cdn.example.com/book.epub", tmp_path, filename="book.epub")

        assert result is None
        assert not (tmp_path / "book.epub.tmp").exists()


class TestCookieRotationDetection:
    """Test that cookie rotation is detected and noted."""

    def test_detects_volskey_rotation(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        # requests.Response.cookies supports `name in cookies` by cookie name
        mock_cookies = MagicMock()
        mock_cookies.__contains__ = lambda _, name: name == "VOLSKEY"
        mock_resp.cookies = mock_cookies

        crawler._check_cookie_rotation(mock_resp)

        assert len(crawler.security_notes) == 1
        assert "VOLSKEY" in crawler.security_notes[0]

    def test_detects_volsess_rotation(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_cookies = MagicMock()
        mock_cookies.__contains__ = lambda _, name: name == "VOLSESS"
        mock_resp.cookies = mock_cookies

        crawler._check_cookie_rotation(mock_resp)

        assert len(crawler.security_notes) == 1
        assert "VOLSESS" in crawler.security_notes[0]

    def test_no_note_for_other_cookies(self, crawler: KmoeCrawler):
        c = MagicMock()
        c.name = "OTHER"
        c.value = "val"
        mock_resp = MagicMock()
        mock_resp.cookies = [c]

        crawler._check_cookie_rotation(mock_resp)

        assert len(crawler.security_notes) == 0
