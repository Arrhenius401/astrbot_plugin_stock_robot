"""恢复询问的模型和主动消息边界；不把框架替身当作真实平台验收。"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest
from stock_robot_plugin_test.pending_queue import PendingQueue
from stock_robot_plugin_test.waiting_messages import notify_waiting


@pytest.fixture
def messaging(monkeypatch):
    module = ModuleType("astrbot.api.event")

    class Chain:
        def message(self, text):
            self.text = text
            return self

    module.MessageChain = Chain
    monkeypatch.setitem(sys.modules, module.__name__, module)
    messages = []
    calls = []

    async def provider(**kwargs):
        calls.append(kwargs)
        return "session-provider"

    async def generate(**kwargs):
        assert kwargs["tools"] is None
        assert kwargs["contexts"] == []
        assert kwargs["chat_provider_id"] == "session-provider"
        return SimpleNamespace(completion_text="腾讯的分析现在可以继续，还需要吗？")

    async def send(umo, chain):
        messages.append((umo, chain.text))
        return True

    context = SimpleNamespace(get_current_chat_provider_id=provider,
                              llm_generate=generate, send_message=send)
    return context, messages, calls


@pytest.mark.asyncio
async def test_three_targets_generate_one_question_and_no_repeated_question(tmp_path, messaging):
    context, messages, calls = messaging
    queue = PendingQueue(tmp_path / "queue.db")
    for symbol in ("00700", "600519", "000001"):
        queue.enqueue("group", "stock", symbol)
    await notify_waiting(queue, context, now=10)
    await notify_waiting(queue, context, now=11)
    assert len(messages) == 1
    assert calls == [{"umo": "group"}]
    assert queue.get("group")["deadline"] == 1810


@pytest.mark.asyncio
async def test_last_reminder_at_thirty_minutes_clears_queue_before_sending(tmp_path, messaging):
    context, messages, _ = messaging
    queue = PendingQueue(tmp_path / "queue.db")
    queue.enqueue("group", "stock", "00700")
    await notify_waiting(queue, context, now=10)
    await notify_waiting(queue, context, now=1810)
    await notify_waiting(queue, context, now=1811)
    assert len(messages) == 2
    assert queue.get("group") is None


@pytest.mark.asyncio
async def test_failed_ai_question_never_starts_confirmation_deadline(tmp_path, messaging):
    context, messages, _ = messaging

    async def failed(**kwargs):
        raise TimeoutError("model")

    context.llm_generate = failed
    queue = PendingQueue(tmp_path / "queue.db")
    queue.enqueue("group", "stock", "00700")
    with pytest.raises(TimeoutError):
        await notify_waiting(queue, context, now=10)
    assert not messages
    assert queue.get("group")["deadline"] is None


@pytest.mark.asyncio
async def test_missing_platform_does_not_mark_question_sent(tmp_path, messaging):
    context, _, _ = messaging

    async def missing(*args):
        return False

    context.send_message = missing
    queue = PendingQueue(tmp_path / "queue.db")
    queue.enqueue("group", "stock", "00700")
    with pytest.raises(OSError, match="平台"):
        await notify_waiting(queue, context, now=10)
    assert queue.get("group")["state"] == "waiting_service"


@pytest.mark.asyncio
async def test_new_request_during_generation_invalidates_old_question(tmp_path, messaging):
    context, messages, _ = messaging
    queue = PendingQueue(tmp_path / "queue.db")
    queue.enqueue("group", "stock", "00700")

    async def generate(**kwargs):
        queue.discard("group")
        return SimpleNamespace(completion_text="旧询问")

    context.llm_generate = generate
    await notify_waiting(queue, context, now=10)
    assert not messages


@pytest.mark.asyncio
async def test_one_failed_final_reminder_does_not_drop_other_sessions(tmp_path, messaging):
    context, messages, _ = messaging
    queue = PendingQueue(tmp_path / "queue.db")
    for umo in ("first", "second"):
        queue.enqueue(umo, "stock", "00700")
        record = queue.get(umo)
        queue.asked(umo, record["id"], "继续吗？", now=10)
    send = context.send_message

    async def partly_failed(umo, chain):
        if umo == "first":
            raise OSError("platform")
        return await send(umo, chain)

    context.send_message = partly_failed
    with pytest.raises(OSError):
        await notify_waiting(queue, context, now=1810)
    assert [umo for umo, _ in messages] == ["second"]
    assert queue.all() == []
