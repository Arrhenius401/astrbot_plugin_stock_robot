"""提供插件导入路径和 AstrBot 日志替身，隔离框架启动副作用。"""

import logging
import sys
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 独立测试只需要框架 logger，完整框架接口由入口测试按需替换。
api = ModuleType("astrbot.api")
api.logger = logging.getLogger("plugin-test")
sys.modules["astrbot"] = ModuleType("astrbot")
sys.modules["astrbot.api"] = api
