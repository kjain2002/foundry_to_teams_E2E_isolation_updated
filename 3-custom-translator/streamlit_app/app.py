"""Streamlit UI — pick a Foundry agent, publish it to Teams via APIM.

Step order:
    welcome -> sub -> acct -> proj -> agent -> details -> publish -> done

Each step shows ONE selector + Back/Next. Discovery is cached in session_state.

Auth: Container Apps Easy Auth + OBO in prod (AUTH_MODE=sso), or
DefaultAzureCredential locally (AUTH_MODE=local).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st

from auth import get_current_user, get_token_provider
from config import get_config
from discovery import (
    FoundryAccount,
    FoundryProject,
    list_agents,
    list_foundry_accounts,
    list_projects,
    list_subscriptions,
)
from publisher import (
    PublishInputs,
    publish,
    validate_bot_short_name,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ─── Page setup ─────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Foundry → Teams Publisher",
    page_icon="🟣",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
      :root { --primary: #8661c5; }
      .stButton>button[kind="primary"] { background-color: var(--primary); border-color: var(--primary); }
      .stProgress > div > div > div > div { background-color: var(--primary); }
      .step-badge { display:inline-block; background:#f3eefd; color:#5c2e91;
        border-radius:999px; padding:2px 10px; font-size:12px; font-weight:600; margin-bottom:8px; }
    </style>
    """,
    unsafe_allow_html=True,
)


STEPS = ["welcome", "sub", "acct", "proj", "agent", "details", "publish", "done"]
LABELED_STEPS = ["sub", "acct", "proj", "agent", "details"]


def _step_label(step: str) -> str:
    if step not in LABELED_STEPS:
        return ""
    n = LABELED_STEPS.index(step) + 1
    return f"Step {n} of {len(LABELED_STEPS)}"


DEFAULTS: dict = {
    "step": "welcome",
    "subscription_id": None,
    "subscription_label": None,
    "account": None,
    "project": None,
    "selected_agent": None,
    "display_name": "",
    "bot_short_name": "",
    "description_short": "",
    "description_full": "",
    "developer_name": "Platform Team",
    "developer_website": "https://example.com",
    "developer_privacy": "https://example.com/privacy",
    "developer_terms": "https://example.com/terms",
    "publish_result": None,
    "_subs": None,
    "_accounts": {},
    "_projects": {},
    "_agents": {},
}
for k, v in DEFAULTS.items():
    st.session_state.setdefault(k, v)


def _reset() -> None:
    for k, v in DEFAULTS.items():
        st.session_state[k] = v


def _go(step: str) -> None:
    st.session_state["step"] = step


def _next() -> None:
    cur = STEPS.index(st.session_state["step"])
    if cur + 1 < len(STEPS):
        _go(STEPS[cur + 1])


def _back() -> None:
    cur = STEPS.index(st.session_state["step"])
    if cur - 1 >= 0:
        _go(STEPS[cur - 1])


def _nav(can_proceed: bool = True, primary_label: str = "Next") -> None:
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("Back", use_container_width=True,
                     key=f"back_{st.session_state['step']}"):
            _back(); st.rerun()
    with col2:
        if st.button(primary_label, type="primary", use_container_width=True,
                     disabled=not can_proceed,
                     key=f"next_{st.session_state['step']}"):
            _next(); st.rerun()


# ─── Sign-in gate ───────────────────────────────────────────────────────────

user = get_current_user()
cfg = get_config()

if not user:
    st.title("Foundry → Teams Publisher")
    st.warning(
        "You're not signed in. Container Apps Easy Auth should redirect you to "
        "Microsoft sign-in automatically. If you're seeing this in local dev, "
        "set `AUTH_MODE=local` in your `.env` file and run `az login`."
    )
    st.stop()


# ─── Header ─────────────────────────────────────────────────────────────────

header_left, header_right = st.columns([3, 1])
with header_left:
    st.title("Publish a Foundry agent to Teams")
    st.caption("Pick a published Foundry agent → we'll wire it through APIM, "
               "create the Bot Service, and hand you a Teams sideload package.")
with header_right:
    st.markdown(f"**{user.name}**")
    st.caption(user.upn)
    if st.button("Restart", use_container_width=True):
        _reset(); st.rerun()

st.divider()

token_provider = get_token_provider()
step = st.session_state["step"]
if (label := _step_label(step)):
    st.markdown(f'<span class="step-badge">{label}</span>', unsafe_allow_html=True)


# ─── Screens ────────────────────────────────────────────────────────────────

# welcome ────────────────────────────────────────────────────────────────────
if step == "welcome":
    st.subheader("Get started")
    st.write(
        "Replacement for the Foundry portal's broken **Publish ▸ Publish to Teams** "
        "button when your Foundry account has `publicNetworkAccess = Disabled`. "
        "You'll need **Azure AI Developer** on the project plus "
        "**Application.ReadWrite.OwnedBy** on Microsoft Graph (delegated)."
    )

    st.info(
        f"**Pre-configured for this tenant**\n\n"
        f"- Subscription / RG  : `{cfg.subscription_id}` / `{cfg.resource_group}`\n"
        f"- APIM bridge        : `{cfg.apim_name}` ({cfg.foundry_bot_api_name})\n\n"
        "Bot Service + APIM operation are deployed into this RG. Foundry agent "
        "discovery still works across all subscriptions you can read."
    )
    if st.button("Start", type="primary"):
        _next(); st.rerun()


# sub — subscription ─────────────────────────────────────────────────────────
elif step == "sub":
    st.subheader("Select a subscription")
    if st.session_state["_subs"] is None:
        with st.spinner("Loading subscriptions you can access…"):
            st.session_state["_subs"] = list_subscriptions(token_provider)
    subs = st.session_state["_subs"]

    if not subs:
        st.error("No subscriptions found. You need Reader on at least one.")
        st.stop()

    sub_map = {s["displayName"]: s["subscriptionId"] for s in subs}
    labels = list(sub_map.keys())
    default_idx = (
        labels.index(st.session_state["subscription_label"])
        if st.session_state["subscription_label"] in labels else 0
    )
    sub_label = st.selectbox("Subscription", labels, index=default_idx)
    st.session_state["subscription_label"] = sub_label
    st.session_state["subscription_id"] = sub_map[sub_label]
    _nav()


# acct — Foundry account ─────────────────────────────────────────────────────
elif step == "acct":
    st.subheader("Select a Foundry account")
    st.caption(f"Subscription: `{st.session_state['subscription_label']}`")
    sub_id = st.session_state["subscription_id"]
    if sub_id not in st.session_state["_accounts"]:
        with st.spinner("Discovering Foundry accounts…"):
            st.session_state["_accounts"][sub_id] = list_foundry_accounts(
                token_provider, sub_id
            )
    accounts = st.session_state["_accounts"][sub_id]

    if not accounts:
        st.warning("No Foundry (AIServices) accounts found in this subscription.")
        if st.button("Back"): _back(); st.rerun()
        st.stop()

    acc_map = {f"{a.name}  ({a.location})": a for a in accounts}
    labels = list(acc_map.keys())
    cur = st.session_state["account"]
    default_idx = next(
        (i for i, a in enumerate(accounts) if cur and a.id == cur.id), 0
    )
    acc_label = st.selectbox("Foundry account", labels, index=default_idx)
    st.session_state["account"] = acc_map[acc_label]
    _nav()


# proj — project ─────────────────────────────────────────────────────────────
elif step == "proj":
    st.subheader("Select a project")
    account: FoundryAccount = st.session_state["account"]
    st.caption(f"Account: `{account.name}` ({account.location})")
    if account.id not in st.session_state["_projects"]:
        with st.spinner("Loading projects…"):
            st.session_state["_projects"][account.id] = list_projects(
                token_provider, account
            )
    projects = st.session_state["_projects"][account.id]

    if not projects:
        st.warning("No projects in this account.")
        if st.button("Back"): _back(); st.rerun()
        st.stop()

    proj_map = {p.name: p for p in projects}
    labels = list(proj_map.keys())
    cur = st.session_state["project"]
    default_idx = labels.index(cur.name) if cur and cur.name in labels else 0
    proj_label = st.selectbox("Project", labels, index=default_idx)
    st.session_state["project"] = proj_map[proj_label]
    _nav()


# agent — pick from existing ────────────────────────────────────────────────
elif step == "agent":
    st.subheader("Pick the agent to publish")
    project: FoundryProject = st.session_state["project"]
    st.caption(f"Project: `{project.name}`")
    if project.endpoint not in st.session_state["_agents"]:
        with st.spinner("Loading agents…"):
            try:
                st.session_state["_agents"][project.endpoint] = list_agents(
                    token_provider, project
                )
            except Exception as exc:
                st.error(f"Couldn't list agents: {exc}")
                st.stop()
    agents = st.session_state["_agents"][project.endpoint]

    if not agents:
        st.warning("No agents in this project yet. Create one in Foundry first.")
        if st.button("Back"): _back(); st.rerun()
        st.stop()

    labels = [f"{a.name}  ({a.id})" for a in agents]
    cur = st.session_state["selected_agent"]
    default_idx = next(
        (i for i, a in enumerate(agents) if cur and a.id == cur.id), 0
    )
    sel = st.selectbox("Agent", labels, index=default_idx)
    st.session_state["selected_agent"] = agents[labels.index(sel)]
    st.caption("APIM will route to this agent's runtime id.")
    _nav()


# details — bot naming / branding ────────────────────────────────────────────
elif step == "details":
    st.subheader("Teams bot details")
    agent = st.session_state["selected_agent"]
    st.caption(f"Publishing agent: `{agent.name}` ({agent.id})")

    st.session_state["display_name"] = st.text_input(
        "Display name (Teams full name, shown to users)",
        value=st.session_state["display_name"] or agent.name,
        placeholder="e.g. Sales Copilot",
        help="Becomes the Teams manifest name.full (max 100 characters).",
    )
    display_err = (
        "Display name must be ≤ 100 characters (Teams name.full limit)."
        if len(st.session_state["display_name"]) > 100
        else None
    )
    if display_err:
        st.warning(display_err)
    st.session_state["bot_short_name"] = st.text_input(
        "Bot short name (lowercase, hyphens — Teams short name + Azure resource names)",
        value=st.session_state["bot_short_name"]
            or agent.name.lower().replace("_", "-").replace(" ", "-"),
        placeholder="e.g. sales-copilot",
        help="3–30 chars, lowercase letters / digits / dashes, start with a "
             "letter, end with letter or digit. Used as the Teams app short "
             "name (name.short, max 30) and for Azure resource names.",
    )
    name_err = validate_bot_short_name(st.session_state["bot_short_name"])
    if st.session_state["bot_short_name"] and name_err:
        st.warning(name_err)

    st.session_state["description_short"] = st.text_input(
        "Short description",
        value=st.session_state["description_short"]
            or f"Chat with {st.session_state['display_name']}",
    )
    st.session_state["description_full"] = st.text_area(
        "Full description",
        value=st.session_state["description_full"]
            or f"A Teams bot that connects to {st.session_state['display_name']}, "
               "hosted privately in Azure AI Foundry.",
        height=80,
    )
    with st.expander("Developer info (Teams manifest)", expanded=False):
        st.session_state["developer_name"] = st.text_input(
            "Developer name", value=st.session_state["developer_name"])
        st.session_state["developer_website"] = st.text_input(
            "Website URL", value=st.session_state["developer_website"])
        st.session_state["developer_privacy"] = st.text_input(
            "Privacy URL", value=st.session_state["developer_privacy"])
        st.session_state["developer_terms"] = st.text_input(
            "Terms URL", value=st.session_state["developer_terms"])

    can_proceed = bool(
        st.session_state["display_name"]
        and st.session_state["bot_short_name"]
        and not name_err
        and not display_err
    )
    _nav(can_proceed=can_proceed, primary_label="Publish")


# publish — execute ─────────────────────────────────────────────────────────
elif step == "publish":
    st.subheader("Publishing…")
    agent = st.session_state["selected_agent"]
    project: FoundryProject = st.session_state["project"]

    inputs = PublishInputs(
        agent_id=agent.id,
        display_name=st.session_state["display_name"],
        bot_short_name=st.session_state["bot_short_name"],
        description_short=st.session_state["description_short"],
        description_full=st.session_state["description_full"],
        developer_name=st.session_state["developer_name"],
        developer_website=st.session_state["developer_website"],
        developer_privacy=st.session_state["developer_privacy"],
        developer_terms=st.session_state["developer_terms"],
    )

    with st.status("Publishing your agent", expanded=True) as status:
        progress_log = st.empty()
        current_line = st.empty()
        history: list[str] = []

        def _progress(msg: str) -> None:
            history.append(msg)
            progress_log.markdown("\n".join(f"- {m}" for m in history[:-1]))
            current_line.info(f"⏳ {msg}")

        try:
            result = publish(
                token_provider=token_provider,
                foundry_project_name=project.name,
                foundry_project_endpoint=project.endpoint,
                inputs=inputs,
                progress=_progress,
            )
            progress_log.markdown("\n".join(f"- {m}" for m in history))
            current_line.empty()
            st.session_state["publish_result"] = result
            status.update(label="Published!", state="complete", expanded=False)
            _next(); st.rerun()
        except Exception as exc:
            logger.exception("Publish failed")
            status.update(label="Publish failed", state="error", expanded=True)
            st.error(f"**Error:** {exc}")
            if st.button("Back to details"):
                _back(); st.rerun()


# done ─────────────────────────────────────────────────────────────────────
elif step == "done":
    result = st.session_state["publish_result"]
    st.success(f"✅ Bot `{result.bot_name}` is wired up and the Teams app is ready.")

    st.markdown("### Sideload the Teams app")
    st.download_button(
        label=f"⬇️ Download {result.teams_zip_name}",
        data=result.teams_zip_bytes,
        file_name=result.teams_zip_name,
        mime="application/zip",
        type="primary",
        use_container_width=True,
    )
    st.caption("Upload via Teams → Apps → Manage your apps → Upload an app → "
               "Upload a custom app. Or push to org-wide via Teams Admin Center.")

    st.markdown("### Resources created")
    st.code(
        f"Bot Service     : {result.bot_name}\n"
        f"Bot AppId       : {result.bot_app_id}\n"
        f"Messaging URL   : {result.endpoint}\n"
        f"Secret in KV    : {result.secret_kv_name} / {result.secret_kv_secret_name}\n"
        f"Secret URI      : {result.secret_kv_uri}\n"
        f"Secret expires  : {result.secret_end_date}",
        language="text",
    )

    st.info(
        "✅ The bot's client secret was written directly to Key Vault — it never "
        "left the server process and is **not** displayed here. "
        f"Reference it from any downstream app as "
        f"`{result.secret_kv_uri}` (Key Vault Secrets User role required).\n\n"
        "Foundry's hosted messaging endpoint does not actually consume this "
        "secret — it is stored as policy so the AAD app never has orphan "
        "credentials with no known location."
    )

    if st.button("Publish another agent", use_container_width=True):
        _reset(); st.rerun()
