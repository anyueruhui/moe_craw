"""文件下载：单线程、多线程分块、并行下载"""

import concurrent.futures
import re
import threading
import time
from pathlib import Path
from urllib.parse import unquote

import requests

_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 "
    "Mobile/15E148 Safari/604.1"
)


class ProgressTracker:
    """Thread-safe progress tracker for multi-chunk downloads"""

    def __init__(self, total_size: int, filename: str):
        self.total_size = total_size
        self.filename = filename
        self._downloaded = 0
        self._lock = threading.Lock()
        self._last_print = time.monotonic()

    def add(self, nbytes: int) -> None:
        with self._lock:
            self._downloaded += nbytes
            now = time.monotonic()
            if now - self._last_print >= 3.0:
                dl_mb = self._downloaded / 1024 / 1024
                total_mb = self.total_size / 1024 / 1024
                pct = self._downloaded * 100 // self.total_size if self.total_size > 0 else 0
                print(f"  [↓] {self.filename}: {pct}% ({dl_mb:.1f}/{total_mb:.1f} MB)")
                self._last_print = now


def extract_filename(resp: requests.Response, url: str) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    if cd:
        fn_match = re.search(r'filename[*]?=["\']?(?:UTF-8\'\')?([^"\';]+)', cd)
        if fn_match:
            return unquote(fn_match.group(1).strip())
    return url.split("/")[-1].split("?")[0] or "download"


def single_download(
    session: requests.Session,
    url: str,
    save_dir: Path,
    filename: str | None = None,
    timeout: int = 15,
) -> Path | None:
    """单线程下载，带进度条和原子写入"""
    resp = session.get(url, stream=True, timeout=timeout)

    if resp.status_code == 403:
        from .crawler import AccountExhaustedError
        raise AccountExhaustedError("CDN download 403")
    if resp.status_code != 200:
        print(f"[!] 下载失败: HTTP {resp.status_code}")
        return None

    if not filename:
        filename = extract_filename(resp, url)

    filename = re.sub(r'[\\/:*?"<>|]', '_', filename)
    save_dir.mkdir(parents=True, exist_ok=True)
    filepath = save_dir / filename

    total_size = int(resp.headers.get("content-length", 0))
    tmp_path = filepath.with_suffix(filepath.suffix + ".tmp")
    try:
        with open(tmp_path, "wb") as f:
            downloaded = 0
            last_time = time.monotonic()
            total_mb = total_size / 1024 / 1024
            for chunk in resp.iter_content(chunk_size=8192):
                size = f.write(chunk)
                downloaded += size
                now = time.monotonic()
                if total_size > 0 and now - last_time >= 5.0:
                    dl_mb = downloaded / 1024 / 1024
                    pct = downloaded * 100 // total_size
                    print(f"  [↓] {filename}: {pct}% ({dl_mb:.1f}/{total_mb:.1f} MB)")
                    last_time = now
            if total_size > 0:
                dl_mb = downloaded / 1024 / 1024
                print(f"  [↓] {filename}: 100% ({dl_mb:.1f}/{total_mb:.1f} MB)")
        tmp_path.replace(filepath)
    except (IOError, OSError) as e:
        print(f"[!] 下载写入失败: {e}")
        tmp_path.unlink(missing_ok=True)
        return None

    size_mb = total_size / 1024 / 1024
    print(f"[+] 已下载: {filename} ({size_mb:.1f} MB)")
    return filepath


def try_chunked_download(
    url: str,
    save_dir: Path,
    filename: str | None,
    workers: int,
    timeout: int = 15,
    main_cdn_url: str | None = None,
) -> Path | None:
    """尝试多线程分块下载，验证 CDN 真正支持 Range"""
    try:
        probe = requests.get(
            url,
            headers={"User-Agent": _UA, "Range": "bytes=0-1023"},
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException:
        return None

    if probe.status_code != 206:
        return None

    probe_cl = int(probe.headers.get("content-length", 0))
    if probe_cl != 1024:
        return None

    cr = probe.headers.get("content-range", "")
    total_match = re.search(r"/(\d+)$", cr)
    if not total_match:
        return None
    total_size = int(total_match.group(1))
    if total_size < 1024 * 1024:
        return None

    if not filename:
        filename = extract_filename(probe, url)
    filename = re.sub(r'[\\/:*?"<>|]', '_', filename)
    save_dir.mkdir(parents=True, exist_ok=True)
    filepath = save_dir / filename

    return chunked_download(
        url, filepath, total_size, filename, workers, timeout, main_cdn_url,
    )


def chunked_download(
    url: str,
    filepath: Path,
    total_size: int,
    filename: str,
    workers: int = 2,
    timeout: int = 15,
    main_cdn_url: str | None = None,
) -> Path | None:
    """多线程分块下载，每个分块 5MB，带重试和断点续传"""
    tmp_path = filepath.with_suffix(filepath.suffix + ".tmp")
    chunk_size = 5 * 1024 * 1024
    num_chunks = (total_size + chunk_size - 1) // chunk_size
    tracker = ProgressTracker(total_size, filename)
    total_mb = total_size / 1024 / 1024
    print(f"  [↓] {filename}: 分块下载 ({workers} 线程, {num_chunks} 块, {total_mb:.1f} MB)")

    part_files: list[Path] = []
    failed_ranges: list[tuple[int, int, int]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for i in range(num_chunks):
            start = i * chunk_size
            end = min(start + chunk_size - 1, total_size - 1)
            part_file = tmp_path.with_suffix(f".part{i}")
            part_files.append(part_file)
            f = pool.submit(download_range, url, start, end, part_file, tracker, timeout)
            futures[f] = (i, start, end)

        for f in concurrent.futures.as_completed(futures):
            i, start, end = futures[f]
            if not f.result():
                failed_ranges.append((i, start, end))

    if failed_ranges:
        for pf in part_files:
            pf.unlink(missing_ok=True)
        return None

    # 合并分块
    try:
        with open(tmp_path, "wb") as out:
            for pf in part_files:
                with open(pf, "rb") as inp:
                    while True:
                        buf = inp.read(1024 * 1024)
                        if not buf:
                            break
                        out.write(buf)
                pf.unlink()
        tmp_path.replace(filepath)
    except (IOError, OSError) as e:
        print(f"[!] 合并分块失败: {e}")
        tmp_path.unlink(missing_ok=True)
        for pf in part_files:
            pf.unlink(missing_ok=True)
        return None

    print(f"[+] 已下载: {filename} ({total_mb:.1f} MB, {workers} 线程)")
    return filepath


def download_range(
    url: str,
    start: int,
    end: int,
    part_file: Path,
    tracker: ProgressTracker,
    timeout: int = 120,
) -> bool:
    """下载文件的指定字节范围，带重试和断点续传"""
    expected_size = end - start + 1
    max_retries = 10

    for attempt in range(max_retries):
        resume_start = start
        if part_file.exists():
            existing_size = part_file.stat().st_size
            if existing_size > 0 and existing_size < expected_size:
                resume_start = start + existing_size
            elif existing_size >= expected_size:
                tracker.add(expected_size)
                return True

        headers = {"User-Agent": _UA, "Range": f"bytes={resume_start}-{end}"}
        try:
            resp = requests.get(url, headers=headers, stream=True, timeout=timeout)
            if resp.status_code != 206:
                if attempt == 0:
                    print(f"[!] 分块 {start}-{end}: HTTP {resp.status_code}")
                resp.close()
                continue

            cl = int(resp.headers.get("content-length", 0))
            expected_chunk = end - resume_start + 1
            if cl != expected_chunk:
                resp.close()
                if attempt == 0:
                    print(f"[!] 分块 {start}-{end}: CL={cl} != {expected_chunk}")
                continue

            mode = "ab" if resume_start > start else "wb"
            with open(part_file, mode) as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    tracker.add(len(chunk))

            actual_size = part_file.stat().st_size
            if actual_size >= expected_size:
                return True

            if attempt < max_retries - 1:
                print(f"  [↻] 分块 {start}-{end}: {actual_size}/{expected_size}，重试")
                continue

        except requests.RequestException as e:
            if attempt < max_retries - 1:
                print(f"  [↻] 分块 {start}-{end}: 连接中断，重试 ({attempt+1}/{max_retries})")
                continue
            print(f"[!] 分块 {start}-{end} 失败: {e}")
            break

    part_file.unlink(missing_ok=True)
    return False


def parallel_download(
    tasks: list[tuple[str, str, dict, str | None]],
    book_dir: Path,
    workers: int,
) -> tuple[int, int]:
    """并行下载多个文件（CDN URL 无需认证，线程安全）"""
    print(f"[*] 并行下载: {len(tasks)} 卷, {workers} 线程")

    success = 0
    fail = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for cdn_url, filename, vol, backup_url in tasks:
            f = pool.submit(download_from_cdn, cdn_url, book_dir, filename, backup_url)
            futures[f] = (filename, vol)

        for f in concurrent.futures.as_completed(futures):
            filename, vol = futures[f]
            try:
                result = f.result()
                if result:
                    success += 1
                else:
                    fail += 1
            except Exception as e:
                print(f"[!] 下载异常 {filename}: {e}")
                fail += 1

    return success, fail


def download_from_cdn(
    url: str,
    save_dir: Path,
    filename: str,
    backup_url: str | None = None,
) -> Path | None:
    """从 CDN 下载单个文件（不使用 session，线程安全）"""
    resp = requests.get(
        url, headers={"User-Agent": _UA}, stream=True, timeout=180
    )
    if resp.status_code != 200:
        print(f"[!] 下载失败: {filename} HTTP {resp.status_code}")
        return None

    save_dir.mkdir(parents=True, exist_ok=True)
    filepath = save_dir / filename
    total_size = int(resp.headers.get("content-length", 0))
    tmp_path = filepath.with_suffix(filepath.suffix + ".tmp")

    try:
        with open(tmp_path, "wb") as f:
            downloaded = 0
            last_time = time.monotonic()
            total_mb = total_size / 1024 / 1024
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
                downloaded += len(chunk)
                now = time.monotonic()
                if total_size > 0 and now - last_time >= 10.0:
                    dl_mb = downloaded / 1024 / 1024
                    pct = downloaded * 100 // total_size
                    print(f"  [↓] {filename}: {pct}% ({dl_mb:.1f}/{total_mb:.1f} MB)")
                    last_time = now
        tmp_path.replace(filepath)
    except (IOError, OSError) as e:
        print(f"[!] 写入失败: {filename}: {e}")
        tmp_path.unlink(missing_ok=True)
        return None

    size_mb = total_size / 1024 / 1024
    print(f"[+] 已下载: {filename} ({size_mb:.1f} MB)")
    return filepath
