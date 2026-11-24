"""调用 AstrBot 原生字生图，不维护独立排版或浏览器。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path


class ReportTooLarge(ValueError):
    """原生输出超过插件交付像素预算。"""


@dataclass(frozen=True)
class RenderedReport:
    png: bytes
    width: int
    height: int


class ReportRenderer:
    def __init__(self, native_render: Callable[..., Awaitable[str]],
                 fonts_ready: Callable[[], bool] | None = None):
        self.native_render = native_render
        self.fonts_ready = fonts_ready or (lambda: False)

    async def render(self, markdown: str, *, width: int = 800) -> RenderedReport:
        options = {"width": width} if width != 800 else {}
        # 原生中文字体不覆盖全部 Unicode 上标，图片副本用普通数字保留注释对应关系。
        def fallback(match: re.Match[str]) -> str:
            # 单位指数不是脚注；保留 m²/cm³ 等正文单位，脚注仍整份统一回退。
            if match[0] in {"²", "³"} and re.search(r"(?<![A-Za-z])(?:m|cm|mm|km)$", markdown[:match.start()]):
                return match[0]
            return "[" + match[0].translate(str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")) + "]"

        printable = markdown if self.fonts_ready() else re.sub(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+", fallback, markdown)
        path = Path(await self.native_render(printable, return_url=False, **options))
        worker = asyncio.create_task(asyncio.to_thread(self._read, path))
        cancelled = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            if not worker.cancelled():
                worker.exception()
            raise asyncio.CancelledError
        return worker.result()

    @staticmethod
    def _read(path: Path) -> RenderedReport:
        from PIL import Image

        try:
            with Image.open(path) as image:
                if image.width * image.height > 24_000_000:
                    raise ReportTooLarge("原生图片超过 2400 万像素")
                output = BytesIO()
                image.save(output, format="PNG")
                return RenderedReport(output.getvalue(), image.width, image.height)
        finally:
            path.unlink(missing_ok=True)
