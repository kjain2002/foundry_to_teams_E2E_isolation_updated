"""One-off probe to see what Foundry agents.list() returns for our project."""
import os, sys
from azure.identity import AzureCliCredential
from azure.ai.projects import AIProjectClient

# Foundry account endpoint (try both)
for acct in ["<your-foundry-account>"]:
    for proj in ["<your-project>"]:
        ep = f"https://{acct}.services.ai.azure.com/api/projects/{proj}"
        print(f"\n=== Trying {ep} ===")
        try:
            client = AIProjectClient(endpoint=ep, credential=AzureCliCredential())
            agents = list(client.agents.list())
            print(f"  Found {len(agents)} agents")
            for ag in agents:
                print(f"  - name={ag.name!r}")
                try:
                    latest = ag.versions.latest
                    print(f"    version={latest.version!r}")
                    defn = latest.definition
                    print(f"    defn type={type(defn).__name__}")
                    print(f"    defn attrs: {[a for a in dir(defn) if not a.startswith('_')][:20]}")
                    print(f"    defn.id={getattr(defn, 'id', '<missing>')!r}")
                    print(f"    defn.agent_id={getattr(defn, 'agent_id', '<missing>')!r}")
                    # Dump full defn
                    if hasattr(defn, 'as_dict'):
                        print(f"    defn.as_dict={defn.as_dict()}")
                except Exception as e:
                    print(f"    error inspecting: {e}")
            if agents:
                sys.exit(0)
        except Exception as e:
            print(f"  failed: {type(e).__name__}: {str(e)[:200]}")
