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
- Long-term memory, user-authored Skills (including splitting one task into multiple Skills), and Plugins with reviewable versions and contents. A completed complex task can offer a button to draft a Skill for you to review and save.
- Task and device lists update as their state changes. Long conversations open at recent rounds; scroll up to read earlier ones. Screenshots load as they approach the viewport.
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

iOS text input uses the driver by default. For fields that reject it, the agent can choose on-screen key taps or Paste from the field's menu. Typing Chinese with key taps requires a Simplified Chinese pinyin keyboard on the phone. See [Development and deployment](docs/development.md#ios-文字输入) (Chinese) for configuration and limits.

For real devices, see [gateway.example.toml](services/gateway/gateway.example.toml). Plugins are optional: `uv run witx plugin install basics` fetches content from the [public marketplace](https://github.com/FoLAWy-py/witx-plugins-preview), displays it for review, and installs it after confirmation. The built-in source is named `witx-preview`; `uv run witx plugin install basics@witx-preview` selects it explicitly. There is no need to add the marketplace first. Plugins have their own license notices.

## Working with tasks

Choose the phones, model, and permission mode when creating a task: Ask first, Approve for me (the default), or Full access (which the Owner must first allow). Plan mode inspects and plans before you confirm execution. Payments and transfers require approval of the specific action in every mode.

- Use `/goal` to set or change the task's goal, and `/side` to open a side question that answers without operating the phone. A task without screenshots can answer side questions from its existing text.
- Long tasks open at their most recent part; scroll up for earlier content. Switching tasks follows the new task's own events. Changing the interface language during manual takeover keeps its connection open.
- Use `/skill` to ask the agent to draft how the task was done, or use the button offered under a result. Ask the agent to split a task into multiple Skills if needed; each draft can be reviewed, edited, tried, and saved by you. Plugin installation and updates also require confirmation.

## Safety controls and recovery

- **Approval covers the current parameters.** Before dispatching a sensitive action, WitX recomputes its parameter digest and requires it to match the approval. The Gateway checks the device and screen, then checks the stop barrier, lease, and deadline again before input. A stop or new lease received during these checks prevents the old input from starting.
- **Verify an unknown outcome first.** The same action is not automatically replayed. An input marked UNKNOWN blocks further input on that device until you resolve it using a newer screenshot. Stopping also waits for the Gateway to confirm its barrier.
- **You decide whether to resume after repeated failures.** If the Worker leaves three consecutive steps unfinished, it waits for the user instead of continuing paid retries. Investigate model service, network, or process errors before resuming. Gateway credentials use the local system key store, with an owner-readable credential file as a fallback; rotation loads the credential saved last.

These checks remain subject to the Preview limitations below. Protect screenshots, task content, model call records, and Gateway credentials as sensitive data.

## Deployment and updates

For a single-host deployment, use [Docker Compose and Nginx](docs/development.md#部署) (Chinese). Run the real-device Gateway on the host alongside the phones. Configure OIDC, HTTPS, and model providers before exposing the service externally.

Before updating an existing deployment, back up the database, screenshots, and Plugins, then apply numbered migrations and update the services. Preserve the recovery ledger separately; see [Development and deployment](docs/development.md#备份恢复) (Chinese) for restore steps and limitations. Keep `.env`, `gateway.toml`, credential files, runtime data, and screenshots containing personal information out of Git.

## Known Preview limitations

- Before an input, the Gateway checks device identity, dimensions, rotation, and the foreground app. Page content can still change within the same app, making earlier coordinates invalid.
- Agent input is rejected for fields the operating system identifies as secure. Ordinary plain-text verification-code fields, custom controls, and verification codes visible in screenshots are not fully protected. Use test devices. Pause and take over manually for passwords, verification codes, and payment verification, and keep that information out of model context.
- A fresh screenshot can serve as completion evidence, but does not guarantee that every task requirement has been met. Check important results manually.
- Deleting a task clears its title, pins, related content, and stored responses, and removes its screenshots. Saved memories, Skills, and existing backups have separate lifecycles; deleting a task does not automatically remove them.
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
