/* Job board page. */
(function () {
  "use strict";

  const state = { items: [], total: 0, offset: 0, limit: 24, loading: false, sources: {} };
  const $ = (selector) => document.querySelector(selector);

  function icon(name, size = 16) {
    return `<svg class="icon" width="${size}" height="${size}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
  }

  function readFilters() {
    return {
      query: $("#job-search")?.value.trim() || "",
      location: $("#job-location")?.value.trim() || "",
      remote: $("#job-remote")?.value || "",
      source: $("#job-source")?.value || "",
      postedDays: $("#job-posted")?.value || "",
      sort: $("#job-sort")?.value || "newest",
      minScore: Number($("#job-min-score")?.value || 0),
    };
  }

  const POSTED_LABELS = {
    "1": "Last 24 hours",
    "3": "Last 3 days",
    "7": "Last 7 days",
    "14": "Last 2 weeks",
    "30": "Last 30 days",
  };

  function queryString(filters, extra = {}) {
    const params = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => {
      if (value === "" || value === null || value === undefined) return;
      if (key === "minScore" && !value) return;
      const name = key === "minScore" ? "min_score" : key === "postedDays" ? "posted_within_days" : key;
      params.set(name, value);
    });
    Object.entries(extra).forEach(([key, value]) => params.set(key, value));
    return params.toString();
  }

  function activeFilterEntries(filters) {
    const entries = [];
    if (filters.query) entries.push(["Search", `“${filters.query}”`]);
    if (filters.location) entries.push(["Location", filters.location]);
    if (filters.remote === "true") entries.push(["Work mode", "Remote only"]);
    if (filters.remote === "false") entries.push(["Work mode", "On-site / hybrid"]);
    if (filters.source) entries.push(["Source", JP.titleCase(filters.source)]);
    if (filters.postedDays) entries.push(["Posted", POSTED_LABELS[filters.postedDays] || `${filters.postedDays} days`]);
    if (filters.minScore) entries.push(["Min score", `${filters.minScore}+`]);
    return entries;
  }

  function renderActiveFilters(filters) {
    const row = $("#active-filters");
    const chips = $("#active-filter-chips");
    if (!row || !chips) return;
    const entries = activeFilterEntries(filters);
    row.hidden = entries.length === 0;
    chips.innerHTML = entries
      .map(([label, value]) => `<span class="filter-chip"><b>${JP.esc(label)}</b>${JP.esc(value)}</span>`)
      .join("");
  }

  function clearFilters() {
    ["#job-search", "#job-location"].forEach((selector) => {
      const input = $(selector);
      if (input) input.value = "";
    });
    ["#job-remote", "#job-source", "#job-posted", "#job-sort"].forEach((selector) => {
      const select = $(selector);
      if (select) select.value = select.id === "job-sort" ? "newest" : "";
    });
    const score = $("#job-min-score");
    if (score) score.value = "0";
    const scoreLabel = $("#min-score-label");
    if (scoreLabel) scoreLabel.textContent = "any";
    loadJobs();
  }

  function setSync(text, tone = "") {
    const element = $("#jobs-sync");
    if (!element) return;
    element.innerHTML = `<span class="status-dot ${tone}"></span> ${JP.esc(text)}`;
  }

  function renderStats(stats) {
    if (!stats) return;
    const jobs = $("#stat-jobs");
    if (jobs) jobs.textContent = stats.total ?? "—";
    const sources = $("#stat-sources");
    if (sources) sources.textContent = Object.keys(stats.sources || {}).length || "0";
    const remote = $("#stat-remote");
    if (remote) remote.textContent = stats.remote ?? "—";
    const scored = $("#stat-scored");
    if (scored) scored.textContent = stats.scored ?? "—";
  }

  function renderSourceOptions(sources) {
    const select = $("#job-source");
    if (!select) return;
    const current = select.value;
    const entries = Object.entries(sources || {}).sort((a, b) => b[1] - a[1]);
    select.innerHTML =
      '<option value="">All sources</option>' +
      entries
        .map(([source, count]) => `<option value="${JP.esc(source)}">${JP.esc(JP.titleCase(source))} (${count})</option>`)
        .join("");
    if (entries.some(([source]) => source === current)) select.value = current;
  }

  function renderGrid(items, append = false) {
    const grid = $("#job-grid");
    if (!grid) return;
    if (!append) grid.innerHTML = "";
    const html = items.map((item) => JP.jobCard(item)).join("");
    if (append) grid.insertAdjacentHTML("beforeend", html);
    else grid.innerHTML = html;
    $("#jobs-empty").hidden = state.total !== 0;
    $("#job-grid").hidden = state.total === 0;
    $("#load-more-row").hidden = state.items.length >= state.total || state.total === 0;
  }

  async function loadStats() {
    try {
      const [all, remote, scored] = await Promise.all([
        JP.api("/api/jobs?limit=1"),
        JP.api("/api/jobs?remote=true&limit=1"),
        JP.api("/api/jobs?min_score=1&limit=1"),
      ]);
      const sourceCount = Object.keys(all.sources || {}).length;
      renderStats({
        total: all.total,
        sources: all.sources,
        remote: remote.total,
        scored: scored.total,
      });
      const foot = $("#stat-scored-foot");
      if (foot) foot.textContent = `${all.total ? Math.round((scored.total / all.total) * 100) : 0}% of board`;
      const jobsFoot = $("#stat-jobs-foot");
      if (jobsFoot) {
        jobsFoot.textContent = `${sourceCount} source${sourceCount === 1 ? "" : "s"} represented`;
      }
    } catch (error) {
      /* Stats are decorative; leave the em dashes on failure. */
    }
  }

  async function loadJobs(append = false) {
    if (state.loading) return;
    state.loading = true;
    if (!append) {
      setSync("Loading stored jobs…");
      const grid = $("#job-grid");
      if (!grid.querySelector(".job-card")) {
        grid.innerHTML = '<div class="skeleton-card"></div><div class="skeleton-card"></div><div class="skeleton-card"></div>';
      }
    }
    const filters = readFilters();
    const offset = append ? state.items.length : 0;
    try {
      const data = await JP.api(`/api/jobs?${queryString(filters, { limit: state.limit, offset })}`);
      state.items = append ? [...state.items, ...(data.items || [])] : data.items || [];
      state.total = data.total || 0;
      state.sources = data.sources || {};
      renderSourceOptions(state.sources);
      renderActiveFilters(filters);
      renderGrid(data.items || [], append);
      const active = activeFilterEntries(filters);
      const summary = $("#jobs-summary");
      if (summary) {
        summary.textContent = state.total
          ? `${state.total} role${state.total === 1 ? "" : "s"}${active.length ? ` · ${active.length} filter${active.length === 1 ? "" : "s"} active` : " on your board"}`
          : "";
      }
      const empty = $("#jobs-empty");
      if (empty) {
        const heading = empty.querySelector("h3");
        const copy = empty.querySelector("p");
        const clear = $("#empty-clear-filters");
        if (clear) clear.hidden = !(state.total === 0 && active.length);
        if (state.total === 0 && active.length) {
          if (heading) heading.textContent = "No roles match these filters";
          if (copy) copy.textContent = "Widen the location, work mode, date or score filters, or clear them to see the whole board.";
        } else {
          if (heading) heading.textContent = "No postings yet";
          if (copy) copy.textContent = "Fetch jobs from the free sources. Every posting comes from a live job board.";
        }
      }
      setSync(`Synced ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`);
      if (!append) loadStats();
    } catch (error) {
      JP.toast(error.message, "error");
      setSync("Load failed", "bad");
    } finally {
      state.loading = false;
    }
  }

  async function refreshJobs() {
    const button = $("#refresh-jobs-btn");
    if (!button || button.disabled) return;
    const filters = readFilters();
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> Fetching…';
    setSync("Fetching from live sources…", "");
    try {
      const data = await JP.api("/api/jobs/refresh", {
        method: "POST",
        body: {
          query: filters.query,
          location: filters.location,
          remote: filters.remote === "" ? null : filters.remote === "true",
          posted_within_days: filters.postedDays ? Number(filters.postedDays) : null,
          sources: filters.source ? [filters.source] : [],
          limit: 30,
          use_llm: true,
        },
      });
      const report = data.report || {};
      const loaded = report.stored ?? report.fetched ?? 0;
      const errors = report.errors || {};
      const errorCount = Object.keys(errors).length;
      JP.toast(
        errorCount
          ? `Fetched ${loaded} live roles; ${errorCount} source${errorCount === 1 ? "" : "s"} had issues.`
          : `Fetched and scored ${loaded} live roles.`,
        errorCount ? "info" : "success"
      );
      if (errorCount) console.warn("Source errors", errors);
      if (data.ranked && data.ranked.length) {
        $("#job-sort").value = "score";
      }
      await loadJobs();
    } catch (error) {
      JP.toast(error.message, "error");
      setSync("Fetch failed", "bad");
    } finally {
      button.disabled = false;
      button.innerHTML = original;
    }
  }

  async function rankJobs() {
    const button = $("#rank-jobs-btn");
    if (!button || button.disabled) return;
    const filters = readFilters();
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> Ranking…';
    setSync("Scoring stored jobs…");
    try {
      const data = await JP.api("/api/jobs/rank", {
        method: "POST",
        body: {
          query: filters.query,
          location: filters.location,
          remote: filters.remote === "" ? null : filters.remote === "true",
          posted_within_days: filters.postedDays ? Number(filters.postedDays) : null,
          sources: filters.source ? [filters.source] : [],
          limit: 30,
          use_llm: true,
        },
      });
      $("#job-sort").value = "score";
      JP.toast(`Ranked ${data.count || 0} roles against your profile.`, "success");
      await loadJobs();
    } catch (error) {
      JP.toast(error.message, "error");
    } finally {
      button.disabled = false;
      button.innerHTML = original;
    }
  }

  function importJobModal() {
    const modal = JP.modal({
      title: "Import a job posting",
      subtitle: "Paste a public URL. JobPilot reads JSON-LD or OpenGraph metadata.",
      body: `<form id="import-form" class="stack-form">
        <label class="field"><span>Job URL</span><input type="url" id="import-url" placeholder="https://company.com/careers/role" required></label>
        <p class="fine-print">Only public pages are fetched. The text is treated as untrusted data.</p>
      </form>`,
      footer: `<button class="btn btn-ghost" data-modal-close>Cancel</button><button class="btn btn-primary" id="import-submit">Import role</button>`,
    });
    modal.element.querySelector("#import-submit").addEventListener("click", async (event) => {
      const url = modal.element.querySelector("#import-url").value.trim();
      if (!url) return;
      event.currentTarget.disabled = true;
      event.currentTarget.textContent = "Importing…";
      try {
        const data = await JP.api("/api/jobs/import", { method: "POST", body: { url } });
        JP.closeModal();
        JP.toast("Job imported.", "success");
        await loadJobs();
        JP.openJob(data.job.id);
      } catch (error) {
        JP.toast(error.message, "error");
        event.currentTarget.disabled = false;
        event.currentTarget.textContent = "Import role";
      }
    });
  }

  function bind() {
    let timer = null;
    const debounced = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => loadJobs(), 280);
    };
    $("#job-search")?.addEventListener("input", debounced);
    $("#job-location")?.addEventListener("input", debounced);
    ["#job-remote", "#job-source", "#job-posted", "#job-sort"].forEach((selector) => {
      $(selector)?.addEventListener("change", () => loadJobs());
    });
    $("#job-min-score")?.addEventListener("input", (event) => {
      const value = Number(event.currentTarget.value);
      $("#min-score-label").textContent = value ? `${value}+` : "any";
      window.clearTimeout(timer);
      timer = window.setTimeout(() => loadJobs(), 220);
    });
    $("#clear-filters-btn")?.addEventListener("click", clearFilters);
    $("#refresh-jobs-btn")?.addEventListener("click", refreshJobs);
    $("#rank-jobs-btn")?.addEventListener("click", rankJobs);
    $("#import-job-btn")?.addEventListener("click", importJobModal);
    $("#load-more-btn")?.addEventListener("click", () => loadJobs(true));
    document.querySelectorAll("[data-empty-action]").forEach((button) => {
      if (button.dataset.emptyAction === "refresh") {
        button.addEventListener("click", refreshJobs);
      }
      if (button.dataset.emptyAction === "clear") {
        button.addEventListener("click", clearFilters);
      }
    });
    document.addEventListener("jobpilot:tracked", () => loadJobs());
    document.addEventListener("jobpilot:ready", (event) => {
      const counts = event.detail?.counts || {};
      if (counts.jobs !== undefined && state.total === 0) {
        $("#stat-jobs").textContent = counts.jobs;
      }
    });
  }

  function applyUrlFilters() {
    const params = new URLSearchParams(window.location.search);
    const set = (selector, value) => {
      const element = $(selector);
      if (element && value !== null) element.value = value;
    };
    set("#job-search", params.get("q"));
    set("#job-location", params.get("location"));
    set("#job-remote", params.get("remote"));
    set("#job-source", params.get("source"));
    set("#job-posted", params.get("posted"));
    set("#job-sort", params.get("sort"));
    const score = params.get("min_score");
    if (score) {
      set("#job-min-score", score);
      const label = $("#min-score-label");
      if (label) label.textContent = Number(score) ? `${score}+` : "any";
    }
  }

  async function init() {
    bind();
    applyUrlFilters();
    await loadJobs();
    const jobId = new URLSearchParams(window.location.search).get("job");
    if (jobId) JP.openJob(jobId);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
