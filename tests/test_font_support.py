"""字体安装不得覆盖、缺字诊断及失败隔离。"""

import hashlib
import logging
import shutil
import struct
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from PIL import ImageFont
from stock_robot_plugin_test import font_support as fonts

FIXTURES = Path(__file__).resolve().parent.parent / ".cache/font-fixtures"
REGULAR_SOURCE = FIXTURES / "StockRobotCJK-Regular.otf"
BOLD_SOURCE = FIXTURES / "StockRobotCJK-Bold.otf"


@pytest.fixture(autouse=True)
def local_download(monkeypatch, request):
    monkeypatch.setattr(fonts, "FALLBACK_SOURCE", FIXTURES / "not-bundled.otf")
    if request.node.name == "test_cmap_formats_check_glyph_presence":
        return
    if not all((FIXTURES / name).exists() for name in (
        "StockRobotCJK-Regular.otf", "StockRobotCJK-Bold.otf", "NotoSansCJKsc-Regular.otf"
    )):
        pytest.skip("真实字体测试需先执行 python scripts/prepare_font_fixtures.py")
    def download(filename, target, deadline, stop=None):
        shutil.copyfile(FIXTURES / fonts.FONT_FILES[filename], target)
    monkeypatch.setattr(fonts, "_download_font", download)



def test_install_real_font_and_reload_preserves_file(tmp_path, caplog):
    target = tmp_path / "中文目录" / "font.ttf"
    with caplog.at_level(logging.INFO):
        fonts.ensure_chinese_font(target.parent)
        before = target.stat()
        fonts.ensure_chinese_font(target.parent)
    assert target.read_bytes() == REGULAR_SOURCE.read_bytes()
    assert target.stat().st_mtime_ns == before.st_mtime_ns
    assert not fonts.missing_characters(target)
    assert ImageFont.truetype(str(target), 25).getbbox("中文") is not None
    assert {p.name for p in target.parent.iterdir()} == {"font.ttf", "font-bold.ttf"}


def test_existing_corrupt_font_is_never_replaced(tmp_path, caplog):
    target = tmp_path / "font.ttf"
    target.write_bytes(b"user owned invalid font")
    fonts.ensure_chinese_font(tmp_path)
    assert target.read_bytes() == b"user owned invalid font"
    assert "检查失败" in caplog.text and str(target) in caplog.text
    assert list(tmp_path.iterdir()) == [target]


def cmap_font(fmt, code=65):
    if fmt == 12:
        sub = struct.pack(">HHIIIIIII", 12, 0, 28, 0, 1, code, code, 1, 0)[:28]
    else:
        sub = struct.pack(">7H", 4, 32, 0, 4, 4, 1, 0)
        sub += struct.pack(">9H", code, 65535, 0, code, 65535, 1, 1, 0, 0)
    cmap = struct.pack(">HHHHI", 0, 1, 3, 10 if fmt == 12 else 1, 12) + sub
    header = struct.pack(">IHHHH", 0x10000, 1, 0, 0, 0)
    return header + struct.pack(">4sIII", b"cmap", 0, 28, len(cmap)) + cmap


def test_existing_loadable_latin_font_warns_without_replacement(
    tmp_path, monkeypatch, caplog
):
    target = tmp_path / "font.ttf"
    original = cmap_font(4)
    target.write_bytes(original)
    monkeypatch.setattr(ImageFont, "truetype", lambda *a, **k: object())
    fonts.ensure_chinese_font(tmp_path)
    assert target.read_bytes() == original
    assert "缺少常用中文" in caplog.text and "未覆盖" in caplog.text
    assert str(target) in caplog.text


def test_cmap_formats_check_glyph_presence(tmp_path):
    for fmt in (4, 12):
        path = tmp_path / str(fmt)
        path.write_bytes(cmap_font(fmt))
        assert fonts.missing_characters(path, "AB中") == "B中"


def test_install_permission_failure_is_logged_and_staging_removed(
    tmp_path, monkeypatch, caplog
):
    def denied(*args):
        raise PermissionError("目录只读")

    monkeypatch.setattr(fonts.os, "link", denied)
    fonts.ensure_chinese_font(tmp_path)
    assert not list(tmp_path.iterdir())
    assert "PermissionError" in caplog.text and "目录只读" in caplog.text


def test_racing_user_file_is_preserved(tmp_path, monkeypatch, caplog):
    def user_wins(source, target):
        target.write_bytes(b"user won")
        raise FileExistsError

    monkeypatch.setattr(fonts.os, "link", user_wins)
    fonts.ensure_chinese_font(tmp_path)
    assert (tmp_path / "font.ttf").read_bytes() == b"user won"
    assert list(tmp_path.iterdir()) == [tmp_path / "font.ttf"]


def test_concurrent_install_publishes_only_complete_font(tmp_path):
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: fonts.ensure_chinese_font(tmp_path), range(2)))
    assert (tmp_path / "font.ttf").read_bytes() == REGULAR_SOURCE.read_bytes()
    assert {p.name for p in tmp_path.iterdir()} == {"font.ttf", "font-bold.ttf"}


def test_known_regular_upgrade_backs_up_and_requires_restart(tmp_path):
    original = REGULAR_SOURCE.with_name("NotoSansCJKsc-Regular.otf").read_bytes()
    (tmp_path / "font.ttf").write_bytes(original)
    fonts.ensure_chinese_font(tmp_path)
    assert (tmp_path / "font.ttf").read_bytes() == REGULAR_SOURCE.read_bytes()
    assert next(iter(tmp_path.glob("*.bak"))).read_bytes() == original
    assert not fonts.superscripts_available(tmp_path)
    # 模拟下次进程启动，覆盖两套实际字体一起检查的路径。
    fonts._RESTART_REQUIRED.discard(str(tmp_path.resolve()))
    assert fonts.superscripts_available(tmp_path)
    (tmp_path / "font-bold.ttf").unlink()
    assert not fonts.superscripts_available(tmp_path)


def test_known_bold_upgrade_is_backed_up_by_full_hash(tmp_path, monkeypatch):
    (tmp_path / "font.ttf").write_bytes(REGULAR_SOURCE.read_bytes())
    original = b"known managed bold bytes"
    digest = hashlib.sha256(original).hexdigest()
    monkeypatch.setitem(fonts._OLD_HASHES, "font-bold.ttf", digest)
    target = tmp_path / "font-bold.ttf"
    target.write_bytes(original)
    fonts.ensure_chinese_font(tmp_path)
    assert target.read_bytes() == BOLD_SOURCE.read_bytes()
    assert target.with_name(f"font-bold.ttf.stock-robot-{digest}.bak").read_bytes() == original


def test_unknown_valid_bold_is_preserved_and_entire_report_falls_back(tmp_path):
    (tmp_path / "font.ttf").write_bytes(REGULAR_SOURCE.read_bytes())
    original = REGULAR_SOURCE.with_name("NotoSansCJKsc-Regular.otf").read_bytes() + b"custom"
    (tmp_path / "font-bold.ttf").write_bytes(original)
    fonts.ensure_chinese_font(tmp_path)
    assert (tmp_path / "font-bold.ttf").read_bytes() == original
    assert not list(tmp_path.glob("*.bak"))
    assert not fonts.superscripts_available(tmp_path)


def test_existing_current_fonts_never_download(tmp_path, monkeypatch):
    shutil.copyfile(REGULAR_SOURCE, tmp_path / "font.ttf")
    shutil.copyfile(BOLD_SOURCE, tmp_path / "font-bold.ttf")
    def offline(*args):
        raise AssertionError("已有完整字体不应联网")
    monkeypatch.setattr(fonts, "_download_font", offline)
    fonts.ensure_chinese_font(tmp_path)
    assert fonts.chinese_fonts_available(tmp_path)
    assert fonts.superscripts_available(tmp_path)


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), ValueError("hash mismatch")])
def test_download_failure_keeps_old_files_and_cleans_temporary(tmp_path, monkeypatch, failure):
    original = REGULAR_SOURCE.with_name("NotoSansCJKsc-Regular.otf").read_bytes()
    (tmp_path / "font.ttf").write_bytes(original)
    staging = []
    def fail(filename, target, deadline, stop=None):
        staging.append(target)
        target.write_bytes(b"partial")
        raise failure
    monkeypatch.setattr(fonts, "_download_font", fail)
    fonts.ensure_chinese_font(tmp_path)
    assert (tmp_path / "font.ttf").read_bytes() == original
    assert list(tmp_path.iterdir()) == [tmp_path / "font.ttf"]
    assert staging and not staging[0].parent.exists()
    assert not fonts.chinese_fonts_available(tmp_path)


@pytest.mark.parametrize("mode", ["valid", "corrupt", "timeout", "oversize"])
def test_stream_download_checks_content_and_deadline(tmp_path, monkeypatch, mode):
    # 真实字体验证两种字重及全部上标；网络由内存响应替代。
    data = REGULAR_SOURCE.read_bytes() if mode == "valid" else b"corrupt"
    def respond(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("timeout")
        return httpx.Response(200, content=data)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(fonts.httpx, "AsyncClient", lambda **kw: original_client(
        transport=httpx.MockTransport(respond), **kw))
    if mode == "oversize":
        monkeypatch.setattr(fonts, "FONT_MAX_BYTES", 2)
    # 绕过本文件本地下载替身，测试生产下载器。
    import time
    if mode == "valid":
        REAL_DOWNLOAD("font.ttf", tmp_path / "font", time.monotonic() + 120)
        assert not fonts.missing_characters(tmp_path / "font")
    else:
        with pytest.raises((ValueError, httpx.ReadTimeout)):
            REAL_DOWNLOAD("font.ttf", tmp_path / "font", time.monotonic() + 120)


REAL_DOWNLOAD = fonts._download_font


def test_total_download_deadline_and_stop(tmp_path):
    import time
    from threading import Event
    with pytest.raises(TimeoutError):
        REAL_DOWNLOAD("font.ttf", tmp_path / "font", time.monotonic() - 1)
    stop = Event()
    stop.set()
    with pytest.raises(TimeoutError):
        REAL_DOWNLOAD("font.ttf", tmp_path / "font", time.monotonic() + 120, stop)
    assert not (tmp_path / "font").exists()


@pytest.mark.parametrize("online", [False, True])
def test_bundled_fallback_and_later_upgrade(tmp_path, monkeypatch, online):
    original = FIXTURES / "NotoSansCJKsc-Regular.otf"
    monkeypatch.setattr(fonts, "FALLBACK_SOURCE", original)
    def offline(*a, **kw):
        raise TimeoutError("offline")
    with monkeypatch.context() as context:
        context.setattr(fonts, "_download_font", offline)
        fonts.ensure_chinese_font(tmp_path)
    assert fonts.chinese_fonts_available(tmp_path)
    fonts._RESTART_REQUIRED.clear()
    assert not fonts.superscripts_available(tmp_path)
    assert all((tmp_path / n).read_bytes() == original.read_bytes() for n in fonts.FONT_FILES)
    if online:
        fonts.ensure_chinese_font(tmp_path)
        for n,h in fonts.FONT_HASHES.items():
            assert hashlib.sha256((tmp_path / n).read_bytes()).hexdigest() == h
        fonts._RESTART_REQUIRED.clear()
        assert fonts.superscripts_available(tmp_path)
        assert len(list(tmp_path.glob("*.bak"))) == 2


def test_mirror_failover_validates_hash(tmp_path, monkeypatch):
    import time
    data = REGULAR_SOURCE.read_bytes()
    calls = []
    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=b"corrupt" if len(calls)==1 else data)
    client = httpx.AsyncClient
    monkeypatch.setattr(fonts.httpx, "AsyncClient", lambda **kw: client(
        transport=httpx.MockTransport(respond), **kw))
    REAL_DOWNLOAD("font.ttf", tmp_path / "font", time.monotonic()+120)
    assert len(calls) == 2
    assert calls[0].startswith(fonts.FONT_DOWNLOAD_SOURCES[0])
    assert calls[1].startswith(fonts.FONT_DOWNLOAD_SOURCES[1])
    assert (tmp_path / "font").read_bytes() == data
