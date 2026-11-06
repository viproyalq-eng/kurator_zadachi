from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from bot.logic import client_display_name, fmt_dt, is_bonus, message_text

KW = ["бонус"]


@pytest.mark.parametrize("text", ["бонус", "Бонус", "БОНУС", " бонус! ", "Бонус 🎁", "бонус."])
def test_bonus_triggers(text):
    assert is_bonus(text, KW)


@pytest.mark.parametrize("text", ["", None, "а де мій бонус?", "бонуси", "привіт", "бонус бонус"])
def test_bonus_does_not_trigger(text):
    assert not is_bonus(text, KW)


def test_bonus_several_keywords():
    assert is_bonus("Bonus", ["бонус", "bonus"])


def test_fmt_dt_uses_timezone():
    dt = datetime(2026, 11, 5, 8, 30, tzinfo=timezone.utc)
    assert fmt_dt(dt, ZoneInfo("Europe/Kyiv")) == "05.11.2026 10:30"


def test_display_name():
    assert client_display_name("Іван", "Петренко") == "Іван Петренко"
    assert client_display_name(None, None) == "Без імені"


def test_message_text_media():
    assert message_text(SimpleNamespace(text="привіт")) == "привіт"
    assert message_text(SimpleNamespace(text=None, caption="дивись", photo=[1])) == "[фото] дивись"
    assert message_text(SimpleNamespace(text=None, caption=None, voice=object())) == "[голосове]"
    assert message_text(SimpleNamespace(text=None, caption=None)) == "[інше]"
