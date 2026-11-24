"""使用已知源字体的空闲 CID 槽补字；不增加字形数，保留授权信息。"""
import argparse
import hashlib
import json
from pathlib import Path

from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

SOURCE_HASHES = {
    "Regular": "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b",
    "Bold": "b5f0d1a190a7f9b43c310a8850630af12553df32c4c050543f9059732d9b4c0a",
}
# 最后两槽避开 GSUB Lookup 41 的替换目标，按全部 cmap 与布局引用复核。
SLOTS = ("cid65534", "cid65533", "cid65532", "cid65531", "cid63107", "cid63106")


def referenced_slots(value, seen=None):
    """递归检查布局表中的字形引用，不依赖字体家族名判断安全性。"""
    seen = seen if seen is not None else set()
    if isinstance(value, str):
        return {value} if value.startswith("cid") else set()
    if id(value) in seen:
        return set()
    seen.add(id(value))
    children = (list(value.keys()) + list(value.values()) if isinstance(value, dict) else
                value if isinstance(value, (tuple, list)) else vars(value).values()
                if hasattr(value, "__dict__") else [])
    found = set()
    for child in children:
        found.update(referenced_slots(child, seen))
    return found


def build(source: Path, target: Path, style: str):
    """固定输入哈希、时间戳与槽位，使构建可复现且错误前置。"""
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != SOURCE_HASHES[style]:
        raise ValueError(f"{style} 源字体 SHA-256 不匹配")
    font = TTFont(source, recalcTimestamp=False)
    cmap = font.getBestCmap()
    if font["maxp"].numGlyphs != 65535 or font["head"].unitsPerEm != 1000:
        raise ValueError("源字体字形数或 UPM 不匹配")
    mapped = {value for table in font["cmap"].tables for value in table.cmap.values()}
    if set(SLOTS) & mapped or any(set(SLOTS) & referenced_slots(font[table]) for table in ("GSUB", "GPOS", "GDEF", "BASE")):
        raise ValueError("目标槽已被映射或布局表引用，拒绝替换")
    top = font["CFF "].cff.topDictIndex[0]
    glyphs = font.getGlyphSet()
    ref = cmap[ord("¹")]
    for char, digit, slot in zip("⁰⁵⁶⁷⁸⁹", "056789", SLOTS, strict=True):
        original = cmap[ord(digit)]
        width = font["hmtx"][ref][0]
        pen = T2CharStringPen(width, glyphs)
        glyphs[original].draw(TransformPen(pen, (.6, 0, 0, .6, 0, 441)))
        fd = top.FDSelect.gidArray[font.getGlyphID(original)]
        top.FDSelect.gidArray[font.getGlyphID(slot)] = fd
        top.CharStrings[slot] = pen.getCharString(private=top.FDArray[fd].Private, globalSubrs=top.GlobalSubrs)
        font["hmtx"][slot] = (width, round(font["hmtx"][original][1] * .6))
        font["vmtx"][slot] = font["vmtx"][ref]
        font["VORG"].VOriginRecords[slot] = font["VORG"].VOriginRecords.get(ref, font["VORG"].defaultVertOriginY)
        for table in font["cmap"].tables:
            if table.isUnicode():
                table.cmap[ord(char)] = slot
    # 衍生字体改名；版权、许可证与 NOTICE 保留，不冒用原字体名称。
    names = {1: "Stock Robot CJK", 2: style, 3: f"StockRobotCJK-{style}-Superscript1",
             4: f"Stock Robot CJK {style}", 6: f"StockRobotCJK-{style}",
             16: "Stock Robot CJK", 17: style}
    for record in font["name"].names:
        if record.nameID in names:
            record.string = names[record.nameID].encode(record.getEncoding())
    top.FamilyName = "Stock Robot CJK"
    top.FullName = f"Stock Robot CJK {style}"
    font["CFF "].cff.fontNames = [f"StockRobotCJK-{style}"]
    if "DSIG" in font:
        del font["DSIG"]
    target.parent.mkdir(parents=True, exist_ok=True)
    font.save(target)
    with TTFont(target) as check:
        if check["maxp"].numGlyphs != 65535 or any(ord(c) not in check.getBestCmap() for c in "⁰¹²³⁴⁵⁶⁷⁸⁹"):
            raise ValueError("输出字体自检失败")
    return {"source_sha256": digest, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regular", type=Path, default=Path("assets/fonts/NotoSansCJKsc-Regular.otf"))
    parser.add_argument("--bold", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("assets/fonts"))
    args = parser.parse_args()
    manifest = {style: build(source, args.output / f"StockRobotCJK-{style}.otf", style)
                for style, source in (("Regular", args.regular), ("Bold", args.bold))}
    (args.output / "superscript-hashes.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
