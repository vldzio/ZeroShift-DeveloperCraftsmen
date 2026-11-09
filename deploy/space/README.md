---
title: ZeroShift Demo API
emoji: 🛡️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
short_description: Demo API for ZeroShift (IAM/SCP governance) with a private FreeLLMAPI gateway
---

# ZeroShift demo API (Hugging Face Space)

Runs the real ZeroShift Lambda handlers against bundled fixtures and answers
with live LLM output through a **private** [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi)
gateway in the same container. Source: <https://github.com/vldzio/ZeroShift-DeveloperCraftsmen>.

## Space secrets (Settings → Variables and secrets)

| Name | Value |
| --- | --- |
| `ENCRYPTION_KEY` | 64 hex chars (`openssl rand -hex 32`); required by the gateway |
| `FREEAPI_CONFIG_JSON` | gateway config with your free-tier provider keys, see `freellmapi.config.example.json` |
| `LANGSMITH_API_KEY` | LangSmith key (tracing is switched on automatically) |
| `LANGSMITH_PROJECT` | `zeroshift-demo` (own dashboard per project) |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | per-IP and global daily limits |
| `CORS_ORIGINS` | your frontend origin, e.g. `https://zeroshift-demo.vercel.app` |
| `RL_PER_IP_DAILY` / `RL_GLOBAL_DAILY` | optional, default 20 / 1000 credits per UTC day |
