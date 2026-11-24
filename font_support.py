"""为 AstrBot 原生本地字生图准备字体，绝不覆盖用户文件。"""

from __future__ import annotations

import hashlib
import os
import struct
import tempfile
from pathlib import Path
from threading import Lock

from astrbot.api import logger

FONT_SOURCE = Path(__file__).parent / "assets/fonts/StockRobotCJK-Regular.otf"
BOLD_FONT_SOURCE = Path(__file__).parent / "assets/fonts/StockRobotCJK-Bold.otf"
CHINESE_SAMPLE = "中文报告股票指数分析风险收益市场价格涨跌⁰¹²³⁴⁵⁶⁷⁸⁹"
_FONT_LOCK = Lock()
_RESTART_REQUIRED: set[str] = set()
_OLD_HASHES = {
    "font.ttf": "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b",
    "font-bold.ttf": "b5f0d1a190a7f9b43c310a8850630af12553df32c4c050543f9059732d9b4c0a",
}


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


def _ensure_font(data_dir: Path, source: Path, filename: str) -> None:
    """检查已有字体；缺失时原子创建。失败仅记录日志，不阻止插件加载。"""
    target = data_dir / filename
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
                logger.debug("字体目标已由其他操作创建，保留现有文件: %s", target)
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


def ensure_chinese_font(data_dir: Path, source: Path = FONT_SOURCE) -> None:
    """按完整哈希管理常规与粗体；未知字体或符号链接均不替换。"""
    with _FONT_LOCK:
        try:
            pairs = [("font.ttf", source), ("font-bold.ttf", BOLD_FONT_SOURCE)]
            unknown = set()
            for filename, bundled in pairs:
                target = data_dir / filename
                if os.path.lexists(target):
                    digest = hashlib.sha256(target.read_bytes()).hexdigest()
                    if target.is_symlink() or digest not in {
                        _OLD_HASHES[filename], hashlib.sha256(bundled.read_bytes()).hexdigest(),
                    }:
                        logger.warning("字体哈希未知，保留用户自定义文件: %s", target)
                        _ensure_font(data_dir, bundled, filename)
                        unknown.add(filename)
            for filename, bundled in pairs:
                if filename in unknown:
                    continue
                target = data_dir / filename
                desired = hashlib.sha256(bundled.read_bytes()).hexdigest()
                if not target.exists():
                    if unknown:
                        continue
                    _ensure_font(data_dir, bundled, filename)
                    if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != desired:
                        return
                    _RESTART_REQUIRED.add(str(data_dir.resolve()))
                    continue
                current = hashlib.sha256(target.read_bytes()).hexdigest()
                if current == desired:
                    continue
                if missing_characters(bundled):
                    raise ValueError("受管字体缺少中文或上标字符")
                backup = target.with_name(f"{filename}.stock-robot-{current}.bak")
                if not backup.exists():
                    os.link(target, backup)
                if hashlib.sha256(backup.read_bytes()).hexdigest() != current:
                    raise ValueError("备份哈希不匹配，停止升级")
                staging = None
                try:
                    with tempfile.NamedTemporaryFile(prefix=".stock-robot-font-", dir=data_dir, delete=False) as file:
                        staging = Path(file.name)
                        file.write(bundled.read_bytes())
                        file.flush()
                        os.fsync(file.fileno())
                    if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != current:
                        raise ValueError("目标字体已变化，停止升级")
                    os.replace(staging, target)
                    _RESTART_REQUIRED.add(str(data_dir.resolve()))
                    logger.info("受管字体已备份并升级: %s；请重启 AstrBot 清除字体缓存", target)
                finally:
                    if staging is not None:
                        staging.unlink(missing_ok=True)
        except Exception as error:  # noqa: BLE001 — 字体安装隔离，不阻止报告安全回退
            logger.warning("受管字体检查失败: %s: %s；请重启 AstrBot 后重试", type(error).__name__, error)


def superscripts_available(data_dir: Path) -> bool:
    """替换后本进程继续回退，重启后才信任两套实际字体的覆盖。"""
    if str(data_dir.resolve()) in _RESTART_REQUIRED:
        return False
    try:
        return all(not missing_characters(data_dir / filename, "⁰¹²³⁴⁵⁶⁷⁸⁹")
                   for filename in ("font.ttf", "font-bold.ttf"))
    except (OSError, ValueError, KeyError, struct.error):
        return False
