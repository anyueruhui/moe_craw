"""Tests for kmoe.crawler chunked download, parallel download, and backup CDN."""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest
import requests

from kmoe.crawler import KmoeCrawler
from kmoe.downloader import (
    ProgressTracker,
    chunked_download,
    download_from_cdn,
    download_range,
    extract_filename,
    parallel_download,
    single_download,
    try_chunked_download,
)


@pytest.fixture
def crawler(tmp_path: Path) -> KmoeCrawler:
    from kmoe.auth import AccountManager
    mgr = AccountManager(
        {"accounts": [{"email": "a@t.com", "passwd": "pw1"}]},
        state_file=tmp_path / "state.json",
    )
    c = KmoeCrawler(
        {"VLIBSID": "vs"}, delay=0, account_manager=mgr, workers=2,
    )
    yield c
    c.session.close()


class TestProgressTracker:
    """Test thread-safe progress tracker."""

    def test_add_accumulates_bytes(self):
        tracker = ProgressTracker(1000, "test.epub")
        tracker.add(200)
        tracker.add(300)
        assert tracker._downloaded == 500

    def test_total_size_stored(self):
        tracker = ProgressTracker(2048, "book.epub")
        assert tracker.total_size == 2048

    def test_filename_stored(self):
        tracker = ProgressTracker(100, "vol.epub")
        assert tracker.filename == "vol.epub"

    def test_prints_progress_periodically(self, capsys):
        tracker = ProgressTracker(1000, "test.epub")
        tracker._last_print = 0  # force print
        tracker.add(500)
        output = capsys.readouterr().out
        assert "50%" in output

    def test_no_print_when_too_soon(self, capsys):
        tracker = ProgressTracker(1000, "test.epub")
        tracker._last_print = time.monotonic()  # just printed
        tracker.add(500)
        output = capsys.readouterr().out
        assert "50%" not in output

    def test_zero_total_no_division_error(self):
        tracker = ProgressTracker(0, "test.epub")
        tracker.add(100)  # should not raise


class TestTryChunkedDownload:
    """Test try_chunked_download probe and dispatch logic."""

    def test_returns_none_on_non_206_probe(self, tmp_path: Path):
        mock_probe = MagicMock()
        mock_probe.status_code = 200
        mock_probe.headers = {"content-length": "1024"}

        with patch("kmoe.downloader.requests.get", return_value=mock_probe):
            result = try_chunked_download(
                "https://cdn.example.com/book.epub", tmp_path, None, workers=2,
            )
        assert result is None

    def test_returns_none_on_wrong_content_length(self, tmp_path: Path):
        mock_probe = MagicMock()
        mock_probe.status_code = 206
        mock_probe.headers = {
            "content-length": "512",
            "content-range": "bytes 0-1023/1048576",
        }

        with patch("kmoe.downloader.requests.get", return_value=mock_probe):
            result = try_chunked_download(
                "https://cdn.example.com/book.epub", tmp_path, None, workers=2,
            )
        assert result is None

    def test_returns_none_on_missing_content_range(self, tmp_path: Path):
        mock_probe = MagicMock()
        mock_probe.status_code = 206
        mock_probe.headers = {"content-length": "1024"}

        with patch("kmoe.downloader.requests.get", return_value=mock_probe):
            result = try_chunked_download(
                "https://cdn.example.com/book.epub", tmp_path, None, workers=2,
            )
        assert result is None

    def test_returns_none_on_small_file(self, tmp_path: Path):
        mock_probe = MagicMock()
        mock_probe.status_code = 206
        mock_probe.headers = {
            "content-length": "1024",
            "content-range": "bytes 0-1023/512000",
        }

        with patch("kmoe.downloader.requests.get", return_value=mock_probe):
            result = try_chunked_download(
                "https://cdn.example.com/book.epub", tmp_path, None, workers=2,
            )
        assert result is None

    def test_returns_none_on_network_error(self, tmp_path: Path):
        with patch("kmoe.downloader.requests.get", side_effect=requests.RequestException):
            result = try_chunked_download(
                "https://cdn.example.com/book.epub", tmp_path, None, workers=2,
            )
        assert result is None

    def test_delegates_to_chunked_download(self, tmp_path: Path):
        total_size = 5 * 1024 * 1024
        mock_probe = MagicMock()
        mock_probe.status_code = 206
        mock_probe.headers = {
            "content-length": "1024",
            "content-range": f"bytes 0-1023/{total_size}",
        }

        with patch("kmoe.downloader.requests.get", return_value=mock_probe), \
             patch("kmoe.downloader.chunked_download", return_value=tmp_path / "book.epub"):
            result = try_chunked_download(
                "https://cdn.example.com/book.epub", tmp_path, None, workers=2,
            )
        assert result == tmp_path / "book.epub"

    def test_passes_main_cdn_url(self, tmp_path: Path):
        total_size = 5 * 1024 * 1024
        mock_probe = MagicMock()
        mock_probe.status_code = 206
        mock_probe.headers = {
            "content-length": "1024",
            "content-range": f"bytes 0-1023/{total_size}",
        }
        main_url = "https://main.cdn/book.epub"

        with patch("kmoe.downloader.requests.get", return_value=mock_probe), \
             patch("kmoe.downloader.chunked_download", return_value=None) as mock_cd:
            try_chunked_download(
                "https://backup.cdn/book.epub", tmp_path, "book.epub",
                workers=2, main_cdn_url=main_url,
            )
        call_args = mock_cd.call_args
        assert call_args[0][0] == "https://backup.cdn/book.epub"
        assert call_args[0][6] == main_url


class TestChunkedDownload:
    """Test _chunked_download orchestration and merge logic."""

    def test_merges_parts_into_final_file(self, crawler: KmoeCrawler, tmp_path: Path):
        total_size = 100
        # With 5MB chunks and 100 bytes total, only 1 chunk
        filepath = tmp_path / "book.epub"

        def fake_download_range(url, start, end, part_file, tracker, timeout=120):
            part_file.write_bytes(b"\x00" * (end - start + 1))
            tracker.add(end - start + 1)
            return True

        with patch("kmoe.downloader.download_range", side_effect=fake_download_range):
            result = chunked_download(
                "https://cdn.example.com/book.epub", filepath, total_size, "book.epub",
            )

        assert result == filepath
        assert filepath.exists()
        assert filepath.stat().st_size == total_size

    def test_returns_none_on_chunk_failure(self, tmp_path: Path):
        total_size = 100
        filepath = tmp_path / "book.epub"

        with patch("kmoe.downloader.download_range", return_value=False):
            result = chunked_download(
                "https://cdn.example.com/book.epub", filepath, total_size, "book.epub",
            )

        assert result is None
        assert not filepath.exists()

    def test_cleans_up_on_merge_error(self, tmp_path: Path):
        total_size = 100
        filepath = tmp_path / "book.epub"

        def fake_download_range(url, start, end, part_file, tracker, timeout=120):
            part_file.write_bytes(b"\x00" * (end - start + 1))
            tracker.add(end - start + 1)
            return True

        with patch("kmoe.downloader.download_range", side_effect=fake_download_range), \
             patch("builtins.open", side_effect=OSError("disk full")):
            result = chunked_download(
                "https://cdn.example.com/book.epub", filepath, total_size, "book.epub",
            )

        assert result is None

    def test_callable(self):
        assert callable(chunked_download)


class TestDownloadRange:
    """Test _download_range individual chunk download."""

    def test_downloads_range_successfully(self, tmp_path: Path):
        part_file = tmp_path / "part0"
        tracker = ProgressTracker(100, "book.epub")
        data = b"\x00" * 50

        mock_resp = MagicMock()
        mock_resp.status_code = 206
        mock_resp.headers = {"content-length": "50"}
        mock_resp.iter_content.return_value = [data]

        with patch("kmoe.downloader.requests.get", return_value=mock_resp):
            ok = download_range("https://cdn/book.epub", 0, 49, part_file, tracker)

        assert ok is True
        assert part_file.exists()
        assert part_file.stat().st_size == 50

    def test_retries_on_non_206(self, tmp_path: Path):
        part_file = tmp_path / "part0"
        tracker = ProgressTracker(100, "book.epub")

        call_count = 0

        def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            if call_count < 3:
                resp.status_code = 500
                resp.headers = {}
            else:
                resp.status_code = 206
                resp.headers = {"content-length": "50"}
                resp.iter_content.return_value = [b"\x00" * 50]
            return resp

        with patch("kmoe.downloader.requests.get", side_effect=mock_get):
            ok = download_range("https://cdn/book.epub", 0, 49, part_file, tracker)

        assert ok is True
        assert call_count == 3

    def test_retries_on_content_length_mismatch(self, tmp_path: Path):
        part_file = tmp_path / "part0"
        tracker = ProgressTracker(100, "book.epub")

        call_count = 0

        def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.status_code = 206
            if call_count == 1:
                resp.headers = {"content-length": "999"}  # wrong
            else:
                resp.headers = {"content-length": "50"}
                resp.iter_content.return_value = [b"\x00" * 50]
            return resp

        with patch("kmoe.downloader.requests.get", side_effect=mock_get):
            ok = download_range("https://cdn/book.epub", 0, 49, part_file, tracker)

        assert ok is True

    def test_returns_false_after_max_retries(self, tmp_path: Path):
        part_file = tmp_path / "part0"
        tracker = ProgressTracker(100, "book.epub")

        with patch("kmoe.downloader.requests.get", side_effect=requests.RequestException("fail")):
            ok = download_range("https://cdn/book.epub", 0, 49, part_file, tracker)

        assert ok is False
        assert not part_file.exists()

    def test_resumes_from_existing_part(self, tmp_path: Path):
        part_file = tmp_path / "part0"
        part_file.write_bytes(b"\x00" * 25)
        tracker = ProgressTracker(100, "book.epub")

        mock_resp = MagicMock()
        mock_resp.status_code = 206
        mock_resp.headers = {"content-length": "25"}
        mock_resp.iter_content.return_value = [b"\x00" * 25]

        with patch("kmoe.downloader.requests.get", return_value=mock_resp) as mock_get:
            ok = download_range("https://cdn/book.epub", 0, 49, part_file, tracker)

        assert ok is True
        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["Range"] == "bytes=25-49"

    def test_skips_if_part_already_complete(self, tmp_path: Path):
        part_file = tmp_path / "part0"
        part_file.write_bytes(b"\x00" * 50)
        tracker = ProgressTracker(100, "book.epub")

        with patch("kmoe.downloader.requests.get") as mock_get:
            ok = download_range("https://cdn/book.epub", 0, 49, part_file, tracker)

        assert ok is True
        mock_get.assert_not_called()

    def test_removes_part_on_final_failure(self, tmp_path: Path):
        part_file = tmp_path / "part0"
        part_file.write_text("partial")
        tracker = ProgressTracker(100, "book.epub")

        with patch("kmoe.downloader.requests.get", side_effect=requests.RequestException("fail")):
            ok = download_range("https://cdn/book.epub", 0, 49, part_file, tracker)

        assert ok is False
        assert not part_file.exists()


class TestParallelDownload:
    """Test _parallel_download orchestration."""

    def test_downloads_all_tasks(self, tmp_path: Path):
        tasks = [
            ("https://cdn/1.epub", "book1.epub", {"volid": "v1"}, None),
            ("https://cdn/2.epub", "book2.epub", {"volid": "v2"}, None),
        ]

        with patch("kmoe.downloader.download_from_cdn", return_value=tmp_path / "f"):
            success, fail = parallel_download(tasks, tmp_path, workers=2)

        assert success == 2
        assert fail == 0

    def test_counts_failures(self, tmp_path: Path):
        tasks = [
            ("https://cdn/1.epub", "book1.epub", {"volid": "v1"}, None),
            ("https://cdn/2.epub", "book2.epub", {"volid": "v2"}, None),
        ]

        with patch("kmoe.downloader.download_from_cdn", side_effect=[tmp_path / "f", None]):
            success, fail = parallel_download(tasks, tmp_path, workers=2)

        assert success == 1
        assert fail == 1

    def test_handles_exceptions(self, tmp_path: Path):
        tasks = [
            ("https://cdn/1.epub", "book1.epub", {"volid": "v1"}, None),
        ]

        with patch("kmoe.downloader.download_from_cdn", side_effect=Exception("boom")):
            success, fail = parallel_download(tasks, tmp_path, workers=1)

        assert success == 0
        assert fail == 1


class TestDownloadFromCdn:
    """Test _download_from_cdn (thread-safe CDN download without session)."""

    def test_downloads_file(self, crawler: KmoeCrawler, tmp_path: Path):
        data = b"hello from CDN"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-length": str(len(data))}
        mock_resp.iter_content.return_value = [data]

        with patch("kmoe.downloader.requests.get", return_value=mock_resp):
            result = download_from_cdn(
                "https://cdn/book.epub", tmp_path, "book.epub",
            )

        assert result is not None
        assert result.name == "book.epub"
        assert result.read_bytes() == data
        # No leftover .tmp
        assert not (tmp_path / "book.epub.tmp").exists()

    def test_returns_none_on_http_error(self, crawler: KmoeCrawler, tmp_path: Path):
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("kmoe.downloader.requests.get", return_value=mock_resp):
            result = download_from_cdn(
                "https://cdn/book.epub", tmp_path, "book.epub",
            )

        assert result is None

    def test_creates_directory(self, crawler: KmoeCrawler, tmp_path: Path):
        target = tmp_path / "sub" / "dir"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-length": "5"}
        mock_resp.iter_content.return_value = [b"hello"]

        with patch("kmoe.downloader.requests.get", return_value=mock_resp):
            result = download_from_cdn(
                "https://cdn/book.epub", target, "book.epub",
            )

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

        with patch("kmoe.downloader.requests.get", return_value=mock_resp), \
             patch("builtins.open", side_effect=broken_open):
            result = download_from_cdn(
                "https://cdn/book.epub", tmp_path, "book.epub",
            )

        assert result is None
        assert not (tmp_path / "book.epub.tmp").exists()


class TestGetBackupCdnUrl:
    """Test _get_backup_cdn_url backup CDN resolution."""

    def test_returns_redirect_url_with_mxomo(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.headers = {
            "location": "https://free2.mxomo.com/dl/book.epub?token=abc"
        }

        with patch.object(crawler.session, "get", return_value=mock_resp):
            result = crawler._get_backup_cdn_url("b1", "v1", 2)

        assert result == "https://free2.mxomo.com/dl/book.epub?token=abc"

    def test_returns_none_on_non_302(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 403

        with patch.object(crawler.session, "get", return_value=mock_resp):
            result = crawler._get_backup_cdn_url("b1", "v1", 2)

        assert result is None

    def test_returns_none_on_non_mxomo_redirect(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.headers = {"location": "https://other.cdn/book.epub"}

        with patch.object(crawler.session, "get", return_value=mock_resp):
            result = crawler._get_backup_cdn_url("b1", "v1", 2)

        assert result is None

    def test_returns_none_on_network_error(self, crawler: KmoeCrawler):
        with patch.object(
            crawler.session, "get", side_effect=requests.RequestException
        ):
            result = crawler._get_backup_cdn_url("b1", "v1", 2)

        assert result is None

    def test_constructs_correct_path_for_epub(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 403  # won't match, just check URL construction
        mock_resp.headers = {}

        with patch.object(crawler.session, "get", return_value=mock_resp) as mock_get:
            crawler._get_backup_cdn_url("b1", "v1", 2)

        call_url = mock_get.call_args[0][0]
        assert "/dl/b1/v1/1/2/0/" in call_url

    def test_constructs_correct_path_for_mobi(self, crawler: KmoeCrawler):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.headers = {}

        with patch.object(crawler.session, "get", return_value=mock_resp) as mock_get:
            crawler._get_backup_cdn_url("b1", "v1", 1)

        call_url = mock_get.call_args[0][0]
        assert "/dl/b1/v1/1/1/0/" in call_url


class TestDownloadFileDispatch:
    """Test download_file dispatches between chunked and single."""

    def test_uses_chunked_when_workers_gt_1(self, crawler: KmoeCrawler, tmp_path: Path):
        crawler.workers = 2
        with patch(
            "kmoe.crawler.try_chunked_download", return_value=tmp_path / "book.epub"
        ) as mock_chunked:
            result = crawler.download_file("https://cdn/book.epub", tmp_path, "book.epub")

        assert result is not None
        mock_chunked.assert_called_once()

    def test_falls_back_to_single_on_chunked_failure(self, crawler: KmoeCrawler, tmp_path: Path):
        crawler.workers = 2

        with patch("kmoe.crawler.try_chunked_download", return_value=None), \
             patch("kmoe.crawler.single_download", return_value=tmp_path / "book.epub"):
            result = crawler.download_file("https://cdn/book.epub", tmp_path, "book.epub")

        assert result is not None

    def test_uses_single_when_workers_is_1(self, crawler: KmoeCrawler, tmp_path: Path):
        crawler.workers = 1
        with patch(
            "kmoe.crawler.single_download", return_value=tmp_path / "book.epub"
        ) as mock_single:
            result = crawler.download_file("https://cdn/book.epub", tmp_path, "book.epub")

        assert result is not None
        mock_single.assert_called_once()
