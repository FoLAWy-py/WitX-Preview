# witx-cloud

The WitX cloud: HTTP API, Gateway hub and agent worker. See
[docs/architecture.md](../../docs/architecture.md).

```sh
witx migrate                 # apply migrations (WITX_ADMIN_DATABASE_URL)
witx api                     # API + hub on :8000
witx worker                  # agent worker
witx admin add-user --tenant demo --subject alice --name Alice
witx admin add-gateway --tenant demo --name laptop   # prints a one-time enrollment code
witx admin list-gateways
witx admin disable-gateway <id>   # enable-gateway <id> undoes it
witx admin revoke-gateway <id>    # for good
```
