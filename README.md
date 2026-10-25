# WitX — Developer Preview

简体中文 | [English](README.en.md)

WitX 让你在网页里用自然语言操作自己的 Android 和 iOS 手机。说清楚要做什么，Agent 会看手机截图、安排步骤，再通过手机旁的 Gateway 执行。你可以查看过程、批准敏感操作，也可以暂停任务后亲自接管。

它面向个人自部署、自用。这个 Preview 适合愿意配置服务、连接测试设备的开发者；请先用测试账号体验，并检查重要任务的结果。

## 可以用来做什么

- **完成手机上的多步任务**：查找信息、填写表单、修改设置，也可以把不同子任务交给多台手机。
- **先计划，再执行**：不确定怎么做时，先用计划模式查看方案，再决定是否执行。
- **保留常用方法**：让 Agent 把完成任务的方法整理成 Skill，自己检查、试用和保存，以后再用。
- **保持人在掌控中**：选择任务权限，遇到需要批准的操作时确认，必要时暂停、停止或接管手机。

界面支持英文和简体中文、浅色和深色主题。Agent 跟随你最新消息的语言回复。长对话先显示最近的内容，向上滚动即可查看更早的过程。

## 开始前

你需要 Python 3.12+、uv、Node.js 22+、pnpm 10.33.0 和 PostgreSQL 15+。本仓库包含 Web、云端服务、Gateway、测试和部署模板。

先用下面的**假设备**检查服务是否连通，不必立即连接手机。接入真实设备时，Android 需要 ADB 与 Appium UiAutomator2；iOS 需要 macOS、Xcode 与 Appium XCUITest，真机还需要签名和开发者模式。配置步骤见[开发与部署](docs/development.md#配置与真实设备)。

模型费用由你自己的供应商账户支付。手机截图和任务内容会发送给你配置的模型供应商；请勿使用包含私人消息、支付信息或其他敏感内容的日常手机。

## 先在本机跑起来

### 1. 安装并配置

先按[数据库说明](docs/development.md#数据库)创建数据库和角色，然后在仓库根目录运行：

```sh
make setup
cp .env.example .env
cp services/gateway/gateway.fake.toml gateway.toml
```

编辑 `.env`，填好数据库连接、模型密钥和 `WITX_DEFAULT_MODEL`。本机体验使用 `WITX_AUTH=dev`；模型名称请改成你的账户可以调用、支持视觉和工具调用的型号。

### 2. 初始化并注册

```sh
set -a; . ./.env; set +a
make db-migrate
psql "$WITX_ADMIN_DATABASE_URL" -c 'GRANT witx_app TO witx_app_login'
uv run witx admin add-user --tenant demo --subject alice --name Alice
uv run witx admin add-gateway --tenant demo --name test-host
```

保存最后一条命令打印的一次性注册码，下一步注册 Gateway 时要用。

### 3. 启动服务

在三个终端分别启动以下服务。API 和 Worker 的终端都先运行 `set -a; . ./.env; set +a`，加载配置。

| 终端 | 命令 |
| --- | --- |
| API | `make dev-api` |
| Worker | `make dev-worker` |
| Web | `make dev-web` |

等 API 启动后，在第四个终端注册并启动 Gateway。注册时按提示粘贴注册码，只需做一次。

```sh
uv run witx-gateway --config gateway.toml enroll
make dev-gateway GATEWAY_CONFIG=gateway.toml
```

打开 [http://127.0.0.1:5173](http://127.0.0.1:5173)，使用示例令牌 `dev-token-alice` 登录，在设备页启用假设备。它只会改变合成画面的颜色，用来确认链路正常，不会模拟真实 App。

准备连接手机时，改用[真实设备配置](services/gateway/gateway.example.toml)，按[设备接入说明](docs/development.md#配置与真实设备)检查环境。

## 怎样使用

创建任务时选择手机和模型，写清楚目标、范围，以及哪些事情需要先问你。可以先从读取信息或修改测试数据的小任务开始。

每个任务可以选择权限模式：

| 模式 | 如何处理操作 |
| --- | --- |
| 先问我 | 改动数据前先问你，查看和导航可以直接进行。 |
| 替我审批（默认） | 消息和下单可以直接执行；付款、删除、分享个人数据、修改账户等操作先问你。 |
| 完全访问 | 放开其他操作的逐项审批，付款和转账仍要批准；需要 Owner 先开启。 |

需要手动操作时，先暂停任务，再接管手机。密码、验证码和支付验证由你处理，不要写进对话或交给 Agent。

几个常用入口：

- `/goal`：设置或修改任务目标。
- `/side`：针对当前任务提问，旁问只回答，不操作手机。
- `/skill`：请 Agent 起草本次任务的操作方法。你可以要求拆成多个 Skill，逐份检查后保存。

也可以[安装可选 Plugin](https://github.com/FoLAWy-py/witx-plugins-preview)，给 Agent 提供现成的方法：

```sh
uv run witx plugin install basics
```

安装和更新前会展示内容，由你确认。自己写的 Skill 和 Plugin 都不会自动获得额外权限。

## 使用前要知道

- **结果需要检查。** 模型可能看错、点错或漏掉要求；页面也可能在两次截图之间变化。完成提示不代表每个要求都已满足。
- **遇到“结果未知”先核实。** 设备会等待你凭新截图确认发生了什么，不要直接重复同一操作。停止任务也需要等待设备确认。
- **敏感内容仍可能出现在截图中。** 系统识别的密码框会阻止 Agent 输入，但普通验证码框、自绘控件和截图里的验证码尚未完全保护。
- **删除任务不等于删除所有副本。** 已保存的记忆、Skill 和既有备份需要分别管理。
- **真实设备仍有兼容性限制。** Gateway 宿主支持 macOS 和 Linux；Windows 尚未验收。具体体验受手机系统、Appium 驱动、签名和 App 本身影响。

## 部署、开发与反馈

- [开发与部署](docs/development.md)：真实设备、Docker Compose、HTTPS、升级和备份恢复。对外部署前请配置 OIDC 和 HTTPS；开发令牌只用于本机。
- [架构](docs/architecture.md)：组件如何配合，以及执行、安全和恢复机制。
- `make check`：运行代码检查、测试和构建。

反馈时请说明复现步骤、软件版本和设备平台，并附脱敏后的错误信息。不要上传密钥、真实设备编号、账号、凭据文件或含个人内容的截图。安全问题请优先通过 GitHub 私密漏洞报告联系维护者（若已启用）。

## 许可证

WitX 原创代码与随附文档采用 [Apache License 2.0](LICENSE)，版权信息见 [NOTICE](NOTICE)。软件按“现状”提供，具体保证与责任条款以 LICENSE 为准。第三方依赖保留各自许可；Web 分发附带 [第三方声明](apps/web/public/THIRD_PARTY_NOTICES.txt)。本许可不授予 WitX 名称、标识或其他商标的使用权，法律允许及许可条款规定的情形除外。

除另有明确约定外，主动提交并拟合入本项目的贡献同样按 Apache-2.0 提供。提交者应有权提供相应代码与素材。
