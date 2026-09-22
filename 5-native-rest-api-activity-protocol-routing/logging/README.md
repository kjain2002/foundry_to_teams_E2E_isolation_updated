# Logging & tracing — watch a Teams message flow end-to-end

Set this up **before** you publish so you can see exactly where a message stalls
when you chat with the deployed agent — in particular whether the **custom MCP
OAuth passthrough** ever renders.

## Two layers

| Layer | Source | Table / view | What it tells you |
| --- | --- | --- | --- |
| **A. Channel** | Azure Bot Service diagnostics → Log Analytics | `ABSBotRequests` | Did the Teams message reach the bot (`ChannelToBot`)? Did a reply/card go back (`BotToChannel`)? Did the OAuth **sign-in card** round-trip, or fail with 401/403? |
| **B. Agent** | Foundry **server-side** traces → Application Insights | `dependencies`, `traces`, `exceptions` (also Foundry portal **Traces**) | Model calls, **MCP tool invocations**, and the authorization step inside the agent run. Where the MCP OAuth passthrough actually happens. |

Layer A catches a stall at the channel boundary; Layer B catches a stall inside
the agent / MCP. Together they cover every hop.

## Enable it

```powershell
# after step2-deploy-bot.ps1 (the Bot Service must exist)
cd logging
./enable-observability.ps1
```

This creates/reuses a Log Analytics workspace + workspace-based Application
Insights, and turns on the Bot Service `BotRequest` diagnostic log. Then do the
**one manual step** for Layer B:

> Foundry portal → your **project** → **Observability / Tracing** →
> **Connect Application Insights** → pick your `appi-<project>` instance.

Server-side agent traces (model + MCP tool + auth spans) then flow to that
Application Insights and to the portal **Traces** tab. No app code change is
needed — these are captured server-side for the running agent.

## Watch a message live

```powershell
# window 1 — live channel hops
./watch-live.ps1

# then send a message to the agent in Teams and watch ChannelToBot -> BotToChannel
```

For the agent internals, open [trace-queries.kql](trace-queries.kql) in
Log Analytics / Application Insights **Logs**, or open the **Traces** tab in the
Foundry portal and expand the latest run.

## Diagnosing "MCP OAuth passthrough not rendering"

1. **Channel (A)** — run query **A1/A4** in [trace-queries.kql](trace-queries.kql).
   - No `ChannelToBot` row → the message never reached the bot (check publish / channel).
   - `ChannelToBot` present but a 401/403 around a sign-in `BotToChannel` → the OAuth
     card is being rejected at the channel.
2. **Agent (B)** — run **B2** (is the MCP tool even called?) then **B3** (OAuth/auth signals).
   - MCP dependency missing → the model isn't invoking the tool.
   - MCP dependency present but `success=false` / 401 → passthrough token isn't reaching MCP.
   - An `authorization`/`consent` trace with no follow-up → the consent step didn't render.
3. Grab the `operation_Id` from B1/B2 and run **B5** to see that one conversation in order.

> Tip: server-side traces take ~2–5 min to appear. Content recording of message
> text is on for portal traces; treat the workspace as sensitive.
