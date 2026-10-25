# WitX — Developer Preview

[简体中文](README.md) | English

WitX lets you operate your own Android and iOS phones through a conversation in your browser. Describe what you want to do, and the agent reads screenshots, plans the steps, and carries them out through a Gateway beside the phones. You can follow its progress, approve sensitive actions, or pause a task and take over yourself.

WitX is for personal, self-hosted use. This Preview is intended for developers who are comfortable setting up services and connecting test devices. Start with test accounts and check important results yourself.

## What you can do

- **Work through tasks on a phone:** find information, fill in forms, change settings, or assign separate parts of a task to several phones.
- **Plan before acting:** use plan mode to review an approach before deciding to run it.
- **Keep useful methods:** ask the agent to turn a completed task into a Skill that you can review, try, save, and use again.
- **Stay in control:** choose a permission mode, review actions that need approval, and pause, stop, or take over when needed.

The interface supports English and Simplified Chinese, with light and dark themes. The agent follows the language of your latest message. Long conversations open at their recent content; scroll up to read earlier steps.

## Before you start

You need Python 3.12+, uv, Node.js 22+, pnpm 10.33.0, and PostgreSQL 15+. This repository includes the web app, cloud service, Gateway, tests, and deployment templates.

Use the **fake device** below to check connectivity before connecting a phone. Real Android devices need ADB and Appium UiAutomator2. iOS needs macOS, Xcode, and Appium XCUITest; physical devices also need signing and Developer Mode. See [Development and deployment](docs/development.md#配置与真实设备) (Chinese) for setup.

Model calls are billed to your own provider account. Screenshots and task content are sent to the model providers you configure. Avoid using an everyday phone containing private messages, payment information, or other sensitive content.

## Run it locally

### 1. Install and configure

Create the database and roles using the [database instructions](docs/development.md#数据库) (Chinese), then run these commands from the repository root:

```sh
make setup
cp .env.example .env
cp services/gateway/gateway.fake.toml gateway.toml
```

Edit `.env` with your database connections, model key, and `WITX_DEFAULT_MODEL`. Use `WITX_AUTH=dev` for local testing. Choose a model your account can access that supports vision and tool calling.

### 2. Initialize and enroll

```sh
set -a; . ./.env; set +a
make db-migrate
psql "$WITX_ADMIN_DATABASE_URL" -c 'GRANT witx_app TO witx_app_login'
uv run witx admin add-user --tenant demo --subject alice --name Alice
uv run witx admin add-gateway --tenant demo --name test-host
```

Keep the one-time enrollment code printed by the last command. You will use it to enroll the Gateway in the next step.

### 3. Start the services

Start each service in a separate terminal. In both the API and Worker terminals, first run `set -a; . ./.env; set +a` to load the configuration.

| Terminal | Command |
| --- | --- |
| API | `make dev-api` |
| Worker | `make dev-worker` |
| Web | `make dev-web` |

Once the API is running, enroll and start the Gateway in a fourth terminal. Paste the enrollment code when prompted; enrollment is only needed once.

```sh
uv run witx-gateway --config gateway.toml enroll
make dev-gateway GATEWAY_CONFIG=gateway.toml
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173), sign in with the example token `dev-token-alice`, and enable the fake device on the Devices page. It changes the color of a synthetic screen to confirm connectivity; it does not simulate real apps.

When you are ready to connect a phone, use the [real-device configuration](services/gateway/gateway.example.toml) and follow the [device setup instructions](docs/development.md#配置与真实设备) (Chinese).

## Using WitX

Choose the phones and model when creating a task, then describe your goal, its scope, and anything the agent should ask you about first. Start with a small task that reads information or changes test data.

Each task has a permission mode:

| Mode | How actions are handled |
| --- | --- |
| Ask first | Ask before changing data; viewing and navigation can proceed. |
| Approve for me (default) | Messages and orders can proceed; payments, deletion, sharing personal data, account changes, and other sensitive actions need approval. |
| Full access | Remove per-action approval for other operations; payments and transfers still need approval. The Owner must enable it first. |

To operate the phone yourself, pause the task and take over. Handle passwords, verification codes, and payment verification yourself; keep them out of the conversation and away from the agent.

A few useful commands:

- `/goal`: set or change the task's goal.
- `/side`: ask about the current task without operating the phone.
- `/skill`: ask the agent to draft how the task was done. You can request several Skills and review each one before saving.

You can also [install an optional Plugin](https://github.com/FoLAWy-py/witx-plugins-preview) to give the agent an existing method to follow:

```sh
uv run witx plugin install basics
```

Installation and updates show their content for your confirmation. Neither your own Skills nor Plugins automatically gain extra permissions.

## Things to know

- **Check the results.** Models can misread a screen, tap the wrong place, or miss a requirement. Pages can change between screenshots. A completion message does not prove that every requirement was met.
- **Verify an unknown outcome first.** The device waits for you to resolve what happened using a newer screenshot. Do not simply repeat the same operation. Stopping a task also waits for the device to confirm.
- **Sensitive content can still appear in screenshots.** The agent cannot type into fields identified by the system as password fields, but ordinary code fields, custom controls, and codes visible in screenshots are not fully protected.
- **Deleting a task does not remove every copy.** Saved memories, Skills, and existing backups need to be managed separately.
- **Real-device compatibility varies.** Gateway hosts support macOS and Linux; Windows has not been validated. Behavior also depends on the phone OS, Appium drivers, signing, and the apps themselves.

## Deployment, development, and feedback

- [Development and deployment](docs/development.md) (Chinese): real devices, Docker Compose, HTTPS, upgrades, and backup and restore. Configure OIDC and HTTPS before external deployment; development tokens are for local use only.
- [Architecture](docs/architecture.md) (Chinese): how the components work together, including execution, safety, and recovery.
- `make check`: run code checks, tests, and builds.

For an issue, include reproduction steps, software versions, the device platform, and redacted error details. Do not upload keys, real device identifiers, accounts, credential files, or screenshots with personal content. For security issues, use GitHub's private vulnerability reporting feature if enabled.

## License

Original WitX code and accompanying documentation are licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for copyright information. The software is provided on an "AS IS" basis; see LICENSE for the applicable warranty and liability terms. Third-party dependencies retain their own licenses. Web distributions include [third-party notices](apps/web/public/THIRD_PARTY_NOTICES.txt). This license does not grant rights to use the WitX name, logo, or other trademarks, except as permitted by law and the license terms.

Unless explicitly agreed otherwise, contributions intentionally submitted for inclusion in this project are also provided under Apache-2.0. Contributors must have the right to submit the code and materials they provide.
