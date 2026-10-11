"""字体安装不得覆盖、缺字诊断及失败隔离。"""

import hashlib
import logging
import struct
from concurrent.futures import ThreadPoolExecutor

from PIL import ImageFont
from stock_robot_plugin_test import font_support as fonts


def test_install_real_font_and_reload_preserves_file(tmp_path, caplog):
    target = tmp_path / "中文目录" / "font.ttf"
    with caplog.at_level(logging.INFO):
        fonts.ensure_chinese_font(target.parent)
        before = target.stat()
        fonts.ensure_chinese_font(target.parent)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == (
        "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b"
    )
    assert target.stat().st_mtime_ns == before.st_mtime_ns
    assert not fonts.missing_characters(target)
    assert ImageFont.truetype(str(target), 25).getname()[0] == "Noto Sans CJK SC"
    assert caplog.text.count("已新增") == 1
    assert list(target.parent.iterdir()) == [target]


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
    assert (tmp_path / "font.ttf").read_bytes() == fonts.FONT_SOURCE.read_bytes()
    assert list(tmp_path.iterdir()) == [tmp_path / "font.ttf"]
