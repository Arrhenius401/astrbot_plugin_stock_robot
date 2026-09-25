"""测试环境：把插件目录加入 sys.path，使测试可直接 import client。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
