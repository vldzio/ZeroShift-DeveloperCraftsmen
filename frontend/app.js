(function () {
  "use strict";

  // ---------- Shared: API URL config ----------

  const params = new URLSearchParams(window.location.search);
  const apiUrlEl = document.getElementById("api-url");
  const initialApiUrl = params.get("api")
    || (window.ZEROSHIFT_CONFIG && window.ZEROSHIFT_CONFIG.API_URL)
    || localStorage.getItem("zeroshift.apiUrl")
    || "";
  apiUrlEl.value = initialApiUrl;
  apiUrlEl.addEventListener("change", () => {
    localStorage.setItem("zeroshift.apiUrl", apiUrlEl.value.trim());
  });

  function getApiUrl() {
    return apiUrlEl.value.trim().replace(/\/+$/, "");
  }

  // ============================================================
  // Router — sidebar tab switching
  // ============================================================

  (function router() {
    const buttons = document.querySelectorAll(".nav-item");
    const views = {
      denial: document.getElementById("view-denial"),
      refactor: document.getElementById("view-refactor"),
      stale: document.getElementById("view-stale"),
      drift: document.getElementById("view-drift"),
      codeanalyzer: document.getElementById("view-codeanalyzer"),
    };

    function switchTo(view) {
      Object.entries(views).forEach(([name, el]) => {
        if (name === view) el.classList.remove("hidden");
        else el.classList.add("hidden");
      });
      buttons.forEach((b) => {
        if (b.dataset.view === view) b.classList.add("active");
        else b.classList.remove("active");
      });
      localStorage.setItem("zeroshift.activeView", view);
    }

    buttons.forEach((b) => b.addEventListener("click", () => switchTo(b.dataset.view)));

    const remembered = localStorage.getItem("zeroshift.activeView");
    if (remembered && views[remembered]) switchTo(remembered);
  })();

  // ============================================================
  // Denial Analysis
  // ============================================================

  (function denial() {
    const els = {
      eventJson: document.getElementById("event-json"),
      analyzeBtn: document.getElementById("analyze-btn"),
      analyzeStatus: document.getElementById("analyze-status"),
      resultEmpty: document.getElementById("result-empty"),
      resultView: document.getElementById("result-view"),
      resultError: document.getElementById("result-error"),
      verdictScp: document.getElementById("verdict-scp"),
      verdictMeta: document.getElementById("verdict-meta"),
      ctBadge: document.getElementById("verdict-ct-badge"),
      summary: document.getElementById("explanation-summary"),
      cause: document.getElementById("explanation-cause"),
      fix: document.getElementById("explanation-fix"),
      conditionKeysSection: document.getElementById("condition-keys-section"),
      conditionKeysList: document.getElementById("condition-keys-list"),
      ctPathSection: document.getElementById("ct-path-section"),
      ctPathList: document.getElementById("ct-path-list"),
      rawResponse: document.getElementById("raw-response"),
      errorMessage: document.getElementById("error-message"),
      fixtureBtns: document.querySelectorAll(".fixture-btn"),
    };

    els.fixtureBtns.forEach((btn) => {
      btn.addEventListener("click", async () => {
        const name = btn.dataset.fixture;
        els.fixtureBtns.forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        try {
          const resp = await fetch(`fixtures/${name}.json`);
          if (!resp.ok) throw new Error(`Failed to load fixture: ${resp.status}`);
          const json = await resp.json();
          els.eventJson.value = JSON.stringify(json, null, 2);
        } catch (err) {
          showError(`Could not load fixture "${name}": ${err.message}. Are you serving over http:// (not file://)?`);
        }
      });
    });

    els.analyzeBtn.addEventListener("click", analyze);

    async function analyze() {
      const apiUrl = getApiUrl();
      if (!apiUrl) return showError("API endpoint is empty. Paste the API Gateway URL into the header field.");
      const raw = els.eventJson.value.trim();
      if (!raw) return showError("Event JSON is empty. Pick a fixture or paste a CloudTrail access-denied event.");

      let eventObj;
      try { eventObj = JSON.parse(raw); }
      catch (err) { return showError(`Invalid JSON: ${err.message}`); }

      setBusy(true);
      hideAll();

      try {
        const resp = await fetch(`${apiUrl}/analyze-denial`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ event: eventObj }),
        });
        const bodyText = await resp.text();
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${bodyText.slice(0, 500)}`);
        let body;
        try { body = JSON.parse(bodyText); }
        catch { throw new Error(`Response was not JSON: ${bodyText.slice(0, 500)}`); }
        renderResult(body);
      } catch (err) {
        showError(err.message || String(err));
      } finally {
        setBusy(false);
      }
    }

    function setBusy(busy) {
      els.analyzeBtn.disabled = busy;
      els.analyzeStatus.textContent = busy ? "Analyzing…" : "";
    }
    function hideAll() {
      els.resultEmpty.classList.add("hidden");
      els.resultView.classList.add("hidden");
      els.resultError.classList.add("hidden");
    }
    function showError(msg) {
      hideAll();
      els.errorMessage.textContent = msg;
      els.resultError.classList.remove("hidden");
    }

    function renderResult(body) {
      hideAll();
      els.rawResponse.textContent = JSON.stringify(body, null, 2);

      if (!body.found) {
        els.verdictScp.textContent = "No matching SCP found";
        els.verdictMeta.textContent = body.message || "The denial may originate from an identity-based policy, permissions boundary, session policy, or resource-based policy.";
        els.ctBadge.classList.add("hidden");
        els.summary.textContent = "—";
        els.cause.textContent = "—";
        els.fix.textContent = "—";
        els.conditionKeysSection.classList.add("hidden");
        renderCtPath(body.controlTowerManagedInPath);
        els.resultView.classList.remove("hidden");
        return;
      }

      const scp = body.scp || {};
      const attachedAt = body.attachedAt || {};
      els.verdictScp.textContent = scp.name || "(unnamed SCP)";
      els.verdictMeta.textContent = `${scp.id || ""} · attached at ${attachedAt.type || "?"} "${attachedAt.name || "?"}" · statement ${body.statementId || "(unnamed)"}`;

      if (body.controlTowerManaged) els.ctBadge.classList.remove("hidden");
      else els.ctBadge.classList.add("hidden");

      const expl = body.explanation || {};
      els.summary.textContent = expl.summary || "—";
      els.cause.textContent = expl.cause || "—";
      els.fix.textContent = expl.suggested_fix || "—";

      const keys = Array.isArray(expl.affected_condition_keys) ? expl.affected_condition_keys : [];
      if (keys.length) {
        els.conditionKeysSection.classList.remove("hidden");
        els.conditionKeysList.innerHTML = "";
        keys.forEach((k) => {
          const li = document.createElement("li");
          li.textContent = k;
          els.conditionKeysList.appendChild(li);
        });
      } else {
        els.conditionKeysSection.classList.add("hidden");
      }

      renderCtPath(body.controlTowerManagedInPath);
      els.resultView.classList.remove("hidden");
    }

    function renderCtPath(items) {
      if (!Array.isArray(items) || items.length === 0) {
        els.ctPathSection.classList.add("hidden");
        return;
      }
      els.ctPathSection.classList.remove("hidden");
      els.ctPathList.innerHTML = "";
      items.forEach((item) => {
        const li = document.createElement("li");
        li.textContent = `${item.scpName || "(unnamed)"}${item.scpId ? ` (${item.scpId})` : ""}`;
        els.ctPathList.appendChild(li);
      });
    }
  })();

  // ============================================================
  // SCP Refactoring
  // ============================================================

  (function refactor() {
    const STEPS = [
      { name: "LoadScp", label: "Load SCP" },
      { name: "CheckControlTower", label: "Check Control Tower" },
      { name: "CheckSize", label: "Check Size" },
      { name: "ProposeRefactor", label: "Propose Refactor (Kimi K2.5)" },
      { name: "VerifyEquivalence", label: "Verify Equivalence" },
      { name: "CreateChangeRequest", label: "Create Change Request" },
    ];

    const POLL_INTERVAL_MS = 1500;
    const POLL_TIMEOUT_MS = 5 * 60 * 1000;

    const els = {
      scpBtns: document.querySelectorAll(".scp-btn"),
      scpInput: document.getElementById("scp-id-input"),
      startBtn: document.getElementById("refactor-btn"),
      statusInline: document.getElementById("refactor-status-inline"),
      empty: document.getElementById("refactor-empty"),
      view: document.getElementById("refactor-view"),
      err: document.getElementById("refactor-error"),
      errLabel: document.getElementById("refactor-error-label"),
      errMsg: document.getElementById("refactor-error-message"),
      stepList: document.getElementById("step-list"),
      result: document.getElementById("refactor-result"),
      terminalLabel: document.getElementById("refactor-terminal-label"),
      terminalDetail: document.getElementById("refactor-terminal-detail"),
      changeId: document.getElementById("refactor-change-id"),
      statOriginal: document.getElementById("stat-original"),
      statCompressed: document.getElementById("stat-compressed"),
      statReduction: document.getElementById("stat-reduction"),
      compressionNotes: document.getElementById("compression-notes"),
      equivalenceSummary: document.getElementById("equivalence-summary"),
      compressedDoc: document.getElementById("compressed-doc"),
      raw: document.getElementById("refactor-raw"),
    };

    let pollTimer = null;
    let pollStart = 0;

    els.scpBtns.forEach((btn) => {
      btn.addEventListener("click", () => {
        els.scpBtns.forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        els.scpInput.value = btn.dataset.scp;
      });
    });

    els.startBtn.addEventListener("click", start);

    async function start() {
      const apiUrl = getApiUrl();
      if (!apiUrl) return showError("API endpoint is empty. Paste the API Gateway URL into the header field.");
      const scpId = els.scpInput.value.trim();
      if (!scpId) return showError("SCP policy ID is empty.");

      stopPolling();
      setBusy(true);
      hideAll();
      renderSteps({ status: "RUNNING", steps: STEPS.map((s) => ({ name: s.name, status: "PENDING" })) });
      els.view.classList.remove("hidden");
      els.result.classList.add("hidden");

      const useAgent = !!document.getElementById("refactor-use-agent")?.checked;
      try {
        const resp = await fetch(`${apiUrl}/refactor-scp`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ scpId, useAgent }),
        });
        const bodyText = await resp.text();
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${bodyText.slice(0, 500)}`);
        const body = JSON.parse(bodyText);
        if (!body.executionArn) throw new Error(`No executionArn in response: ${bodyText}`);
        beginPolling(apiUrl, body.executionArn);
      } catch (err) {
        setBusy(false);
        showError(err.message || String(err));
      }
    }

    function beginPolling(apiUrl, executionArn) {
      pollStart = Date.now();
      const tick = async () => {
        try {
          const resp = await fetch(`${apiUrl}/refactor-status?executionArn=${encodeURIComponent(executionArn)}`);
          const body = await resp.json();
          renderSteps(body);
          if (body.status && body.status !== "RUNNING") {
            stopPolling();
            setBusy(false);
            renderTerminal(body);
            return;
          }
          if (Date.now() - pollStart > POLL_TIMEOUT_MS) {
            stopPolling();
            setBusy(false);
            showError("Polling timed out after 5 minutes.");
            return;
          }
        } catch (err) {
          stopPolling();
          setBusy(false);
          showError(`Polling failed: ${err.message || err}`);
        }
      };
      tick();
      pollTimer = setInterval(tick, POLL_INTERVAL_MS);
    }

    function stopPolling() {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }

    function renderSteps(body) {
      const stepsMap = {};
      (body.steps || []).forEach((s) => { stepsMap[s.name] = s; });

      els.stepList.innerHTML = "";
      STEPS.forEach((meta) => {
        const s = stepsMap[meta.name] || { name: meta.name, status: "PENDING" };
        const li = document.createElement("li");
        li.className = `step step-${s.status.toLowerCase()}`;
        const iconGlyph = ({ PENDING: "·", RUNNING: "…", SUCCEEDED: "✓", SKIPPED: "—", FAILED: "×" })[s.status] || "·";
        li.innerHTML = `
          <span class="step-icon">${iconGlyph}</span>
          <div class="step-body">
            <div class="step-name">${meta.label}</div>
            <div class="step-detail">${stepDetail(meta.name, s, body)}</div>
          </div>
        `;
        els.stepList.appendChild(li);
      });
    }

    function stepDetail(name, s, body) {
      if (s.status === "PENDING") return "waiting";
      if (s.status === "RUNNING") return "in progress…";
      if (s.status === "SKIPPED") return "skipped";
      const out = s.output || {};
      if (name === "LoadScp" && out.originalSize) return `loaded · ${out.originalSize.toLocaleString()} bytes`;
      if (name === "CheckControlTower") {
        return out.controlTowerManaged ? "Control Tower–managed · will short-circuit" : "not CT-managed";
      }
      if (name === "CheckSize") {
        if (out.overThreshold === false) return `under threshold · ${(out.originalSize || 0).toLocaleString()} bytes`;
        return `over threshold · ${(out.originalSize || 0).toLocaleString()} bytes`;
      }
      if (name === "ProposeRefactor") {
        const p = out.proposal || {};
        if (p.new_size_bytes != null) return `compressed to ${p.new_size_bytes.toLocaleString()} bytes · ${p.statements_removed_or_merged || 0} statements merged`;
        return "proposal received";
      }
      if (name === "VerifyEquivalence") {
        const e = out.equivalence || {};
        if (e.isEquivalent === true) return `${e.totalActionsChecked || 0} actions checked · 0 divergences`;
        if (e.isEquivalent === false) return `${(e.divergences || []).length} divergences on ${e.totalActionsChecked || 0} actions`;
        return "checking equivalence";
      }
      if (name === "CreateChangeRequest") {
        const cr = (out.changeRequest && out.changeRequest.changeRequest) || out.changeRequest || {};
        if (cr.changeRequestId) return `change request: ${cr.changeRequestId}`;
        return "change request created";
      }
      return "done";
    }

    function renderTerminal(body) {
      const status = body.status;
      const terminal = body.terminal;
      const output = body.finalOutput || {};

      els.raw.textContent = JSON.stringify(body, null, 2);

      if (status === "FAILED") {
        showError(`Execution failed. ${output.cause || ""}`);
        return;
      }

      if (terminal === "SKIPPED_CONTROL_TOWER" || terminal === "SKIPPED_UNDER_THRESHOLD") {
        els.errLabel.textContent = "Workflow skipped";
        els.errMsg.textContent = output.reason || "Workflow short-circuited.";
        els.err.classList.remove("hidden");
        return;
      }
      if (terminal === "REJECTED_NOT_EQUIVALENT") {
        els.errLabel.textContent = "Refactor rejected";
        const divergences = output.divergences || [];
        const preview = divergences.slice(0, 5).map((d) => `${d.action}: original=${d.original_decision}, proposed=${d.proposed_decision}`).join("; ");
        els.errMsg.textContent = `Proposed SCP is not equivalent to original. ${divergences.length} diverging action(s). ${preview}`;
        els.err.classList.remove("hidden");
        return;
      }

      // Accepted / SUCCEEDED path
      const ps = output.proposalSummary || {};
      const eq = output.equivalence || {};
      const cr = (output.changeRequest && output.changeRequest) || {};

      els.terminalLabel.textContent = "Change request created";
      els.terminalDetail.textContent = ps.compressionNotes || "Refactor accepted.";
      els.changeId.textContent = `Change request: ${cr.changeRequestId || "(pending)"} · Template: ${cr.changeTemplateName || "—"}${cr.fixtureMode ? " · fixture-mode" : ""}`;

      const orig = ps.originalSize || 0;
      const newSize = ps.newSize || 0;
      const pct = orig > 0 ? Math.round((1 - newSize / orig) * 100) : 0;
      els.statOriginal.textContent = `${orig.toLocaleString()} B`;
      els.statCompressed.textContent = `${newSize.toLocaleString()} B`;
      els.statReduction.textContent = `-${pct}%`;
      els.compressionNotes.textContent = ps.compressionNotes || "—";
      els.equivalenceSummary.textContent = eq.isEquivalent
        ? `${eq.totalActionsChecked || 0} concrete actions simulated against both documents. All decisions match.`
        : `${(eq.divergences || []).length} divergences on ${eq.totalActionsChecked || 0} actions.`;

      const proposedDoc = (output.proposalSummary && output.proposalSummary.compressed_document)
        || (body.finalOutput && body.finalOutput.compressed_document)
        || {};
      els.compressedDoc.textContent = JSON.stringify(proposedDoc, null, 2);

      els.result.classList.remove("hidden");
    }

    function setBusy(busy) {
      els.startBtn.disabled = busy;
      els.statusInline.textContent = busy ? "Running workflow…" : "";
    }
    function hideAll() {
      els.empty.classList.add("hidden");
      els.err.classList.add("hidden");
    }
    function showError(msg) {
      hideAll();
      els.errLabel.textContent = "Error";
      els.errMsg.textContent = msg;
      els.err.classList.remove("hidden");
    }
  })();

  // ============================================================
  // Stale SCP Detection
  // ============================================================

  (function stale() {
    const els = {
      lookbackInput: document.getElementById("stale-lookback-input"),
      runBtn: document.getElementById("stale-btn"),
      statusInline: document.getElementById("stale-status-inline"),
      empty: document.getElementById("stale-empty"),
      view: document.getElementById("stale-view"),
      err: document.getElementById("stale-error"),
      errMsg: document.getElementById("stale-error-message"),
      summaryLabel: document.getElementById("stale-summary-label"),
      summaryCount: document.getElementById("stale-summary-count"),
      summaryMeta: document.getElementById("stale-summary-meta"),
      findingsList: document.getElementById("stale-findings-list"),
      raw: document.getElementById("stale-raw"),
    };

    els.runBtn.addEventListener("click", run);

    async function run() {
      const apiUrl = getApiUrl();
      if (!apiUrl) return showError("API endpoint is empty. Paste the API Gateway URL into the header field.");
      const lookback = parseInt(els.lookbackInput.value, 10) || 180;

      setBusy(true);
      hideAll();

      try {
        const resp = await fetch(`${apiUrl}/detect-stale-scps`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ lookbackDays: lookback }),
        });
        const bodyText = await resp.text();
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${bodyText.slice(0, 500)}`);
        const outer = JSON.parse(bodyText);
        // The Lambda wraps the payload in an API-Gateway-style envelope.
        const body = outer.body ? JSON.parse(outer.body) : outer;
        render(body);
      } catch (err) {
        showError(err.message || String(err));
      } finally {
        setBusy(false);
      }
    }

    function render(body) {
      els.raw.textContent = JSON.stringify(body, null, 2);
      const findings = Array.isArray(body.findings) ? body.findings : [];
      const scanned = body.scpsScanned || 0;
      const skipped = body.scpsSkippedControlTower || 0;

      els.summaryCount.textContent = findings.length === 0
        ? "No dead-deny statements found"
        : `${findings.length} dead-deny statement${findings.length === 1 ? "" : "s"} found`;
      els.summaryMeta.textContent = `Scanned ${scanned} SCP${scanned === 1 ? "" : "s"} · Skipped ${skipped} Control-Tower–managed · Lookback ${body.lookbackDays || 180} days`;

      els.findingsList.innerHTML = "";
      if (findings.length === 0) {
        const card = document.createElement("div");
        card.className = "result-empty";
        card.textContent = "All Deny statements have observed activity in the lookback window — no recommendations.";
        els.findingsList.appendChild(card);
      } else {
        findings.forEach((f) => els.findingsList.appendChild(renderFinding(f)));
      }
      els.view.classList.remove("hidden");
    }

    function renderFinding(f) {
      const card = document.createElement("div");
      card.className = "stale-card";
      const actions = Array.isArray(f.actions) ? f.actions : [];
      const preview = actions.slice(0, 20);
      const remainder = actions.length - preview.length;
      const listItems = preview
        .map((a) => `<li>${a} <span style="color:var(--text-dim);">· ${(f.action_counts && f.action_counts[a] != null ? f.action_counts[a] : 0)}</span></li>`)
        .join("");
      const remainderNote = remainder > 0 ? `<li style="color:var(--text-dim);">…and ${remainder} more</li>` : "";
      card.innerHTML = `
        <div class="stale-card-header">
          <span class="badge badge-dead-deny">Dead deny</span>
          <span class="stale-card-scp">${escapeHtml(f.scp_name || "(unnamed)")}</span>
          <span class="stale-card-stmt">${escapeHtml(f.scp_id || "")} · Sid: ${escapeHtml(f.statement_sid || "(unnamed)")}</span>
        </div>
        <div class="stale-card-rec">${escapeHtml(f.recommendation || "")}</div>
        <div class="section-label" style="margin-top:8px;">Actions covered (count in last ${f.lookback_days || 180} days)</div>
        <ul class="stale-card-actions">${listItems}${remainderNote}</ul>
      `;
      return card;
    }

    function setBusy(busy) {
      els.runBtn.disabled = busy;
      els.statusInline.textContent = busy ? "Scanning…" : "";
    }
    function hideAll() {
      els.empty.classList.add("hidden");
      els.view.classList.add("hidden");
      els.err.classList.add("hidden");
    }
    function showError(msg) {
      hideAll();
      els.errMsg.textContent = msg;
      els.err.classList.remove("hidden");
    }
    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }
  })();

  // ============================================================
  // IAM Drift Detection (Part 2)
  // ============================================================

  (function drift() {
    const DRIFT_STEPS = [
      { name: "LoadRole", label: "Load Role" },
      { name: "CheckManaged", label: "Check ManagedBy tag" },
      { name: "ScanUsage", label: "Scan CloudTrail usage" },
      { name: "ReadIntentRegistry", label: "Read intent registry" },
      { name: "ProposeReplacement", label: "Propose replacement (Kimi K2.5)" },
      { name: "SimulateEquivalence", label: "Verify equivalence" },
      { name: "ScoreRisk", label: "Score risk tier" },
      { name: "WaitGracePeriod", label: "Grace period (MEDIUM only)" },
      { name: "ApplyPolicy", label: "Apply policy version" },
      { name: "UpdateIntentRegistry", label: "Update intent registry" },
    ];
    const POLL_INTERVAL_MS = 1500;
    const POLL_TIMEOUT_MS = 5 * 60 * 1000;

    const els = {
      lookback: document.getElementById("drift-lookback-input"),
      scanBtn: document.getElementById("drift-scan-btn"),
      status: document.getElementById("drift-status-inline"),
      empty: document.getElementById("drift-empty"),
      view: document.getElementById("drift-view"),
      err: document.getElementById("drift-error"),
      errMsg: document.getElementById("drift-error-message"),
      summaryCount: document.getElementById("drift-summary-count"),
      summaryMeta: document.getElementById("drift-summary-meta"),
      roleList: document.getElementById("drift-role-list"),
      raw: document.getElementById("drift-raw"),
      remediation: document.getElementById("drift-remediation"),
      remediationRole: document.getElementById("drift-remediation-role"),
      stepList: document.getElementById("drift-step-list"),
      remediationResult: document.getElementById("drift-remediation-result"),
      terminalLabel: document.getElementById("drift-terminal-label"),
      terminalDetail: document.getElementById("drift-terminal-detail"),
      terminalMeta: document.getElementById("drift-terminal-meta"),
      remediationRaw: document.getElementById("drift-remediation-raw"),
      onboardBtn: document.getElementById("onboard-discover-btn"),
      onboardList: document.getElementById("onboard-list"),
      useAgent: document.getElementById("drift-use-agent"),
      traceSection: document.getElementById("drift-agent-trace-section"),
      traceList: document.getElementById("drift-agent-trace"),
    };

    let pollTimer = null;

    els.scanBtn.addEventListener("click", scan);
    els.onboardBtn.addEventListener("click", discoverRoles);

    async function scan() {
      const apiUrl = getApiUrl();
      if (!apiUrl) return showError("API endpoint is empty. Paste the API Gateway URL into the header field.");
      const lookback = parseInt(els.lookback.value, 10) || 90;

      setBusy(true);
      hideAll();

      try {
        const resp = await fetch(`${apiUrl}/detect-drift`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ lookbackDays: lookback }),
        });
        const bodyText = await resp.text();
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${bodyText.slice(0, 500)}`);
        const outer = JSON.parse(bodyText);
        const body = outer.body ? JSON.parse(outer.body) : outer;
        renderScan(body);
      } catch (err) {
        showError(err.message || String(err));
      } finally {
        setBusy(false);
      }
    }

    function renderScan(body) {
      els.raw.textContent = JSON.stringify(body, null, 2);
      const findings = Array.isArray(body.findings) ? body.findings : [];
      const withDrift = findings.filter((f) => (f.actionsToRemove || []).length > 0);
      els.summaryCount.textContent = withDrift.length === 0
        ? "No drift detected"
        : `${withDrift.length} role${withDrift.length === 1 ? "" : "s"} with drift`;
      els.summaryMeta.textContent = `Scanned ${findings.length} managed role${findings.length === 1 ? "" : "s"} · Lookback ${body.lookbackDays || 90} days`;

      els.roleList.innerHTML = "";
      findings.forEach((f) => els.roleList.appendChild(renderRoleCard(f)));
      els.view.classList.remove("hidden");
    }

    function renderRoleCard(f) {
      const card = document.createElement("div");
      card.className = "role-card";
      const tier = (f.riskTier || "LOW").toLowerCase();
      const drifted = (f.actionsToRemove || []).length;
      const findingsBySource = (f.findings || []).reduce((acc, x) => { acc[x.source] = (acc[x.source] || 0) + 1; return acc; }, {});
      const proposed = (f.proposal && f.proposal.proposed_policy) || {};
      const rationale = (f.proposal && f.proposal.rationale) || "";

      card.innerHTML = `
        <div class="role-card-header">
          <span class="badge badge-risk-${tier}">${(f.riskTier || "").toUpperCase()}</span>
          <span class="role-card-name">${escapeHtml(f.roleName || f.roleArn || "(unknown)")}</span>
          <span class="role-card-meta">env: ${escapeHtml(f.environment || "unknown")} · policy: ${escapeHtml(f.attachedPolicyId || "—")}${f.isRealRole ? " · real IAM" : " · fixture"}</span>
        </div>
        <div class="section-label" style="margin-top:6px;">Drift</div>
        <div class="section-body">${drifted === 0 ? "No drift for this role." : `${drifted} action(s) flagged`}${
          Object.keys(findingsBySource).length
            ? ` · <span class="badge badge-source-unused">${findingsBySource.UNUSED_CLOUDTRAIL || 0} unused</span>` +
              ` <span class="badge badge-source-registry">${(findingsBySource.PENDING_REMOVAL_EXPIRED || 0) + (findingsBySource.PENDING_REMOVAL_ACTIVE_PRESERVED || 0) + (findingsBySource.INTENT_ACTIVE_PRESERVED || 0)} registry</span>`
            : ""
        }</div>
        ${drifted > 0 ? `
        <div class="section-label" style="margin-top:8px;">Kimi rationale</div>
        <div class="section-body">${escapeHtml(rationale || "—")}</div>
        <div class="section-label" style="margin-top:8px;">Policy diff</div>
        <div class="policy-diff">
          <div><div class="side-label">Current</div><pre>${escapeHtml(JSON.stringify(f.proposal && f.proposal.proposed_policy ? {"see":"kept","note":"proposal shown below"} : {}, null, 2))}</pre></div>
          <div><div class="side-label">Proposed</div><pre>${escapeHtml(JSON.stringify(proposed, null, 2))}</pre></div>
        </div>
        <div class="role-card-actions">
          <button class="primary" data-remediate="${escapeHtml(f.roleArn)}">Remediate this role</button>
        </div>
        ` : ""}
      `;
      const btn = card.querySelector("button[data-remediate]");
      if (btn) btn.addEventListener("click", () => remediate(f.roleArn, f.roleName));
      return card;
    }

    async function remediate(roleArn, roleName) {
      const apiUrl = getApiUrl();
      if (!apiUrl) return showError("API endpoint is empty.");
      stopPolling();
      els.remediation.classList.remove("hidden");
      els.remediationResult.classList.add("hidden");
      els.remediationRole.textContent = `Executing state machine for ${roleName || roleArn}`;
      renderSteps({ steps: DRIFT_STEPS.map((s) => ({ name: s.name, status: "PENDING" })) });

      try {
        const resp = await fetch(`${apiUrl}/remediate-role`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ roleArn, useAgent: !!els.useAgent.checked }),
        });
        const bodyText = await resp.text();
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${bodyText.slice(0, 500)}`);
        const body = JSON.parse(bodyText);
        if (!body.executionArn) throw new Error(`No executionArn in response: ${bodyText}`);
        els.traceSection.classList.add("hidden");
        els.traceList.innerHTML = "";
        pollRemediation(apiUrl, body.executionArn);
      } catch (err) {
        showError(err.message || String(err));
      }
    }

    async function loadAgentTrace(apiUrl, agentExecutionId) {
      if (!agentExecutionId) return;
      try {
        const resp = await fetch(`${apiUrl}/agent-reasoning?agentExecutionId=${encodeURIComponent(agentExecutionId)}`);
        const outer = await resp.json();
        const body = outer.body ? JSON.parse(outer.body) : outer;
        const steps = body.steps || [];
        els.traceList.innerHTML = "";
        steps.forEach((s) => {
          const li = document.createElement("li");
          li.className = "step step-succeeded";
          const nextArrow = s.nextNode ? ` → ${s.nextNode}` : "";
          li.innerHTML = `
            <span class="step-icon">✓</span>
            <div class="step-body">
              <div class="step-name">${escapeHtml(s.node || "?")}${nextArrow}</div>
              <div class="step-detail">${escapeHtml(s.summary || "")}</div>
            </div>
          `;
          els.traceList.appendChild(li);
        });
        if (steps.length) els.traceSection.classList.remove("hidden");
      } catch (err) {
        console.warn("agent-reasoning fetch failed", err);
      }
    }

    function pollRemediation(apiUrl, executionArn) {
      const start = Date.now();
      const tick = async () => {
        try {
          const resp = await fetch(`${apiUrl}/remediation-status?executionArn=${encodeURIComponent(executionArn)}`);
          const body = await resp.json();
          renderSteps(body);
          if (body.status && body.status !== "RUNNING") {
            stopPolling();
            renderTerminal(body);
            return;
          }
          if (Date.now() - start > POLL_TIMEOUT_MS) {
            stopPolling();
            showError("Polling timed out after 5 minutes.");
          }
        } catch (err) {
          stopPolling();
          showError(`Polling failed: ${err.message || err}`);
        }
      };
      tick();
      pollTimer = setInterval(tick, POLL_INTERVAL_MS);
    }

    function stopPolling() {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }

    function renderSteps(body) {
      const stepMap = {};
      (body.steps || []).forEach((s) => { stepMap[s.name] = s; });
      els.stepList.innerHTML = "";
      DRIFT_STEPS.forEach((meta) => {
        const s = stepMap[meta.name] || { name: meta.name, status: "PENDING" };
        const li = document.createElement("li");
        li.className = `step step-${s.status.toLowerCase()}`;
        const icon = ({ PENDING: "·", RUNNING: "…", SUCCEEDED: "✓", SKIPPED: "—", FAILED: "×" })[s.status] || "·";
        li.innerHTML = `
          <span class="step-icon">${icon}</span>
          <div class="step-body">
            <div class="step-name">${meta.label}</div>
            <div class="step-detail">${detailFor(meta.name, s)}</div>
          </div>
        `;
        els.stepList.appendChild(li);
      });
    }

    function detailFor(name, s) {
      if (s.status === "PENDING") return "waiting";
      if (s.status === "RUNNING") return "in progress…";
      if (s.status === "SKIPPED") return "skipped";
      const out = s.output || {};
      if (name === "LoadRole") return out.roleArn || "loaded";
      if (name === "CheckManaged") return out.isManaged ? "ManagedBy=ZeroShift" : "not managed · short-circuit";
      if (name === "ScanUsage") return `${(out.actionsToRemove || []).length} action(s) flagged`;
      if (name === "ReadIntentRegistry") return `${(out.intentEntries || []).length} intent entries`;
      if (name === "ProposeReplacement") return (out.proposal && out.proposal.rationale) ? "proposal received" : "no proposal";
      if (name === "SimulateEquivalence") return (out.equivalence && out.equivalence.isEquivalent) ? `${out.equivalence.totalChecked || 0} actions checked · 0 divergences` : `${(out.equivalence && out.equivalence.divergences || []).length} divergences`;
      if (name === "ScoreRisk") return `tier: ${out.riskTier || "?"}`;
      if (name === "WaitGracePeriod") return "grace period elapsed";
      if (name === "ApplyPolicy") {
        const r = out.applyResult || {};
        return r.applied ? `applied · policy version ${r.newVersionId}` : `no-op (${r.reason || "fixture"})`;
      }
      if (name === "UpdateIntentRegistry") return `${out.removedCount || 0} registry entries updated`;
      return "done";
    }

    function renderTerminal(body) {
      els.remediationRaw.textContent = JSON.stringify(body, null, 2);
      const output = body.finalOutput || {};
      const status = output.status || body.status;

      if (status === "REMEDIATION_APPLIED") {
        els.terminalLabel.textContent = "Remediation applied";
        els.terminalDetail.textContent = `${(output.actionsRemoved || []).length} action(s) removed · tier ${output.riskTier || "?"}`;
        const applyResult = output.applyResult || {};
        els.terminalMeta.textContent = applyResult.applied
          ? `Real IAM policy mutated · new version ${applyResult.newVersionId || "?"}${applyResult.reason === "agentic" ? " · via agent" : ""}`
          : `${applyResult.reason || "fixture_role"} · outcome ${applyResult.outcome || "unknown"}`;
        if (applyResult.agentExecutionId) {
          loadAgentTrace(getApiUrl(), applyResult.agentExecutionId);
        }
      } else if (status === "APPROVAL_REQUIRED") {
        els.terminalLabel.textContent = "Human approval required";
        els.terminalDetail.textContent = `HIGH-tier drift held for approval`;
        els.terminalMeta.textContent = "Safe-mode v1: proposal recorded in DynamoDB. Policy NOT mutated.";
      } else if (status === "NO_DRIFT") {
        els.terminalLabel.textContent = "No drift";
        els.terminalDetail.textContent = "Every action has recent activity or ACTIVE intent-registry status.";
        els.terminalMeta.textContent = "";
      } else if (status === "SKIPPED_NOT_MANAGED") {
        els.terminalLabel.textContent = "Skipped";
        els.terminalDetail.textContent = "Role does not carry the ManagedBy=ZeroShift tag.";
        els.terminalMeta.textContent = "";
      } else if (status === "REJECTED_NOT_EQUIVALENT") {
        showError(`Equivalence check failed. ${(output.divergences || []).length} divergence(s).`);
        return;
      } else {
        els.terminalLabel.textContent = status || "Complete";
        els.terminalDetail.textContent = "";
        els.terminalMeta.textContent = "";
      }
      els.remediationResult.classList.remove("hidden");
    }

    async function discoverRoles() {
      const apiUrl = getApiUrl();
      if (!apiUrl) return showError("API endpoint is empty.");
      els.onboardBtn.disabled = true;
      try {
        const resp = await fetch(`${apiUrl}/discover-roles`, { method: "POST" });
        const outer = await resp.json();
        const body = outer.body ? JSON.parse(outer.body) : outer;
        renderOnboardList(body.roles || []);
      } catch (err) {
        showError(err.message || String(err));
      } finally {
        els.onboardBtn.disabled = false;
      }
    }

    function renderOnboardList(roles) {
      els.onboardList.innerHTML = "";
      const unmanaged = roles.filter((r) => !r.isManaged);
      if (unmanaged.length === 0) {
        els.onboardList.innerHTML = `<div class="section-body" style="color:var(--text-muted);">All discovered roles are already managed.</div>`;
      } else {
        unmanaged.forEach((r) => {
          const row = document.createElement("div");
          row.className = "onboard-row";
          row.innerHTML = `
            <span class="role-arn">${escapeHtml(r.roleArn)}</span>
            <select class="env-select">
              <option value="non-prod">non-prod</option>
              <option value="prod-adjacent">prod-adjacent</option>
              <option value="prod">prod</option>
            </select>
            <button class="onboard-apply">Onboard</button>
          `;
          const btn = row.querySelector(".onboard-apply");
          const sel = row.querySelector(".env-select");
          btn.addEventListener("click", async () => {
            btn.disabled = true;
            btn.textContent = "Onboarding…";
            try {
              await fetch(`${getApiUrl()}/onboard-roles`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ roleArns: [r.roleArn], environment: sel.value }),
              });
              btn.textContent = "Onboarded";
            } catch (err) {
              btn.disabled = false;
              btn.textContent = "Retry";
              showError(err.message || String(err));
            }
          });
          els.onboardList.appendChild(row);
        });
      }
      els.onboardList.classList.remove("hidden");
    }

    function setBusy(busy) {
      els.scanBtn.disabled = busy;
      els.status.textContent = busy ? "Scanning managed roles…" : "";
    }
    function hideAll() {
      els.empty.classList.add("hidden");
      els.view.classList.add("hidden");
      els.err.classList.add("hidden");
    }
    function showError(msg) {
      hideAll();
      els.errMsg.textContent = msg;
      els.err.classList.remove("hidden");
    }
    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }
  })();

  // ============================================================
  // Code Analyzer (Part 1)
  // ============================================================

  (function codeanalyzer() {
    const els = {
      fixtureBtns: document.querySelectorAll(".code-fixture-btn"),
      input: document.getElementById("code-input"),
      role: document.getElementById("code-target-role"),
      commit: document.getElementById("code-commit-hash"),
      apply: document.getElementById("code-apply-check"),
      analyzeBtn: document.getElementById("code-analyze-btn"),
      githubUrl: document.getElementById("code-github-url"),
      githubBtn: document.getElementById("code-github-btn"),
      status: document.getElementById("code-status-inline"),
      empty: document.getElementById("code-empty"),
      view: document.getElementById("code-view"),
      err: document.getElementById("code-error"),
      errMsg: document.getElementById("code-error-message"),
      summaryCount: document.getElementById("code-summary-count"),
      summaryMeta: document.getElementById("code-summary-meta"),
      staticList: document.getElementById("code-static-list"),
      augmenterSection: document.getElementById("code-augmenter-section"),
      augmenterNotes: document.getElementById("code-augmenter-notes"),
      augmenterList: document.getElementById("code-augmenter-list"),
      deltaRole: document.getElementById("code-delta-role"),
      addList: document.getElementById("code-add-list"),
      removeList: document.getElementById("code-remove-list"),
      applyStatus: document.getElementById("code-apply-status"),
      raw: document.getElementById("code-raw"),
    };

    els.fixtureBtns.forEach((btn) => {
      btn.addEventListener("click", async () => {
        els.fixtureBtns.forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const name = btn.dataset.fixture;
        try {
          const resp = await fetch(`fixtures/code_samples/${name}.py`);
          if (!resp.ok) throw new Error(`Failed to load fixture: ${resp.status}`);
          els.input.value = await resp.text();
        } catch (err) {
          showError(`Could not load code sample "${name}": ${err.message}. Serve via http:// (not file://).`);
        }
      });
    });

    els.analyzeBtn.addEventListener("click", () => analyze({ mode: "code" }));
    els.githubBtn.addEventListener("click", () => analyze({ mode: "github" }));

    async function analyze({ mode }) {
      const apiUrl = getApiUrl();
      if (!apiUrl) return showError("API endpoint is empty.");
      const targetRole = els.role.value.trim();
      if (!targetRole) return showError("Target role ARN is required.");

      const payload = {
        targetRoleArn: targetRole,
        commitHash: els.commit.value.trim() || null,
        apply: !!els.apply.checked,
      };
      let route = "/analyze-code";
      if (mode === "github") {
        const url = els.githubUrl.value.trim();
        if (!url) return showError("GitHub URL is required.");
        payload.repoUrl = url;
        route = "/analyze-github";
      } else {
        const code = els.input.value;
        if (!code.trim()) return showError("Paste some Python source first.");
        payload.code = code;
      }

      setBusy(true);
      hideAll();

      try {
        const resp = await fetch(`${apiUrl}${route}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const bodyText = await resp.text();
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${bodyText.slice(0, 500)}`);
        const outer = JSON.parse(bodyText);
        const body = outer.body ? JSON.parse(outer.body) : outer;
        render(body);
      } catch (err) {
        showError(err.message || String(err));
      } finally {
        setBusy(false);
      }
    }

    function render(body) {
      els.raw.textContent = JSON.stringify(body, null, 2);
      const staticActions = ((body.staticExtraction || {}).actions) || [];
      const augmenter = body.llmAugmentation || {};
      const additional = augmenter.additionalActions || [];
      const delta = body.delta || {};

      els.summaryCount.textContent = `${staticActions.length + additional.length} action(s) inferred`;
      els.summaryMeta.textContent = `static: ${staticActions.length} · augmenter: ${additional.length} (${augmenter.confidence || "n/a"}) · target: ${body.targetRoleArn || "?"}`;

      _fillList(els.staticList, staticActions);
      _fillList(els.augmenterList, additional);
      els.augmenterNotes.textContent = augmenter.notes || "—";
      if (!additional.length) {
        els.augmenterSection.style.opacity = 0.6;
      } else {
        els.augmenterSection.style.opacity = 1;
      }

      els.deltaRole.textContent = delta.roleArn || body.targetRoleArn || "—";
      _fillList(els.addList, delta.toAdd || []);
      _fillList(els.removeList, delta.toMarkPendingRemoval || []);

      const applyResult = body.applyResult;
      if (applyResult) {
        els.applyStatus.textContent = `Applied · ${applyResult.activeAdded} ACTIVE rows written · ${applyResult.pendingRemovalMarked} PENDING_REMOVAL rows marked (grace expires ${applyResult.gracePeriodExpiry}).`;
        els.applyStatus.style.color = "var(--ok)";
      } else {
        els.applyStatus.textContent = "Dry run — check 'Apply changes to intent registry' to persist.";
        els.applyStatus.style.color = "var(--text-muted)";
      }

      els.view.classList.remove("hidden");
    }

    function _fillList(el, items) {
      el.innerHTML = "";
      if (!items.length) {
        const li = document.createElement("li");
        li.textContent = "(none)";
        li.style.color = "var(--text-dim)";
        el.appendChild(li);
        return;
      }
      items.forEach((a) => {
        const li = document.createElement("li");
        li.textContent = a;
        el.appendChild(li);
      });
    }

    function setBusy(busy) {
      els.analyzeBtn.disabled = busy;
      els.githubBtn.disabled = busy;
      els.status.textContent = busy ? "Analyzing…" : "";
    }
    function hideAll() {
      els.empty.classList.add("hidden");
      els.view.classList.add("hidden");
      els.err.classList.add("hidden");
    }
    function showError(msg) {
      hideAll();
      els.errMsg.textContent = msg;
      els.err.classList.remove("hidden");
    }
  })();
})();
