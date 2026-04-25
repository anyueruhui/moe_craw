"""核心爬虫逻辑：搜索、详情、下载"""

import concurrent.futures
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import unquote

import requests

from .auth import AccountManager
from .config import BASE_URL, DEFAULT_TIMEOUT

_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 "
    "Mobile/15E148 Safari/604.1"
)


class AccountExhaustedError(Exception):
    """账号额度耗尽或 session 失效，需要切换账号"""


class _ProgressTracker:
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


class KmoeCrawler:
    def __init__(
        self,
        cookies: dict,
        delay: float = 1.0,
        account_manager: AccountManager | None = None,
        workers: int = 1,
    ):
        self.session = requests.Session()
        self.session.cookies.update(cookies)
        self.session.headers.update({
            "User-Agent": _UA,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,zh-TW;q=0.7",
            "Referer": f"{BASE_URL}/",
        })
        self.delay = delay
        self.timeout = DEFAULT_TIMEOUT
        self._account_manager = account_manager
        self.request_count = 0
        self.security_notes: list[str] = []
        self.workers = workers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.session.close()

    def replace_session(self, cookies: dict) -> None:
        self.session.cookies.clear()
        self.session.cookies.update(cookies)

    # ── 网络请求（带超时和重试） ──────────────────────

    def _get(self, url: str, max_retries: int = 3, **kwargs) -> requests.Response:
        """GET 请求，带超时和瞬时错误重试"""
        kwargs.setdefault("timeout", self.timeout)

        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                self.request_count += 1
                resp = self.session.get(url, **kwargs)
                self._check_cookie_rotation(resp)
                self._sync_cookies()
                time.sleep(self.delay)
                return resp
            except requests.exceptions.RequestException as e:
                last_exc = e
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    print(f"[!] 请求失败: {e}, {wait}s 后重试 ({attempt + 1}/{max_retries})")
                    time.sleep(wait)

        raise last_exc  # type: ignore[misc]

    def _check_cookie_rotation(self, resp: requests.Response) -> None:
        for name in ("VOLSKEY", "VOLSESS"):
            if name in resp.cookies:
                self.security_notes.append(
                    f"Session cookie {name} was rotated by server"
                )

    def _sync_cookies(self) -> None:
        if not self._account_manager:
            return
        cookies = {
            c.name: c.value
            for c in self.session.cookies
            if c.name in ("VOLSKEY", "VOLSESS", "VLIBSID")
        }
        self._account_manager.sync_cookies(cookies)

    # ── 搜索 ──────────────────────────────────────────

    def search(self, keyword: str) -> list[dict]:
        """搜索漫画，用两步提取法解析 disp_divinfo JS 调用"""
        resp = self._get(f"{BASE_URL}/list.php", params={"s": keyword})
        if resp.status_code != 200:
            print(f"[!] 搜索失败: HTTP {resp.status_code}")
            return []

        # 第一步：匹配 disp_divinfo() 函数调用
        func_pattern = re.compile(r'disp_divinfo\s*\(([^)]+)\)', re.DOTALL)
        # 第二步：提取所有引号内的字符串参数
        arg_pattern = re.compile(r'"([^"]*)"')

        results: list[dict] = []
        for func_match in func_pattern.finditer(resp.text):
            args = arg_pattern.findall(func_match.group(1))
            # args: "div_info_", "N", book_url, cover_url, ..., score, name, author, status, update
            # 跳过前两个前缀参数 ("div_info_" + 数字)
            data = args[2:]
            if len(data) < 12:
                continue

            name = re.sub(r"<[^>]+>", "", data[8]).strip()
            results.append({
                "book_url": data[0],
                "cover_url": data[1],
                "score": data[7],
                "name": name,
                "author": data[9],
                "status": data[10],
                "update": data[11],
            })

        print(f"[*] 搜索 '{keyword}': {len(results)} 个结果")
        for i, r in enumerate(results):
            print(f"  [{i + 1}] {r['name']} - {r['author']} [{r['score']}]")
        return results

    # ── 详情 ──────────────────────────────────────────

    def get_book_detail(self, book_url: str) -> dict | None:
        """获取漫画详情页，提取 bookid/quota/hash 等变量"""
        resp = self._get(book_url)
        if resp.status_code != 200:
            print(f"[!] 详情页失败: HTTP {resp.status_code}")
            return None

        text = resp.text

        def extract_var(name: str) -> str:
            m = re.search(rf'var\s+{name}\s*=\s*(?:parseInt\(\s*)?["\']?([^";\'\)]+)', text)
            return m.group(1).strip() if m else ""

        bookid = extract_var("bookid")
        hash_match = re.search(r'book_data\.php\?h=([A-Za-z0-9]+)', text)
        title_match = re.search(r'<title>([^<]+)</title>', text)

        detail = {
            "url": book_url,
            "title": title_match.group(1).strip() if title_match else bookid,
            "bookid": bookid,
            "uin": extract_var("uin"),
            "is_vip": extract_var("is_vip"),
            "ulevel": extract_var("ulevel"),
            "quota_now": extract_var("quota_now"),
            "quota_used": extract_var("quota_used"),
            "data_hash": hash_match.group(1) if hash_match else "",
        }

        print(f"[*] 漫画: {detail['title']}")
        print(f"    bookid={bookid}, uin={detail['uin']}, vip={detail['is_vip']}, lv={detail['ulevel']}")
        print(f"    quota: {detail['quota_used']}/{detail['quota_now']}, hash={detail['data_hash'][:20]}...")

        if detail["uin"] in detail["data_hash"]:
            self.security_notes.append("book_data.php hash 包含用户 ID 明文 (uin in hash)")

        return detail

    # ── 卷列表 ────────────────────────────────────────

    def get_volumes(self, data_hash: str) -> list[dict]:
        """获取卷列表，解析 book_data.php 返回的数据"""
        url = f"{BASE_URL}/book_data.php?h={data_hash}"
        resp = self._get(url)
        if resp.status_code != 200:
            print(f"[!] 卷数据失败: HTTP {resp.status_code}")
            return []

        volumes: list[dict] = []
        for m in re.finditer(r'volinfo=([^"]+)', resp.text):
            fields = m.group(1).split(",")
            if len(fields) >= 15:
                volumes.append({
                    "volid": fields[0],
                    "status": fields[1],
                    "category": fields[3],
                    "seq": fields[4],
                    "name": fields[5],
                    "pages": fields[6],
                    "size_mobi": fields[9],
                    "size_epub_small": fields[10],
                    "size_epub": fields[11],
                })

        print(f"[*] 获取到 {len(volumes)} 卷")
        for v in volumes[:5]:
            print(f"    {v['name']} (id={v['volid']}, {v['pages']}p, mobi={v['size_mobi']}MB)")
        if len(volumes) > 5:
            print(f"    ... 共 {len(volumes)} 卷")
        return volumes

    # ── 下载 URL ──────────────────────────────────────

    def get_download_url(
        self, bookid: str, volid: str, file_type: int = 1, vip_line: int = 0
    ) -> dict | None:
        """通过 getdownurl.php 获取真实下载 URL"""
        url = (
            f"{BASE_URL}/getdownurl.php"
            f"?b={bookid}&v={volid}&mobi={file_type}&vip={vip_line}&json=1"
        )
        resp = self._get(url)

        if resp.status_code == 403:
            raise AccountExhaustedError("getdownurl 403 (session/quota)")
        if resp.status_code != 200:
            print(f"[!] getdownurl 失败: HTTP {resp.status_code}")
            return None

        try:
            data = resp.json()
        except json.JSONDecodeError:
            print(f"[!] getdownurl 返回非 JSON: {resp.text[:200]}")
            return None

        if data.get("code") != 200:
            msg = data.get("msg", "")
            if any(kw in msg for kw in ("额度", "額度", "權限", "limit", "quota", "等級", "验证")):
                raise AccountExhaustedError(f"getdownurl: {msg}")
            print(f"[!] getdownurl 错误: {msg}")
            return None

        dl_url = data.get("url", "")
        if "u=" in dl_url:
            self.security_notes.append("下载 URL 包含用户 ID 参数 (u=xxx)，可追溯")

        return {
            "url": dl_url,
            "name": data.get("name", ""),
            "disp": data.get("disp", ""),
        }

    # ── 文件下载 ──────────────────────────────────────

    def download_file(
        self, url: str, save_dir: Path, filename: str | None = None,
        backup_url: str | None = None,
    ) -> Path | None:
        """从 CDN 下载文件，workers > 1 时尝试多线程分块下载

        backup_url: 备用 CDN URL（支持 Range），用于分块下载
        """
        if self.workers > 1:
            # 优先使用备用 CDN（支持 Range）做分块下载
            chunked_url = backup_url or url
            result = self._try_chunked_download(
                chunked_url, save_dir, filename, main_cdn_url=url,
            )
            if result is not None:
                return result
            if backup_url:
                # 备用 CDN 分块下载失败，尝试主 CDN
                print("[*] 备用 CDN 分块下载失败，尝试主 CDN")
                result = self._try_chunked_download(url, save_dir, filename)
                if result is not None:
                    return result
            print("[*] 分块下载不可用，回退单线程")

        return self._single_download(url, save_dir, filename)

    def _single_download(
        self, url: str, save_dir: Path, filename: str | None = None
    ) -> Path | None:
        """单线程下载，带进度条和原子写入"""
        self.request_count += 1
        resp = self.session.get(url, stream=True, timeout=self.timeout)

        if resp.status_code == 403:
            raise AccountExhaustedError("CDN download 403")
        if resp.status_code != 200:
            print(f"[!] 下载失败: HTTP {resp.status_code}")
            return None

        if not filename:
            filename = _extract_filename(resp, url)

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

    def _try_chunked_download(
        self, url: str, save_dir: Path, filename: str | None,
        main_cdn_url: str | None = None,
    ) -> Path | None:
        """尝试多线程分块下载，验证 CDN 真正支持 Range"""
        # 探测：用小 Range 请求检测支持
        try:
            probe = requests.get(
                url,
                headers={"User-Agent": _UA, "Range": "bytes=0-1023"},
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.RequestException:
            return None

        if probe.status_code != 206:
            return None

        # 验证 probe 的 Content-Length 确实是 1024
        probe_cl = int(probe.headers.get("content-length", 0))
        if probe_cl != 1024:
            return None

        # 从 Content-Range 提取总大小
        cr = probe.headers.get("content-range", "")
        total_match = re.search(r"/(\d+)$", cr)
        if not total_match:
            return None
        total_size = int(total_match.group(1))
        if total_size < 1024 * 1024:
            return None

        if not filename:
            filename = _extract_filename(probe, url)
        filename = re.sub(r'[\\/:*?"<>|]', '_', filename)
        save_dir.mkdir(parents=True, exist_ok=True)
        filepath = save_dir / filename

        return self._chunked_download(url, filepath, total_size, filename, main_cdn_url)

    def _chunked_download(
        self, url: str, filepath: Path, total_size: int, filename: str,
        main_cdn_url: str | None = None,
    ) -> Path | None:
        """多线程分块下载，每个分块 5MB，带重试和断点续传

        如果部分分块失败且有 main_cdn_url，用主 CDN 单线程补齐缺失分块。
        """
        tmp_path = filepath.with_suffix(filepath.suffix + ".tmp")
        # 使用 5MB 小分块，提高不稳定 CDN 的成功率
        chunk_size = 5 * 1024 * 1024
        num_chunks = (total_size + chunk_size - 1) // chunk_size
        tracker = _ProgressTracker(total_size, filename)
        total_mb = total_size / 1024 / 1024
        print(f"  [↓] {filename}: 分块下载 ({self.workers} 线程, {num_chunks} 块, {total_mb:.1f} MB)")

        part_files: list[Path] = []
        failed_ranges: list[tuple[int, int, int]] = []  # (chunk_idx, start, end)

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {}
            for i in range(num_chunks):
                start = i * chunk_size
                end = min(start + chunk_size - 1, total_size - 1)
                part_file = tmp_path.with_suffix(f".part{i}")
                part_files.append(part_file)
                f = pool.submit(self._download_range, url, start, end, part_file, tracker)
                futures[f] = (i, start, end)

            for f in concurrent.futures.as_completed(futures):
                i, start, end = futures[f]
                if not f.result():
                    failed_ranges.append((i, start, end))

        # 如果有失败的分块且有主 CDN URL，尝试用主 CDN 补齐
        if failed_ranges and main_cdn_url:
            print(f"  [↻] {len(failed_ranges)}/{num_chunks} 分块失败，用主 CDN 补齐...")
            fallback_ok = True
            for i, start, end in sorted(failed_ranges):
                expected_size = end - start + 1
                part_file = part_files[i]
                # 主 CDN 不支持 Range，需要下载完整文件再截取
                # 更好的方案：直接用主 CDN 单线程下载整个文件
                fallback_ok = False
                break

            if not fallback_ok:
                # 用主 CDN 单线程下载整个文件
                print(f"  [↻] 改用主 CDN 单线程下载")
                for pf in part_files:
                    pf.unlink(missing_ok=True)
                return None

        elif failed_ranges:
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

        print(f"[+] 已下载: {filename} ({total_mb:.1f} MB, {self.workers} 线程)")
        return filepath

    def _download_range(
        self,
        url: str,
        start: int,
        end: int,
        part_file: Path,
        tracker: _ProgressTracker,
    ) -> bool:
        """下载文件的指定字节范围，带重试和断点续传"""
        expected_size = end - start + 1
        max_retries = 10

        for attempt in range(max_retries):
            # 检查已有 part_file 大小，支持断点续传
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
                resp = requests.get(url, headers=headers, stream=True, timeout=120)
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

                # 验证最终文件大小
                actual_size = part_file.stat().st_size
                if actual_size >= expected_size:
                    return True

                # 大小不足，重试补齐
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

    # ── 批量下载 ──────────────────────────────────────

    def batch_download_book(
        self,
        book_url: str,
        save_dir: Path | None = None,
        file_type: int = 2,
        start_vol: int = 0,
        max_vols: int = 0,
        default_output: Path | None = None,
    ) -> None:
        """批量下载一本漫画，支持多账号轮换和并行下载"""
        if save_dir is None:
            save_dir = default_output or Path("~/Downloads").expanduser()

        detail = self.get_book_detail(book_url)
        if not detail or not detail["data_hash"]:
            print("[!] 无法获取漫画信息")
            return

        volumes = self.get_volumes(detail["data_hash"])
        if not volumes:
            print("[!] 无卷数据")
            return

        volumes = volumes[start_vol:]
        if max_vols > 0:
            volumes = volumes[:max_vols]

        book_dir = save_dir / re.sub(r'[\\/:*?"<>|]', '_', detail["title"])
        print(f"\n[*] 开始下载 {len(volumes)} 卷 -> {book_dir}")
        print(f"    类型: {'mobi' if file_type == 1 else 'epub'} (epub 优先)")

        # 阶段 1：顺序获取下载 URL（需要认证 session）
        tasks = self._collect_download_tasks(volumes, detail, file_type, book_url, book_dir)

        if not tasks:
            print("[!] 无可下载的卷")
            return

        # 阶段 2：下载文件
        if self.workers > 1 and len(tasks) > 1:
            success, fail = self._parallel_download(tasks, book_dir)
        else:
            success, fail = 0, 0
            for cdn_url, filename, _vol, backup_url in tasks:
                result = self.download_file(
                    cdn_url, book_dir, filename=filename, backup_url=backup_url
                )
                if result:
                    success += 1
                else:
                    fail += 1

        print(f"\n[*] 完成: {success} 成功, {fail} 失败")

    def _collect_download_tasks(
        self,
        volumes: list[dict],
        detail: dict,
        file_type: int,
        book_url: str,
        book_dir: Path,
    ) -> list[tuple[str, str, dict, str | None]]:
        """顺序获取每卷的 CDN 下载 URL，处理账号轮换"""
        tasks: list[tuple[str, str, dict, str | None]] = []
        for vol in volumes:
            dl_info = self._resolve_download_info(vol, detail, file_type, book_url)
            if dl_info and dl_info["url"]:
                filename = self._make_filename(detail["title"], vol["name"], dl_info["ext"])
                backup = dl_info.get("backup_url")
                tasks.append((dl_info["url"], filename, vol, backup))
            else:
                print(f"[-] 跳过: {vol['name']}")
        return tasks

    def _resolve_download_info(
        self, vol: dict, detail: dict, file_type: int, book_url: str
    ) -> dict | None:
        """获取单卷下载 URL，支持账号轮换"""
        max_attempts = self._account_manager.account_count if self._account_manager else 1
        for attempt in range(max_attempts):
            try:
                return self._try_resolve(vol, detail, file_type)
            except AccountExhaustedError as e:
                print(f"[!] 账号不可用: {e}")
                if not self._account_manager:
                    return None
                new_cookies = self._account_manager.switch_account(str(e))
                if new_cookies:
                    self.replace_session(new_cookies)
                    detail = self.get_book_detail(book_url) or detail
                    continue
                else:
                    print("[!] 所有账号已耗尽")
                    return None
        print(f"[-] 所有账号均失败: {vol['name']}")
        return None

    def _try_resolve(
        self, vol: dict, detail: dict, file_type: int
    ) -> dict | None:
        """尝试获取下载 URL，失败时抛 AccountExhaustedError

        同时获取备用 CDN URL（支持 Range）用于分块下载。
        主 CDN (dl.kmoe9.com) 返回 206 但忽略 Range，
        备用 CDN (free2.mxomo.com) 真正支持 Range。
        """
        ext = "epub" if file_type == 2 else "mobi"
        dl_info = None

        if file_type == 2:
            dl_info = self.get_download_url(detail["bookid"], vol["volid"], file_type=2)
            if not dl_info or not dl_info.get("url"):
                print(f"    {vol['name']}: epub 不可用，回退 mobi")
                dl_info = self.get_download_url(detail["bookid"], vol["volid"], file_type=1)
                ext = "mobi"
        else:
            dl_info = self.get_download_url(detail["bookid"], vol["volid"], file_type=1)

        if dl_info and dl_info["url"]:
            result = {"url": dl_info["url"], "ext": ext}
            # 尝试获取备用 CDN URL（用于分块下载）
            if self.workers > 1:
                backup = self._get_backup_cdn_url(detail["bookid"], vol["volid"], file_type)
                if backup:
                    result["backup_url"] = backup
            return result
        return None

    def _get_backup_cdn_url(
        self, bookid: str, volid: str, file_type: int
    ) -> str | None:
        """通过 /dl/ 路径获取备用 CDN URL（支持 Range）"""
        # tabdisp: 1=mobi, 2=epub; line=1 → 备用 CDN
        tabdisp = 2 if file_type == 2 else 1
        dl_path = f"{BASE_URL}/dl/{bookid}/{volid}/1/{tabdisp}/0/"
        try:
            resp = self.session.get(
                dl_path, timeout=self.timeout, allow_redirects=False
            )
            if resp.status_code == 302:
                backup_url = resp.headers.get("location", "")
                if "mxomo.com" in backup_url:
                    self.request_count += 1
                    return backup_url
        except requests.RequestException:
            pass
        return None

    def _parallel_download(
        self, tasks: list[tuple[str, str, dict, str | None]], book_dir: Path
    ) -> tuple[int, int]:
        """并行下载多个文件（CDN URL 无需认证，线程安全）"""
        print(f"[*] 并行下载: {len(tasks)} 卷, {self.workers} 线程")

        success = 0
        fail = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {}
            for cdn_url, filename, vol, backup_url in tasks:
                f = pool.submit(self._download_from_cdn, cdn_url, book_dir, filename, backup_url)
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

    def _download_from_cdn(
        self, url: str, save_dir: Path, filename: str,
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

    @staticmethod
    def _make_filename(book_title: str, vol_name: str, ext: str) -> str:
        name = re.split(r"\s*[:：]\s*", book_title)[0].strip()
        name = re.sub(r'[\\/:*?"<>|]', '', name)
        vol = vol_name.replace(" ", "").strip()
        vol = re.sub(r'(\d+)', lambda m: m.group(1).zfill(2), vol)
        return f"{name}_{vol}.{ext}"


def _extract_filename(resp: requests.Response, url: str) -> str:
    """从 Content-Disposition 或 URL 中提取文件名"""
    cd = resp.headers.get("Content-Disposition", "")
    if cd:
        fn_match = re.search(r'filename[*]?=["\']?(?:UTF-8\'\')?([^"\';]+)', cd)
        if fn_match:
            return unquote(fn_match.group(1).strip())
    return url.split("/")[-1].split("?")[0] or "download"
