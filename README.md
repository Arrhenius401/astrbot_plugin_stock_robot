# astrbot_plugin_stock_robot

把 [stock_robot](https://github.com/Arrhenius401/stock_robot) 的个股、指数分析封装为 AstrBot 工具，在聊天中发送研报图片。需要 AstrBot 4.27 以上、5.0 以下及 Python 3.11 以上。

## 服务准备

启用插件后立即返回，后台先检查 `base_url/health`。健康的已有服务直接复用，停用插件不会关闭它。

默认地址为 `http://127.0.0.1:25618`。本机回环 HTTP 地址（localhost、127.0.0.1、::1，且没有路径前缀）可自动安装、启动独立服务。其他地址只复用，不在远端安装。`auto_install=false` 时也只复用，不会启动已有但停止的实例。

**当前还未内置已验收的公开归档。** 冷安装需要填写 `source_archive_url`，使用固定提交或标签的 ZIP 地址，包含 `pyproject.toml` 和 `requirements-core.lock.txt`；启用 RAG 时还需要 `requirements-rag.lock.txt`。不能用持续变化的 main.zip。未填写时会明确提示，已有健康服务仍可使用。公开固定归档验收后再设置内置默认值。

安装优先使用 uv，否则使用 Python venv/pip；按带哈希的锁清单安装依赖，再以 `--no-deps` 安装源码。`bootstrap_extras` 留空只装核心依赖，填写 `rag` 安装检索依赖，其他值不接受。RAG 体积较大，首次安装可能超过聊天工具的等待时间。

## 模型配置

首次创建服务配置时，尝试复制 AstrBot 默认 OpenAI 或 Anthropic 模型的当前 Key、模型和 API 地址。Azure 及未确认类型不复制。没有可用模型仍可完成安装，以关闭 LLM 的配置启动；在独立服务 Web UI 中补齐模型即可。

配置仅首次复制。之后修改 AstrBot 模型不会同步到服务，已有服务配置不会被覆盖。代理、自定义请求头等扩展字段不复制，依赖这些字段的模型需要自行在服务侧确认兼容性。

## 配置项

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| base_url | http://127.0.0.1:25618 | 服务地址，同时决定自建服务监听端口 |
| web_url | 空 | 图片失败时的报告库链接；空值使用 base_url |
| timeout_seconds | 100 | 工具流程时间上限，与 AstrBot 工具超时收敛 |
| startup_timeout_seconds | 60 | 单次等待服务就绪的上限，不代表安装总时限 |
| auto_install | true | 允许本机自动准备；false 仅复用 |
| bootstrap_extras | 空 | 空或 rag |
| source_archive_url | 空 | 已验收的固定源码归档 |

准备、分析、取报告和图片渲染共用本次工具时间。等待超时会给出重试提示，后台安装继续；并发调用共用一个准备任务。停用时先取消并等待准备任务，再回收插件创建的进程。

## 数据、日志与恢复

文件位于 AstrBot 的 `data/plugin_data/astrbot_plugin_stock_robot/`：

```text
service.log                  # 安装和服务输出
instance/
  src/                       # 固定源码及 .bootstrap-source.json 来源记录
  venv/                      # 独立环境
  install-state.json         # 程序安装完成标记，独立于模型配置
  .stock_robot/config.yaml   # 服务配置、凭据和其他状态
  reports/                   # 报告
```

失败先检查日志。已安装程序不会因为 Key 缺失或配置错误反复重装。自建进程仍存活但健康检查失败时，保留进程并提示诊断；只有确认退出才允许重启。首次启动超时会回收该次自建进程。

如果报告显示“AI 解读当前不可用”，先检查副本 `.stock_robot/runtime-*.log` 和模型配置。首次复制仅迁移 provider、Key、model、base_url，不复制 AstrBot 的生成参数。包含自动预算改动的新版本默认 `llm.max_tokens: null`：OpenAI 兼容接口省略输出上限，Anthropic 必填接口查询模型能力，信息缺失时回退8192。已有安装中的整数配置（例如2000）会保留；若要切换自动，在服务配置页清空“最大 Token 数”并保存，或将服务副本配置中的该项设为null后停用再启用插件。早期真实 WebChat 验收曾在2000预算下降级、提高到8192后恢复 AI 解读；这不是所有模型的通用推荐上限，自动预算也不能保证所有未知模型首次调用可用。保留配置、安装目录与报告，无需重装程序。

修改 extras 后重载插件，会按对应锁清单补装依赖并更新安装标记；改回核心模式不会自动卸载原有 RAG 包。修改归档地址不会自动升级现有源码。

手工重装或升级时，先停用插件、确认自建进程已退出并备份数据。只移除 `instance/src`、`instance/venv`、`instance/install-state.json` 后重新启用。保留 `.stock_robot`、`reports` 和其他用户数据，不删除整个 instance。

日志会隐藏常见凭据，仍应限制数据目录的访问权限。Windows 的文件模式设置不能代替 ACL。

## 手动部署

也可自行部署 stock_robot 并常驻运行，将插件指向其地址：

```powershell
./.venv/Scripts/stock-robot.exe run --host 127.0.0.1 --port 25618
```

若手机需要打开报告库，在 `web_url` 填可访问的局域网地址。插件默认仅监听回环地址，不自动向局域网开放服务。
