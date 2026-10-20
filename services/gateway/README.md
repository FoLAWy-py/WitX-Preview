# witx-gateway

Runs next to the phones, dials out to the cloud and executes admitted input.
Configuration example (iOS Simulator): [gateway.example.toml](gateway.example.toml).

```sh
witx-gateway --config gateway.toml enroll   # once: paste the code from `witx admin add-gateway`
witx-gateway --config gateway.toml
```

Enrolling stores the Gateway's credential in the system key store (Keychain, Credential
Manager, Secret Service), or in `<journal>.credential` (mode 0600) where there is none.
The Gateway rotates it every 30 days by itself.

Supported devices: Android over ADB, iOS through a local Appium server (XCUITest),
and a fake in-memory phone for tests and demos.
