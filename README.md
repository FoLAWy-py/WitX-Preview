# WitX — Developer Preview

简体中文 | [English](README.en.md)

WitX 是供个人自部署、自用的手机操作 Agent。你在网页里提出请求，云端 Agent 根据真实截图操作自己的 Android / iOS 手机，手机旁的 Gateway 通过 Appium 执行输入。

本仓库包含云端、Gateway、Web、共享协议、测试与部署模板。Preview 适合开发者在测试设备上、有人监督的情况下体验；任务正确率、设备兼容性和性能仍在改进。

```text
浏览器 ── HTTPS / SSE ── API + Hub ── PostgreSQL
                          │              ▲
                          │           Worker / Agent
                          │
                          └── WSS ── Gateway ── Appium ── Android / iOS
```

## 能做什么

- 对话任务、计划模式、多模型切换、定时任务与多设备子任务。
- 三档权限模式、独立风险判定、具体动作审批与执行审计。
- 暂停、停止、人工接管，以及断线和进程重启后的状态恢复。
- 长期记忆、自己编写的 Skill（可将一个任务拆成多个 Skill），以及可检查版本和内容的 Plugin。
- 长任务按轮次逐段查看，向上滚动可继续读取更早的对话。
- 英文 / 简体中文、浅色 / 深色界面。

## 开始前

需要 Python 3.12+、uv、Node 22+、pnpm 10.33.0 和 PostgreSQL 15+。真实 Android 设备需要 ADB 和 Appium UiAutomator2；iOS 需要 macOS、Xcode 和 Appium XCUITest。先用下方假设备例子检查服务连通性，再连接测试手机。

模型调用由你配置的供应商账户计费。`.env.example` 的模型名称是配置示例，请改为你的账户可以调用、支持视觉和工具调用的模型。截图和任务上下文会发给你允许的模型供应商。

## 本地启动

准备数据库角色和数据库，步骤见 [开发与部署](docs/development.md#数据库)。然后在仓库根目录运行：

```sh
make setup
cp .env.example .env
# 编辑 .env：数据库连接、OPENAI_API_KEY 和 WITX_DEFAULT_MODEL。
set -a; . ./.env; set +a
make db-migrate
psql "$WITX_ADMIN_DATABASE_URL" -c 'GRANT witx_app TO witx_app_login'
uv run witx admin add-user --tenant demo --subject alice --name Alice
uv run witx admin add-gateway --tenant demo --name test-host
```

最后一条命令打印一次性注册码。复制假设备配置，然后注册：

```sh
cp services/gateway/gateway.fake.toml gateway.toml
```

分别在四个终端运行（API 和 Worker 的终端也需要先加载 `.env`）：

```sh
make dev-api
make dev-worker
make dev-web
# API 启动后，在 Gateway 终端先注册一次，按提示粘贴注册码：
uv run witx-gateway --config gateway.toml enroll
make dev-gateway GATEWAY_CONFIG=gateway.toml
```

打开 http://127.0.0.1:5173，以 `.env.example` 中的开发令牌 `dev-token-alice` 登录，在设备页启用假设备。假设备仅用于检查链路，每次输入只改变合成画面的颜色，不模拟真实 App。

iOS 输入默认使用驱动写入；遇到不接受该方式的输入框，可由 Agent 选择逐键输入或通过输入框菜单粘贴。逐键输入中文需要手机有简体拼音键盘，适用范围与配置见 [开发与部署](docs/development.md#ios-文字输入)。

真实设备配置见 [gateway.example.toml](services/gateway/gateway.example.toml)。Plugin 可选：`uv run witx plugin install basics` 会从[公开 marketplace](https://github.com/FoLAWy-py/witx-plugins-preview)获取并展示内容，确认后安装；内置源名称为 `witx-preview`，也可显式运行 `uv run witx plugin install basics@witx-preview`，无需先添加 marketplace。Plugin 具有独立的许可声明。

## Preview 的已知限制

- Gateway 输入前核对设备身份、尺寸、旋转和前台 App，同一个 App 内的页面内容变化仍可能让旧坐标失效。
- 系统标识的安全输入框会拒绝 Agent 输入；普通明文验证码框、自绘控件以及截图中显示的验证码尚未得到完整保护。请在测试设备上使用，涉及密码、验证码和支付验证时暂停并人工接管，避免把这些内容交给模型。
- 新截图可以作为完成证据，但不能保证任务的每个要求都已满足。关键结果需要人工检查。
- 删除任务会清除标题、置顶、相关内容及存储的响应，并清理对应截图；已保存的记忆、Skill 和既有备份有独立生命周期，删除任务不会自动删除它们。
- Gateway 宿主支持 macOS 和 Linux；Windows 尚未验收。真实设备仍受 Appium、驱动、签名和 App 行为影响。

付款和转账按策略要求对具体动作批准。对真实账户和有外部副作用的任务，请保持人工监督。开发令牌只用于本机测试；对外部署请配置 OIDC 与 HTTPS。

## 文档与验证

- [架构](docs/architecture.md)：组件、动作生命周期和安全机制。
- [开发与部署](docs/development.md)：配置、测试、Compose、备份恢复和反馈。
- `make check`：lint、严格类型检查、Python / Web / 浏览器测试和构建。

反馈时请附复现步骤、软件版本、设备平台和脱敏错误信息。提交前移除密钥、设备标识、账号、截图中的个人内容及人工接管输入。发现安全问题时，请先通过 GitHub 的私密漏洞报告入口联系维护者（若已启用），不要把敏感细节贴入公开 issue。

## 许可证

WitX 原创代码与随附文档采用 [Apache License 2.0](LICENSE)，版权信息见 [NOTICE](NOTICE)。软件按“现状”提供，具体保证与责任条款以 LICENSE 为准。第三方依赖保留各自许可；Web 分发附带 [第三方声明](apps/web/public/THIRD_PARTY_NOTICES.txt)。本许可不授予 WitX 名称、标识或其他商标的使用权，法律允许及许可条款规定的情形除外。

除另有明确约定外，主动提交并拟合入本项目的贡献同样按 Apache-2.0 提供。提交者应有权提供相应代码与素材。
