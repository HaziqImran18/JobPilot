/* JobPilot shared frontend runtime: API, markdown, modals, job actions. */
(function () {
  "use strict";

  const state = {
    bootstrap: null,
    theme: document.documentElement.getAttribute("data-theme") || "dark",
    modal: null,
  };

  /* ---------------------------------------------------------------- utilities */

  const escapeHtml = (value) =>
    String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g, (character) => {
      switch (character) {
        case "&": return "&amp;";
        case "<": return "&lt;";
        case ">": return "&gt;";
        case '"': return "&quot;";
        default: return "&#39;";
      }
    });

  const escapeAttr = escapeHtml;

  function formatDate(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  }

  function formatRelative(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const diff = Date.now() - date.getTime();
    const minutes = Math.round(diff / 60000);
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.round(hours / 24);
    if (days < 30) return `${days}d ago`;
    return formatDate(value);
  }

  function formatMoney(value) {
    const number = Number(value);
    if (!Number.isFinite(number) || number === 0) return "$0.0000";
    if (number < 0.01) return `$${number.toFixed(4)}`;
    if (number < 1) return `$${number.toFixed(3)}`;
    return `$${number.toFixed(2)}`;
  }

  function titleCase(value) {
    return String(value || "")
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, (character) => character.toUpperCase());
  }

  function truncate(value, length = 120) {
    const text = String(value || "");
    return text.length > length ? `${text.slice(0, length - 1)}…` : text;
  }

  function initials(name) {
    return String(name || "JP")
      .trim()
      .split(/\s+/)
      .slice(0, 2)
      .map((part) => part[0] || "")
      .join("")
      .toUpperCase() || "JP";
  }

  /* --------------------------------------------------------------------- API */

  async function api(path, options = {}) {
    const request = { ...options, headers: { ...(options.headers || {}) } };
    request.headers["X-JobPilot"] = "1";
    if (request.body && typeof request.body !== "string") {
      request.headers["Content-Type"] = "application/json";
      request.body = JSON.stringify(request.body);
    }
    let response;
    try {
      response = await fetch(path, request);
    } catch (error) {
      throw new Error("Cannot reach the JobPilot server. Is it still running?");
    }
    const text = await response.text();
    let data = {};
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (error) {
        data = { error: text.slice(0, 400) };
      }
    }
    if (!response.ok) {
      throw new Error(data.error || `Request failed (${response.status})`);
    }
    return data;
  }

  /* ----------------------------------------------------------------- markdown */

  function markdown(source) {
    if (!source) return "";
    let text = String(source);
    const blocks = [];
    const inline = [];

    text = text.replace(/```([\w+-]*)\n?([\s\S]*?)```/g, (_match, _language, code) => {
      blocks.push(`<pre><code>${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`);
      return `\u0000B${blocks.length - 1}\u0000`;
    });

    text = escapeHtml(text);

    text = text.replace(/`([^`\n]+)`/g, (_match, code) => {
      inline.push(`<code>${code}</code>`);
      return `\u0000I${inline.length - 1}\u0000`;
    });

    text = text.replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_match, label, url) => `<a href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`
    );

    text = text
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/~~([^~]+)~~/g, "<del>$1</del>");

    const lines = text.split(/\r?\n/);
    const output = [];
    let list = null;
    let paragraph = [];

    const flushParagraph = () => {
      if (paragraph.length) {
        output.push(`<p>${paragraph.join("<br>")}</p>`);
        paragraph = [];
      }
    };
    const closeList = () => {
      if (list) {
        output.push(`</${list}>`);
        list = null;
      }
    };

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) {
        flushParagraph();
        closeList();
        continue;
      }
      if (/^\u0000B\d+\u0000$/.test(trimmed)) {
        flushParagraph();
        closeList();
        output.push(trimmed);
        continue;
      }
      let match = trimmed.match(/^(#{1,4})\s+(.*)$/);
      if (match) {
        flushParagraph();
        closeList();
        const level = Math.min(match[1].length, 4);
        output.push(`<h${level}>${match[2]}</h${level}>`);
        continue;
      }
      if (/^([-*_])\1{2,}$/.test(trimmed)) {
        flushParagraph();
        closeList();
        output.push("<hr>");
        continue;
      }
      match = trimmed.match(/^[-*+]\s+(.*)$/);
      if (match) {
        flushParagraph();
        if (list !== "ul") {
          closeList();
          output.push("<ul>");
          list = "ul";
        }
        output.push(`<li>${match[1]}</li>`);
        continue;
      }
      match = trimmed.match(/^\d+[.)]\s+(.*)$/);
      if (match) {
        flushParagraph();
        if (list !== "ol") {
          closeList();
          output.push("<ol>");
          list = "ol";
        }
        output.push(`<li>${match[1]}</li>`);
        continue;
      }
      if (/^&gt;\s?/.test(trimmed)) {
        flushParagraph();
        closeList();
        output.push(`<blockquote>${trimmed.replace(/^&gt;\s?/, "")}</blockquote>`);
        continue;
      }
      paragraph.push(trimmed);
    }
    flushParagraph();
    closeList();

    let html = output.join("\n");
    html = html.replace(/\u0000B(\d+)\u0000/g, (_match, index) => blocks[Number(index)] || "");
    html = html.replace(/\u0000I(\d+)\u0000/g, (_match, index) => inline[Number(index)] || "");
    return html;
  }

  /* ------------------------------------------------------------------- toasts */

  function toast(message, type = "info", timeout = 4200) {
    const stack = document.getElementById("toasts");
    if (!stack) return;
    const icons = { success: "check", error: "alert", info: "sparkles" };
    const element = document.createElement("div");
    element.className = `toast ${type}`;
    element.innerHTML = `<svg class="icon" width="17" height="17" aria-hidden="true"><use href="#i-${icons[type] || "sparkles"}"></use></svg><span>${escapeHtml(message)}</span>`;
    stack.appendChild(element);
    const remove = () => {
      element.classList.add("leaving");
      window.setTimeout(() => element.remove(), 220);
    };
    element.addEventListener("click", remove);
    window.setTimeout(remove, timeout);
  }

  /* -------------------------------------------------------------------- modal */

  function closeModal() {
    const root = document.getElementById("modal-root");
    if (!root) return;
    root.hidden = true;
    root.innerHTML = "";
    document.body.style.overflow = "";
    state.modal = null;
  }

  function openModal(options) {
    const root = document.getElementById("modal-root");
    if (!root) return { close: closeModal, body: null };
    const title = options.title || "";
    const subtitle = options.subtitle || "";
    const body = options.body || "";
    const footer = options.footer || "";
    root.hidden = false;
    root.innerHTML = `
      <div class="modal-overlay" data-modal-overlay>
        <section class="modal ${options.wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-label="${escapeAttr(title)}">
          <header class="modal-head">
            <div>
              <h3>${escapeHtml(title)}</h3>
              ${subtitle ? `<p>${escapeHtml(subtitle)}</p>` : ""}
            </div>
            <button class="icon-btn" data-modal-close aria-label="Close"><svg class="icon" width="18" height="18"><use href="#i-x"></use></svg></button>
          </header>
          <div class="modal-body">${body}</div>
          ${footer ? `<footer class="modal-foot">${footer}</footer>` : ""}
        </section>
      </div>`;
    document.body.style.overflow = "hidden";
    const overlay = root.querySelector("[data-modal-overlay]");
    overlay.addEventListener("mousedown", (event) => {
      if (event.target === overlay) closeModal();
    });
    root.querySelectorAll("[data-modal-close]").forEach((button) => button.addEventListener("click", closeModal));
    state.modal = { close: closeModal, element: root };
    window.setTimeout(() => {
      const focusable = root.querySelector("input, textarea, select, button:not([data-modal-close])");
      if (focusable) focusable.focus();
    }, 40);
    return state.modal;
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.modal) closeModal();
  });

  function confirmDialog(message, options = {}) {
    return new Promise((resolve) => {
      const modal = openModal({
        title: options.title || "Are you sure?",
        body: `<p style="color:var(--muted);font-size:13px">${escapeHtml(message)}</p>`,
        footer: `<button class="btn btn-ghost" data-cancel>Cancel</button><button class="btn ${options.danger ? "btn-danger" : "btn-primary"}" data-confirm>${escapeHtml(options.confirm || "Confirm")}</button>`,
      });
      modal.element.querySelector("[data-cancel]").addEventListener("click", () => {
        closeModal();
        resolve(false);
      });
      modal.element.querySelector("[data-confirm]").addEventListener("click", () => {
        closeModal();
        resolve(true);
      });
    });
  }

  /* ---------------------------------------------------------------- job cards */

  function normalizeJob(item) {
    if (!item) return {};
    if (item.job) {
      const job = { ...item.job };
      const match = item.match || {};
      job.score = match.score !== undefined ? match.score : job.score;
      job.matched_skills = match.matched_skills || job.matched_skills || [];
      job.missing_skills = match.missing_skills || job.missing_skills || [];
      job.rationale = match.rationale || job.rationale || "";
      job.breakdown = match.breakdown || job.breakdown || null;
      job.model = match.model || job.model || "";
      return job;
    }
    return { ...item };
  }

  function scoreColor(score) {
    if (score >= 75) return "var(--success)";
    if (score >= 55) return "var(--accent)";
    if (score >= 35) return "var(--warning)";
    return "var(--danger)";
  }

  function scoreRing(score, label = "Fit score") {
    if (score === null || score === undefined || Number.isNaN(Number(score))) {
      return `<div class="score-ring none" title="Not scored yet"><span>—</span></div>`;
    }
    const value = Math.max(0, Math.min(100, Math.round(Number(score))));
    return `<div class="score-ring" style="--score:${value};--ring-color:${scoreColor(value)}" title="${escapeAttr(label)} ${value}/100"><span>${value}</span></div>`;
  }

  function salaryLabel(job) {
    if (job.salary_label) return job.salary_label;
    if (typeof job.salary === "object" && job.salary) {
      const minimum = job.salary.minimum;
      const maximum = job.salary.maximum;
      const currency = job.salary.currency || "";
      const period = job.salary.period || "";
      if (minimum && maximum) return `${currency} ${Math.round(minimum).toLocaleString()}–${Math.round(maximum).toLocaleString()} ${period}`.trim();
      if (minimum) return `${currency} ${Math.round(minimum).toLocaleString()}+ ${period}`.trim();
    }
    if (typeof job.salary === "string") return job.salary;
    return "";
  }

  function chips(items, className = "", limit = 4) {
    const list = (items || []).filter(Boolean);
    if (!list.length) return "";
    const shown = list.slice(0, limit).map((item) => `<span class="chip ${className}">${escapeHtml(item)}</span>`);
    if (list.length > limit) shown.push(`<span class="chip more">+${list.length - limit}</span>`);
    return `<div class="chips">${shown.join("")}</div>`;
  }

  function skillChips(job) {
    const matched = (job.matched_skills || []).filter(Boolean);
    const gaps = (job.missing_skills || []).filter(Boolean);
    if (matched.length || gaps.length) {
      return `<div class="chips">${[
        ...matched.slice(0, 5).map((item) => `<span class="chip match">${escapeHtml(item)}</span>`),
        ...gaps.slice(0, 3).map((item) => `<span class="chip gap">${escapeHtml(item)}</span>`),
      ].join("")}</div>`;
    }
    return chips(job.top_skills || [], "", 5);
  }

  function jobCard(item, options = {}) {
    const job = normalizeJob(item);
    if (!job.id) return "";
    const source = titleCase(job.source || "manual");
    const location = job.location || (job.remote ? "Remote" : "Location not listed");
    const salary = salaryLabel(job);
    const score = job.score;
    const rationale = job.rationale || "";
    return `
      <article class="job-card" data-job-id="${escapeAttr(job.id)}">
        <div class="job-top">
          <div class="job-heading">
            <span class="job-source">${escapeHtml(source)}</span>
            <h3 class="job-title"><a href="#" data-job-action="open" data-job-id="${escapeAttr(job.id)}">${escapeHtml(job.title || "Untitled role")}</a></h3>
            <p class="job-company">${escapeHtml(job.company || "Unknown company")}</p>
          </div>
          ${scoreRing(score)}
        </div>
        <div class="job-meta">
          <span><svg class="icon" width="12" height="12"><use href="#i-target"></use></svg>${escapeHtml(location)}</span>
          ${job.remote ? '<span><svg class="icon" width="12" height="12"><use href="#i-zap"></use></svg>Remote</span>' : ""}
          ${salary ? `<span class="job-salary"><svg class="icon" width="12" height="12"><use href="#i-dollar"></use></svg>${escapeHtml(salary)}</span>` : ""}
          ${job.posted_at ? `<span><svg class="icon" width="12" height="12"><use href="#i-clock"></use></svg>${escapeHtml(formatRelative(job.posted_at))}</span>` : ""}
        </div>
        ${skillChips(job)}
        ${rationale ? `<p class="job-rationale">${escapeHtml(rationale)}</p>` : ""}
        <div class="job-foot">
          <button class="btn btn-ghost btn-sm" data-job-action="open" data-job-id="${escapeAttr(job.id)}">View role</button>
          <button class="btn btn-secondary btn-sm" data-job-action="tailor" data-job-id="${escapeAttr(job.id)}">Tailor CV</button>
          <button class="icon-btn" title="Track application" data-job-action="track" data-job-id="${escapeAttr(job.id)}">
            <svg class="icon" width="16" height="16"><use href="#i-plus"></use></svg>
          </button>
        </div>
      </article>`;
  }

  /* ------------------------------------------------------------- job actions */

  async function openJob(jobId) {
    const modal = openModal({
      title: "Loading role…",
      subtitle: jobId,
      body: '<div class="skeleton-card" style="min-height:220px"></div>',
    });
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      const job = data.job;
      const detail = job.score_detail || {};
      const breakdown = detail.breakdown || job.breakdown || {};
      const matched = detail.matched_skills || job.matched_skills || [];
      const missing = detail.missing_skills || job.missing_skills || [];
      const score = detail.score !== undefined && detail.score !== null ? detail.score : job.score;
      const salary = salaryLabel(job);
      const breakdownHtml = Object.keys(breakdown).length
        ? `<div class="detail-section"><h4>Score breakdown</h4><div class="breakdown">${Object.entries(breakdown)
            .filter(([, value]) => typeof value === "number")
            .map(([key, value]) => `<div class="breakdown-row"><span>${escapeHtml(titleCase(key))}</span><div class="meter"><span style="width:${Math.max(0, Math.min(100, value))}%"></span></div><b>${Math.round(value)}</b></div>`)
            .join("")}</div></div>`
        : "";
      const modalBody = `
        <div class="job-detail-head">
          <div class="job-heading">
            <span class="job-source">${escapeHtml(titleCase(job.source || "manual"))}</span>
            <h3 class="job-title">${escapeHtml(job.title || "")}</h3>
            <p class="job-company">${escapeHtml(job.company || "")}</p>
          </div>
          <div class="detail-score">${scoreRing(score)}<small>fit score</small></div>
        </div>
        <div class="detail-meta">
          <span><svg class="icon" width="13" height="13"><use href="#i-target"></use></svg>${escapeHtml(job.location || "Location not listed")}</span>
          ${job.remote ? '<span><svg class="icon" width="13" height="13"><use href="#i-zap"></use></svg>Remote</span>' : ""}
          ${salary ? `<span><svg class="icon" width="13" height="13"><use href="#i-dollar"></use></svg>${escapeHtml(salary)}</span>` : ""}
          ${job.employment_type ? `<span><svg class="icon" width="13" height="13"><use href="#i-briefcase"></use></svg>${escapeHtml(titleCase(job.employment_type))}</span>` : ""}
          ${job.posted_at ? `<span><svg class="icon" width="13" height="13"><use href="#i-clock"></use></svg>${escapeHtml(formatDate(job.posted_at))}</span>` : ""}
        </div>
        ${detail.rationale ? `<div class="tailor-banner" style="margin-bottom:12px"><svg class="icon" width="16" height="16"><use href="#i-sparkles"></use></svg><div><strong>Why this score</strong><p>${escapeHtml(detail.rationale)}</p></div></div>` : ""}
        ${matched.length ? `<div class="detail-section"><h4>Matched skills</h4>${chips(matched, "match", 12)}</div>` : ""}
        ${missing.length ? `<div class="detail-section"><h4>Gaps to address</h4>${chips(missing, "gap", 12)}</div>` : ""}
        ${breakdownHtml}
        <div class="detail-section">
          <h4>Description</h4>
          <div class="detail-description">${escapeHtml(job.description || "No description was provided by this source.")}</div>
        </div>
        ${(job.tags || []).length ? `<div class="detail-section"><h4>Tags</h4>${chips(job.tags, "", 14)}</div>` : ""}`;
      const footer = `
        <button class="btn btn-ghost" data-modal-close>Close</button>
        ${job.url ? `<a class="btn btn-ghost" href="${escapeAttr(job.url)}" target="_blank" rel="noopener noreferrer"><svg class="icon" width="15" height="15"><use href="#i-external"></use></svg>Original</a>` : ""}
        <button class="btn btn-secondary" data-job-action="track" data-job-id="${escapeAttr(job.id)}">Track</button>
        <button class="btn btn-primary" data-job-action="tailor" data-job-id="${escapeAttr(job.id)}">Tailor CV</button>`;
      openModal({ title: job.title || "Role", subtitle: job.company || "", body: modalBody, footer, wide: true });
    } catch (error) {
      toast(error.message, "error");
      closeModal();
    }
  }

  async function tailorJob(jobId) {
    openModal({
      title: "Tailoring your CV…",
      subtitle: "Checking every claim against your profile",
      body: '<div class="skeleton-card" style="min-height:260px"></div>',
    });
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(jobId)}/tailor`, {
        method: "POST",
        body: { use_llm: true },
      });
      const result = data.tailoring;
      const unsupported = result.unsupported || [];
      const fileName = String(result.cv_path || "").split(/[\\/]/).pop();
      const download = fileName
        ? `<a class="btn btn-ghost" href="/api/files/${encodeURIComponent(fileName)}"><svg class="icon" width="15" height="15"><use href="#i-download"></use></svg>Download CV</a>`
        : "";
      const banner = unsupported.length
        ? `<div class="tailor-banner bad"><svg class="icon" width="17" height="17"><use href="#i-shield"></use></svg><div><strong>${unsupported.length} unsupported claim${unsupported.length === 1 ? "" : "s"} removed</strong>${unsupported.map((claim) => `<p>${escapeHtml(truncate(claim, 220))}</p>`).join("")}</div></div>`
        : `<div class="tailor-banner"><svg class="icon" width="17" height="17"><use href="#i-shield"></use></svg><div><strong>Every claim verified</strong><p>Generated from your profile facts, with real metrics preserved.</p></div></div>`;
      const body = `
        ${banner}
        <div class="detail-section"><h4>Headline</h4><p style="font-size:14px;color:var(--text)">${escapeHtml(result.headline || "")}</p></div>
        <div class="detail-section"><h4>Summary</h4><p style="color:var(--text-soft);font-size:13px">${escapeHtml(result.summary || "")}</p></div>
        <div class="detail-section"><h4>Selected bullets</h4><ul class="tailor-bullets">${(result.bullets || []).map((bullet) => `<li>${escapeHtml(bullet)}</li>`).join("")}</ul></div>
        <div class="detail-section"><h4>Keywords</h4>${chips(result.keywords || [], "match", 12)}</div>
        <div class="detail-section"><h4>Cover letter</h4><div class="cover-letter">${escapeHtml(result.cover_letter || "")}</div></div>
        <p class="fine-print" style="margin-top:12px">Model: ${escapeHtml(result.model || "deterministic")} · saved to ${escapeHtml(result.cv_path || "uploads")}</p>`;
      const footer = `
        <button class="btn btn-ghost" data-modal-close>Close</button>
        ${download}
        <button class="btn btn-primary" data-job-action="track" data-job-id="${escapeAttr(jobId)}" data-cv-path="${escapeAttr(result.cv_path || "")}">Track this application</button>`;
      openModal({ title: `Tailored CV — ${result.job_title || ""}`, subtitle: result.company || "", body, footer, wide: true });
    } catch (error) {
      toast(error.message, "error");
      closeModal();
    }
  }

  async function trackJob(jobId, cvPath, trigger) {
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(jobId)}/track`, {
        method: "POST",
        body: { cv_path: cvPath || "" },
      });
      toast(`Tracking ${data.application.job_title} at ${data.application.company}.`, "success");
      if (trigger) {
        trigger.disabled = true;
        trigger.textContent = "Tracked";
      }
      document.dispatchEvent(new CustomEvent("jobpilot:tracked", { detail: data.application }));
      return data.application;
    } catch (error) {
      if (String(error.message).toLowerCase().includes("unique")) {
        toast("That role is already on your board.", "info");
      } else {
        toast(error.message, "error");
      }
      return null;
    }
  }

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-job-action]");
    if (!trigger) return;
    const action = trigger.dataset.jobAction;
    const jobId = trigger.dataset.jobId;
    if (!jobId || trigger.tagName === "A" && action === "open" && trigger.getAttribute("href") !== "#") return;
    event.preventDefault();
    if (action === "open") openJob(jobId);
    else if (action === "tailor") tailorJob(jobId);
    else if (action === "track") trackJob(jobId, trigger.dataset.cvPath || "", trigger);
  });

  /* ------------------------------------------------------------------- shell */

  function applyTheme(theme) {
    state.theme = theme;
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("jobpilot-theme", theme);
    } catch (error) { /* ignore */ }
    const toggle = document.getElementById("theme-toggle");
    if (toggle) {
      const use = toggle.querySelector("use");
      if (use) use.setAttribute("href", theme === "dark" ? "#i-sun" : "#i-moon");
    }
  }

  function setModeBadge(mode) {
    const labels = { live: "Live", unconfigured: "Setup" };
    const label = labels[mode] || "Live";
    document.querySelectorAll("#top-mode-badge, #session-mode").forEach((badge) => {
      badge.textContent = label;
    });
    const sidebarLabel = document.getElementById("mode-label");
    const sidebarDetail = document.getElementById("mode-detail");
    const dot = document.getElementById("mode-dot");
    const sidebarLabels = {
      live: "Live mode",
      unconfigured: "Setup required",
    };
    const sidebarDetails = {
      live: "OpenAI · approvals on",
      unconfigured: "Add OPENAI_API_KEY to .env",
    };
    if (sidebarLabel) sidebarLabel.textContent = sidebarLabels[mode] || "Live mode";
    if (sidebarDetail) sidebarDetail.textContent = sidebarDetails[mode] || sidebarDetails.live;
    if (dot) dot.className = `status-dot ${mode === "live" ? "" : "warn"}`;
  }

  async function loadBootstrap() {
    try {
      const data = await api("/api/bootstrap");
      state.bootstrap = data;
      const profile = data.profile || {};
      const avatar = document.getElementById("user-avatar");
      if (avatar) avatar.textContent = profile.initials || initials(profile.name);
      const name = document.getElementById("user-name");
      if (name) name.textContent = profile.name || "Your profile";
      const headline = document.getElementById("user-headline");
      if (headline) headline.textContent = profile.headline || profile.location || "Set up your profile";
      setModeBadge(data.mode);
      const budget = data.budget || {};
      const budgetText = document.getElementById("budget-text");
      if (budgetText) budgetText.textContent = formatMoney(budget.daily_spent || 0);
      const budgetPill = document.getElementById("budget-pill");
      if (budgetPill) {
        budgetPill.title = `${formatMoney(budget.daily_spent || 0)} of ${formatMoney(budget.daily_cap || 1)} daily cap · ${budget.calls_today || 0} calls`;
      }
      const counts = data.counts || {};
      const setCount = (id, value) => {
        const element = document.getElementById(id);
        if (element) element.textContent = value ? String(value) : "";
      };
      setCount("nav-jobs-count", counts.jobs);
      setCount("nav-apps-count", counts.applications);
      setCount("nav-memory-count", counts.memories);
      document.dispatchEvent(new CustomEvent("jobpilot:ready", { detail: data }));
      return data;
    } catch (error) {
      setModeBadge("unconfigured");
      console.warn("Bootstrap failed", error);
      return null;
    }
  }

  function initShell() {
    const storedTheme = (() => {
      try { return localStorage.getItem("jobpilot-theme"); } catch (error) { return null; }
    })();
    applyTheme(storedTheme === "light" ? "light" : "dark");

    document.getElementById("theme-toggle")?.addEventListener("click", () => {
      applyTheme(state.theme === "dark" ? "light" : "dark");
    });

    const sidebar = document.getElementById("sidebar");
    const scrim = document.getElementById("sidebar-scrim");
    const closeSidebar = () => {
      sidebar?.classList.remove("open");
      scrim?.classList.remove("visible");
    };
    document.getElementById("menu-toggle")?.addEventListener("click", () => {
      sidebar?.classList.add("open");
      scrim?.classList.add("visible");
    });
    scrim?.addEventListener("click", closeSidebar);

    const newChat = async () => {
      if (document.body.dataset.page === "chat") {
        document.dispatchEvent(new CustomEvent("jobpilot:new-chat"));
        return;
      }
      try {
        const data = await api("/api/sessions", { method: "POST" });
        window.location.href = `/?session=${encodeURIComponent(data.session.id)}`;
      } catch (error) {
        toast(error.message, "error");
      }
    };
    document.getElementById("new-chat-top")?.addEventListener("click", newChat);

    document.addEventListener("keydown", (event) => {
      if (event.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) {
        const search = document.querySelector("#job-search, #session-search, #recall-query, #memory-text");
        if (search) {
          event.preventDefault();
          search.focus();
        }
      }
    });

    loadBootstrap();
  }

  window.JP = {
    api,
    esc: escapeHtml,
    md: markdown,
    toast,
    modal: openModal,
    closeModal,
    confirm: confirmDialog,
    formatDate,
    formatRelative,
    formatMoney,
    titleCase,
    truncate,
    initials,
    normalizeJob,
    scoreRing,
    scoreColor,
    chips,
    jobCard,
    openJob,
    tailorJob,
    trackJob,
    loadBootstrap,
    state,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initShell);
  } else {
    initShell();
  }
})();
