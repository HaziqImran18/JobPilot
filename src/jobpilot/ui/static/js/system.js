/* System page: health checks, budget, sources, traces and model usage. */
(function () {
  "use strict";

  const state = { stats: null, mcp: null, traces: [], usage: [] };
  const $ = (selector) => document.getElementById(selector.replace(/^#/, ""));

  function icon(name, size = 14) {
    return `<svg class="icon" width="${size}" height="${size}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
  }

  function renderStats(stats) {
    state.stats = stats;
    const mode = $("sys-mode");
    if (mode) mode.textContent = stats.mode === "live" ? "Live" : "Setup";
    const modeFoot = $("sys-mode-foot");
    if (modeFoot) modeFoot.textContent = `v${stats.version} · ${stats.embedding_provider} embeddings`;
    const budget = stats.budget || {};
    const spend = Number(budget.daily_spent || 0);
    const cap = Number(budget.daily_cap || 1);
    if ($("sys-spend")) $("sys-spend").textContent = JP.formatMoney(spend);
    const spendBar = $("sys-spend-bar");
    if (spendBar) spendBar.style.width = `${Math.min(100, (spend / cap) * 100)}%`;
    const database = String(stats.database || "").split(/[\\/]/).pop();
    if ($("sys-db")) $("sys-db").textContent = database || "SQLite";
    if ($("sys-db-foot")) {
      const skills = stats.profile?.skills || 0;
      $("sys-db-foot").textContent = `${skills} profile skills · ${stats.counts?.jobs || 0} jobs`;
    }

    const budgetSpend = $("budget-spend");
    if (budgetSpend) budgetSpend.textContent = JP.formatMoney(spend);
    if ($("budget-cap")) $("budget-cap").textContent = `of ${JP.formatMoney(cap)} daily`;
    if ($("budget-bar")) $("budget-bar").style.width = `${Math.min(100, (spend / cap) * 100)}%`;
    if ($("budget-calls")) $("budget-calls").textContent = `${budget.calls_today || 0} calls today`;
    if ($("budget-remaining")) $("budget-remaining").textContent = `${JP.formatMoney(Math.max(0, cap - spend))} remaining · ${(budget.tokens_today || 0).toLocaleString()} tokens`;

    renderSettings(stats.settings || {});
  }

  function renderSettings(settings) {
    const container = $("settings-list");
    if (!container) return;
    const rows = [
      ["Runtime", settings.mode],
      ["Fast model", settings.model_fast],
      ["Strong model", settings.model_strong],
      ["Embeddings", settings.embedding_provider],
      ["Session cap", JP.formatMoney(settings.max_budget_usd)],
      ["Daily cap", JP.formatMoney(settings.daily_budget_usd)],
      ["Max steps", settings.max_steps_per_run],
      ["Default location", settings.default_location],
      ["Remote default", settings.default_remote ? "yes" : "no"],
      ["Preload MCP", settings.preload_mcp ? "yes" : "no"],
    ];
    container.innerHTML = rows
      .filter(([, value]) => value !== undefined && value !== null && value !== "")
      .map(([label, value]) => `<div class="settings-row"><span>${JP.esc(label)}</span><span>${JP.esc(String(value))}</span></div>`)
      .join("");
  }

  function renderChecks(report) {
    const grid = $("check-grid");
    if (!grid) return;
    const checks = (report.checks || []).filter((check) => check.group !== "sources");
    grid.innerHTML = checks.length
      ? checks
          .map(
            (check) => `<article class="check-card ${check.ok ? "" : "bad"}">
              <div class="check-top">
                ${icon(check.ok ? "check" : "alert", 15)}
                <strong>${JP.esc(JP.titleCase(check.name))}</strong>
                <span class="check-state ${check.ok ? "ok" : "bad"}">${check.ok ? "pass" : "fail"}</span>
              </div>
              <p class="check-detail">${JP.esc(check.detail || "No detail.")}</p>
              <div class="check-foot">${JP.esc(check.group || "system")}${check.latency_ms !== null && check.latency_ms !== undefined ? ` · ${check.latency_ms}ms` : ""}</div>
            </article>`
          )
          .join("")
      : '<p class="muted" style="font-size:12px">No checks returned.</p>';

    const sourceChecks = (report.checks || []).filter((check) => check.group === "sources");
    const tbody = document.querySelector("#source-table tbody");
    if (tbody) {
      tbody.innerHTML = sourceChecks.length
        ? sourceChecks
            .map(
              (check) => `<tr>
                <td>${JP.esc(check.name.replace(/^Source:\s*/i, ""))}</td>
                <td><span class="trace-status ${check.ok ? "completed" : "error"}">${icon(check.ok ? "check" : "alert", 12)} ${check.ok ? "up" : "down"}</span></td>
                <td>${check.latency_ms !== null && check.latency_ms !== undefined ? `${check.latency_ms}ms` : "—"}</td>
                <td>${JP.esc(check.detail || "")}</td>
              </tr>`
            )
            .join("")
        : '<tr><td colspan="4" class="table-empty">No source checks yet.</td></tr>';
    }
    if ($("source-count")) $("source-count").textContent = String(sourceChecks.length);

    const modeFoot = $("sys-mode-foot");
    if (modeFoot) modeFoot.textContent = `${report.mode === "live" ? "Live" : "Setup"} · v${report.version} · checked ${JP.formatRelative(report.generated_at)}`;
  }

  function renderMcp(mcp) {
    state.mcp = mcp;
    if ($("sys-tools")) $("sys-tools").textContent = mcp.count ?? 0;
    if ($("sys-tools-foot")) {
      const errors = Object.keys(mcp.errors || {}).length;
      $("sys-tools-foot").textContent = errors ? `${errors} server${errors === 1 ? "" : "s"} failed` : `${mcp.servers?.length || 0} servers ready`;
    }
  }

  async function loadDoctor(options = {}) {
    const query = new URLSearchParams();
    if (options.probe) query.set("probe", "1");
    if (options.mcp) query.set("mcp", "1");
    const grid = $("check-grid");
    grid.innerHTML = '<div class="skeleton-card"></div><div class="skeleton-card"></div>';
    document.querySelectorAll("#doctor-quick, #doctor-sources, #doctor-mcp, #doctor-full").forEach((button) => {
      button.disabled = true;
    });
    try {
      const report = await JP.api(`/api/system/doctor${query.toString() ? `?${query}` : ""}`);
      renderChecks(report);
      JP.toast(options.probe || options.mcp ? "Health check complete." : "Quick check complete.", "success", 2200);
    } catch (error) {
      JP.toast(error.message, "error");
      grid.innerHTML = `<p style="color:var(--danger);font-size:12px">${JP.esc(error.message)}</p>`;
    } finally {
      document.querySelectorAll("#doctor-quick, #doctor-sources, #doctor-mcp, #doctor-full").forEach((button) => {
        button.disabled = false;
      });
    }
  }

  async function loadMcp() {
    try {
      const data = await JP.api("/api/system/mcp?start=1");
      renderMcp(data);
    } catch (error) {
      if ($("sys-tools")) $("sys-tools").textContent = "!";
      if ($("sys-tools-foot")) $("sys-tools-foot").textContent = "MCP unavailable";
    }
  }

  async function loadStats() {
    try {
      const stats = await JP.api("/api/system/stats");
      renderStats(stats);
    } catch (error) {
      JP.toast(error.message, "error");
    }
  }

  async function loadTraces() {
    const body = document.querySelector("#trace-table tbody");
    if (!body) return;
    try {
      const data = await JP.api("/api/system/traces?limit=30");
      state.traces = data.traces || [];
      body.innerHTML = state.traces.length
        ? state.traces
            .map(
              (trace) => `<tr data-run-id="${JP.esc(trace.run_id)}" style="cursor:pointer">
                <td class="mono">${JP.esc(trace.run_id.slice(0, 14))}</td>
                <td class="mono">${JP.esc((trace.session_id || "—").slice(0, 12))}</td>
                <td>${JP.esc(JP.formatRelative(trace.started_at))}</td>
                <td>${trace.steps || 0}</td>
                <td><span class="trace-status ${JP.esc(trace.status)}">${icon(trace.status === "completed" ? "check" : trace.status === "error" ? "alert" : "clock", 12)} ${JP.esc(trace.status)}</span></td>
                <td>${JP.formatMoney(trace.cost_usd || 0)}</td>
                <td style="text-align:right;color:var(--accent)">${icon("chevron-right", 14)}</td>
              </tr>`
            )
            .join("")
        : '<tr><td colspan="7" class="table-empty">No runs traced yet. Ask the agent something.</td></tr>';
      body.querySelectorAll("[data-run-id]").forEach((row) => {
        row.addEventListener("click", () => openTrace(row.dataset.runId));
      });
    } catch (error) {
      body.innerHTML = `<tr><td colspan="7" class="table-empty">${JP.esc(error.message)}</td></tr>`;
    }
  }

  async function openTrace(runId) {
    try {
      const data = await JP.api(`/api/system/traces/${encodeURIComponent(runId)}`);
      const events = data.events || [];
      const body = `<div class="trace-json">${JP.esc(
        events.map((event) => JSON.stringify(event)).join("\n")
      )}</div>`;
      JP.modal({
        title: "Run trace",
        subtitle: `${events.length} events · ${runId}`,
        body,
        wide: true,
        footer: '<button class="btn btn-ghost" data-modal-close>Close</button>',
      });
    } catch (error) {
      JP.toast(error.message, "error");
    }
  }

  async function loadUsage() {
    const body = document.querySelector("#usage-table tbody");
    if (!body) return;
    try {
      const data = await JP.api("/api/system/usage");
      state.usage = data.usage || [];
      body.innerHTML = state.usage.length
        ? state.usage
            .map((row) => {
              const tokens = (row.prompt_tokens || 0) + (row.completion_tokens || 0);
              return `<tr>
                <td>${JP.esc(JP.formatRelative(row.created_at))}</td>
                <td class="mono">${JP.esc((row.run_id || "—").slice(0, 14))}</td>
                <td>${JP.esc(row.model || "—")}</td>
                <td>${tokens.toLocaleString()}</td>
                <td>${JP.formatMoney(row.cost || 0)}</td>
                <td>${row.cached ? '<span class="badge badge-success">hit</span>' : '<span class="badge badge-ghost">miss</span>'}</td>
              </tr>`;
            })
            .join("")
        : '<tr><td colspan="6" class="table-empty">No model calls recorded yet.</td></tr>';
      if ($("usage-count")) $("usage-count").textContent = String(state.usage.length);
    } catch (error) {
      body.innerHTML = `<tr><td colspan="6" class="table-empty">${JP.esc(error.message)}</td></tr>`;
    }
  }

  function bind() {
    $("#doctor-quick")?.addEventListener("click", () => loadDoctor());
    $("#doctor-sources")?.addEventListener("click", () => loadDoctor({ probe: true }));
    $("#doctor-mcp")?.addEventListener("click", () => loadDoctor({ mcp: true }));
    $("#doctor-full")?.addEventListener("click", () => loadDoctor({ probe: true, mcp: true }));
    $("#refresh-traces")?.addEventListener("click", loadTraces);
  }

  async function init() {
    bind();
    await loadStats();
    loadDoctor();
    loadMcp();
    loadTraces();
    loadUsage();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
