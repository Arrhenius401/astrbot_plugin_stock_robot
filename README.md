<p align="center">
  <img src="https://raw.githubusercontent.com/Arrhenius401/astrbot_plugin_stock_robot/master/logo.png" alt="股票分析助手" width="128">
</p>

<h1 align="center">股票分析助手</h1>

<p align="center"><strong>在 AstrBot 聊天中分析股票与指数，直接收取研报图片</strong></p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#怎么使用">使用示例</a> ·
  <a href="#常见问题">常见问题</a> ·
  <a href="https://github.com/Arrhenius401/stock_robot">Stock Robot</a>
</p>

本插件将 [Stock Robot](https://github.com/Arrhenius401/stock_robot) 的分析能力接入 AstrBot。你可以询问一只 A 股或一个指数，机器人会发送准备与分析进度，并将完整研报以图片发到当前聊天。

## 快速开始

需要 **AstrBot 4.27 及以上、5.0 以下**，运行 AstrBot 的 Python 版本须为 **3.11 及以上**。聊天模型需要支持工具调用，并允许使用本插件提供的工具。

### 1. 安装并启用插件

在 AstrBot **插件市场**中搜索“股票分析助手”，安装并启用。

也支持在插件管理中通过[仓库地址](https://github.com/Arrhenius401/astrbot_plugin_stock_robot)安装，适用于上架前体验或手动安装。

保留默认配置，插件会自动在 AstrBot 所在环境中安装并启动 Stock Robot 分析服务。首次安装需要能访问 GitHub 和 Python 依赖下载源。

### 2. 等待首次准备完成

首次准备包括下载最新正式发布版、安装依赖和启动服务，可能需要几分钟。你可以在插件数据目录的 `service.log` 中查看进度；后续启用会直接复用已有安装。

### 3. 发送第一条分析请求

在已启用模型对话的聊天中发送：

> 帮我分析一下平安银行。

机器人会发送分析进度，随后将完整研报以图片发到聊天中。

首次配置会尝试沿用 AstrBot 默认的 OpenAI 或 Anthropic 模型。你也可以在运行 AstrBot 的主机上打开 [分析服务配置页](http://127.0.0.1:25618)，设置用于生成研报的模型。

## 怎么使用

直接告诉机器人你想了解哪只股票或哪个指数，名称、代码都可以：

| 想做什么 | 消息示例 |
| --- | --- |
| 按股票名称 | 帮我分析一下贵州茅台。 |
| 按股票代码 | 分析一下 600519。 |
| 按指数名称 | 沪深 300 最近表现怎么样？帮我生成一份研报。 |
| 按指数代码 | 帮我分析指数 399006。 |

研报由 Stock Robot 生成，并通过 AstrBot 的文字转图能力发送到当前聊天。

## 按需配置

在 AstrBot 的插件配置页修改以下选项，保存后重载插件。

| 配置项 | 默认值 | 什么时候需要改 |
| --- | --- | --- |
| `base_url` | `http://127.0.0.1:25618` | 连接已有服务，或更换本机服务端口。 |
| `web_url` | 留空 | 设置用户能打开的报告库地址；留空使用 `base_url`，仅在图片失败时发送。 |
| `timeout_seconds` | `100` 秒 | 分析经常超时时调整；实际还受 AstrBot 工具调用超时限制。 |
| `startup_timeout_seconds` | `60` 秒 | 调整每次调用等待服务就绪的时间。 |
| `install_timeout_seconds` | `2400` 秒（40 分钟） | 后台安装总时限；慢速首次安装可增大，修改后重载。 |
| `package_index_url` | 留空 | 指定服务器可快速访问的 Python simple 包源，同时用于 uv 和 pip；留空沿用安装工具设置。 |
| `auto_install` | `true` | 关闭后只连接已运行的服务，不安装或启动服务。 |
| `bootstrap_extras` | 留空 | 需要检索依赖时填 `rag`；首次下载较大，通常保持留空。 |
| `source_archive_url` | 留空 | 需要指定固定版本或提交时填源码 ZIP 地址；通常留空使用最新正式发布版。 |

### 设置研报模型

首次配置会尝试复制 AstrBot 默认 OpenAI 或 Anthropic 模型的 API Key、模型名和接口地址。此后，研报模型在 Stock Robot 的 Web 配置页中单独管理；切换 AstrBot 的聊天模型时，研报模型保持原有设置。Azure 模型、代理和自定义请求头等扩展设置需在服务侧配置并确认兼容性。

### 连接已经部署的服务

将 `base_url` 设置为 Stock Robot 的实际地址。插件会复用健康的已有服务，停用插件不会关闭该服务。

如果需要手动启动，进入已安装 Stock Robot 的目录后运行，例如 Windows PowerShell：

```powershell
./.venv/Scripts/stock-robot.exe run --host 127.0.0.1 --port 25618
```

自动准备适用于不带路径前缀的本机回环 HTTP 地址（`localhost`、`127.0.0.1`、`::1`）；连接远程服务时，请先在对应主机完成部署。

**Docker 部署注意：** `127.0.0.1` 指向 AstrBot 容器自身。连接宿主机或另一个容器中的服务时，请使用该容器实际可访问的地址，并确保插件数据目录已持久化。

## 常见问题

### 首次使用提示服务未就绪

首次安装可能需要较长时间，后台准备会继续进行，稍后重新发送请求即可。持续未就绪时，可在 `service.log` 中查看下载、依赖安装和启动情况。

AstrBot 主日志和 `service.log` 会记录 `[1/5] 下载源码`、`[2/5] 创建环境`、`[3/5] 安装依赖`、`[4/5] 检查程序`、`[5/5] 启动服务`。序号表示当前阶段，各阶段耗时不同，不代表完成百分比。准备期间每 30 秒记录总耗时及最近一条脱敏输出；没有新输出也会说明正在等待。

聊天显示“本次等待结束，后台继续准备”时，安装任务仍在运行；“后台安装超时”则表示已达到 `install_timeout_seconds` 上限。错误会包含所在阶段、耗时及可用的最近输出。服务启动仍使用 `startup_timeout_seconds` 作为独立时限。

如果依赖解析或下载很慢，可以设置 `package_index_url`，并按需增大 `install_timeout_seconds`，随后重载插件。已校验源码、虚拟环境和安装工具缓存会保留并复用，复用缓存不保证未下载完的包能按字节续传。成功安装的实例不会因修改包源而自动重装。

### 机器人没有调用插件

分析通过 AstrBot 的模型工具调用触发。请检查当前聊天的模型是否支持工具调用，以及 `analyze_stock`、`analyze_index` 是否已启用。

对于同名标的或股票、指数共用的代码，可以在请求中补充具体名称或类型，帮助模型识别。

### 报告显示“AI 解读当前不可用”

在分析服务的 Web 配置页检查模型、API Key 和接口地址。首次未能读取 AstrBot 默认模型时，也可在这里完成设置。详细原因记录在 `instance/.stock_robot/runtime-*.log` 中。

如果日志提示输出长度不足，支持自动输出预算的新版本可在服务配置页清空“最大 Token 数”并保存。已有配置会保留原来的整数上限，保存新设置后即可使用；实际输出预算由模型与供应商共同决定。

### 分析经常超时

检查插件的 `timeout_seconds` 与 AstrBot 的 `provider_settings.tool_call_timeout`，实际使用两者中较小的值。准备服务、分析、取报告和生成图片共用这一次调用的时间，调整时请同时关注两个超时设置。

### 图片生成失败，或者手机打不开报告库链接

图片生成失败时，插件会发送报告库链接，也可查看 AstrBot 的文字转图日志排查原因。

手机访问需要使用手机可达的服务地址，并将其填入 `web_url`。默认的 `127.0.0.1` 用于本机访问；服务监听地址和网络访问需在部署时另行设置。

## 升级与数据保留

插件与 Stock Robot 服务分别更新。日常重载会保留当前服务版本；需要升级分析服务时，按下方步骤重新安装。

自动安装的数据位于 AstrBot 的 `data/plugin_data/astrbot_plugin_stock_robot/`：

| 路径 | 内容 |
| --- | --- |
| `service.log` | 安装与服务启动日志 |
| `instance/.stock_robot/` | 模型配置、凭据、缓存与运行日志 |
| `instance/reports/` | 已生成的报告 |
| `instance/src/` | Stock Robot 源码 |
| `instance/venv/` | 独立 Python 环境 |
| `instance/install-state.json` | 安装状态记录 |

需要主动升级或重装自动安装的服务时：

1. 停用插件，确认插件创建的服务进程已退出，并备份插件数据目录。
2. 仅移除 `instance/src/`、`instance/venv/` 和 `instance/install-state.json`。
3. 保留 `instance/.stock_robot/` 与 `instance/reports/`，重新启用插件。

`source_archive_url` 留空时会重新获取当时最新正式发布版；填写固定归档地址时会安装指定源码。自行部署的服务请按 Stock Robot 的部署方式升级。

配置目录包含 API Key，分享日志或备份时请隐去凭据。

<details>
<summary>进阶说明：安装来源、可选依赖与服务生命周期</summary>

- 默认选择最新正式 GitHub Release，不包含草稿或预发布；解析后下载固定标签源码。GitHub API 明确限流时，使用官方 latest 页的同仓库标签重定向作为备用，无需 GitHub Token。查询失败不会自动切换到开发分支。
- 自定义归档须包含 `pyproject.toml` 与 `requirements-core.lock.txt`；使用 `rag` 时还须包含 `requirements-rag.lock.txt`。建议使用固定提交或标签 ZIP，避免持续变化的分支归档。
- 安装优先使用 uv，否则使用 Python venv/pip；依赖按带哈希的锁清单安装。将 `bootstrap_extras` 改为 `rag` 后重载会补装依赖，改回留空不会自动卸载已安装的 RAG 包。
- 来源标签、查询渠道和下载摘要记录在 `instance/src/.bootstrap-source.json` 与安装状态文件中。下载摘要是实际内容的记录，不代表发布方签名验证。
- 插件停用时会回收自己创建的服务，保留配置与报告；复用的外部服务不会被关闭。自建进程仍存活但健康检查失败时，应先查看日志诊断，不会自动杀掉该进程并反复重启。

</details>

## 使用边界

个股分析面向 A 股，指数的数据覆盖范围取决于 Stock Robot。公开数据可能延迟或缺失，报告会受到数据源与模型可用性的影响。本工具用于学习与研究，报告不构成投资建议。
