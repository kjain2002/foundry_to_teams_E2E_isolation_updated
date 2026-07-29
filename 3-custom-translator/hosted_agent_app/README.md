# Hosted-agent chat & verify app (parallel / compatibility build)

A **self-contained** Streamlit app that talks to a **deployed hosted Foundry
agent** through its Responses endpoint, exercises an **MCP data tool** and the
**hosted Code Interpreter**, and **verifies** generated files through the
Foundry container file API.

This folder is a *parallel* implementation. It does **not** modify — and does
not import from — the sibling `../streamlit_app` publisher. If the hosted-agent
approach doesn't fit, the shared-router publisher still runs untouched.

## Architecture

```
User
  └─ Streamlit UI (or Teams/Bot Framework adapter — interfaces stubbed)
       └─ hosted Foundry agent Responses endpoint
            ├─ an MCP data tool (Foundry toolbox)  →  function_call items
            └─ hosted Code Interpreter          →  code_interpreter_call items
                 └─ client-created container (caller identity creates it)
                      └─ generated files via the container file API
```

**Identity model:** the *client* creates the Code Interpreter container using
the caller's identity; the hosted-agent managed identity *uses* the supplied
`USE_CONTAINER_ID=cntr_...` but does not create it.

## Modules

| File | Purpose |
|------|---------|
| `config.py` | Env-driven typed config (endpoints, model, container behaviour). No secrets in code. |
| `auth.py` | `local` (DefaultAzureCredential) or `sso` (Easy Auth + OBO) token provider. |
| `hosted_agent_client.py` | Calls the Responses endpoint; retains full raw JSON, status, errors, timeout. |
| `activity.py` | Parses `response.output[]` into typed items; **evidence over inference**. |
| `containers.py` | Create/reuse container, upload template, list/download files, verify PPTX (`PK` + ZIP). |
| `ppt_skill.py` | PPT skill instructions — **template** vs **no-template** modes. |
| `attachments.py` | Teams attachment abstraction + validation; download hook is stubbed, not faked. |
| `logging_utils.py` | Structured per-turn diagnostics with secret redaction. |
| `app.py` | Streamlit chat UI. |

## Guarantees

- Raw Responses JSON is retained, viewable, and downloadable per turn.
- **MCP tool calls** and **Code Interpreter calls** are separately identified.
- A "file created" claim is shown **only** with container-file evidence
  (a `code_interpreter_call` item and/or a new assistant file that downloads).
- Generated `.pptx` files are verified as real Office packages (`PK` header + ZIP open).
- Secrets (bearer tokens, secrets, Authorization headers, consent-URL query) are
  never logged.

## Run locally

```powershell
# from this folder
copy .env.example .env    # then edit .env
conda run -n agent_to_teams --no-capture-output `
  python -m streamlit run app.py --server.headless true --server.port 8502
```

`AUTH_MODE=local` uses your `az login` session. VPN / private-endpoint access to
the Foundry resource is required to reach the agent + container APIs. For a
custom corporate CA set `CA_BUNDLE_PATH`.

## Tests

```powershell
# from this folder
conda run -n agent_to_teams --no-capture-output python -m pytest tests -q
```

All HTTP is mocked (`httpx.MockTransport`); no live Foundry access is needed.
Coverage: no-template PPT, template-upload PPT, MCP-tool-only, combined
MCP-tool+PPT, failed/timeout, no-tool-activity, CI-activity present, file
list/download, invalid template extension, reset clears template, redaction.

## Remaining integration step — Teams attachment delivery

`attachments.TeamsBotFrameworkDownloader.download` is intentionally **not**
implemented against live Bot Framework/Teams auth (cannot be tested locally).
To enable Teams template input, implement it using the bot connector token
(`MICROSOFT_APP_ID` / `MICROSOFT_APP_PASSWORD`) or a Graph `Files.Read` token to
fetch the attachment `contentUrl`, then reuse `ingest_attachment_to_container`.
The existing Teams flow under `../../publish-agent` is not modified.
