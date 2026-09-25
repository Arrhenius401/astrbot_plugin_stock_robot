# astrbot_plugin_stock_robot

把 [stock_robot](https://github.com/Arrhenius401/stock_robot)（AI 股票分析研报助手）的
个股/指数分析能力封装为 AstrBot 的 LLM 工具：在 QQ 等聊天平台里询问股票，机器人会调用
你本机部署的 stock_robot 服务完成分析，并把完整研报渲染为图片直接发送。

## 前置条件

1. 已部署 stock_robot 并能在本机启动服务（常驻运行，建议加入 Windows 启动项）：

   ```powershell
   ./.venv/Scripts/stock-robot.exe run --host 127.0.0.1 --port 8765
2. AstrBot ≥ 4.27。