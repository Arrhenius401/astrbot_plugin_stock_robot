"""通过官方会话模型接口生成自然询问，并主动发送到原会话。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from .pending_queue import PendingQueue


class SessionEvent:
    """只提供投递所需接口，通过 UMO 重建路由，不伪造入站消息事件。"""

    def __init__(self, context: Any, umo: str):
        self.context = context
        self.unified_msg_origin = umo

    def plain_result(self, text):
        from astrbot.api.event import MessageChain

        return MessageChain().message(text)

    def chain_result(self, chain):
        from astrbot.api.event import MessageChain

        return MessageChain(chain=chain)

    async def send(self, chain):
        sent = await self.context.send_message(self.unified_msg_origin, chain)
        if sent is not True:
            raise OSError("主动消息未找到可用平台")


async def generate_text(context: Any, umo: str, facts: dict) -> str:
    """只传入任务事实，不携带工具、会话历史或旧事件对象。"""
    provider_id = await context.get_current_chat_provider_id(umo=umo)
    response = await context.llm_generate(
        chat_provider_id=provider_id,
        prompt=json.dumps(facts, ensure_ascii=False),
        tools=None,
        contexts=[],
        system_prompt=("你负责为股票分析助手生成一条简短、自然的中文消息。"
                       "严格依据输入事实，不调用工具，不声称已分析或已投递尚未完成的报告。"
                       "询问时列出全部标的并问是否仍需要分析；末次提醒须说明已取消积压任务，"
                       "如仍需要可以重新提出。只输出发给用户的正文。"),
    )
    text = getattr(response, "completion_text", "")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("会话模型未生成有效文本")
    return text.strip()


async def send_text(context: Any, umo: str, text: str) -> None:
    from astrbot.api.event import MessageChain

    sent = await context.send_message(umo, MessageChain().message(text))
    if sent is not True:
        raise OSError("未找到可主动发送消息的会话平台")


async def notify_waiting(queue: PendingQueue, context: Any, *, now: float | None = None,
                         allow_questions: bool = True) -> None:
    """到期先移除确认资格；恢复后的询问成功送达才启动 30 分钟等待。"""
    failures = []
    for record in queue.expire(now=now):
        try:
            async with asyncio.timeout(30):
                text = await generate_text(context, record["umo"], {
                    "event": "final_reminder_cancelled", "targets": record["targets"],
                })
                await send_text(context, record["umo"], text)
        except Exception as error:  # noqa: BLE001 — 各平台独立失败不能丢弃其他会话提醒
            failures.append(error)
    if not allow_questions:
        if failures:
            raise failures[0]
        return
    for record in queue.all():
        if record["state"] != "waiting_service":
            continue
        instant = time.time() if now is None else now
        if instant < record.get("notification_retry_at", 0):
            continue
        try:
            async with asyncio.timeout(30):
                text = record["question"] or await generate_text(context, record["umo"], {
                    "event": "service_ready_ask_confirmation", "targets": record["targets"],
                })
                # 生成文案期间可能已有新请求撤销旧队列。
                if not queue.cache_question(record["umo"], record["id"], text):
                    continue
                await send_text(context, record["umo"], text)
            queue.asked(record["umo"], record["id"], text, now=now)
        except Exception as error:  # noqa: BLE001 — 每会话独立记录模型／平台失败
            queue.notification_failed(record["umo"], record["id"], now=instant)
            failures.append(error)
    if failures:
        raise failures[0]


async def notification_loop(queue: PendingQueue, context: Any, ready, logger,
                            *, worker_lock=None, recover=None) -> None:
    """轮询仅观察服务状态，不因等待任务触发安装或分析。"""
    while True:
        owned = worker_lock is None or worker_lock.file is not None or worker_lock.acquire()
        if owned:
            try:
                await notify_waiting(queue, context, allow_questions=ready())
            except Exception as error:  # noqa: BLE001 — 模型和平台 SDK 隔离边界
                logger.warning("等待队列询问未完成：%s", type(error).__name__)
            if recover is not None and ready():
                try:
                    recover()
                except Exception as error:  # noqa: BLE001 — 恢复失败不终止后续接管
                    logger.warning("积压任务恢复尚未完成：%s", type(error).__name__)
        await asyncio.sleep(1)
