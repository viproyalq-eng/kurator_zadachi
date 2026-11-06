import json

import pytest

from bot.config import ConfigError, load_config

CREDS = json.dumps({"client_email": "bot@x.iam.gserviceaccount.com", "private_key": "k"})


def test_missing_token():
    with pytest.raises(ConfigError):
        load_config({})


def test_full_config():
    cfg = load_config({
        "BOT_TOKEN": "1:A", "ADMIN_CHAT_ID": "123", "GOOGLE_SHEET_ID": "sid",
        "GOOGLE_CREDENTIALS_JSON": CREDS, "DB_PATH": "/data/bot.db",
        "WELCOME_TEXT": "Привіт!\\nЛаскаво просимо", "TASKS_CHAT_ID": "-1001234567890",
    })
    assert cfg.tasks_chat_id == -1001234567890
    assert cfg.admin_chat_ids == [123]
    assert cfg.sheets_enabled
    assert cfg.db_path == "/data/bot.db"
    assert cfg.welcome_text == "Привіт!\nЛаскаво просимо"
    assert cfg.warnings == []


def test_bad_json_disables_sheets():
    cfg = load_config({"BOT_TOKEN": "1:A", "GOOGLE_SHEET_ID": "sid", "GOOGLE_CREDENTIALS_JSON": "{not json"})
    assert not cfg.sheets_enabled
    assert any("JSON" in w for w in cfg.warnings)


def test_bad_admin_id_is_warning():
    cfg = load_config({"BOT_TOKEN": "1:A", "ADMIN_CHAT_ID": "abc"})
    assert cfg.admin_chat_ids == []
    assert any("ADMIN_CHAT_ID" in w for w in cfg.warnings)


def test_default_welcome_is_viktor_text():
    cfg = load_config({"BOT_TOKEN": "1:A"})
    assert cfg.welcome_text.startswith("Приветствую👋")
    assert "Меня зовут Виктор" in cfg.welcome_text
    assert cfg.welcome_text.endswith("голосовые😉")
    assert "\n\n📚" in cfg.welcome_text


def test_several_admins():
    cfg = load_config({"BOT_TOKEN": "1:A", "ADMIN_CHAT_ID": "111, 222,333 ;111, abc"})
    assert cfg.admin_chat_ids == [111, 222, 333]
    assert any("abc" in w for w in cfg.warnings)


def test_task_defaults():
    from datetime import time
    cfg = load_config({"BOT_TOKEN": "1:A"})
    assert cfg.tasks_time == time(7, 0)
    assert cfg.tasks_per_day == 20 and cfg.tasks_min_days == 10
    assert cfg.team_usernames == {"vladbazil", "antoniohoshko", "assistant_vip_club"}
    assert cfg.timezone.key == "Europe/Kyiv"
    assert any("TASKS_CHAT_ID" in w for w in cfg.warnings)


def test_task_overrides():
    cfg = load_config({"BOT_TOKEN": "1:A", "TASKS_TIME": "08:30", "TASKS_PER_DAY": "15",
                       "TASKS_MIN_DAYS": "abc", "EXCLUDED_USERNAMES": "@One, two"})
    assert cfg.tasks_time.hour == 8 and cfg.tasks_time.minute == 30
    assert cfg.tasks_per_day == 15 and cfg.tasks_min_days == 10
    assert cfg.team_usernames == {"one", "two"}
