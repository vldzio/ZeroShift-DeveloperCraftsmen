# Hugging Face Docker Space: private FreeLLMAPI gateway + ZeroShift demo API in one container.
#
#   public :7860  ->  demo_api (FastAPI; rate limits, BYO key, LangSmith tracing)
#   private :3001 ->  FreeLLMAPI (bound to 127.0.0.1, never exposed)
#
# The project code is cloned at build time, so this Space repo only needs this
# Dockerfile + README.md. Rebuild the Space to pick up new commits.
FROM ghcr.io/tashfeenahmed/freellmapi:latest

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ARG REPO=https://github.com/vldzio/ZeroShift-DeveloperCraftsmen.git
ARG REF=demo-ready
ARG CACHEBUST=1
RUN git clone --depth 1 --branch "${REF}" "${REPO}" /srv/app \
 && python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /srv/app/demo_api/requirements.txt \
 && chown -R node:node /srv/app

COPY start.sh /srv/start.sh
RUN tr -d '' < /srv/start.sh > /tmp/start.sh && install -m 755 /tmp/start.sh /srv/start.sh && rm /tmp/start.sh

USER node
ENV HOME=/home/node \
    FREEAPI_DB_PATH=/app/server/data/freeapi.db \
    DEMO_PROJECT=zeroshift \
    LANGSMITH_PROJECT=zeroshift-demo
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:7860/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["/srv/start.sh"]
