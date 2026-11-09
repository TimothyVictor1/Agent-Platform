# Frontend

## Mock mode

Run without the backend:

```sh
NEXT_PUBLIC_MOCK=1 pnpm dev
```

Mock mode serves fixture REST data and a scripted session with 3 agents and 200
WebSocket events over 60 seconds. Mock sessions are saved in localStorage under
`agentplatform.mock.sessions` and persist across reloads.
