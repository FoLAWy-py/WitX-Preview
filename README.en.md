# WitX — Developer Preview

[简体中文](README.md) | English

WitX is a self-hosted phone-use agent for personal use. Describe a task in the web app, and the cloud agent uses real screenshots to operate your Android or iOS phones. A Gateway running alongside the phones executes inputs through Appium.

This repository contains the cloud service, Gateway, web app, shared contracts, tests, and deployment templates. The Preview is intended for developers using test devices under human supervision. Task accuracy, device compatibility, and performance are still being improved.

```text
Browser ── HTTPS / SSE ── API + Hub ── PostgreSQL
                            │              ▲
                            │           Worker / Agent
                            │
                            └── WSS ── Gateway ── Appium ── Android / iOS
```

## Features

- Conversational tasks, plan mode, model switching, scheduled tasks, and subtasks across multiple devices.
- Three permission modes, independent risk assessment, approval of specific actions, and execution audit records.
- Pause, stop, manual takeover, and state recovery after disconnects or process restarts.
- Long-term memory, user-authored Skills, and Plugins with reviewable versions and contents.
- English and Simplified Chinese, with light and dark themes.

## Prerequisites

You need Python 3.12+, uv, Node 22+, pnpm 10.33.0, and PostgreSQL 15+. Real Android devices require ADB and Appium UiAutomator2. iOS requires macOS, Xcode, and Appium XCUITest. Start with the fake-device example below to check connectivity before connecting a test phone.

Model calls are billed to the provider account you configure. The model names in `.env.example` are configuration examples: replace them with models your account can access that support vision and tool calling. Screenshots and task context are sent to the model providers you allow.

## Run locally

Create the database roles and database as described in [Development and deployment](docs/development.md#数据库) (Chinese). Then run the following from the repository root:

```sh
make setup
cp .env.example .env
# Edit .env: database connections, OPENAI_API_KEY, and WITX_DEFAULT_MODEL.
set -a; . ./.env; set +a
make db-migrate
psql "$WITX_ADMIN_DATABASE_URL" -c 'GRANT witx_app TO witx_app_login'
uv run witx admin add-user --tenant demo --subject alice --name Alice
uv run witx admin add-gateway --tenant demo --name test-host
```

The last command prints a one-time enrollment code. Copy the fake-device configuration before enrolling:

```sh
cp services/gateway/gateway.fake.toml gateway.toml
```

Run the services in four separate terminals. Load `.env` in the API and Worker terminals before starting them:

```sh
make dev-api
make dev-worker
make dev-web
# After the API starts, enroll once in the Gateway terminal and paste the code:
uv run witx-gateway --config gateway.toml enroll
make dev-gateway GATEWAY_CONFIG=gateway.toml
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173), sign in with the development token `dev-token-alice` from `.env.example`, and enable the fake device on the Devices page. The fake device only checks the service connection: each input changes the color of a synthetic screen. It does not simulate real apps.

For real devices, see [gateway.example.toml](services/gateway/gateway.example.toml). Plugins are optional: `uv run witx plugin install basics` fetches content from the [public marketplace](https://github.com/FoLAWy-py/witx-plugins-preview), displays it for review, and installs it after confirmation. The built-in source is named `witx-preview`; `uv run witx plugin install basics@witx-preview` selects it explicitly. There is no need to add the marketplace first. Plugins have their own license notices.

## Known Preview limitations

- Before an input, the Gateway checks device identity, dimensions, rotation, and the foreground app. Page content can still change within the same app, making earlier coordinates invalid.
- Agent input is rejected for fields the operating system identifies as secure. Ordinary plain-text verification-code fields, custom controls, and verification codes visible in screenshots are not fully protected. Use test devices. Pause and take over manually for passwords, verification codes, and payment verification, and keep that information out of model context.
- A fresh screenshot can serve as completion evidence, but does not guarantee that every task requirement has been met. Check important results manually.
- A deleted task's tombstone may still retain its title. Saved memories, Skills, and existing backups have separate lifecycles. Do not put private information requiring strict erasure into this Preview.
- Gateway hosts support macOS and Linux. Windows has not been validated. Real-device behavior also depends on Appium, drivers, signing, and the apps themselves.

The policy requires approval of specific payment and transfer actions. Keep human supervision when using real accounts or running tasks with external side effects. Development tokens are for local testing only; configure OIDC and HTTPS for externally accessible deployments.

## Documentation and verification

- [Architecture](docs/architecture.md) (Chinese): components, action lifecycle, and safety mechanisms.
- [Development and deployment](docs/development.md) (Chinese): configuration, testing, Compose, backup and recovery, and feedback.
- `make check`: linting, strict type checks, Python tests, web unit tests, browser tests, and builds.

When reporting an issue, include reproduction steps, software versions, the device platform, and redacted error details. Remove keys, device identifiers, account details, personal information in screenshots, and manual-takeover input before submitting. For security issues, contact the maintainer through GitHub's private vulnerability reporting feature if enabled; do not post sensitive details in a public issue.

## License

Original WitX code and accompanying documentation are licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for copyright information. The software is provided on an "AS IS" basis; see LICENSE for the applicable warranty and liability terms. Third-party dependencies retain their own licenses. Web distributions include [third-party notices](apps/web/public/THIRD_PARTY_NOTICES.txt). This license does not grant rights to use the WitX name, logo, or other trademarks, except as permitted by law and the license terms.

Unless explicitly agreed otherwise, contributions intentionally submitted for inclusion in this project are also provided under Apache-2.0. Contributors must have the right to submit the code and materials they provide.
