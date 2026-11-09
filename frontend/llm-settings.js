/*
 * Demo-deployment helpers for the ZeroShift console.
 *  1. A small "LLM key" panel so visitors can bring their own OpenAI-compatible
 *     key. The key lives in sessionStorage only (cleared when the tab closes),
 *     is sent only to the configured ZeroShift API, and bypasses the shared
 *     free-tier daily limit.
 *  2. A banner explaining what the demo is.
 * Safe to remove: the console works without it against a real AWS deployment.
 */
(function () {
  "use strict";

  const KEY = "zeroshift.byo.key";
  const URL_KEY = "zeroshift.byo.baseUrl";
  const PROVIDERS = [
    ["https://api.openai.com/v1", "OpenAI"],
    ["https://api.groq.com/openai/v1", "Groq"],
    ["https://openrouter.ai/api/v1", "OpenRouter"],
    ["https://generativelanguage.googleapis.com/v1beta/openai", "Google Gemini"],
    ["https://api.mistral.ai/v1", "Mistral"],
    ["https://api.cerebras.ai/v1", "Cerebras"],
  ];

  const store = {
    get(k) { try { return sessionStorage.getItem(k) || ""; } catch (_) { return ""; } },
    set(k, v) { try { v ? sessionStorage.setItem(k, v) : sessionStorage.removeItem(k); } catch (_) { /* private mode */ } },
  };

  // --- attach BYO headers to calls that go to the configured API ---
  const realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    try {
      const url = typeof input === "string" ? input : input.url;
      const apiEl = document.getElementById("api-url");
      const api = apiEl ? apiEl.value.trim().replace(/\/+$/, "") : "";
      const key = store.get(KEY);
      if (key && api && url.startsWith(api)) {
        init = Object.assign({}, init);
        const h = new Headers(init.headers || {});
        h.set("X-LLM-Api-Key", key);
        h.set("X-LLM-Base-Url", store.get(URL_KEY) || PROVIDERS[0][0]);
        init.headers = h;
      }
    } catch (_) { /* never block a request on the helper */ }
    return realFetch(input, init);
  };

  document.addEventListener("DOMContentLoaded", () => {
    // banner
    const banner = document.createElement("div");
    banner.style.cssText = "background:#16233a;color:#bcd4ff;font:13px/1.45 system-ui,sans-serif;padding:8px 14px;text-align:center;border-bottom:1px solid #2a3a5a";
    banner.innerHTML =
      "Live demo &mdash; runs the real ZeroShift Lambdas on bundled fixtures (no AWS account). " +
      "AI answers come from a free-tier LLM gateway, so each visitor gets a small daily allowance; " +
      "use <b>LLM key</b> (top right) to bring your own. " +
      '<a href="https://github.com/vldzio/ZeroShift-DeveloperCraftsmen" target="_blank" rel="noopener" style="color:#9fc4ff">Source</a>';
    document.body.insertBefore(banner, document.body.firstChild);

    // settings panel
    const host = document.querySelector(".api-status");
    if (!host) return;
    const wrap = document.createElement("details");
    wrap.style.cssText = "margin-left:12px;position:relative;font-size:13px";
    const options = PROVIDERS.map(([u, n]) => `<option value="${u}">${n}</option>`).join("");
    wrap.innerHTML =
      '<summary style="cursor:pointer;color:var(--accent,#6ea8ff);list-style:none">LLM key<span id="byo-dot" style="margin-left:6px"></span></summary>' +
      '<div style="position:absolute;right:0;top:28px;z-index:20;width:300px;padding:12px;background:var(--bg-elevated,#12151d);border:1px solid var(--border-strong,#3a4152);border-radius:8px">' +
      '<div style="color:var(--text-muted,#8a91a3);margin-bottom:8px">Optional. Use your own OpenAI-compatible key instead of the shared free tier. Stored in this tab only.</div>' +
      `<select id="byo-provider" style="width:100%;margin-bottom:8px">${options}</select>` +
      '<input id="byo-key" type="password" autocomplete="off" placeholder="API key" style="width:100%;margin-bottom:8px" />' +
      '<button id="byo-save" class="primary">Save</button> <button id="byo-clear">Clear</button>' +
      "</div>";
    host.appendChild(wrap);

    const keyEl = wrap.querySelector("#byo-key");
    const provEl = wrap.querySelector("#byo-provider");
    const dot = wrap.querySelector("#byo-dot");
    const refresh = () => { dot.textContent = store.get(KEY) ? "●" : ""; dot.style.color = "#6fdc8c"; };
    provEl.value = store.get(URL_KEY) || PROVIDERS[0][0];
    refresh();
    wrap.querySelector("#byo-save").addEventListener("click", () => {
      store.set(KEY, keyEl.value.trim()); store.set(URL_KEY, provEl.value); keyEl.value = ""; refresh(); wrap.open = false;
    });
    wrap.querySelector("#byo-clear").addEventListener("click", () => {
      store.set(KEY, ""); keyEl.value = ""; refresh();
    });
  });
})();
