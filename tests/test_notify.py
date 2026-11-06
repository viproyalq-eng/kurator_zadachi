from bot.notify import Notifier


async def test_cooldown():
    sent, now = [], [0.0]

    async def send(chat_id, text):
        sent.append(text)

    n = Notifier(1, send, clock=lambda: now[0])
    assert await n.notify("a", key="k", cooldown=60)
    assert not await n.notify("b", key="k", cooldown=60)
    now[0] = 61
    assert await n.notify("c", key="k", cooldown=60)
    assert sent == ["a", "c"]


async def test_no_admin_does_not_crash():
    assert not await Notifier(None, None).notify("x")


async def test_send_error_swallowed():
    async def boom(chat_id, text):
        raise RuntimeError("down")

    assert not await Notifier(1, boom).notify("x")


async def test_sends_to_all_admins_even_if_one_fails():
    got = []

    async def send(chat_id, text):
        if chat_id == 2:
            raise RuntimeError("chat not found")
        got.append(chat_id)

    assert await Notifier([1, 2, 3], send).notify("x")
    assert got == [1, 3]
