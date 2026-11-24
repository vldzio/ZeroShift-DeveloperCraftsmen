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

  var CSS = [
    ".wake-backdrop{position:fixed;inset:0;z-index:2147483000;display:flex;align-items:center;justify-content:center;padding:20px;background:rgba(10,10,12,.62);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}",
    ".wake-card{width:100%;max-width:440px;background:#ffffff;color:#1f1d1a;border-radius:12px;padding:28px 28px 22px;box-shadow:0 24px 70px rgba(0,0,0,.35);text-align:left}",
    ".wake-head{display:flex;align-items:center;gap:12px;margin:0 0 10px}",
    ".wake-title{font-size:19px;font-weight:650;letter-spacing:-.01em;margin:0}",
    ".wake-spin{width:20px;height:20px;flex:none;border-radius:50%;border:2.5px solid #f1d5c9;border-top-color:#d97757;animation:wake-rot .8s linear infinite}",
    ".wake-done{width:20px;height:20px;flex:none;border-radius:50%;background:#2f7d4a;color:#fff;display:none;align-items:center;justify-content:center;font-size:13px;font-weight:700}",
    ".wake-card.is-ready .wake-spin{display:none}.wake-card.is-ready .wake-done{display:inline-flex}",
    ".wake-text{font-size:14.5px;line-height:1.6;color:#5c5650;margin:0 0 14px}",
    ".wake-meta{font:500 12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#7a736c;margin:0 0 18px}",
    ".wake-actions{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}",
    ".wake-primary{appearance:none;border:0;border-radius:8px;padding:11px 18px;font:600 14px system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;background:#d97757;color:#fff;cursor:pointer}",
    ".wake-primary:hover{background:#c4633f}.wake-primary[hidden]{display:none}",
    ".wake-link{appearance:none;background:none;border:0;padding:6px 0;font:500 13px system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;color:#7a736c;text-decoration:underline;text-underline-offset:3px;cursor:pointer}",
    ".wake-link:hover{color:#1f1d1a}",
    ".wake-card :focus-visible{outline:2px solid #d97757;outline-offset:3px}",
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
        '<div class="wake-head"><span class="wake-spin" aria-hidden="true"></span><span class="wake-done" aria-hidden="true">&#10003;</span>' +
          '<h2 class="wake-title" id="wake-title">Waking up the demo server</h2></div>' +
        '<p class="wake-text" id="wake-text" aria-live="polite">This demo runs on a free server that sleeps when nobody is using it. ' +
          'It is starting up now, which usually takes 30 to 60 seconds.</p>' +
        '<p class="wake-meta" aria-hidden="true">Waiting <span class="wake-elapsed">0:00</span></p>' +
        '<div class="wake-actions"><button type="button" class="wake-primary" hidden>Start the demo</button>' +
          '<button type="button" class="wake-link">Continue anyway</button></div>' +
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
      link: wrap.querySelector(".wake-link")
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
    els.meta.textContent = "Started in " + fmt(Date.now() - started);
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
