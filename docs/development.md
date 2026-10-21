# 开发与部署

## 数据库

需要 PostgreSQL 15+。使用有建库、建角色权限的管理员创建本机开发环境，按你的 PostgreSQL 认证方式设置连接参数：

```sql
CREATE ROLE witx_owner LOGIN CREATEROLE;
CREATE ROLE witx_app_login LOGIN;
CREATE DATABASE witx OWNER witx_owner;
```

`.env` 中迁移 / 管理连接使用 `witx_owner`，应用连接使用 `witx_app_login`。迁移完成后用 owner 执行 `GRANT witx_app TO witx_app_login`。以上示例没有密码，仅适用于你已配置本机认证的开发数据库；实际部署使用独立强密码。应用角色不能拥有表，否则行级安全不能发挥预期作用。

## 命令

| 命令 | 作用 |
| --- | --- |
| `make setup` | 按 uv.lock 和 pnpm-lock.yaml 安装依赖 |
| `make check` | lint、类型检查、测试与构建 |
| `make contracts` | 从 Python API 合同生成 Web 类型 |
| `make db-migrate` | 执行编号迁移 |
| `make dev-api` / `dev-worker` / `dev-web` | 启动服务 |
| `make dev-gateway GATEWAY_CONFIG=gateway.toml` | 启动 Gateway |

每个 API / Worker 终端先运行 `set -a; . ./.env; set +a`。修改前阅读 [AGENTS.md](../AGENTS.md)。

## 测试

Python 测试使用假模型和假设备，不调用付费模型、不操作真机。数据库测试逐个创建隔离的临时库。设置 `WITX_TEST_DATABASE_URL`，或把 `initdb` / `pg_ctl` 加入 PATH，由非 root 测试进程启动临时集群；两者都没有时数据库测试会跳过，不能把跳过当作验证通过。

Web 单元测试用 Vitest，浏览器测试用 Playwright 的合成 API 数据。先安装 Chromium：

```sh
pnpm --filter @witx/web exec playwright install chromium
make check
```

浏览器测试默认启动独立的 Vite 端口 5174。不要让 SOCKS 代理接管本机请求。修改安全机制需要覆盖相应行为的测试。

更新 Web 依赖后运行 `uv run python scripts/third_party_notices.py` 并提交更新后的第三方许可声明；`make check` 会检查它是否匹配已安装依赖。

## 配置与真实设备

配置字段见 [.env.example](../.env.example)；Gateway 见 [gateway.example.toml](../services/gateway/gateway.example.toml)。开发服务监听 loopback。Appium 也应只监听本机，并以 `--log-level warn` 运行，避免日志记录人工接管输入。

Android 安装 ADB、Appium 和 UiAutomator2 driver；iOS 在 macOS 安装 Xcode 与 XCUITest driver，真机还需要开发者模式、信任和 WebDriverAgent 签名。在 Gateway 所在电脑运行：

```sh
witx-gateway --config gateway.toml doctor
witx-gateway --config gateway.toml setup
```

`doctor` 只检查；`setup` 先列出可修复步骤，经本机用户确认后执行。Gateway 自己管理会话和各设备端口。journal 放本地磁盘，凭据保存在系统密钥存储或权限为 0600 的凭据文件中。

## 部署

```sh
cp deploy/.env.example deploy/.env
# 填写数据库密码、模型、OIDC 和公开地址。
docker compose -f deploy/compose.yaml up -d --build
docker compose -f deploy/compose.yaml run --rm api admin add-user --tenant demo --subject YOUR_OIDC_SUBJECT --name Owner
docker compose -f deploy/compose.yaml run --rm api admin add-gateway --tenant demo --name gateway-host
```

Compose 提供 PostgreSQL、迁移、API / Hub、Worker 和 Nginx。按 `deploy/nginx.conf` 的示例配置 TLS、证书挂载及 443 端口，设置正确的 `WITX_PUBLIC_URL`。请勿把开发令牌认证暴露到公网。Gateway 在手机旁的宿主机运行，不在云端容器里；Linux / macOS 服务模板位于 `deploy/gateway`。

截图和 Plugin 默认在私有卷；可配置 S3 兼容对象存储。API 的 `/health/ready` 检查数据库和对象存储，`witx check` 额外检查模型目录可用性；模型列表可用不保证每次推理成功。日志默认结构化并脱敏，`/metrics` 需要配置 token。扩展 API 实例时，人工接管和 MCP 会话需要粘性路由。

## Preview 插件源

内置源 `witx-preview` 对应公开仓库 `https://github.com/FoLAWy-py/witx-plugins-preview.git`，不需要手动添加。安装、查看和更新：

```sh
uv run witx plugin install basics
uv run witx plugin list
uv run witx plugin update basics
```

也可以显式指定 `basics@witx-preview`。安装展示全文，更新展示差异，确认后才生效；锁文件记录 marketplace、commit 和内容摘要。

如果之前从旧的 `witx` 源安装过 `basics`，请先运行 `uv run witx plugin remove basics`，再运行上面的安装命令，检查并确认新的来源。已有安装的来源不会被静默改写。

两个 Preview 仓库的 CI 相互检查：WitX-Preview 检查插件兼容性及内置源名称是否匹配 marketplace；插件仓库使用公开 WitX-Preview main 运行兼容检查。首次配套发布时先推送插件仓库，再推送 WitX-Preview。

## 备份恢复

`deploy/backup.sh` 备份数据库、截图和 Plugin；`deploy/restore.sh <时间戳>` 停止服务、恢复数据并运行 `witx admin recover`。默认备份保留 30 天。请另行安全保全对象存储的 `ledger/` 恢复账本，两个脚本刻意不回滚它；单机磁盘全部丢失时，仅普通备份不足以重建后续删除 / 撤销状态。S3 部署可使用版本控制，并实际演练恢复。

截图、模型请求 / 响应和工具结果可能含个人信息；备份同样敏感。请结合 README 的 Preview 限制设置留存并保护存储。不要向 issue、Git 或镜像构建上下文提交运行数据、密钥、真实设备标识或含隐私的截图。
