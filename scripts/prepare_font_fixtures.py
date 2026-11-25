"""按固定版本和 SHA-256 下载开发测试资源，不进入发布包。"""
import hashlib
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/Arrhenius401/astrbot_plugin_stock_robot/395f4ca95eb365f9f33266ad54261d46943b96d1/assets/fonts/"
FILES = {
    "StockRobotCJK-Regular.otf": "1aa9b25e91ba8ba42edac9d3ca187f37a35509a70ba72af4dcb697180ad92484",
    "StockRobotCJK-Bold.otf": "4ba5a49137a6965e3eafd51e4c3072802fe61e63cfbe1ca75f7dba0b33fa48ec",
    "NotoSansCJKsc-Regular.otf": "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b",
}

if __name__ == "__main__":
    destination = Path(__file__).resolve().parent.parent / ".cache/font-fixtures"
    destination.mkdir(parents=True, exist_ok=True)
    for name, expected in FILES.items():
        target = destination / name
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            continue
        with urllib.request.urlopen(BASE + name, timeout=120) as response:
            data = response.read(20 * 1024 * 1024 + 1)
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"{name}: SHA-256 mismatch")
        target.write_bytes(data)
        print(name)
