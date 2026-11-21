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
    banner.className = "demo-banner";
    banner.setAttribute("role", "region");
    banner.setAttribute("aria-label", "About this demo");
    banner.innerHTML =
      "<b>Live demo on fixture data.</b> It runs the real ZeroShift Lambdas on bundled fixtures (no AWS account). " +
      "AI answers come from a free-tier LLM gateway, so each visitor gets a small daily allowance; " +
      "use <b>LLM key</b> (top right) to bring your own. " +
      '<a href="https://github.com/vldzio/ZeroShift-DeveloperCraftsmen" target="_blank" rel="noopener">Source</a>';
    document.body.insertBefore(banner, document.body.firstChild);

    // settings panel
    const host = document.querySelector(".api-status");
    if (!host) return;
    const wrap = document.createElement("details");
    wrap.className = "byo";
    const options = PROVIDERS.map(([u, n]) => `<option value="${u}">${n}</option>`).join("");
    wrap.innerHTML =
      '<summary>LLM key<span id="byo-dot" class="byo-dot"></span></summary>' +
      '<div class="byo-panel">' +
      '<p>Optional. Use your own OpenAI-compatible key instead of the shared free tier. Stored in this tab only.</p>' +
      `<label for="byo-provider">Provider</label><select id="byo-provider">${options}</select>` +
      '<label for="byo-key">API key</label><input id="byo-key" type="password" autocomplete="off" placeholder="Paste your key" />' +
      '<div class="byo-actions"><button id="byo-save" class="primary">Save</button> <button id="byo-clear" class="secondary">Clear</button></div>' +
      "</div>";
    host.appendChild(wrap);

    const keyEl = wrap.querySelector("#byo-key");
    const provEl = wrap.querySelector("#byo-provider");
    const dot = wrap.querySelector("#byo-dot");
    const refresh = () => { dot.textContent = store.get(KEY) ? "● own key" : ""; };
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
