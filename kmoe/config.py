"""配置加载与环境变量支持"""

import json
import os
from pathlib import Path

BASE_URL = "https://koz.moe"
DEFAULT_TIMEOUT = 15
DEFAULT_DELAY = 1.0
DEFAULT_OUTPUT = "~/Downloads"

CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.json"
STATE_FILE = Path(__file__).resolve().parent.parent / "state.json"


def load_config() -> dict:
    """加载配置文件，支持环境变量补充账号"""
    cfg = _load_config_file()
    _migrate_old_format(cfg)
    _inject_env_account(cfg)
    return cfg


def _load_config_file() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        print(f"[!] 配置文件 JSON 语法错误: {e}")
        return {}
    except OSError as e:
        print(f"[!] 无法读取配置文件: {e}")
        return {}
    else:
        print(f"[*] 已加载配置: {CONFIG_FILE}")
        return cfg


def _migrate_old_format(cfg: dict) -> None:
    """兼容旧格式：顶层 email/passwd → accounts[0]"""
    if "accounts" not in cfg:
        email = cfg.get("email", "")
        passwd = cfg.get("passwd", "")
        if email or passwd:
            cfg["accounts"] = [{"email": email, "passwd": passwd}]
            cfg.pop("email", None)
            cfg.pop("passwd", None)
            print("[*] 已自动识别旧格式配置")


def _inject_env_account(cfg: dict) -> None:
    """从环境变量 KMOE_EMAIL / KMOE_PASSWORD 补充账号"""
    env_email = os.environ.get("KMOE_EMAIL", "")
    env_passwd = os.environ.get("KMOE_PASSWORD", "")
    if not env_email or not env_passwd:
        return

    accounts = cfg.setdefault("accounts", [])
    existing = {a["email"] for a in accounts if "email" in a}
    if env_email not in existing:
        accounts.append({"email": env_email, "passwd": env_passwd})
        print(f"[*] 已从环境变量添加账号: {env_email}")
