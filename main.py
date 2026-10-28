"""AstrBot 插件：把 stock_robot 的个股/指数分析封装为 LLM 工具，报告以图片发送。"""

import asyncio
import inspect
import json
import math
import time
from copy import copy
from functools import partial
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_plugin_data_path,
)

from .bootstrap import (
    INSTALL_TIMEOUT_SECONDS,
    InstallProgress,
    run_command,
)
from .client import (
    DEFAULT_TIMEOUT_SECONDS,
    ReportKind,
    StockRobotClient,
    progress_message,
    public_error_detail,
    resolve_effective_timeout,
)
from .disk_support import FileLock
from .font_support import ensure_chinese_font
from .launcher import ServiceLauncher
from .pending_queue import PendingQueue, canonical_symbol
from .plugin_settings import migrate_port, resolve_legacy_extras
from .report_delivery import ReportDelivery
from .report_renderer import ReportRenderer
from .report_storage import ReportStorage
from .waiting_messages import SessionEvent, generate_text, notification_loop

# 每个插件版本只自动安装已完成兼容验收的主仓正式版，不追随 latest。
MANAGED_RELEASE_TAG = "v0.2.2"


async def _render_report(markdown: str, *, return_url: bool = False) -> str:
    """使用 AstrBot 本地 Markdown 渲染，避免远程 T2I 对长报告返回非图片。"""
    from astrbot.core import html_renderer

    return await html_renderer.render_t2i(
        markdown,
        use_network=False,
        return_url=return_url,
        template_name=None,
    )


async def read_llm_config(context: Context) -> dict | None:
    """只在首次配置时复制默认模型，不随聊天会话同步。"""
    try:
        provider = await context.get_using_provider_async(umo=None)
        if provider is None:
            return None
        config = provider.provider_config
        provider_type = config.get("type")
        if not isinstance(provider_type, str):
            return None
        kind = {
            "openai_chat_completion": "openai",
            "anthropic_chat_completion": "claude",
        }.get(provider_type)
        if kind is None or config.get("api_version"):
            logger.warning("默认模型类型无法复制，请在分析服务 Web UI 配置模型")
            return None
        keys = provider.get_keys()
        key = provider.get_current_key()
        model = provider.get_model()
        if inspect.isawaitable(key):
            key = await key
        if inspect.isawaitable(model):
            model = await model
        if (
            not isinstance(keys, (list, tuple))
            or not keys
            or not isinstance(key, str)
            or not key.strip()
            or not isinstance(model, str)
            or not model.strip()
        ):
            logger.warning("默认模型的 Key 或模型名为空，服务暂以关闭 LLM 的配置运行")
            return None
        client = getattr(provider, "client", None)
        base_url = (
            getattr(provider, "base_url", None)
            if kind == "claude"
            else getattr(client, "base_url", None)
        )
        return {
            "enabled": True,
            "provider": kind,
            "api_key": key,
            "model": model,
            "base_url": str(base_url or config.get("api_base") or ""),
        }
    except Exception as exc:  # noqa: BLE001 — AstrBot 模型接口隔离，失败允许无模型启动
        logger.warning(
            "读取默认模型失败（%s），请在分析服务 Web UI 配置模型", type(exc).__name__
        )
        return None


class StockRobotPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_dir = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_stock_robot"
        self._extras = resolve_legacy_extras(config, data_dir / "instance")
        base_url = f"http://127.0.0.1:{migrate_port(config, data_dir)}"
        self._base_url = base_url
        self._storage = ReportStorage(data_dir)
        self._delivery = ReportDelivery(
            data_dir / "delivery_tmp",
            ReportRenderer(_render_report),
            storage=self._storage,
            send_message=getattr(context, "send_message", None),
        )
        self._cleanup_uncertain = False
        self._storage.cleanup_raw = bool(config.get("cleanup_previous_reports", True))
        self._storage.busy = lambda: self._delivery._reserved > 0
        self._storage.can_clean_raw = lambda: (
            not self._cleanup_uncertain
            and self._launcher is not None
            and self._launcher.owns_ready_service()
        )
        self._analysis_locks: dict[tuple[str, str], tuple[asyncio.Lock, int]] = {}
        self._analysis_tasks: set[asyncio.Task] = set()
        self._stopping = False
        self._timeout_seconds = int(
            self.config.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS
        )
        self._launcher: ServiceLauncher | None = None
        self._startup_timeout = float(self.config.get("startup_timeout_seconds", 60))
        if not math.isfinite(self._startup_timeout) or self._startup_timeout <= 0:
            raise ValueError("startup_timeout_seconds 必须大于 0")
        self._install_timeout = float(
            self.config.get("install_timeout_seconds", INSTALL_TIMEOUT_SECONDS)
        )
        if not math.isfinite(self._install_timeout) or self._install_timeout <= 0:
            raise ValueError("install_timeout_seconds 必须为大于 0 的有限秒数")
        self._client = StockRobotClient(base_url, self._timeout_seconds)
        self._pending = PendingQueue(data_dir / "waiting.sqlite3")
        self._waiting_lock = FileLock(data_dir / "waiting-worker.lock")
        self._waiting_task: asyncio.Task | None = None
        self._batch_tasks: dict[str, asyncio.Task] = {}
        self._data_dir = data_dir

    async def initialize(self):
        """准备任务由 launcher 持有，插件加载立即返回。"""
        data_dir = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_stock_robot"
        try:
            await asyncio.to_thread(ensure_chinese_font, Path(get_astrbot_data_path()))
        except Exception as error:  # noqa: BLE001 — 路径/线程准备失败也不阻止插件加载
            logger.warning(
                "无法准备 AstrBot 中文字体: %s: %s", type(error).__name__, error
            )
        progress = InstallProgress(data_dir / "service.log")
        self._launcher = ServiceLauncher(
            base_url=self._base_url,
            archive_url=None,
            extras=self._extras,
            auto_install=True,
            timeout=self._startup_timeout,
            data_dir=data_dir,
            llm=None,
            runner=partial(
                run_command,
                log_path=data_dir / "service.log",
                timeout=self._install_timeout,
                progress=progress,
            ),
            install_timeout=self._install_timeout,
            package_index_url=str(self.config.get("package_index_url") or "").strip(),
            progress=progress,
            llm_loader=partial(read_llm_config, self.context),
            managed_release_tag=MANAGED_RELEASE_TAG,
        )
        self._launcher.start_background()
        await self._storage.start()
        self._waiting_task = asyncio.create_task(notification_loop(
            self._pending, self.context,
            lambda: self._launcher is not None and getattr(self._launcher, "state", "idle") == "ready",
            logger, worker_lock=self._waiting_lock, recover=self._recover_batches,
        ))

    async def terminate(self):
        """先回收任务和自建服务，HTTP 客户端始终关闭。"""
        self._stopping = True
        if self._waiting_task is not None:
            self._waiting_task.cancel()
            await asyncio.gather(self._waiting_task, return_exceptions=True)
        self._waiting_lock.close()
        try:
            try:
                for task in list(self._analysis_tasks):
                    task.cancel()
                await asyncio.gather(
                    *list(self._analysis_tasks), return_exceptions=True
                )
                await self._delivery.stop()
            finally:
                try:
                    await self._storage.stop()
                finally:
                    if self._launcher is not None:
                        await self._launcher.stop()
        finally:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # LLM 工具

    @filter.on_llm_request()
    async def waiting_confirmation_context(self, event: AstrMessageEvent, request):
        """只在原会话等待确认期间给 AI 暴露确认工具和当前任务范围。"""
        record = self._pending.get(event.unified_msg_origin)
        waiting = (record is not None and record["state"] == "awaiting_confirmation"
                   and time.time() < record["deadline"])
        if not waiting:
            if request.func_tool is not None:
                request.func_tool = copy(request.func_tool)
                request.func_tool.remove_tool("confirm_waiting_analysis")
            return
        request.system_prompt += (
            "\n当前会话有服务恢复后的分析询问：" + json.dumps(record, ensure_ascii=False)
            + "\n仅当本轮用户明确在回答该询问时调用 confirm_waiting_analysis；"
            "群内任何用户均可确认。肯定则 confirm，拒绝则 cancel；"
            "只确认部分时填写对应代码。含糊、暂缓或无关聊天不调用。"
            "本轮用户提出新的分析需求时调用正常分析工具，由插件清空旧队列。"
            "不要将普通的‘好’机械视为确认，不得自行替用户同意。"
        )

    @filter.llm_tool(name="confirm_waiting_analysis")
    async def confirm_waiting_analysis(
        self, event: AstrMessageEvent, batch_id: str, action: str, symbols: str = "",
    ):
        """处理用户对服务恢复询问的明确回答，不能用于新分析请求。

        Args:
            batch_id(string): 当前询问中给出的任务 ID，必须原样传入
            action(string): confirm 表示明确同意，cancel 表示明确拒绝
            symbols(string): 全部确认时留空；部分确认时填询问内的代码，以英文逗号分隔
        """
        umo = event.unified_msg_origin
        record = self._pending.get(umo)
        if record is None or record["id"] != batch_id:
            return "等待任务已变化或已清除，请勿重新调用分析工具执行旧任务。"
        if action == "cancel":
            self._pending.discard(umo)
            return "积压分析已取消。请自然地简短回应用户。"
        if action != "confirm":
            return "回答不明确，尚未执行任何分析。"
        chosen = [x.strip() for x in symbols.split(",") if x.strip()] if symbols else None
        try:
            accepted = self._pending.confirm(umo, batch_id, symbols=chosen)
        except ValueError:
            return "所选标的不属于当前等待队列，请先澄清。"
        if accepted is None:
            return "等待已到期或任务已执行，请勿重新分析旧任务。"
        self._start_batch(event, accepted)
        return "已确认积压任务，插件会依次分析。只简短确认，勿重复调用分析工具。"

    def _start_batch(self, event, record):
        if record["id"] in self._batch_tasks or self._stopping:
            return
        task = asyncio.create_task(self._run_confirmed_batch(event, record))
        self._batch_tasks[record["id"]] = task
        self._analysis_tasks.add(task)
        task.add_done_callback(self._analysis_tasks.discard)
        task.add_done_callback(lambda _: self._batch_tasks.pop(record["id"], None))

    def _recover_batches(self):
        for record in self._pending.accepted():
            self._start_batch(SessionEvent(self.context, record["umo"]), record)

    async def _run_confirmed_batch(self, event, record):
        lock = FileLock(self._data_dir / f"batch-{record['id']}.lock")
        if not lock.acquire():
            return
        try:
            # 取到独占锁后重读，防止其他 worker 已完成后恢复陈旧快照。
            latest = next((item for item in self._pending.accepted() if item["id"] == record["id"]), None)
            if latest is None:
                return
            record = latest
            steps = record.setdefault("steps", [dict(item, state="waiting") for item in record["targets"]])
            self._pending.save_accepted(record)
            for step in steps:
                if step["state"] in {"done", "failed"}:
                    continue
                if step["state"] == "analyzing":
                    # API 没有幂等请求号，崩溃后不能证明分析是否已结束。
                    step.update(state="failed", error="analysis_interrupted_unknown_result")
                    self._pending.save_accepted(record)
                    logger.warning("分析被中断且结果不明，未重复调用：%s", step["symbol"])
                    try:
                        async with asyncio.timeout(30):
                            text = await generate_text(self.context, event.unified_msg_origin, {
                                "event": "analysis_interrupted", "symbol": step["symbol"],
                                "fact": "重载时分析被中断，无法确认结果；未自动重跑，如仍需要请重新提出",
                            })
                            await event.send(event.plain_result(text))
                    except Exception as error:  # noqa: BLE001 — 无可用模型或平台时保留诊断
                        logger.warning("分析中断提示未发出：%s", type(error).__name__)
                    continue
                if step["state"] == "report_ready":
                    if not self._delivery.reserve():
                        return
                    try:
                        await self._deliver_confirmed_report(event, step["markdown"],
                                                             step["symbol"], step["kind"],
                                                             record=record, step=step)
                    finally:
                        self._delivery.release()
                else:
                    if (self._launcher is not None
                            and getattr(self._launcher, "state", "ready") != "ready"):
                        return
                    step["state"] = "analyzing"
                    self._pending.save_accepted(record)
                    await self._run_analysis(event, step["kind"], step["symbol"], [],
                                             from_queue=True, record=record, step=step)
                    if step["state"] == "waiting":
                        return
                    if step["state"] == "analyzing":
                        step.update(state="failed", error="analysis_or_report_failed")
                        self._pending.save_accepted(record)
        except asyncio.CancelledError:
            logger.info("积压分析被中断，保留已接受任务记录：%s", record["id"])
            raise
        except Exception as error:  # noqa: BLE001 — 后台任务隔离，保留记录供恢复
            logger.warning("积压分析执行失败：%s", type(error).__name__)
        else:
            self._pending.finish(record["id"])
        finally:
            lock.close()

    async def _deliver_confirmed_report(self, event, markdown, symbol, kind, *, record=None, step=None):
        """只重试文案与投递，已经生成的研报不会再次分析。"""
        task_id = await self._storage.io(self._storage.begin, markdown, symbol, kind)
        saved = step if step is not None else {}
        bridge = saved.get("bridge")
        bridge_sent = saved.get("bridge_sent", False)
        success = False
        error_name = ""
        try:
            for attempt in range(saved.get("attempts", 0), 3):
                try:
                    delay = saved.get("retry_at", 0) - time.time()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    async with asyncio.timeout(30):
                        if bridge is None:
                            bridge = await generate_text(self.context, event.unified_msg_origin, {
                                "event": "report_ready", "symbol": symbol,
                                "fact": "用户已确认；报告已生成，即将发送，尚未送达",
                            })
                            saved["bridge"] = bridge
                            if record is not None:
                                self._pending.save_accepted(record)
                        if not bridge_sent:
                            await event.send(event.plain_result(bridge))
                            bridge_sent = True
                            saved["bridge_sent"] = True
                            if record is not None:
                                self._pending.save_accepted(record)
                    success = await self._delivery.deliver_reserved(event, markdown, symbol, kind)
                    if success:
                        saved["state"] = "done"
                        saved.pop("markdown", None)
                        if record is not None:
                            self._pending.save_accepted(record)
                        return
                except Exception as error:  # noqa: BLE001 — 模型和消息平台 SDK 隔离
                    error_name = type(error).__name__
                    logger.warning("积压报告投递尚未完成，第 %s 次：%s", attempt + 1, error_name)
                saved.update(attempts=attempt + 1, retry_at=time.time() + 30)
                if record is not None:
                    self._pending.save_accepted(record)
            saved["state"] = "failed"
            saved["error"] = error_name or "delivery_failed"
            if record is not None:
                self._pending.save_accepted(record)
        finally:
            await self._storage.io(self._storage.finish, task_id, None,
                                   "success" if success else "failed", "queued_delivery", error_name)

    @filter.llm_tool(name="analyze_stock")
    async def analyze_stock(self, event: AstrMessageEvent, symbol: str):
        """分析一只 A 股或港股个股，并把完整研报以图片发送给用户。

        当用户询问某只 A 股或港股个股的分析、评价、是否值得关注或买入时使用。

        Args:
            symbol(string): A 股 6 位代码或港股 5 位代码，例如 600519、00700.HK
        """
        note: list[str] = []
        await self._run_analysis(event, "stock", symbol, note)
        return note[-1] if note else "流程已结束。只回一句简短确认，不要再调用工具。"

    @filter.llm_tool(name="analyze_index")
    async def analyze_index(self, event: AstrMessageEvent, symbol: str):
        """分析一个 A 股指数，并把完整研报以图片发送给用户。

        当用户询问大盘或指数（如上证指数、沪深 300、中证 500）的走势与估值时使用。

        Args:
            symbol(string): 6 位指数代码，例如 000300、399006
        """
        note: list[str] = []
        await self._run_analysis(event, "index", symbol, note)
        return note[-1] if note else "流程已结束。只回一句简短确认，不要再调用工具。"

    # ------------------------------------------------------------------
    # 流程与辅助

    def _resolve_effective_timeout(self, event: AstrMessageEvent) -> int:
        """读取 AstrBot 工具超时并收敛；读取失败回退插件配置值。"""
        tool_timeout: int | None = None
        try:
            cfg = self.context.get_config(umo=event.unified_msg_origin)
            provider_settings = (
                cfg.get("provider_settings", {}) if hasattr(cfg, "get") else {}
            )
            raw = provider_settings.get("tool_call_timeout")
            tool_timeout = int(raw) if raw is not None else None
        except Exception:  # noqa: BLE001 — 配置读取失败按插件配置超时处理
            logger.warning(
                "读取 AstrBot tool_call_timeout 失败，使用插件配置超时", exc_info=True
            )
        return resolve_effective_timeout(self._timeout_seconds, tool_timeout)

    async def _run_analysis(
        self,
        event: AstrMessageEvent,
        kind: ReportKind,
        symbol: str,
        note: list[str],
        *,
        from_queue: bool = False,
        record=None,
        step=None,
    ) -> None:
        """分析／取报告使用工具截止时间；完整报告由受管理任务独立发送。"""
        symbol = canonical_symbol(kind, symbol)
        if not from_queue:
            umo = event.unified_msg_origin
            existing = self._pending.get(umo)
            ready = (self._launcher is None
                     or getattr(self._launcher, "state", "ready") == "ready")
            if existing is not None and (existing["state"] == "awaiting_confirmation" or ready):
                self._pending.discard(umo)
            if (self._launcher is not None
                    and getattr(self._launcher, "state", "ready") in {"idle", "starting"}):
                evicted = self._pending.enqueue(umo, kind, symbol)
                detail = f"；已替换最早的 {evicted['symbol']}" if evicted else ""
                note.append("服务暂不可用，已加入本会话的等待队列" + detail
                            + "。恢复后会统一询问是否仍需分析，尚未开始分析。请自然告知用户。")
                return
        if self._stopping or not await self._storage.before_task(
            self._delivery.reserve
        ):
            if step is not None:
                step["state"] = "waiting"
                self._pending.save_accepted(record)
            await event.send(
                event.plain_result("⏳ 报告任务较多或插件正在停用，请稍后再试。")
            )
            note.append("当前繁忙，已提示用户。只回简短确认，不要再次调用工具。")
            return
        effective_timeout = self._resolve_effective_timeout(event)
        deadline = asyncio.get_running_loop().time() + effective_timeout - 5
        submitted = False
        key = (kind, symbol)
        lock, users = self._analysis_locks.get(key, (asyncio.Lock(), 0))
        self._analysis_locks[key] = (lock, users + 1)
        task = asyncio.current_task()
        if task is not None and not from_queue:
            self._analysis_tasks.add(task)
        try:
            async with asyncio.timeout(max(0, effective_timeout - 5)):
                # 同一标的的分析与取报告互斥，避免取到另一次调用的“最新报告”。
                async with lock:
                    markdown = await self._perform_analysis(
                        event, kind, symbol, note, deadline
                    )
            if markdown is not None:
                if from_queue:
                    if step is not None:
                        step.update(state="report_ready", markdown=markdown)
                        self._pending.save_accepted(record)
                    await self._deliver_confirmed_report(event, markdown, symbol, kind,
                                                         record=record, step=step)
                    return
                self._delivery.submit(event, markdown, symbol, kind)
                submitted = True
                note.append(
                    "报告已生成，正在后台发送。只回一句简短确认，不要声称已送达，"
                    "不要复述报告内容，不要再调用工具。"
                )
        except TimeoutError:
            await asyncio.wait_for(
                event.send(
                    event.plain_result(
                        "❌ 本次调用时间已耗尽，请稍后重试；后台准备任务会继续。"
                    )
                ),
                timeout=5,
            )
            note.append(
                "调用时间已耗尽，原因已发给用户。只回简短确认，不要再调用工具。"
            )
        finally:
            if not submitted:
                self._delivery.release()
            if task is not None and not from_queue:
                self._analysis_tasks.discard(task)
            _, users = self._analysis_locks[key]
            if users == 1:
                self._analysis_locks.pop(key, None)
            else:
                self._analysis_locks[key] = (lock, users - 1)

    async def _perform_analysis(
        self,
        event: AstrMessageEvent,
        kind: ReportKind,
        symbol: str,
        note: list[str],
        deadline: float,
    ) -> str | None:
        """执行分析流程：进度 → 分析 → 报告取回；成功返回完整原文。

        消息一律用 `await event.send(...)` 显式发送，不用生成器 yield：
        AstrBot 的权限代理（_PermissionGuardedTool）会完整消费插件的异步生成器，
        并且只保留最后一个 yield，多段 yield 会被静默丢弃。
        """

        def remaining() -> int:
            return max(1, int(deadline - asyncio.get_running_loop().time()))

        await event.send(event.plain_result(progress_message(symbol)))

        if self._launcher is not None:
            outcome = await self._launcher.ensure_ready(
                wait_timeout=max(
                    0,
                    min(
                        self._startup_timeout,
                        deadline - asyncio.get_running_loop().time(),
                    ),
                )
            )
            if not outcome.ok:
                message = (
                    f"⏳ 分析服务准备中：{public_error_detail(outcome.reason)}"
                    if getattr(outcome, "pending", False)
                    else f"❌ 分析服务未就绪：{public_error_detail(outcome.reason)}"
                )
                await event.send(event.plain_result(message))
                note.append(
                    "服务未就绪，原因已发给用户。只回简短确认，不要再调用工具。"
                )
                return
        effective_timeout = remaining()

        # 超时/取消或错误响应不能证明服务端分析停止；保持保守清理状态。
        try:
            if kind == "stock":
                outcome = await self._client.analyze_stock(symbol, effective_timeout)
            else:
                outcome = await self._client.analyze_index(symbol, effective_timeout)
        except BaseException:
            self._cleanup_uncertain = True
            raise
        if not outcome.ok:
            self._cleanup_uncertain = True
            await event.send(event.plain_result(outcome.user_message))
            note.append(
                "分析失败，原因已直接发给用户。只回一句简短确认，不要编造分析内容，不要再调用工具。"
            )
            return

        report_id = await self._client.latest_report_id(kind, symbol, remaining())
        markdown = (
            await self._client.download_report(report_id, remaining())
            if report_id
            else None
        )
        if markdown is None or not markdown.strip():
            logger.warning("报告定位或下载失败: kind=%s symbol=%s", kind, symbol)
            await event.send(
                event.plain_result(
                    "❌ 分析已完成，但未能取回报告，请检查插件日志或稍后重试。"
                )
            )
            note.append(
                "报告取回失败，原因已发给用户。只回一句简短确认，不要再调用工具。"
            )
            return
        return markdown
