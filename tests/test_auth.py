"""Tests for kmoe.auth — AccountManager state management, login, and rotation."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from kmoe.auth import AccountManager


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


@pytest.fixture
def mgr(state_file: Path) -> AccountManager:
    cfg = {
        "accounts": [
            {"email": "a@test.com", "passwd": "pw1"},
            {"email": "b@test.com", "passwd": "pw2"},
            {"email": "c@test.com", "passwd": "pw3"},
        ]
    }
    return AccountManager(cfg, state_file=state_file)


class TestStateIO:
    """Test state file read/write operations."""

    def test_load_state_from_disk(self, state_file: Path):
        state_file.write_text(json.dumps({"active_account": 1}))
        m = AccountManager({}, state_file=state_file)
        assert m._load_state()["active_account"] == 1

    def test_load_state_missing_file(self, state_file: Path):
        m = AccountManager({}, state_file=state_file)
        assert m._load_state() == {}

    def test_load_state_invalid_json(self, state_file: Path):
        state_file.write_text("not json")
        m = AccountManager({}, state_file=state_file)
        assert m._load_state() == {}

    def test_save_state_atomic_write(self, state_file: Path):
        m = AccountManager({}, state_file=state_file)
        m._save_state({"active_account": 2})
        # Original file should exist (not .tmp)
        assert state_file.exists()
        assert json.loads(state_file.read_text())["active_account"] == 2
        # No leftover .tmp
        assert not state_file.with_suffix(".tmp").exists()

    def test_load_state_caches_in_memory(self, state_file: Path):
        state_file.write_text(json.dumps({"active_account": 0}))
        m = AccountManager({}, state_file=state_file)
        m._load_state()
        # Delete file — cached value should still work
        state_file.unlink()
        assert m._load_state()["active_account"] == 0


class TestProperties:
    """Test computed properties."""

    def test_account_count(self, mgr: AccountManager):
        assert mgr.account_count == 3

    def test_account_count_empty(self, state_file: Path):
        m = AccountManager({}, state_file=state_file)
        assert m.account_count == 0

    def test_active_index_default(self, mgr: AccountManager):
        assert mgr.active_index == 0

    def test_active_index_from_state(self, state_file: Path):
        state_file.write_text(json.dumps({"active_account": 2}))
        m = AccountManager({}, state_file=state_file)
        assert m.active_index == 2

    def test_active_email(self, mgr: AccountManager):
        assert mgr.active_email == "a@test.com"

    def test_active_email_empty_config(self, state_file: Path):
        m = AccountManager({}, state_file=state_file)
        assert m.active_email == ""


class TestGetActiveCookies:
    """Test cookie retrieval from state."""

    def test_returns_cookies_when_present(self, state_file: Path):
        state_file.write_text(json.dumps({
            "accounts": [{"vlibsid": "vs1", "volskey": "vk1", "volsess": "ve1"}],
            "active_account": 0,
        }))
        m = AccountManager({}, state_file=state_file)
        cookies = m.get_active_cookies()
        assert cookies == {"VLIBSID": "vs1", "VOLSKEY": "vk1", "VOLSESS": "ve1"}

    def test_returns_none_when_missing(self, state_file: Path):
        state_file.write_text(json.dumps({
            "accounts": [{"vlibsid": "vs1"}],
            "active_account": 0,
        }))
        m = AccountManager({}, state_file=state_file)
        assert m.get_active_cookies() is None

    def test_returns_none_when_empty(self, state_file: Path):
        m = AccountManager({}, state_file=state_file)
        assert m.get_active_cookies() is None

    def test_returns_none_when_index_out_of_range(self, state_file: Path):
        state_file.write_text(json.dumps({
            "accounts": [{"vlibsid": "vs1", "volskey": "vk1", "volsess": "ve1"}],
            "active_account": 5,
        }))
        m = AccountManager({}, state_file=state_file)
        assert m.get_active_cookies() is None


class TestSyncCookies:
    """Test cookie synchronization back to state."""

    def test_syncs_new_cookies(self, state_file: Path):
        state_file.write_text(json.dumps({"active_account": 0}))
        m = AccountManager({}, state_file=state_file)
        m.sync_cookies({"VLIBSID": "new_vs", "VOLSKEY": "new_vk", "VOLSESS": "new_ve"})
        saved = json.loads(state_file.read_text())
        assert saved["accounts"][0]["vlibsid"] == "new_vs"

    def test_skips_empty_cookies(self, state_file: Path):
        state_file.write_text(json.dumps({"active_account": 0}))
        m = AccountManager({}, state_file=state_file)
        m.sync_cookies({"VLIBSID": "vs1", "VOLSKEY": "", "VOLSESS": ""})
        saved = json.loads(state_file.read_text())
        assert saved["accounts"][0]["vlibsid"] == "vs1"
        assert "volskey" not in saved["accounts"][0]

    def test_no_write_when_unchanged(self, state_file: Path):
        state_file.write_text(json.dumps({
            "accounts": [{"vlibsid": "vs1", "volskey": "vk1", "volsess": "ve1"}],
            "active_account": 0,
        }))
        m = AccountManager({}, state_file=state_file)
        m._load_state()  # populate cache
        mtime_before = state_file.stat().st_mtime
        m.sync_cookies({"VLIBSID": "vs1", "VOLSKEY": "vk1", "VOLSESS": "ve1"})
        mtime_after = state_file.stat().st_mtime
        assert mtime_before == mtime_after


class TestResetAccounts:
    """Test exhausted flag reset."""

    def test_clears_exhausted_flags(self, state_file: Path):
        state_file.write_text(json.dumps({
            "accounts": [
                {"exhausted": True, "exhausted_reason": "quota"},
                {"exhausted": False},
                {"exhausted": True, "exhausted_reason": "login failed"},
            ],
            "active_account": 0,
        }))
        m = AccountManager({}, state_file=state_file)
        m.reset_accounts()
        saved = json.loads(state_file.read_text())
        for acc in saved["accounts"]:
            assert acc.get("exhausted") is not True
            assert acc.get("exhausted_reason") is None or acc.get("exhausted_reason") is None

    def test_noop_when_no_exhausted(self, state_file: Path):
        state_file.write_text(json.dumps({"accounts": [{"vlibsid": "vs1"}]}))
        m = AccountManager({}, state_file=state_file)
        m.reset_accounts()
        # Should not create .tmp or modify file
        assert not (state_file.with_suffix(".tmp")).exists()


class TestSwitchAccount:
    """Test account rotation logic."""

    def test_switches_to_next_available(self, state_file: Path):
        cfg = {"accounts": [
            {"email": "a@t.com", "passwd": "pw1"},
            {"email": "b@t.com", "passwd": "pw2"},
        ]}
        state_file.write_text(json.dumps({
            "accounts": [{}, {}],
            "active_account": 0,
        }))
        m = AccountManager(cfg, state_file=state_file)
        mock_cookies = {"VLIBSID": "vs_b", "VOLSKEY": "vk_b", "VOLSESS": "ve_b"}

        with patch.object(m, "login", return_value=mock_cookies) as mock_login:
            result = m.switch_account("quota exceeded")

        assert result == mock_cookies
        mock_login.assert_called_once_with(1)
        state = json.loads(state_file.read_text())
        assert state["accounts"][0]["exhausted"] is True
        assert state["active_account"] == 1

    def test_skips_already_exhausted(self, state_file: Path):
        cfg = {"accounts": [
            {"email": "a@t.com", "passwd": "pw1"},
            {"email": "b@t.com", "passwd": "pw2"},
            {"email": "c@t.com", "passwd": "pw3"},
        ]}
        state_file.write_text(json.dumps({
            "accounts": [{}, {"exhausted": True}, {}],
            "active_account": 0,
        }))
        m = AccountManager(cfg, state_file=state_file)

        with patch.object(m, "login", return_value={"VLIBSID": "x", "VOLSKEY": "y", "VOLSESS": "z"}) as mock_login:
            result = m.switch_account("reason")

        # Should skip index 1 (exhausted), try index 2
        mock_login.assert_called_once_with(2)
        assert result is not None

    def test_returns_none_when_all_exhausted(self, state_file: Path):
        cfg = {"accounts": [
            {"email": "a@t.com", "passwd": "pw1"},
            {"email": "b@t.com", "passwd": "pw2"},
        ]}
        state_file.write_text(json.dumps({
            "accounts": [{}, {}],
            "active_account": 0,
        }))
        m = AccountManager(cfg, state_file=state_file)

        with patch.object(m, "login", return_value=None):
            result = m.switch_account("reason")

        assert result is None
        # Both accounts should be marked exhausted
        state = json.loads(state_file.read_text())
        assert state["accounts"][0]["exhausted"] is True
        assert state["accounts"][1]["exhausted"] is True

    def test_returns_none_with_empty_config(self, state_file: Path):
        m = AccountManager({}, state_file=state_file)
        assert m.switch_account("reason") is None


class TestLogin:
    """Test login with mocked HTTP."""

    def test_login_success(self, state_file: Path):
        cfg = {"accounts": [{"email": "user@t.com", "passwd": "pw"}]}
        m = AccountManager(cfg, state_file=state_file)

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        c1, c2, c3 = MagicMock(), MagicMock(), MagicMock()
        c1.name, c1.value = "VLIBSID", "vs_123"
        c2.name, c2.value = "VOLSKEY", "vk_456"
        c3.name, c3.value = "VOLSESS", "ve_789"

        mock_session = MagicMock()
        mock_session.post.return_value = mock_resp
        mock_session.cookies = [c1, c2, c3]

        with patch("kmoe.auth.requests.Session", return_value=mock_session):
            cookies = m.login(0)

        assert cookies is not None
        assert cookies["VLIBSID"] == "vs_123"
        # State should be persisted
        state = json.loads(state_file.read_text())
        assert state["accounts"][0]["vlibsid"] == "vs_123"
        assert state["active_account"] == 0

    def test_login_failure_no_vlibsid(self, state_file: Path):
        cfg = {"accounts": [{"email": "user@t.com", "passwd": "wrong"}]}
        m = AccountManager(cfg, state_file=state_file)

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_session = MagicMock()
        mock_session.post.return_value = mock_resp
        # First call (login_do.php) — no VLIBSID
        mock_session.cookies = []
        # Second call (my.php) — still no VLIBSID
        mock_session.get.return_value = MagicMock()

        with patch("kmoe.auth.requests.Session", return_value=mock_session):
            cookies = m.login(0)

        assert cookies is None

    def test_login_out_of_range(self, state_file: Path):
        m = AccountManager({"accounts": [{"email": "a", "passwd": "b"}]}, state_file=state_file)
        assert m.login(5) is None

    def test_login_missing_credentials(self, state_file: Path):
        m = AccountManager({"accounts": [{"email": "", "passwd": ""}]}, state_file=state_file)
        assert m.login(0) is None

    def test_login_network_error(self, state_file: Path):
        cfg = {"accounts": [{"email": "a@t.com", "passwd": "pw"}]}
        m = AccountManager(cfg, state_file=state_file)

        mock_session = MagicMock()
        mock_session.post.side_effect = requests.ConnectionError("timeout")

        with patch("kmoe.auth.requests.Session", return_value=mock_session):
            cookies = m.login(0)

        assert cookies is None

    def test_login_retries_with_my_php(self, state_file: Path):
        """Login should try /my.php if first response lacks VLIBSID."""
        cfg = {"accounts": [{"email": "a@t.com", "passwd": "pw"}]}
        m = AccountManager(cfg, state_file=state_file)

        c1 = MagicMock()
        c1.name, c1.value = "VLIBSID", "vs_retry"

        mock_session = MagicMock()
        mock_session.post.return_value = MagicMock(status_code=200)
        mock_session.cookies = [c1]

        with patch("kmoe.auth.requests.Session", return_value=mock_session):
            cookies = m.login(0)

        assert cookies is not None
        assert cookies["VLIBSID"] == "vs_retry"
