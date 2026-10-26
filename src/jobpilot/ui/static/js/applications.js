/* Applications board: drag-and-drop pipeline, notes and status history. */
(function () {
  "use strict";

  const STATUSES = [
    ["saved", "Saved"],
    ["tailoring", "Tailoring"],
    ["ready", "Ready"],
    ["applied", "Applied"],
    ["interviewing", "Interviewing"],
    ["offer", "Offer"],
    ["rejected", "Rejected"],
    ["withdrawn", "Withdrawn"],
  ];
  const state = { applications: [], board: {}, stats: {}, followups: [] };
  const $ = (selector) => document.querySelector(selector);

  function icon(name, size = 15) {
    return `<svg class="icon" width="${size}" height="${size}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
  }

  function renderStats(stats) {
    const total = stats.total || 0;
    const progress = total - (stats.rejected || 0) - (stats.withdrawn || 0);
    $("#app-stat-total").textContent = total;
    $("#app-stat-progress").textContent = progress;
    $("#app-stat-interviews").textContent = stats.interviewing || 0;
    $("#app-stat-offers").textContent = stats.offer || 0;
  }

  function renderFollowups(followups) {
    const callout = $("#followup-callout");
    if (!callout) return;
    if (!followups.length) {
      callout.hidden = true;
      return;
    }
    callout.hidden = false;
    $("#followup-text").textContent = followups
      .slice(0, 4)
      .map((item) => `${item.job_title} at ${item.company}${item.next_action_at ? ` (${item.next_action_at.slice(0, 10)})` : ""}`)
      .join(" · ");
  }

  function appCard(application) {
    const meta = [];
    if (application.next_action_at) {
      meta.push(`<span>${icon("clock", 11)} ${JP.esc(application.next_action_at.slice(0, 10))}</span>`);
    }
    if (application.cv_path) {
      meta.push(`<span>${icon("file", 11)} CV</span>`);
    }
    meta.push(`<span>${JP.esc(JP.formatRelative(application.updated_at))}</span>`);
    return `<article class="app-card" draggable="true" data-app-id="${JP.esc(application.id)}" tabindex="0">
      <h4>${JP.esc(application.job_title || "Untitled role")}</h4>
      <p>${JP.esc(application.company || "")}</p>
      ${application.notes ? `<p class="app-notes">${JP.esc(application.notes)}</p>` : ""}
      <div class="app-meta">${meta.join("")}</div>
    </article>`;
  }

  function renderBoard() {
    const board = $("#application-board");
    if (!board) return;
    const boardData = state.board || {};
    board.innerHTML = STATUSES.map(([status, label]) => {
      const items = boardData[status] || [];
      return `<section class="board-column" data-status="${status}">
        <header class="board-column-head">
          <span class="status-pip ${status}"></span>
          <h3>${label}</h3>
          <span class="count">${items.length}</span>
        </header>
        <div class="board-cards" data-dropzone="${status}">
          ${items.length ? items.map(appCard).join("") : '<p class="board-empty">Drop a card here</p>'}
        </div>
      </section>`;
    }).join("");
    $("#applications-empty").hidden = (state.stats.total || 0) > 0;
    board.hidden = (state.stats.total || 0) === 0;
    bindDragAndDrop();
  }

  function bindDragAndDrop() {
    let dragged = null;
    document.querySelectorAll(".app-card").forEach((card) => {
      card.addEventListener("dragstart", (event) => {
        dragged = card.dataset.appId;
        card.classList.add("dragging");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", dragged);
      });
      card.addEventListener("dragend", () => {
        card.classList.remove("dragging");
        dragged = null;
        document.querySelectorAll(".board-column").forEach((column) => column.classList.remove("drag-over"));
      });
      card.addEventListener("click", () => openApplication(card.dataset.appId));
      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openApplication(card.dataset.appId);
        }
      });
    });

    document.querySelectorAll(".board-column").forEach((column) => {
      column.addEventListener("dragover", (event) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        column.classList.add("drag-over");
      });
      column.addEventListener("dragleave", (event) => {
        if (!column.contains(event.relatedTarget)) column.classList.remove("drag-over");
      });
      column.addEventListener("drop", async (event) => {
        event.preventDefault();
        column.classList.remove("drag-over");
        const applicationId = dragged || event.dataTransfer.getData("text/plain");
        const status = column.dataset.status;
        if (!applicationId || !status) return;
        const current = state.applications.find((item) => item.id === applicationId);
        if (current && current.status === status) return;
        try {
          await JP.api(`/api/applications/${encodeURIComponent(applicationId)}`, {
            method: "PATCH",
            body: { status },
          });
          JP.toast(`Moved to ${JP.titleCase(status)}.`, "success");
          await loadBoard();
        } catch (error) {
          JP.toast(error.message, "error");
        }
      });
    });
  }

  async function loadBoard() {
    try {
      const data = await JP.api("/api/applications");
      state.applications = data.applications || [];
      state.board = data.board || {};
      state.stats = data.stats || {};
      state.followups = data.followups || [];
      renderStats(state.stats);
      renderFollowups(state.followups);
      renderBoard();
    } catch (error) {
      JP.toast(error.message, "error");
      $("#application-board").innerHTML = `<p class="board-empty">Could not load the pipeline: ${JP.esc(error.message)}</p>`;
    }
  }

  function statusOptions(selected) {
    return STATUSES.map(
      ([value, label]) => `<option value="${value}" ${value === selected ? "selected" : ""}>${label}</option>`
    ).join("");
  }

  function addApplicationModal() {
    const modal = JP.modal({
      title: "Add an application",
      subtitle: "Track a role manually or link it to a stored job id.",
      body: `<form class="stack-form" id="app-form">
        <div class="grid-2">
          <label class="field"><span>Job title</span><input type="text" id="new-app-title" required placeholder="AI Engineer"></label>
          <label class="field"><span>Company</span><input type="text" id="new-app-company" placeholder="Company name"></label>
          <label class="field"><span>Job ID (optional)</span><input type="text" id="new-app-job-id" placeholder="job_…"></label>
          <label class="field"><span>Status</span><select id="new-app-status">${statusOptions("saved")}</select></label>
          <label class="field"><span>Next action</span><input type="date" id="new-app-next"></label>
          <label class="field"><span>Posting URL</span><input type="url" id="new-app-url" placeholder="https://…"></label>
        </div>
        <label class="field"><span>Notes</span><textarea id="new-app-notes" rows="3" placeholder="Referral, deadline, contact…"></textarea></label>
      </form>`,
      footer: `<button class="btn btn-ghost" data-modal-close>Cancel</button><button class="btn btn-primary" type="button" id="create-app">Add to board</button>`,
    });
    const createButton = modal.element.querySelector("#create-app");
    const submit = async () => {
      const title = modal.element.querySelector("#new-app-title").value.trim();
      if (!title) {
        JP.toast("A job title is required.", "error");
        return;
      }
      createButton.disabled = true;
      try {
        await JP.api("/api/applications", {
          method: "POST",
          body: {
            job_id: modal.element.querySelector("#new-app-job-id").value.trim(),
            job_title: title,
            company: modal.element.querySelector("#new-app-company").value.trim(),
            url: modal.element.querySelector("#new-app-url").value.trim(),
            status: modal.element.querySelector("#new-app-status").value,
            notes: modal.element.querySelector("#new-app-notes").value.trim(),
            next_action_at: modal.element.querySelector("#new-app-next").value || null,
          },
        });
        JP.closeModal();
        JP.toast("Application added.", "success");
        await loadBoard();
      } catch (error) {
        JP.toast(error.message, "error");
        createButton.disabled = false;
      }
    };
    createButton.addEventListener("click", submit);
    modal.element.querySelector("#app-form").addEventListener("submit", (event) => {
      event.preventDefault();
      submit();
    });
  }

  async function openApplication(applicationId) {
    const modal = JP.modal({
      title: "Loading application…",
      body: '<div class="skeleton-card" style="min-height:220px"></div>',
    });
    try {
      const data = await JP.api(`/api/applications/${encodeURIComponent(applicationId)}`);
      const application = data.application;
      const events = application.events || [];
      const fileName = String(application.cv_path || "").split(/[\\/]/).pop();
      const timeline = events
        .map(
          (event) => `<div class="timeline-item">
            <strong>${JP.esc(JP.titleCase(event.status))}</strong>
            <p>${JP.esc(event.note || "")}</p>
            <small>${JP.esc(JP.formatDate(event.created_at))} · ${JP.esc(JP.formatRelative(event.created_at))}</small>
          </div>`
        )
        .join("");
      const body = `
        <div class="app-detail-head">
          <h3>${JP.esc(application.job_title)}</h3>
          <p>${JP.esc(application.company || "")}</p>
        </div>
        <div class="grid-2" style="margin-top:16px">
          <label class="field"><span>Status</span><select id="detail-status">${statusOptions(application.status)}</select></label>
          <label class="field"><span>Next action</span><input type="date" id="detail-next" value="${JP.esc((application.next_action_at || "").slice(0, 10))}"></label>
        </div>
        <label class="field" style="margin-top:12px"><span>Notes</span><textarea id="detail-notes" rows="4">${JP.esc(application.notes || "")}</textarea></label>
        <div class="detail-section"><h4>History</h4><div class="timeline">${timeline || '<p class="muted">No events yet.</p>'}</div></div>
        <div class="detail-section"><h4>Links</h4>
          ${application.url ? `<a class="btn btn-ghost btn-sm" href="${JP.esc(application.url)}" target="_blank" rel="noopener noreferrer">${icon("external", 14)} Original posting</a>` : '<span class="muted" style="font-size:12px">No posting URL recorded.</span>'}
          ${fileName ? `<a class="btn btn-ghost btn-sm" href="/api/files/${encodeURIComponent(fileName)}" style="margin-left:6px">${icon("download", 14)} Tailored CV</a>` : ""}
        </div>`;
      const footer = `
        <button class="btn btn-danger" id="delete-app">${icon("trash", 14)} Delete</button>
        <span style="flex:1"></span>
        <button class="btn btn-ghost" data-modal-close>Cancel</button>
        <button class="btn btn-primary" id="save-app">Save changes</button>`;
      const ready = JP.modal({
        title: application.job_title,
        subtitle: application.company || "",
        body,
        footer,
        wide: true,
      });
      ready.element.querySelector("#save-app").addEventListener("click", async (event) => {
        event.currentTarget.disabled = true;
        try {
          await JP.api(`/api/applications/${encodeURIComponent(applicationId)}`, {
            method: "PATCH",
            body: {
              status: ready.element.querySelector("#detail-status").value,
              notes: ready.element.querySelector("#detail-notes").value,
              next_action_at: ready.element.querySelector("#detail-next").value || "",
            },
          });
          JP.closeModal();
          JP.toast("Application updated.", "success");
          await loadBoard();
        } catch (error) {
          JP.toast(error.message, "error");
          event.currentTarget.disabled = false;
        }
      });
      ready.element.querySelector("#delete-app").addEventListener("click", async () => {
        const confirmed = await JP.confirm("Delete this application and its event history?", {
          title: "Delete application",
          confirm: "Delete",
          danger: true,
        });
        if (!confirmed) return;
        try {
          await JP.api(`/api/applications/${encodeURIComponent(applicationId)}`, { method: "DELETE" });
          JP.toast("Application deleted.", "success");
          await loadBoard();
        } catch (error) {
          JP.toast(error.message, "error");
        }
      });
    } catch (error) {
      JP.toast(error.message, "error");
      JP.closeModal();
    }
  }

  function bind() {
    $("#add-application")?.addEventListener("click", addApplicationModal);
    $("#empty-add-application")?.addEventListener("click", addApplicationModal);
    $("#refresh-board")?.addEventListener("click", loadBoard);
    $("#dismiss-followups")?.addEventListener("click", () => {
      $("#followup-callout").hidden = true;
    });
    document.addEventListener("jobpilot:tracked", () => loadBoard());
  }

  function init() {
    bind();
    loadBoard();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
