"""账号管理：登录、轮换、状态持久化"""

import json
from pathlib import Path

import requests

from .config import BASE_URL, STATE_FILE

_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 "
    "Mobile/15E148 Safari/604.1"
)


class AccountManager:
    """管理多账号登录、cookie 持久化、账号轮换"""

    def __init__(self, config: dict, state_file: Path = STATE_FILE):
        self._config = config
        self._state_file = state_file
        self._state_cache: dict | None = None

    # ── 状态读写 ──────────────────────────────────────

    def _load_state(self) -> dict:
        if self._state_cache is None:
            self._state_cache = {}
            if self._state_file.exists():
                try:
                    with open(self._state_file) as f:
                        self._state_cache = json.load(f)
                except json.JSONDecodeError as e:
                    print(f"[!] state.json 解析失败，将使用空状态: {e}")
                except OSError as e:
                    print(f"[!] state.json 读取失败: {e}")
        return self._state_cache

    def _save_state(self, state: dict) -> None:
        try:
            tmp = self._state_file.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(state, f, indent=4)
            tmp.replace(self._state_file)
        except OSError as e:
            print(f"[!] 状态保存失败 (不影响下载): {e}")

    # ── 公开接口 ──────────────────────────────────────

    @property
    def account_count(self) -> int:
        return len(self._config.get("accounts", []))

    @property
    def active_index(self) -> int:
        return self._load_state().get("active_account", 0)

    @property
    def active_email(self) -> str:
        accounts = self._config.get("accounts", [])
        idx = self.active_index
        if accounts and idx < len(accounts):
            return accounts[idx].get("email", "")
        return ""

    def get_active_cookies(self) -> dict | None:
        """从 state 获取当前活跃账号的 cookies"""
        state = self._load_state()
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

    def login(self, account_index: int = 0) -> dict | None:
        """登录指定账号，返回 cookie 字典"""
        accounts = self._config.get("accounts", [])
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
            "User-Agent": _UA,
            "Referer": f"{BASE_URL}/login.php",
            "Origin": BASE_URL,
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

        cookies = _extract_cookies(s)

        if not cookies["VLIBSID"]:
            try:
                s.get(f"{BASE_URL}/my.php", timeout=30)
            except requests.RequestException as e:
                print(f"[!] 登录后 cookie 刷新请求失败: {e}")
            cookies = _extract_cookies(s)

        if not cookies["VLIBSID"]:
            print("[!] 登录成功但未能获取 VLIBSID cookie")
            return None

        print(f"[+] 登录成功: VLIBSID={cookies['VLIBSID'][:20]}...")

        self._persist_login(account_index, cookies)
        return cookies

    def switch_account(self, reason: str) -> dict | None:
        """标记当前账号耗尽，切换到下一个可用账号"""
        accounts = self._config.get("accounts", [])
        if not accounts:
            return None

        state = self._load_state()
        current = state.get("active_account", 0)

        st_accs = state.setdefault("accounts", [])
        self._ensure_slot(st_accs, current)
        st_accs[current]["exhausted"] = True
        st_accs[current]["exhausted_reason"] = reason
        print(f"[*] 账号[{current}] 已标记为耗尽: {reason}")

        for i in range(1, len(accounts) + 1):
            next_idx = (current + i) % len(accounts)
            st_acc = st_accs[next_idx] if next_idx < len(st_accs) else {}
            if not st_acc.get("exhausted", False):
                print(f"[*] 切换到账号[{next_idx}]: {accounts[next_idx]['email']}")
                state["active_account"] = next_idx
                self._save_state(state)
                cookies = self.login(next_idx)
                if cookies:
                    return cookies
                self._ensure_slot(st_accs, next_idx)
                st_accs[next_idx]["exhausted"] = True
                st_accs[next_idx]["exhausted_reason"] = "login failed"

        self._save_state(state)
        print("[!] 没有可用的账号了")
        return None

    def reset_accounts(self) -> None:
        """重置所有账号的耗尽标记"""
        state = self._load_state()
        changed = False
        for acc in state.get("accounts", []):
            if acc.get("exhausted", False):
                acc["exhausted"] = False
                acc["exhausted_reason"] = None
                changed = True
        if changed:
            self._save_state(state)

    def sync_cookies(self, cookies: dict[str, str]) -> None:
        """将 cookie 变更同步回 state"""
        state = self._load_state()
        idx = state.get("active_account", 0)
        accs = state.setdefault("accounts", [])
        self._ensure_slot(accs, idx)

        changed = False
        for name, value in cookies.items():
            key = name.lower()
            if value and accs[idx].get(key) != value:
                accs[idx][key] = value
                changed = True
        if changed:
            self._save_state(state)

    # ── 内部辅助 ──────────────────────────────────────

    def _persist_login(self, account_index: int, cookies: dict) -> None:
        state = self._load_state()
        accs = state.setdefault("accounts", [])
        self._ensure_slot(accs, account_index)
        accs[account_index].update({
            "vlibsid": cookies["VLIBSID"],
            "volskey": cookies["VOLSKEY"],
            "volsess": cookies["VOLSESS"],
        })
        state["active_account"] = account_index
        self._save_state(state)

    @staticmethod
    def _ensure_slot(accs: list, idx: int) -> None:
        while len(accs) <= idx:
            accs.append({})


def _extract_cookies(session: requests.Session) -> dict[str, str]:
    """从 session 中提取认证 cookie"""
    vlibsid = volskey = volsess = ""
    for c in session.cookies:
        if c.name == "VLIBSID":
            vlibsid = c.value
        elif c.name == "VOLSKEY":
            volskey = c.value
        elif c.name == "VOLSESS":
            volsess = c.value
    return {"VLIBSID": vlibsid, "VOLSKEY": volskey or "", "VOLSESS": volsess or ""}
