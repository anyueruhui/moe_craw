"""核心爬虫逻辑：搜索、详情、下载"""

import json
import re
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


class KmoeCrawler:
    def __init__(
        self,
        cookies: dict,
        delay: float = 1.0,
        account_manager: AccountManager | None = None,
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
            if any(kw in msg for kw in ("额度", "權限", "limit", "quota", "等級", "验证")):
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
        self, url: str, save_dir: Path, filename: str | None = None
    ) -> Path | None:
        """从 CDN 下载文件，带进度条和原子写入"""
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
        """批量下载一本漫画，支持多账号轮换"""
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

        success = 0
        fail = 0
        for vol in volumes:
            if self._download_volume(vol, detail, book_dir, file_type, book_url):
                success += 1
            else:
                fail += 1

        print(f"\n[*] 完成: {success} 成功, {fail} 失败")

    def _download_volume(
        self, vol: dict, detail: dict, book_dir: Path, file_type: int, book_url: str
    ) -> bool:
        max_attempts = self._account_manager.account_count if self._account_manager else 1

        for attempt in range(max_attempts):
            try:
                return self._try_download_volume(vol, detail, book_dir, file_type)
            except AccountExhaustedError as e:
                print(f"[!] 账号不可用: {e}")
                if not self._account_manager:
                    return False
                new_cookies = self._account_manager.switch_account(str(e))
                if new_cookies:
                    self.replace_session(new_cookies)
                    detail = self.get_book_detail(book_url) or detail
                    continue
                else:
                    print("[!] 所有账号已耗尽，停止下载")
                    return False

        print(f"[-] 所有账号均失败: {vol['name']}")
        return False

    def _try_download_volume(
        self, vol: dict, detail: dict, book_dir: Path, file_type: int
    ) -> bool:
        dl_info = None
        ext = "epub" if file_type == 2 else "mobi"

        if file_type == 2:
            dl_info = self.get_download_url(detail["bookid"], vol["volid"], file_type=2)
            if not dl_info or not dl_info.get("url"):
                print(f"    {vol['name']}: epub 不可用，回退 mobi")
                dl_info = self.get_download_url(detail["bookid"], vol["volid"], file_type=1)
                ext = "mobi"
        else:
            dl_info = self.get_download_url(detail["bookid"], vol["volid"], file_type=1)

        if dl_info and dl_info["url"]:
            filename = self._make_filename(detail["title"], vol["name"], ext)
            result = self.download_file(dl_info["url"], book_dir, filename=filename)
            return result is not None
        else:
            print(f"[-] 跳过: {vol['name']}")
            return False

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
