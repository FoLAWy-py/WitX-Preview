# witx-contracts

Wire formats shared by the cloud, the Gateway and the Web app:

- `witx_contracts.gateway`: the WebSocket protocol between Gateway and hub.
- `witx_contracts.api`: request and response models of `/api/v1`.

After changing `api.py`, run `make contracts` to regenerate the TypeScript types.
