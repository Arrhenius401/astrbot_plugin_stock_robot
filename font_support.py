"""为 AstrBot 原生本地字生图准备字体，绝不覆盖用户文件。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import struct
import tempfile
import time
from pathlib import Path
from threading import Event, RLock

import httpx
from astrbot.api import logger

FONT_REVISION = "395f4ca95eb365f9f33266ad54261d46943b96d1"
FONT_BASE_URL = (
    "https://raw.githubusercontent.com/Arrhenius401/astrbot_plugin_stock_robot/"
    f"{FONT_REVISION}/assets/fonts/"
)
FONT_DOWNLOAD_SOURCES = (
    "https://ghfast.top/" + FONT_BASE_URL,
    f"https://cdn.jsdelivr.net/gh/Arrhenius401/astrbot_plugin_stock_robot@{FONT_REVISION}/assets/fonts/",
    FONT_BASE_URL,
)
DOWNLOAD_ATTEMPT_SECONDS = 30
FALLBACK_SOURCE = Path(__file__).parent / "assets/fonts/NotoSansCJKsc-Regular.otf"
FALLBACK_HASH = "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b"
FONT_HASHES = {
    "font.ttf": "1aa9b25e91ba8ba42edac9d3ca187f37a35509a70ba72af4dcb697180ad92484",
    "font-bold.ttf": "4ba5a49137a6965e3eafd51e4c3072802fe61e63cfbe1ca75f7dba0b33fa48ec",
}
FONT_FILES = {"font.ttf": "StockRobotCJK-Regular.otf", "font-bold.ttf": "StockRobotCJK-Bold.otf"}
DOWNLOAD_TIMEOUT_SECONDS = 120
FONT_MAX_BYTES = 20 * 1024 * 1024
CHINESE_SAMPLE = "中文报告股票指数分析风险收益市场价格涨跌⁰¹²³⁴⁵⁶⁷⁸⁹"
_FONT_LOCK = RLock()
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


def _ensure_font(data_dir: Path, source: Path, filename: str, *, sample: str = CHINESE_SAMPLE) -> None:
    """检查已有字体；缺失时原子创建。失败仅记录日志，不阻止插件加载。"""
    target = data_dir / filename
    staging: Path | None = None
    try:
        if not os.path.lexists(target):
            from PIL import ImageFont

            ImageFont.truetype(str(source), 25)
            if missing_characters(source, sample):
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
        missing = missing_characters(target, sample)
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


def _install_fonts(data_dir: Path, source: Path, bold_source: Path) -> None:
    """按完整哈希管理常规与粗体；未知字体或符号链接均不替换。"""
    with _FONT_LOCK:
        try:
            pairs = [("font.ttf", source), ("font-bold.ttf", bold_source)]
            unknown = set()
            for filename, bundled in pairs:
                target = data_dir / filename
                if os.path.lexists(target):
                    digest = hashlib.sha256(target.read_bytes()).hexdigest()
                    if target.is_symlink() or digest not in {
                        _OLD_HASHES[filename], FONT_HASHES[filename], FALLBACK_HASH,
                    }:
                        logger.warning("字体哈希未知，保留用户自定义文件: %s", target)
                        _ensure_font(data_dir, bundled, filename)
                        unknown.add(filename)
            for filename, bundled in pairs:
                if filename in unknown:
                    continue
                target = data_dir / filename
                desired = FONT_HASHES[filename]
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


def _download_font(filename: str, target: Path, deadline: float, stop: Event | None = None) -> None:
    """流式下载至隔离目录，固定摘要且限制单文件大小和总等待时间。"""
    for base_url in FONT_DOWNLOAD_SOURCES:
        if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
            raise TimeoutError("字体下载超时或已停止")
        try:
            _download_from_source(filename, target, deadline, stop, base_url)
            return
        except (httpx.HTTPError, TimeoutError, ValueError) as error:
            target.unlink(missing_ok=True)
            logger.warning("字体下载源失败，尝试备用源: %s %s: %s", base_url, type(error).__name__, error)
            last_error = error
    raise last_error


def _download_from_source(filename: str, target: Path, deadline: float,
                          stop: Event | None, base_url: str) -> None:
    remaining = min(DOWNLOAD_ATTEMPT_SECONDS, deadline - time.monotonic())
    if remaining <= 0 or (stop is not None and stop.is_set()):
        raise TimeoutError("字体下载总时限已到")
    async def consume():
        digest = hashlib.sha256()
        size = 0
        async with (
            asyncio.timeout(remaining),
            httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(min(15, remaining))) as client,
            client.stream("GET", base_url + FONT_FILES[filename]) as response,
        ):
            response.raise_for_status()
            with target.open("wb") as output:
                async for chunk in response.aiter_bytes(64 * 1024):
                    if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
                        raise TimeoutError("字体下载超时或已停止")
                    size += len(chunk)
                    if size > FONT_MAX_BYTES:
                        raise ValueError("字体下载超过大小限制")
                    digest.update(chunk)
                    output.write(chunk)
        if digest.hexdigest() != FONT_HASHES[filename]:
            raise ValueError("字体 SHA-256 校验失败")

    asyncio.run(consume())
    if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
        raise TimeoutError("字体下载超时或已停止")
    from PIL import ImageFont

    ImageFont.truetype(str(target), 25)
    if missing_characters(target):
        raise ValueError("下载字体缺少中文或上标字符")


def _prepare_fallback(data_dir: Path) -> None:
    """仅补缺失字重；旧常规体兼任粗体，角标由渲染层回退。"""
    if not FALLBACK_SOURCE.exists():
        return
    if hashlib.sha256(FALLBACK_SOURCE.read_bytes()).hexdigest() != FALLBACK_HASH:
        raise ValueError("内置托底字体 SHA-256 校验失败")
    for filename in FONT_FILES:
        target = data_dir / filename
        if os.path.lexists(target) and (target.is_symlink() or
            hashlib.sha256(target.read_bytes()).hexdigest() not in
            {FONT_HASHES[filename], _OLD_HASHES[filename], FALLBACK_HASH}):
            return
    for filename in FONT_FILES:
        if not os.path.lexists(data_dir / filename):
            _ensure_font(data_dir, FALLBACK_SOURCE, filename, sample=CHINESE_SAMPLE.split("⁰")[0])
            if (data_dir / filename).exists():
                _RESTART_REQUIRED.add(str(data_dir.resolve()))


def ensure_chinese_font(data_dir: Path, *, stop: Event | None = None) -> None:
    """已有完整字体离线复用；仅为缺失或已知旧版下载完整字库。"""
    # 安装函数使用同一锁；RLock 避免嵌套锁死并串行化下载和安装。
    with _FONT_LOCK:
        try:
            if stop is not None and stop.is_set():
                return
            _prepare_fallback(data_dir)
            needed = []
            unknown = False
            for filename in FONT_FILES:
                target = data_dir / filename
                if os.path.lexists(target):
                    digest = hashlib.sha256(target.read_bytes()).hexdigest()
                    if target.is_symlink() or digest not in {FONT_HASHES[filename], _OLD_HASHES[filename], FALLBACK_HASH}:
                        unknown = True
                    elif digest != FONT_HASHES[filename]:
                        needed.append(filename)
                else:
                    needed.append(filename)
            # 与原有保护一致：存在自定义字体时不补齐另一份缺失字重。
            if unknown:
                needed = [name for name in needed if os.path.lexists(data_dir / name)]
            deadline = time.monotonic() + DOWNLOAD_TIMEOUT_SECONDS
            with tempfile.TemporaryDirectory(prefix="stock-robot-fonts-") as temporary:
                sources = {name: Path(temporary) / name for name in FONT_FILES}
                for filename in needed:
                    logger.info("正在下载完整中文字体: %s", FONT_FILES[filename])
                    _download_font(filename, sources[filename], deadline, stop)
                if stop is not None and stop.is_set():
                    return
                _install_fonts(data_dir, sources["font.ttf"], sources["font-bold.ttf"])
        except Exception as error:  # noqa: BLE001 — 离线不阻止插件加载
            logger.warning("字体下载或准备失败: %s: %s；保留已有字体，下次加载重试", type(error).__name__, error)


def chinese_fonts_available(data_dir: Path) -> bool:
    """检查实际使用的两种字重；不要求自定义字体包含全部上标。"""
    try:
        from PIL import ImageFont

        for filename in FONT_FILES:
            target = data_dir / filename
            ImageFont.truetype(str(target), 25)
            if missing_characters(target, CHINESE_SAMPLE.split("⁰")[0]):
                return False
        return True
    except Exception:  # noqa: BLE001 — 无效或不可读字体走文件交付
        return False


def superscripts_available(data_dir: Path) -> bool:
    """替换后本进程继续回退，重启后才信任两套实际字体的覆盖。"""
    if str(data_dir.resolve()) in _RESTART_REQUIRED:
        return False
    try:
        return all(not missing_characters(data_dir / filename, "⁰¹²³⁴⁵⁶⁷⁸⁹")
                   for filename in ("font.ttf", "font-bold.ttf"))
    except (OSError, ValueError, KeyError, struct.error):
        return False
