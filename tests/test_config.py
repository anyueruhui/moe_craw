"""Tests for kmoe.config — configuration loading and environment variable support."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kmoe.config import (
    BASE_URL,
    DEFAULT_DELAY,
    DEFAULT_OUTPUT,
    DEFAULT_TIMEOUT,
    _inject_env_account,
    _migrate_old_format,
    load_config,
)


class TestConstants:
    """Verify module constants are defined correctly."""

    def test_base_url(self):
        assert BASE_URL == "https://koz.moe"

    def test_default_timeout(self):
        assert DEFAULT_TIMEOUT == 15

    def test_default_delay(self):
        assert DEFAULT_DELAY == 1.0

    def test_default_output(self):
        assert DEFAULT_OUTPUT == "~/Downloads"


class TestLoadConfig:
    """Test load_config end-to-end with file I/O."""

    def test_loads_valid_config(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "accounts": [{"email": "a@b.com", "passwd": "123"}],
            "type": "epub",
            "delay": 0.5,
        }))
        with patch("kmoe.config.CONFIG_FILE", cfg_file):
            cfg = load_config()
        assert cfg["accounts"][0]["email"] == "a@b.com"
        assert cfg["type"] == "epub"
        assert cfg["delay"] == 0.5

    def test_returns_empty_when_file_missing(self, tmp_path):
        missing = tmp_path / "nonexistent.json"
        with patch("kmoe.config.CONFIG_FILE", missing):
            cfg = load_config()
        assert cfg == {}

    def test_returns_empty_on_invalid_json(self, tmp_path):
        bad = tmp_path / "config.json"
        bad.write_text("{invalid json")
        with patch("kmoe.config.CONFIG_FILE", bad):
            cfg = load_config()
        assert cfg == {}

    def test_returns_empty_on_empty_file(self, tmp_path):
        empty = tmp_path / "config.json"
        empty.write_text("")
        with patch("kmoe.config.CONFIG_FILE", empty):
            cfg = load_config()
        assert cfg == {}


class TestMigrateOldFormat:
    """Test backward compatibility with old top-level email/passwd format."""

    def test_migrates_top_level_credentials(self):
        cfg = {"email": "user@test.com", "passwd": "secret", "delay": 1.0}
        _migrate_old_format(cfg)
        assert "accounts" in cfg
        assert cfg["accounts"] == [{"email": "user@test.com", "passwd": "secret"}]
        assert "email" not in cfg
        assert "passwd" not in cfg
        # Non-credential fields preserved
        assert cfg["delay"] == 1.0

    def test_no_migration_when_accounts_exist(self):
        cfg = {"accounts": [{"email": "existing@test.com", "passwd": "pw"}]}
        _migrate_old_format(cfg)
        assert len(cfg["accounts"]) == 1
        assert cfg["accounts"][0]["email"] == "existing@test.com"

    def test_no_migration_when_no_credentials(self):
        cfg = {"delay": 2.0}
        _migrate_old_format(cfg)
        assert "accounts" not in cfg

    def test_migrates_partial_credentials(self):
        cfg = {"email": "only_email@test.com", "passwd": ""}
        _migrate_old_format(cfg)
        assert cfg["accounts"] == [{"email": "only_email@test.com", "passwd": ""}]


class TestInjectEnvAccount:
    """Test environment variable account injection."""

    def test_injects_env_account(self):
        cfg: dict = {}
        with patch.dict(os.environ, {"KMOE_EMAIL": "env@test.com", "KMOE_PASSWORD": "env_pw"}):
            _inject_env_account(cfg)
        assert len(cfg["accounts"]) == 1
        assert cfg["accounts"][0]["email"] == "env@test.com"
        assert cfg["accounts"][0]["passwd"] == "env_pw"

    def test_skips_when_email_missing(self):
        cfg: dict = {}
        with patch.dict(os.environ, {"KMOE_PASSWORD": "pw"}, clear=True):
            _inject_env_account(cfg)
        assert "accounts" not in cfg

    def test_skips_when_password_missing(self):
        cfg: dict = {}
        with patch.dict(os.environ, {"KMOE_EMAIL": "e@t.com"}, clear=True):
            _inject_env_account(cfg)
        assert "accounts" not in cfg

    def test_deduplicates_existing_account(self):
        cfg = {"accounts": [{"email": "env@test.com", "passwd": "old_pw"}]}
        with patch.dict(os.environ, {"KMOE_EMAIL": "env@test.com", "KMOE_PASSWORD": "new_pw"}):
            _inject_env_account(cfg)
        # Should NOT add duplicate
        assert len(cfg["accounts"]) == 1
        # Original unchanged (dedup checks email, doesn't overwrite)
        assert cfg["accounts"][0]["passwd"] == "old_pw"

    def test_appends_to_existing_accounts(self):
        cfg = {"accounts": [{"email": "file@test.com", "passwd": "pw1"}]}
        with patch.dict(os.environ, {"KMOE_EMAIL": "env@test.com", "KMOE_PASSWORD": "pw2"}):
            _inject_env_account(cfg)
        assert len(cfg["accounts"]) == 2
        assert cfg["accounts"][1]["email"] == "env@test.com"

    def test_no_env_vars_set(self):
        cfg: dict = {}
        with patch.dict(os.environ, {}, clear=True):
            _inject_env_account(cfg)
        assert "accounts" not in cfg
