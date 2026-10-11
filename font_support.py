"""为 AstrBot 原生本地字生图准备字体，绝不覆盖用户文件。"""

from __future__ import annotations

import os
import struct
import tempfile
from pathlib import Path

from astrbot.api import logger

FONT_SOURCE = Path(__file__).parent / "assets/fonts/NotoSansCJKsc-Regular.otf"
CHINESE_SAMPLE = "中文报告股票指数分析风险收益市场价格涨跌"


def missing_characters(path: Path, sample: str = CHINESE_SAMPLE) -> str:
    """读取 Unicode cmap；支持常见 TrueType/OpenType 及 TTC 首个字体。"""
    data = path.read_bytes()

    def u16(offset: int) -> int:
        return struct.unpack_from(">H", data, offset)[0]

    def u32(offset: int) -> int:
        return struct.unpack_from(">I", data, offset)[0]

    base = u32(12) if data[:4] == b"ttcf" else 0
    tables = {
        data[p : p + 4]: u32(p + 8)
        for p in range(base + 12, base + 12 + u16(base + 4) * 16, 16)
    }
    cmap = tables[b"cmap"]
    supported = False
    found: set[int] = set()
    for i in range(u16(cmap + 2)):
        record = cmap + 4 + i * 8
        platform, encoding = u16(record), u16(record + 2)
        if platform != 0 and not (platform == 3 and encoding in (1, 10)):
            continue
        start = cmap + u32(record + 4)
        fmt = u16(start)
        if fmt in (12, 13):
            supported = True
            for j in range(u32(start + 12)):
                p = start + 16 + j * 12
                first, last, glyph = u32(p), u32(p + 4), u32(p + 8)
                for char in sample:
                    code = ord(char)
                    if first <= code <= last and (
                        glyph if fmt == 13 else glyph + code - first
                    ):
                        found.add(code)
        elif fmt == 4:
            supported = True
            count = u16(start + 6) // 2
            ends = start + 14
            starts = ends + count * 2 + 2
            deltas = starts + count * 2
            ranges = deltas + count * 2
            for char in sample:
                code = ord(char)
                for j in range(count):
                    if u16(starts + j * 2) <= code <= u16(ends + j * 2):
                        delta = u16(deltas + j * 2)
                        slot = ranges + j * 2
                        offset = u16(slot)
                        glyph = (
                            u16(slot + offset + (code - u16(starts + j * 2)) * 2)
                            if offset
                            else code
                        )
                        if glyph and (glyph + delta) % 65536:
                            found.add(code)
                        break
    if not supported:
        raise ValueError("字体没有可检查的 Unicode 字符映射")
    return "".join(char for char in sample if ord(char) not in found)


def ensure_chinese_font(data_dir: Path, source: Path = FONT_SOURCE) -> None:
    """检查已有字体；缺失时原子创建。失败仅记录日志，不阻止插件加载。"""
    target = data_dir / "font.ttf"
    staging: Path | None = None
    try:
        if not os.path.lexists(target):
            from PIL import ImageFont

            ImageFont.truetype(str(source), 25)
            if missing_characters(source):
                raise ValueError("随插件分发的字体缺少中文样本")
            data_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                prefix=".stock-robot-font-", dir=data_dir, delete=False
            ) as file:
                staging = Path(file.name)
                file.write(source.read_bytes())
                file.flush()
                os.fsync(file.fileno())
            try:
                # 同目录硬链接原子发布完整文件；目标已存在时绝不覆盖。
                os.link(staging, target)
                logger.info(
                    "已新增 AstrBot 中文字体: %s；影响原生本地字生图，"
                    "如已有字体缓存请重启 AstrBot。",
                    target,
                )
            except FileExistsError:
                pass
        from PIL import ImageFont

        ImageFont.truetype(str(target), 25)
        missing = missing_characters(target)
        if missing:
            logger.warning(
                "已有字体 %s 缺少常用中文字符（%s），本地字生图可能显示方框。"
                "插件未覆盖此文件；请备份后自行选择中文字体并重启 AstrBot。"
                "本检查不代表所有中文字符覆盖。",
                target,
                missing,
            )
    except Exception as error:  # noqa: BLE001 — 字体不可用不阻止业务加载
        logger.warning(
            "AstrBot 字体准备或检查失败: path=%s reason=%s: %s；"
            "已有文件保持不变，请检查字体有效性、目录权限和磁盘空间。",
            target,
            type(error).__name__,
            error,
        )
    finally:
        if staging is not None:
            try:
                staging.unlink(missing_ok=True)
            except OSError as error:
                logger.warning("字体临时文件清理失败: %s: %s", staging, error)
