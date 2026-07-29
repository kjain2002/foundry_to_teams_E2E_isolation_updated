# syntax=docker/dockerfile:1.6
# Multi-stage build:
#   1. bicep stage compiles publish-agent.bicep -> arm/publish-agent.json
#   2. runtime stage runs Streamlit
#
# The compiled ARM JSON is what the app actually deploys at runtime via ARM REST.
# Recompiling on every build ensures the JSON cannot drift from the Bicep source.

FROM mcr.microsoft.com/azure-cli:latest AS bicep
WORKDIR /work
RUN az bicep install
COPY publish-agent.bicep            ./publish-agent.bicep
COPY policies                       ./policies
RUN mkdir -p arm && \
    az bicep build --file publish-agent.bicep --outfile arm/publish-agent.json

# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime
WORKDIR /app

# System deps for httpx + msal (none additional needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY streamlit_app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Streamlit app code
COPY streamlit_app/*.py ./
# Manifest template + icons (relative paths in publisher.py expect ../manifest/)
COPY manifest                       ../manifest/
# Compiled ARM JSON from stage 1
COPY --from=bicep /work/arm         ./arm/

ENV STREAMLIT_SERVER_PORT=8000 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

EXPOSE 8000
CMD ["streamlit", "run", "app.py", "--server.port", "8000", "--server.address", "0.0.0.0"]
