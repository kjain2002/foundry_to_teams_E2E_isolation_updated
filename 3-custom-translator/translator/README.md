# Translator — Bot Framework ↔ Foundry Agents API

This container sits between Azure Bot Service and Foundry. It accepts the Bot
Framework **Activity Protocol** on `POST /api/messages`, translates each user
turn into the Foundry Agents API multi-step flow (thread → message → run →
poll → fetch reply), and returns the assistant text as a Bot Framework reply.

## Layout
- `app/main.py` — aiohttp entrypoint, `BotFrameworkAdapter`, message handler
- `app/foundry.py` — Foundry SDK wrapper (thread/message/run/poll)
- `app/state.py` — conversationId → threadId store (Azure Table or in-memory)
- `app/config.py` — env-driven settings

## Required env
| Var | Purpose |
|---|---|
| `BOT_APP_ID` | Bot's Entra AppId (used as JWT audience) |
| `BOT_APP_PASSWORD` | Bot's client secret (for SingleTenant bots) |
| `BOT_APP_TENANT_ID` | Bot's home tenant |
| `FOUNDRY_PROJECT_ENDPOINT` | e.g. `https://<your-foundry>.services.ai.azure.com/api/projects/<your-project>` |
| `FOUNDRY_AGENT_NAME` | Prompt Agent name (e.g. `<your-agent>`) |
| `THREAD_TABLE_URL` | (optional) e.g. `https://<your-storage>.table.core.windows.net/` |
| `THREAD_TABLE_NAME` | (optional, default `threads`) |

Auth to Foundry is via the container's **managed identity** (which must hold
`Cognitive Services User` on the Foundry account).

## Local run
```powershell
$env:BOT_APP_ID="..."; $env:BOT_APP_PASSWORD="..."; $env:BOT_APP_TENANT_ID="..."
$env:FOUNDRY_PROJECT_ENDPOINT="https://<your-foundry>.services.ai.azure.com/api/projects/<your-project>"
$env:FOUNDRY_AGENT_NAME="<your-agent>"
pip install -r requirements.txt
python -m app.main
```

## Build & push
```powershell
az acr build -r <acrName> -t translator:$(git rev-parse --short HEAD) .
```
