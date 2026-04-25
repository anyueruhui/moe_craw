#!/usr/bin/env python3
"""
Kmoe 站点安全测试脚本 - 自动化流程测试
支持多账号轮换：当某账号额度耗尽或 session 失效时，自动切换下一个账号
"""

import re
import json
import time
import argparse
from pathlib import Path
from urllib.parse import unquote

import requests


BASE_URL = "https://koz.moe"
DOWNLOAD_DIR = Path("./downloads")
CONFIG_FILE = Path(__file__).parent / "config.json"
STATE_FILE = Path(__file__).parent / "state.json"


class AccountExhaustedError(Exception):
    """账号额度耗尽或 session 失效，需要切换账号"""
    pass


class KmoeCrawler:
    def __init__(self, cookies: dict, delay: float = 1.0, cfg: dict | None = None):
        self.session = requests.Session()
        self.session.cookies.update(cookies)
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 "
                "Mobile/15E148 Safari/604.1"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,zh-TW;q=0.7",
            "Referer": f"{BASE_URL}/",
        })
        self.delay = delay
        self.cfg = cfg or {}
        self.request_count = 0
        self.security_notes: list[str] = []

    def replace_session(self, cookies: dict) -> None:
        """切换到另一个账号的 cookies"""
        self.session.cookies.clear()
        self.session.cookies.update(cookies)

    def _get(self, url: str, **kwargs) -> requests.Response:
        self.request_count += 1
        resp = self.session.get(url, **kwargs)
        for cookie_name in ("VOLSKEY", "VOLSESS"):
            if cookie_name in resp.cookies:
                self.security_notes.append(
                    f"Session cookie {cookie_name} was rotated by server"
                )
        self._sync_config()
        time.sleep(self.delay)
        return resp

    def _sync_config(self):
        """将当前 session cookie 写入 state.json"""
        state = _load_state()
        accounts = state.setdefault("accounts", [])
        idx = state.get("active_account", 0)

        changed = False
        for name in ("VOLSKEY", "VOLSESS", "VLIBSID"):
            val = ""
            for c in self.session.cookies:
                if c.name == name:
                    val = c.value
            if not val:
                continue
            while len(accounts) <= idx:
                accounts.append({})
            key = name.lower()
            if accounts[idx].get(key) != val:
                accounts[idx][key] = val
                changed = True

        if changed:
            _save_state(state)

    def search(self, keyword: str) -> list[dict]:
        """搜索漫画，解析 disp_divinfo JS 数据调用"""
        resp = self._get(f"{BASE_URL}/list.php", params={"s": keyword})
        if resp.status_code != 200:
            print(f"[!] 搜索失败: HTTP {resp.status_code}")
            return []

        pattern = re.compile(
            r'disp_divinfo\(\s*"div_info_"\s*\+\s*"\d+",\s*'
            r'"(https?://[^"]+)",\s*'
            r'"(https?://[^"]+)",\s*'
            r'"([^"]*)",\s*'
            r'"[^"]*",\s*'
            r'"[^"]*",\s*'
            r'"[^"]*",\s*'
            r'"[^"]*",\s*'
            r'"([^"]*)",\s*'
            r'"([^"]+)",\s*'
            r'"([^"]+)",\s*'
            r'"([^"]+)",\s*'
            r'"([^"]*)"\s*\)'
        )

        results = []
        for m in pattern.finditer(resp.text):
            name = re.sub(r"<[^>]+>", "", m.group(5)).strip()
            results.append({
                "book_url": m.group(1),
                "cover_url": m.group(2),
                "score": m.group(4),
                "name": name,
                "author": m.group(6),
                "status": m.group(7),
                "update": m.group(8),
            })

        print(f"[*] 搜索 '{keyword}': {len(results)} 个结果")
        for i, r in enumerate(results):
            print(f"  [{i+1}] {r['name']} - {r['author']} [{r['score']}]")
        return results

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
        uin = extract_var("uin")
        is_vip = extract_var("is_vip")
        ulevel = extract_var("ulevel")
        quota_now = extract_var("quota_now")
        quota_used = extract_var("quota_used")

        hash_match = re.search(r'book_data\.php\?h=([A-Za-z0-9]+)', text)
        data_hash = hash_match.group(1) if hash_match else ""

        title_match = re.search(r'<title>([^<]+)</title>', text)
        title = title_match.group(1).strip() if title_match else bookid

        detail = {
            "url": book_url,
            "title": title,
            "bookid": bookid,
            "uin": uin,
            "is_vip": is_vip,
            "ulevel": ulevel,
            "quota_now": quota_now,
            "quota_used": quota_used,
            "data_hash": data_hash,
        }

        print(f"[*] 漫画: {title}")
        print(f"    bookid={bookid}, uin={uin}, vip={is_vip}, lv={ulevel}")
        print(f"    quota: {quota_used}/{quota_now}, hash={data_hash[:20]}...")

        if uin in data_hash:
            self.security_notes.append(
                "book_data.php hash 包含用户 ID 明文 (uin in hash)"
            )

        return detail

    def get_volumes(self, data_hash: str) -> list[dict]:
        """获取卷列表，解析 book_data.php 返回的 postMessage 数据"""
        url = f"{BASE_URL}/book_data.php?h={data_hash}"
        resp = self._get(url)
        if resp.status_code != 200:
            print(f"[!] 卷数据失败: HTTP {resp.status_code}")
            return []

        volumes = []
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

    def get_download_url(
        self, bookid: str, volid: str, file_type: int = 1, vip_line: int = 0
    ) -> dict | None:
        """通过 getdownurl.php 获取真实下载 URL，403 时抛 AccountExhaustedError"""
        url = (
            f"{BASE_URL}/getdownurl.php"
            f"?b={bookid}&v={volid}&mobi={file_type}&vip={vip_line}&json=1"
        )
        resp = self._get(url)

        if resp.status_code == 403:
            raise AccountExhaustedError(f"getdownurl 403 (session/quota)")
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
            # 额度/权限类错误 → 切换账号
            if any(kw in msg for kw in ("额度", "權限", "limit", "quota", "等級", "验证")):
                raise AccountExhaustedError(f"getdownurl: {msg}")
            print(f"[!] getdownurl 错误: {msg}")
            return None

        dl_url = data.get("url", "")
        if "u=" in dl_url:
            self.security_notes.append(
                "下载 URL 包含用户 ID 参数 (u=xxx)，可追溯"
            )

        return {
            "url": dl_url,
            "name": data.get("name", ""),
            "disp": data.get("disp", ""),
        }

    def download_file(self, url: str, save_dir: Path, filename: str | None = None) -> Path | None:
        """从 CDN 下载文件，403 时抛 AccountExhaustedError"""
        self.request_count += 1
        resp = self.session.get(url, stream=True)

        if resp.status_code == 403:
            raise AccountExhaustedError(f"CDN download 403")
        if resp.status_code != 200:
            print(f"[!] 下载失败: HTTP {resp.status_code}")
            return None

        if not filename:
            cd = resp.headers.get("Content-Disposition", "")
            if cd:
                fn_match = re.search(
                    r'filename[*]?=["\']?(?:UTF-8\'\')?([^"\';]+)', cd
                )
                if fn_match:
                    filename = unquote(fn_match.group(1).strip())
            if not filename:
                filename = url.split("/")[-1].split("?")[0] or "download"

        filename = re.sub(r'[\\/:*?"<>|]', '_', filename)
        save_dir.mkdir(parents=True, exist_ok=True)
        filepath = save_dir / filename

        total = 0
        tmp_path = filepath.with_suffix(filepath.suffix + '.tmp')
        try:
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
                    total += len(chunk)
            tmp_path.replace(filepath)
        except (IOError, OSError) as e:
            print(f"[!] 下载写入失败: {e}")
            tmp_path.unlink(missing_ok=True)
            return None

        size_mb = total / 1024 / 1024
        print(f"[+] 已下载: {filename} ({size_mb:.1f} MB)")
        return filepath

    @staticmethod
    def _make_filename(book_title: str, vol_name: str, ext: str) -> str:
        """生成格式: 漫画名_卷名.ext"""
        name = re.split(r"\s*[:：]\s*", book_title)[0].strip()
        name = re.sub(r'[\\/:*?"<>|]', '', name)
        vol = vol_name.replace(" ", "").strip()
        vol = re.sub(r'(\d+)', lambda m: m.group(1).zfill(2), vol)
        return f"{name}_{vol}.{ext}"

    def batch_download_book(
        self,
        book_url: str,
        save_dir: Path | None = None,
        file_type: int = 2,
        start_vol: int = 0,
        max_vols: int = 0,
    ):
        """
        批量下载一本漫画，支持多账号轮换
        遇到 AccountExhaustedError 时自动切换下一个账号重试
        """
        if save_dir is None:
            save_dir = DOWNLOAD_DIR

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
            if not self._download_volume(
                vol, detail, book_dir, file_type, book_url
            ):
                fail += 1
            else:
                success += 1

        print(f"\n[*] 完成: {success} 成功, {fail} 失败")

    def _download_volume(
        self, vol: dict, detail: dict, book_dir: Path, file_type: int, book_url: str
    ) -> bool:
        """下载单个卷，失败时尝试切换账号重试"""
        max_attempts = len(self.cfg.get("accounts", [])) + 1

        for attempt in range(max_attempts):
            try:
                return self._try_download_volume(vol, detail, book_dir, file_type)
            except AccountExhaustedError as e:
                print(f"[!] 账号不可用: {e}")
                new_cookies = switch_account(self.cfg, str(e))
                if new_cookies:
                    self.replace_session(new_cookies)
                    account = self.cfg["accounts"][self.cfg["active_account"]]
                    print(f"[*] 已切换到账号: {account['email']}")
                    # 刷新详情页以更新 detail（quota/hash 可能不同）
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
        """尝试下载单个卷（不含账号切换逻辑）"""
        dl_info = None
        ext = "epub" if file_type == 2 else "mobi"

        if file_type == 2:
            dl_info = self.get_download_url(
                detail["bookid"], vol["volid"], file_type=2
            )
            if not dl_info or not dl_info.get("url"):
                print(f"    {vol['name']}: epub 不可用，回退 mobi")
                dl_info = self.get_download_url(
                    detail["bookid"], vol["volid"], file_type=1
                )
                ext = "mobi"
        else:
            dl_info = self.get_download_url(
                detail["bookid"], vol["volid"], file_type=1
            )

        if dl_info and dl_info["url"]:
            filename = self._make_filename(detail["title"], vol["name"], ext)
            result = self.download_file(dl_info["url"], book_dir, filename=filename)
            return result is not None
        else:
            print(f"[-] 跳过: {vol['name']}")
            return False


def security_report(crawler: KmoeCrawler, elapsed: float):
    print("\n" + "=" * 60)
    print("  安全测试报告 - Kmoe 自动化流程检测")
    print("=" * 60)
    print(f"  总请求数: {crawler.request_count}")
    print(f"  总耗时: {elapsed:.1f}s")
    print(f"  平均 RPS: {crawler.request_count/max(elapsed,0.1):.2f}")
    print()

    print("  [CRITICAL] 发现的安全问题:")
    print()
    print("  1. captcha_show() 无实际 CAPTCHA 验证")
    print("     - 函数名暗示有验证码，但实际只是下载分发逻辑")
    print("     - 直接调用 /getdownurl.php 即可绕过")
    print()
    print("  2. /dl/ 路径有保护(403)，但 /getdownurl.php 完全等价")
    print("     - /getdownurl.php 返回的 CDN URL 可直接下载")
    print("     - 两条路径达到同样效果，/dl/ 的保护形同虚设")
    print()
    print("  3. book_data.php hash 包含用户 ID 明文")
    print("     - hash 格式: <timestamp>X<bookid><uid><hmac>")
    print("     - 用户 ID 可被提取，用于用户枚举")
    print()
    print("  4. Session cookie 滚动更新")
    notes = set(crawler.security_notes)
    if notes:
        for note in notes:
            print(f"     - {note}")
    else:
        print("     - 未检测到 cookie 轮换（可能与请求量有关）")
    print()
    print("  5. CDN 签名 URL 分析:")
    print("     - 封面图签名过期时间 ~2035 年（过长）")
    print("     - 下载文件签名含用户 ID (u=xxx)，可追溯")
    print("     - 但签名 URL 一旦获取，在过期前可无限次下载")
    print()
    print("  [建议]")
    print("  - 为 /getdownurl.php 增加真正的 CAPTCHA 或 TOTP 验证")
    print("  - 增加 API 频率限制 (如: 每分钟最多 N 次下载)")
    print("  - 缩短 CDN 签名 URL 有效期至 1-6 小时")
    print("  - 考虑增加 User-Agent/行为分析来检测自动化")
    print("  - book_data.php hash 中移除用户 ID 明文")
    print("=" * 60)


# ── 状态管理 ──────────────────────────────────────────


def _load_state() -> dict:
    """加载运行时状态（cookies, exhausted 标记等）"""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict) -> None:
    """持久化运行时状态到 state.json"""
    tmp = STATE_FILE.with_suffix('.tmp')
    with open(tmp, "w") as f:
        json.dump(state, f, indent=4)
    tmp.replace(STATE_FILE)


# ── 配置加载 ──────────────────────────────────────────


def load_config() -> dict:
    """加载用户配置，不写入任何文件"""
    if not CONFIG_FILE.exists():
        return {}

    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    print(f"[*] 已加载配置: {CONFIG_FILE}")

    # 兼容旧格式：顶层 email/passwd → accounts[0]
    if "accounts" not in cfg:
        email = cfg.get("email", "")
        passwd = cfg.get("passwd", "")
        if email or passwd:
            account = {"email": email, "passwd": passwd}
            cfg["accounts"] = [account]
            cfg.pop("email", None)
            cfg.pop("passwd", None)
            print("[*] 已自动识别旧格式配置")

    return cfg


# ── 登录 & 账号管理 ───────────────────────────────────


def login(cfg: dict, account_index: int = 0) -> dict | None:
    """登录指定账号，返回 cookie 字典，状态写入 state.json"""
    accounts = cfg.get("accounts", [])
    if account_index >= len(accounts):
        return None

    account = accounts[account_index]
    email = account.get("email", "")
    passwd = account.get("passwd", "")
    if not email or not passwd:
        return None

    print(f"[*] 登录账号[{account_index}]: {email}")
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 "
            "Mobile/15E148 Safari/604.1"
        ),
        "Referer": "https://koz.moe/login.php",
        "Origin": "https://koz.moe",
    })

    try:
        resp = s.post(
            f"{BASE_URL}/login_do.php",
            data={"email": email, "passwd": passwd, "keepalive": "1"},
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"[!] 登录请求失败: {e}")
        return None

    vlibsid = volskey = volsess = ""
    for c in s.cookies:
        if c.name == "VLIBSID": vlibsid = c.value
        elif c.name == "VOLSKEY": volskey = c.value
        elif c.name == "VOLSESS": volsess = c.value

    if not vlibsid:
        try:
            s.get(f"{BASE_URL}/my.php", timeout=30)
        except requests.RequestException:
            pass
        for c in s.cookies:
            if c.name == "VLIBSID": vlibsid = c.value
            elif c.name == "VOLSKEY": volskey = c.value
            elif c.name == "VOLSESS": volsess = c.value

    if not vlibsid:
        print(f"[!] 登录失败: HTTP {resp.status_code}")
        return None

    print(f"[+] 登录成功: VLIBSID={vlibsid[:20]}...")

    # 持久化到 state.json
    state = _load_state()
    accs = state.setdefault("accounts", [])
    while len(accs) <= account_index:
        accs.append({})
    accs[account_index].update({
        "vlibsid": vlibsid,
        "volskey": volskey or "",
        "volsess": volsess or "",
    })
    state["active_account"] = account_index
    _save_state(state)

    return {"VLIBSID": vlibsid, "VOLSKEY": volskey or "", "VOLSESS": volsess or ""}


def switch_account(cfg: dict, reason: str) -> dict | None:
    """标记当前账号耗尽，切换到下一个可用账号"""
    accounts = cfg.get("accounts", [])
    if not accounts:
        return None

    state = _load_state()
    current = state.get("active_account", 0)

    # 标记当前账号
    st_accs = state.setdefault("accounts", [])
    while len(st_accs) <= current:
        st_accs.append({})
    st_accs[current]["exhausted"] = True
    st_accs[current]["exhausted_reason"] = reason
    print(f"[*] 账号[{current}] 已标记为耗尽: {reason}")

    # 寻找下一个未耗尽的账号（环形搜索）
    for i in range(1, len(accounts) + 1):
        next_idx = (current + i) % len(accounts)
        st_acc = st_accs[next_idx] if next_idx < len(st_accs) else {}
        if not st_acc.get("exhausted", False):
            print(f"[*] 切换到账号[{next_idx}]: {accounts[next_idx]['email']}")
            state["active_account"] = next_idx
            _save_state(state)
            cookies = login(cfg, next_idx)
            if cookies:
                return cookies
            # 登录失败也标记为耗尽
            while len(st_accs) <= next_idx:
                st_accs.append({})
            st_accs[next_idx]["exhausted"] = True
            st_accs[next_idx]["exhausted_reason"] = "login failed"

    _save_state(state)
    print("[!] 没有可用的账号了")
    return None


def reset_accounts():
    """重置所有账号的耗尽标记（每次新下载会话开始时调用）"""
    state = _load_state()
    changed = False
    for acc in state.get("accounts", []):
        if acc.get("exhausted", False):
            acc["exhausted"] = False
            acc["exhausted_reason"] = None
            changed = True
    if changed:
        _save_state(state)


def _get_active_cookies() -> dict | None:
    """从 state.json 中获取当前活跃账号的 cookies"""
    state = _load_state()
    accs = state.get("accounts", [])
    idx = state.get("active_account", 0)
    if accs and idx < len(accs):
        acc = accs[idx]
        vlibsid = acc.get("vlibsid", "")
        volskey = acc.get("volskey", "")
        volsess = acc.get("volsess", "")
        if all([vlibsid, volskey, volsess]):
            return {"VLIBSID": vlibsid, "VOLSKEY": volskey, "VOLSESS": volsess}
    return None


# ── 主入口 ────────────────────────────────────────────


def main():
    cfg = load_config()
    reset_accounts()

    parser = argparse.ArgumentParser(
        description="Kmoe 站点安全测试 - 多账号自动轮换"
    )
    parser.add_argument("--cookie-vlibsid")
    parser.add_argument("--cookie-volskey")
    parser.add_argument("--cookie-volsess")
    parser.add_argument("--search", "-s", help="搜索关键词")
    parser.add_argument("--book-url", help="漫画详情页 URL")
    parser.add_argument("--download", "-d", action="store_true", help="执行下载")
    parser.add_argument("--download-all", action="store_true", help="下载搜索到的所有漫画")
    parser.add_argument("--type", choices=["mobi", "epub"], default=cfg.get("type", "epub"))
    parser.add_argument("--start", type=int, default=cfg.get("start", 0), help="从第 N 卷开始")
    parser.add_argument("--max", type=int, default=cfg.get("max", 0), help="最多下载 N 卷 (0=全部)")
    parser.add_argument("--delay", type=float, default=cfg.get("delay", 1.0), help="请求间隔(秒)")
    parser.add_argument("--output", "-o", default=cfg.get("output", "./downloads"))
    parser.add_argument("--login", action="store_true", help="强制重新登录")
    args = parser.parse_args()

    # 获取 cookies：CLI 参数 > state.json > 自动登录
    vlibsid = args.cookie_vlibsid
    volskey = args.cookie_volskey
    volsess = args.cookie_volsess

    if not all([vlibsid, volskey, volsess]):
        active = _get_active_cookies()
        if active:
            vlibsid, volskey, volsess = active["VLIBSID"], active["VOLSKEY"], active["VOLSESS"]

    if args.login or not all([vlibsid, volskey, volsess]):
        idx = _load_state().get("active_account", 0)
        auto_cookies = login(cfg, idx)
        if auto_cookies:
            vlibsid, volskey, volsess = auto_cookies["VLIBSID"], auto_cookies["VOLSKEY"], auto_cookies["VOLSESS"]
        elif not all([vlibsid, volskey, volsess]):
            parser.error(
                "Cookie 缺失且自动登录失败。请在 config.json 中配置 accounts，"
                "或手动填写 cookie。"
            )

    cookies = {"VLIBSID": vlibsid, "VOLSKEY": volskey, "VOLSESS": volsess}
    file_type = 1 if args.type == "mobi" else 2
    crawler = KmoeCrawler(cookies, delay=args.delay, cfg=cfg)
    start_time = time.time()

    if args.search:
        results = crawler.search(args.search)
        if args.download_all and results:
            for r in results:
                print(f"\n{'─' * 50}")
                crawler.batch_download_book(
                    r["book_url"],
                    save_dir=Path(args.output),
                    file_type=file_type,
                    start_vol=args.start,
                    max_vols=args.max,
                )
        elif args.download and results:
            crawler.batch_download_book(
                results[0]["book_url"],
                save_dir=Path(args.output),
                file_type=file_type,
                start_vol=args.start,
                max_vols=args.max,
            )
        elif results:
            print(f"\n  添加 -d 下载第一个结果，--download-all 下载全部")
            for r in results:
                print(f"    {r['book_url']}")

    elif args.book_url:
        crawler.batch_download_book(
            args.book_url,
            save_dir=Path(args.output),
            file_type=file_type,
            start_vol=args.start,
            max_vols=args.max,
        )
    else:
        parser.print_help()
        return

    elapsed = time.time() - start_time
    security_report(crawler, elapsed)


if __name__ == "__main__":
    main()
