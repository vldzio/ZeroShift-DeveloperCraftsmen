/*
 * wake.js - tells visitors when the demo's backend is waking up.
 *
 * Free hosting puts the API to sleep when idle; the first request can take
 * 30-60 s. On page load this checks GET {API_URL}/. If the API answers quickly
 * nothing is shown. Otherwise a notice explains the wait, polls until the API
 * answers, then offers a "Start the demo" button.
 *
 * API URL, in order: ?api=<url>, window.DEMO_CONFIG.API_URL,
 * window.ZEROSHIFT_CONFIG.API_URL, localStorage "zeroshift.apiUrl".
 * Load it after the config script. No dependencies.
 */
(function () {
  "use strict";

  var FAST_MS = 1200;       // answer within this and no notice is shown
  var TRY_TIMEOUT_MS = 10000;
  var POLL_MS = 3000;
  var SLOW_MS = 150000;     // after this, say it is taking longer than usual

  function apiUrl() {
    var fromQuery = new URLSearchParams(window.location.search).get("api");
    var cfg = window.DEMO_CONFIG || window.ZEROSHIFT_CONFIG || {};
    var stored = "";
    try { stored = window.localStorage.getItem("zeroshift.apiUrl") || ""; } catch (e) { /* storage blocked */ }
    return String(fromQuery || cfg.API_URL || stored || "").trim().replace(/\/+$/, "");
  }

  var base = apiUrl();
  if (!/^https?:\/\//i.test(base)) return;

  function ping() {
    var ctrl = typeof AbortController === "function" ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, TRY_TIMEOUT_MS) : null;
    return fetch(base + "/", { cache: "no-store", signal: ctrl ? ctrl.signal : undefined })
      .then(function (r) { return r.ok; }, function () { return false; })
      .then(function (ok) { if (timer) clearTimeout(timer); return ok; });
  }

  // Styled with the demos' shared "calm instrument" tokens, so it follows each page's
  // light/dark mode and accent. Fallbacks are the shared base palette.
  var CSS = [
    ".wake-backdrop{--wk-card:var(--card,var(--bg-elevated,#fff));--wk-text:var(--text,#1b1d17);--wk-muted:var(--muted,var(--text-muted,#555b4d));",
      "--wk-border:var(--border,#d9d6c8);--wk-border-strong:var(--border-strong,#b9b5a5);--wk-accent:var(--accent,#8a5a00);--wk-accent-soft:var(--accent-soft,#f6ead2);",
      "--wk-on-accent:var(--accent-text,var(--on-accent,#fff));--wk-good:var(--good,var(--ok,#2f6b2f));--wk-focus:var(--focus,#2b4fa8);",
      "--wk-mono:var(--mono,ui-monospace,SFMono-Regular,Menlo,Consolas,monospace);--wk-sans:var(--sans,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif);",
      "position:fixed;inset:0;z-index:2147483000;display:flex;align-items:center;justify-content:center;padding:20px;",
      "background:color-mix(in srgb,var(--bg,#f7f6f1) 72%,transparent);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);font:16px/1.55 var(--wk-sans)}",
    ".wake-card{width:100%;max-width:460px;background:var(--wk-card);color:var(--wk-text);border:1px solid var(--wk-border);border-radius:14px;padding:24px;",
      "box-shadow:0 1px 2px #1b1d170d,0 12px 40px #1b1d1724;text-align:left}",
    ".wake-eyebrow{font:600 12px/1.4 var(--wk-mono);letter-spacing:.08em;text-transform:uppercase;color:var(--wk-accent);margin:0 0 6px}",
    ".wake-title{font-size:1.2rem;font-weight:700;line-height:1.25;letter-spacing:-.01em;margin:0 0 8px;color:var(--wk-text)}",
    ".wake-text{color:var(--wk-muted);margin:0 0 16px;font-size:15px}",
    ".wake-status{display:flex;align-items:center;gap:10px;padding:10px 12px;margin:0 0 18px;border:1px solid var(--wk-border);border-radius:8px;",
      "background:var(--wk-accent-soft);font:500 13px/1.4 var(--wk-mono);color:var(--wk-text)}",
    ".wake-spin{width:12px;height:12px;flex:none;border-radius:50%;border:2px solid var(--wk-accent);border-top-color:transparent;animation:wake-rot .9s linear infinite}",
    ".wake-done{display:none;width:16px;height:16px;flex:none;border-radius:50%;background:var(--wk-good);color:var(--wk-card);align-items:center;justify-content:center;font:700 10px/1 var(--wk-sans)}",
    ".wake-card.is-ready .wake-spin{display:none}.wake-card.is-ready .wake-done{display:inline-flex}",
    ".wake-actions{display:flex;flex-wrap:wrap;gap:10px}",
    ".wake-card .wake-btn{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:0 18px;border-radius:8px;font:650 15px var(--wk-sans);cursor:pointer;transition:filter .15s,background-color .15s}",
    ".wake-card .wake-primary{background:var(--wk-accent);color:var(--wk-on-accent);border:1px solid var(--wk-accent)}",
    ".wake-card .wake-primary:hover{filter:brightness(1.08)}",
    ".wake-card .wake-secondary{background:transparent;color:var(--wk-accent);border:1px solid var(--wk-border-strong)}",
    ".wake-card .wake-secondary:hover{background:var(--wk-accent-soft);filter:none}",
    ".wake-card .wake-btn[hidden]{display:none}",
    ".wake-card :focus-visible{outline:2px solid var(--wk-focus);outline-offset:3px}",
    "@keyframes wake-rot{to{transform:rotate(360deg)}}",
    "@media (prefers-reduced-motion:reduce){.wake-spin{animation-duration:2.4s}}"
  ].join("");

  var els = null;
  var started = Date.now();
  var tick = null;
  var ready = false;
  var closed = false;
  var lastFocus = null;

  function fmt(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }

  function show() {
    if (els || closed) return;
    var style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);

    var wrap = document.createElement("div");
    wrap.className = "wake-backdrop";
    wrap.innerHTML =
      '<div class="wake-card" role="dialog" aria-modal="true" aria-labelledby="wake-title" aria-describedby="wake-text">' +
        '<p class="wake-eyebrow">Demo server</p>' +
        '<h2 class="wake-title" id="wake-title">Waking up the demo server</h2>' +
        '<p class="wake-text" id="wake-text" aria-live="polite">This demo runs on a free server that sleeps when nobody is using it. ' +
          'It is starting up now, which usually takes 30 to 60 seconds.</p>' +
        '<div class="wake-status"><span class="wake-spin" aria-hidden="true"></span><span class="wake-done" aria-hidden="true">&#10003;</span>' +
          '<span class="wake-meta">Starting up&hellip; <span class="wake-elapsed">0:00</span></span></div>' +
        '<div class="wake-actions"><button type="button" class="wake-btn wake-primary" hidden>Start the demo</button>' +
          '<button type="button" class="wake-btn wake-secondary">Continue anyway</button></div>' +
      '</div>';
    document.body.appendChild(wrap);

    els = {
      wrap: wrap,
      card: wrap.querySelector(".wake-card"),
      title: wrap.querySelector(".wake-title"),
      text: wrap.querySelector(".wake-text"),
      meta: wrap.querySelector(".wake-meta"),
      elapsed: wrap.querySelector(".wake-elapsed"),
      primary: wrap.querySelector(".wake-primary"),
      link: wrap.querySelector(".wake-secondary")
    };
    els.primary.addEventListener("click", close);
    els.link.addEventListener("click", close);
    wrap.addEventListener("keydown", trapFocus);

    lastFocus = document.activeElement;
    els.link.focus();
    tick = setInterval(function () {
      var waited = Date.now() - started;
      els.elapsed.textContent = fmt(waited);
      if (!ready && waited > SLOW_MS && !els.card.classList.contains("is-slow")) {
        els.card.classList.add("is-slow");
        els.text.textContent = "This is taking longer than usual. The server may be restarting; it will keep trying, " +
          "or you can continue and try again in a minute.";
      }
    }, 1000);
  }

  function trapFocus(e) {
    if (e.key !== "Tab") return;
    var items = [els.primary, els.link].filter(function (b) { return !b.hidden; });
    var first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  function markReady() {
    ready = true;
    if (!els) return;
    clearInterval(tick);
    els.card.classList.add("is-ready");
    els.title.textContent = "The demo is ready";
    els.text.textContent = "The server is up. Go ahead and try it out.";
    els.meta.textContent = "Ready · started in " + fmt(Date.now() - started);
    els.primary.hidden = false;
    els.link.hidden = true;
    els.primary.focus();
  }

  function close() {
    closed = true;
    clearInterval(tick);
    if (els) { els.wrap.remove(); els = null; }
    if (lastFocus && typeof lastFocus.focus === "function") { try { lastFocus.focus(); } catch (e) { /* ignore */ } }
  }

  function poll() {
    ping().then(function (ok) {
      if (ok) { markReady(); return; }
      setTimeout(poll, POLL_MS);
    });
  }

  function start() {
    var fastTimer = setTimeout(show, FAST_MS);
    ping().then(function (ok) {
      if (ok) {
        if (!els) { clearTimeout(fastTimer); return; }  // answered quickly: never shown
        markReady();
        return;
      }
      clearTimeout(fastTimer);
      show();
      setTimeout(poll, POLL_MS);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
