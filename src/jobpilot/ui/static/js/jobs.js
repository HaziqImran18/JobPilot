/* Job board page. */
(function () {
  "use strict";

  const state = {
    items: [],
    total: 0,
    exact: 0,
    related: 0,
    limit: 24,
    loading: false,
    sources: {},
    lastRun: null,
  };
  const $ = (selector) => document.querySelector(selector);

  function icon(name, size = 16) {
    return `<svg class="icon" width="${size}" height="${size}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
  }

  function readFilters() {
    return {
      query: $("#job-search")?.value.trim() || "",
      location: $("#job-location")?.value.trim() || "",
      category: $("#job-category")?.value || "",
      workplace: $("#job-workplace")?.value || "",
      seniority: $("#job-seniority")?.value || "",
      employment: $("#job-employment")?.value || "",
      source: $("#job-source")?.value || "",
      postedDays: $("#job-posted")?.value || "",
      sort: $("#job-sort")?.value || "relevance",
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

  const CATEGORY_LABELS = {
    software: "Software",
    ai_ml: "AI / ML",
    data: "Data",
    devops: "DevOps & cloud",
    security: "Security",
    engineering: "Engineering",
    finance: "Finance",
    banking: "Banking",
    accounting: "Accounting",
    sales: "Sales",
    marketing: "Marketing",
    hr: "HR",
    support: "Support",
    operations: "Operations",
    management: "Management",
    healthcare: "Healthcare",
    education: "Education",
    legal: "Legal",
    design: "Design",
    admin: "Admin",
  };

  const WORKPLACE_LABELS = { remote: "Remote", hybrid: "Hybrid", onsite: "On-site" };

  function queryString(filters, extra = {}) {
    const params = new URLSearchParams();
    const map = {
      query: "query",
      location: "location",
      category: "category",
      workplace: "workplace_type",
      seniority: "seniority",
      employment: "employment_type",
      source: "source",
      postedDays: "posted_within_days",
      sort: "sort",
      minScore: "min_score",
    };
    Object.entries(filters).forEach(([key, value]) => {
      if (value === "" || value === null || value === undefined) return;
      if (key === "minScore" && !value) return;
      params.set(map[key] || key, value);
    });
    Object.entries(extra).forEach(([key, value]) => {
      if (value === null || value === undefined || value === "") return;
      params.set(key, value);
    });
    return params.toString();
  }

  function activeFilterEntries(filters) {
    const entries = [];
    if (filters.query) entries.push(["Search", `“${filters.query}”`]);
    if (filters.location) entries.push(["Location", filters.location]);
    if (filters.category) entries.push(["Category", CATEGORY_LABELS[filters.category] || filters.category]);
    if (filters.workplace) entries.push(["Workplace", WORKPLACE_LABELS[filters.workplace] || filters.workplace]);
    if (filters.seniority) entries.push(["Seniority", JP.titleCase(filters.seniority)]);
    if (filters.employment) entries.push(["Employment", JP.titleCase(filters.employment)]);
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

  function resetFilters() {
    ["#job-search", "#job-location"].forEach((selector) => {
      const input = $(selector);
      if (input) input.value = "";
    });
    ["#job-category", "#job-workplace", "#job-seniority", "#job-employment", "#job-source", "#job-posted"].forEach((selector) => {
      const select = $(selector);
      if (select) select.value = "";
    });
    const sort = $("#job-sort");
    if (sort) sort.value = "relevance";
    const score = $("#job-min-score");
    if (score) score.value = "0";
    const scoreLabel = $("#min-score-label");
    if (scoreLabel) scoreLabel.textContent = "any";
  }

  function clearFilters() {
    resetFilters();
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
    grid.hidden = state.total === 0;
    $("#load-more-row").hidden = state.items.length >= state.total || state.total === 0;
  }

  function renderInterpreted(interpreted) {
    const row = $("#interpreted-row");
    if (!row) return;
    if (!interpreted) {
      row.innerHTML = "";
      return;
    }
    const entries = [];
    if (interpreted.query) entries.push(["Role", interpreted.query]);
    if (interpreted.location) entries.push(["Location", interpreted.location]);
    if (interpreted.category) entries.push(["Category", interpreted.category]);
    if (interpreted.workplace_type) entries.push(["Workplace", WORKPLACE_LABELS[interpreted.workplace_type] || interpreted.workplace_type]);
    if (interpreted.seniority) entries.push(["Seniority", JP.titleCase(interpreted.seniority)]);
    if (interpreted.employment_type) entries.push(["Employment", JP.titleCase(interpreted.employment_type)]);
    if (interpreted.posted_within_days) entries.push(["Posted", `Last ${interpreted.posted_within_days} days`]);
    row.innerHTML = entries
      .map(([label, value]) => `<span class="interpreted-chip"><b>${JP.esc(label)}</b>${JP.esc(value)}</span>`)
      .join("");
  }

  function renderSourceStatus(status) {
    const holder = $("#source-status");
    if (!holder) return;
    const items = status || [];
    if (!items.length) {
      holder.innerHTML = '<span class="source-state empty">No live source has run yet.</span>';
      return;
    }
    holder.innerHTML = items
      .map((source) => {
        const tone = source.state === "ok" ? "ok" : source.state === "error" ? "bad" : "empty";
        const detail =
          source.state === "ok"
            ? `${source.count} result${source.count === 1 ? "" : "s"}${source.latency_ms ? ` · ${source.latency_ms} ms` : ""}`
            : source.state === "error"
              ? source.error || "failed"
              : "no results";
        return `<span class="source-state ${tone}" title="${JP.esc(detail)}"><span class="status-dot ${tone === "bad" ? "bad" : ""}"></span><b>${JP.esc(JP.titleCase(source.name))}</b><small>${JP.esc(detail)}</small></span>`;
      })
      .join("");
  }

  function renderInsight(data, filters) {
    const panel = $("#insight-panel");
    if (!panel) return;
    const interpreted = data.interpreted || {};
    const status = data.source_status || [];
    const hasSearch = filters.query || filters.location || filters.category || filters.workplace;
    if (!hasSearch && !status.length) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    const title = $("#insight-title");
    const copy = $("#insight-copy");
    const time = $("#insight-time");
    const count = data.total || 0;
    if (title) {
      title.textContent = data.ok === false ? "Live search could not complete" : `${count} role${count === 1 ? "" : "s"} match this request`;
    }
    if (copy) {
      const bits = [];
      if (data.cached) bits.push("Served from a recent live snapshot");
      else if (data.fetched !== undefined) bits.push(`${data.fetched} new posting${data.fetched === 1 ? "" : "s"} retrieved`);
      if (data.exact !== undefined) bits.push(`${data.exact} exact · ${data.related || 0} related`);
      if (data.duration_ms) bits.push(`${(data.duration_ms / 1000).toFixed(1)}s`);
      copy.textContent = data.message || bits.join(" · ");
    }
    if (time) {
      time.textContent = data.retrieved_at ? `Retrieved ${JP.formatRelative(data.retrieved_at)}` : "";
    }
    renderInterpreted(interpreted);
    renderSourceStatus(status);
  }

  async function loadStats() {
    try {
      const [all, remote, scored] = await Promise.all([
        JP.api("/api/jobs?limit=1&live=0"),
        JP.api("/api/jobs?workplace_type=remote&limit=1&live=0"),
        JP.api("/api/jobs?min_score=1&limit=1&live=0"),
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

  async function loadJobs(append = false, options = {}) {
    if (state.loading) return;
    state.loading = true;
    const live = options.live === undefined ? true : options.live;
    if (!append) {
      setSync(live ? "Searching live sources…" : "Filtering the stored board…");
      const grid = $("#job-grid");
      if (grid && !grid.querySelector(".job-card")) {
        grid.innerHTML = '<div class="skeleton-card"></div><div class="skeleton-card"></div><div class="skeleton-card"></div>';
      }
    }
    const filters = readFilters();
    const offset = append ? state.items.length : 0;
    try {
      const data = await JP.api(`/api/jobs?${queryString(filters, { limit: state.limit, offset })}${live ? "" : "&live=0"}`);
      state.items = append ? [...state.items, ...(data.items || [])] : data.items || [];
      state.total = data.total || 0;
      state.exact = data.exact || 0;
      state.related = data.related || 0;
      state.sources = data.sources || {};
      state.lastRun = data;
      renderSourceOptions(state.sources);
      renderActiveFilters(filters);
      renderGrid(data.items || [], append);
      renderInsight(data, filters);
      if (data.stats) renderStats(data.stats);
      const active = activeFilterEntries(filters);
      const summary = $("#jobs-summary");
      if (summary) {
        summary.textContent = state.total
          ? `${state.total} role${state.total === 1 ? "" : "s"}${active.length ? ` · ${active.length} filter${active.length === 1 ? "" : "s"} active` : " on your board"}${state.related ? ` · ${state.related} related` : ""}`
          : data.message || "";
      }
      const empty = $("#jobs-empty");
      if (empty) {
        const heading = empty.querySelector("h3");
        const copy = empty.querySelector("p");
        const clear = $("#empty-clear-filters");
        if (clear) clear.hidden = !(state.total === 0 && active.length);
        if (state.total === 0 && active.length) {
          if (heading) heading.textContent = data.ok === false ? "Live search failed" : "No roles match these filters";
          if (copy) copy.textContent = data.message || "Widen the category, location, workplace or date filters, or clear them to see the whole board.";
        } else {
          if (heading) heading.textContent = "No postings yet";
          if (copy) copy.textContent = "Search live sources. Every posting comes from a real job board, never a demo list.";
        }
      }
      if (data.message && data.ok === false) {
        setSync("Live search failed", "bad");
        JP.toast(data.message, "error");
      } else {
        const stamp = data.retrieved_at || new Date().toISOString();
        setSync(`${data.cached ? "Snapshot" : "Searched"} ${JP.formatRelative(stamp)}${data.duration_ms ? ` · ${(data.duration_ms / 1000).toFixed(1)}s` : ""}`, data.ok === false ? "bad" : "");
      }
      if (!append) loadStats();
    } catch (error) {
      JP.toast(error.message, "error");
      setSync("Load failed", "bad");
    } finally {
      state.loading = false;
    }
  }

  function refreshPayload(filters) {
    return {
      query: filters.query,
      location: filters.location,
      category: filters.category,
      workplace_type: filters.workplace,
      seniority: filters.seniority,
      employment_type: filters.employment,
      posted_within_days: filters.postedDays ? Number(filters.postedDays) : null,
      sources: filters.source ? [filters.source] : [],
      limit: 30,
      sort: filters.sort,
      use_llm: true,
    };
  }

  async function refreshJobs() {
    const button = $("#refresh-jobs-btn");
    if (!button || button.disabled) return;
    const filters = readFilters();
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> Searching…';
    setSync("Fetching live postings from every enabled source…", "");
    try {
      const data = await JP.api("/api/jobs/refresh", {
        method: "POST",
        body: refreshPayload(filters),
      });
      const report = data.report || {};
      const loaded = report.fetched ?? 0;
      const errorCount = Object.keys(report.errors || {}).length;
      if (data.ok === false) {
        JP.toast(data.message || "Every live source failed. Check the source status.", "error");
      } else if (errorCount) {
        JP.toast(`Retrieved ${loaded} live posting${loaded === 1 ? "" : "s"}; ${errorCount} source${errorCount === 1 ? "" : "s"} had issues.`, "info");
      } else {
        JP.toast(`Retrieved ${loaded} live posting${loaded === 1 ? "" : "s"} and applied your filters.`, "success");
      }
      await loadJobs(false, { live: false });
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
    setSync("Scoring stored jobs against your profile…");
    try {
      const data = await JP.api("/api/jobs/rank", {
        method: "POST",
        body: refreshPayload(filters),
      });
      const sort = $("#job-sort");
      if (sort) sort.value = "score";
      JP.toast(`Ranked ${data.count || 0} role${data.count === 1 ? "" : "s"} against your profile.`, "success");
      await loadJobs(false, { live: false });
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
        await loadJobs(false, { live: false });
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
    const filtered = () => {
      window.clearTimeout(timer);
      // Typing keywords should search live sources, not just filter the stored
      // board, so a first-time user sees real jobs even with an empty board.
      timer = window.setTimeout(() => loadJobs(false, { live: true }), 320);
    };
    const searched = (event) => {
      event?.preventDefault();
      window.clearTimeout(timer);
      loadJobs();
    };
    $("#job-search")?.addEventListener("input", filtered);
    $("#job-location")?.addEventListener("input", filtered);
    $("#job-search")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") searched(event);
    });
    $("#job-location")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") searched(event);
    });
    ["#job-category", "#job-workplace", "#job-seniority", "#job-employment", "#job-source", "#job-posted", "#job-sort"].forEach((selector) => {
      $(selector)?.addEventListener("change", () => loadJobs(false, { live: true }));
    });
    $("#job-min-score")?.addEventListener("input", (event) => {
      const value = Number(event.currentTarget.value);
      $("#min-score-label").textContent = value ? `${value}+` : "any";
      window.clearTimeout(timer);
      timer = window.setTimeout(() => loadJobs(false, { live: true }), 220);
    });
    $("#clear-filters-btn")?.addEventListener("click", clearFilters);
    $("#refresh-jobs-btn")?.addEventListener("click", refreshJobs);
    $("#rank-jobs-btn")?.addEventListener("click", rankJobs);
    $("#import-job-btn")?.addEventListener("click", importJobModal);
    $("#load-more-btn")?.addEventListener("click", () => loadJobs(true, { live: false }));
    document.querySelectorAll("[data-empty-action]").forEach((button) => {
      if (button.dataset.emptyAction === "refresh") {
        button.addEventListener("click", refreshJobs);
      }
      if (button.dataset.emptyAction === "clear") {
        button.addEventListener("click", clearFilters);
      }
    });
    document.addEventListener("jobpilot:tracked", () => loadJobs(false, { live: false }));
  }

  function applyUrlFilters() {
    const params = new URLSearchParams(window.location.search);
    const set = (selector, value) => {
      const element = $(selector);
      if (element && value !== null) element.value = value;
    };
    set("#job-search", params.get("q"));
    set("#job-location", params.get("location"));
    set("#job-category", params.get("category"));
    const remoteParam = params.get("remote");
    const workplace =
      params.get("workplace_type") ||
      (remoteParam === "true" ? "remote" : remoteParam === "false" ? "onsite" : null);
    set("#job-workplace", workplace);
    set("#job-seniority", params.get("seniority"));
    set("#job-employment", params.get("employment_type"));
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
    const params = new URLSearchParams(window.location.search);
    const wantsLive = ["q", "location", "category", "workplace_type", "seniority", "employment_type"].some((key) => params.get(key));
    await loadJobs(false, { live: wantsLive });
    const jobId = params.get("job");
    if (jobId) JP.openJob(jobId);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
