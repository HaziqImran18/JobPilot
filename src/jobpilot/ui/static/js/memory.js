/* Memory page: audit, add, confirm, forget and test recall. */
(function () {
  "use strict";

  const state = { status: "active", memories: [], counts: {} };
  const $ = (selector) => document.querySelector(selector);

  function icon(name, size = 15) {
    return `<svg class="icon" width="${size}" height="${size}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
  }

  function renderCounts() {
    const counts = state.counts || {};
    $("#count-active").textContent = counts.active || 0;
    $("#count-pending").textContent = counts.needs_confirmation || 0;
    $("#count-forgotten").textContent = counts.forgotten || 0;
    const total = Object.values(counts).reduce((sum, value) => sum + Number(value || 0), 0);
    const summary = $("#memory-summary");
    if (summary) {
      summary.textContent = `${total} memories stored · ${counts.active || 0} active · ${counts.needs_confirmation || 0} awaiting confirmation`;
    }
  }

  function memoryCard(memory) {
    const importance = Math.round(Number(memory.importance || 0) * 100);
    const confidence = Math.round(Number(memory.confidence || 0) * 100);
    const source = memory.provenance?.source || "agent";
    const actions = [];
    if (memory.status === "needs_confirmation") {
      actions.push(`<button class="btn btn-primary" data-memory-action="confirm" data-memory-id="${JP.esc(memory.id)}">${icon("check", 12)} Confirm</button>`);
      actions.push(`<button class="btn btn-ghost" data-memory-action="forget" data-memory-id="${JP.esc(memory.id)}">Dismiss</button>`);
    } else if (memory.status === "forgotten") {
      actions.push(`<button class="btn btn-secondary" data-memory-action="restore" data-memory-id="${JP.esc(memory.id)}">${icon("refresh", 12)} Restore</button>`);
    } else {
      actions.push(`<button class="btn btn-ghost" data-memory-action="forget" data-memory-id="${JP.esc(memory.id)}">${icon("trash", 12)} Forget</button>`);
    }
    return `<article class="memory-card ${memory.status === "needs_confirmation" ? "pending" : ""} ${memory.status === "forgotten" ? "forgotten" : ""}">
      <div class="memory-card-top">
        <span class="memory-kind ${memory.kind === "episodic" ? "episodic" : ""}">${JP.esc(memory.kind || "semantic")}</span>
        <span class="badge badge-ghost">${JP.esc(memory.status.replace(/_/g, " "))}</span>
        <span class="memory-date">${JP.esc(JP.formatRelative(memory.created_at))}</span>
      </div>
      <p class="memory-text">${JP.esc(memory.text)}</p>
      ${memory.key ? `<p class="memory-provenance">${JP.esc(memory.key)} = ${JP.esc(memory.value)}</p>` : ""}
      <div class="memory-meter"><div class="meter"><span style="width:${importance}%"></span></div><span>importance ${(memory.importance || 0).toFixed(2)}</span></div>
      <div class="memory-meter"><div class="meter"><span style="width:${confidence}%;background:var(--indigo)"></span></div><span>confidence ${(memory.confidence || 0).toFixed(2)}</span></div>
      <div class="memory-provenance">source: ${JP.esc(source)}${memory.use_count ? ` · used ${memory.use_count}×` : ""}${memory.last_used_at ? ` · last ${JP.formatRelative(memory.last_used_at)}` : ""}</div>
      <div class="memory-actions">${actions.join("")}</div>
    </article>`;
  }

  function renderMemories() {
    const grid = $("#memory-grid");
    if (!grid) return;
    grid.innerHTML = state.memories.map(memoryCard).join("");
    grid.hidden = state.memories.length === 0;
    $("#memory-empty").hidden = state.memories.length !== 0;
    if (!state.memories.length) {
      const empty = $("#memory-empty");
      const title = empty.querySelector("h3");
      const text = empty.querySelector("p");
      if (state.status === "needs_confirmation") {
        title.textContent = "Nothing waiting";
        text.textContent = "Memories inferred from conversation will appear here before they become active.";
      } else if (state.status === "forgotten") {
        title.textContent = "No forgotten memories";
        text.textContent = "Forgotten memories stop appearing in recall immediately and are listed here for audit.";
      } else {
        title.textContent = "Nothing here yet";
        text.textContent = "Tell the agent what matters — “remember that I prefer remote roles” — or add a memory above.";
      }
    }
  }

  async function loadMemories() {
    try {
      const [list, all] = await Promise.all([
        JP.api(`/api/memories?status=${encodeURIComponent(state.status)}`),
        JP.api("/api/memories?status=all"),
      ]);
      state.memories = list.memories || [];
      state.counts = all.counts || list.counts || {};
      renderCounts();
      renderMemories();
    } catch (error) {
      JP.toast(error.message, "error");
    }
  }

  async function act(memoryId, action) {
    try {
      if (action === "forget") {
        const confirmed = await JP.confirm("Forget this memory? It will stop appearing in recall immediately.", {
          title: "Forget memory",
          confirm: "Forget",
          danger: true,
        });
        if (!confirmed) return;
        await JP.api(`/api/memories/${encodeURIComponent(memoryId)}`, { method: "DELETE" });
        JP.toast("Memory forgotten.", "success");
      } else {
        await JP.api(`/api/memories/${encodeURIComponent(memoryId)}/confirm`, { method: "POST", body: {} });
        JP.toast(action === "restore" ? "Memory restored." : "Memory confirmed.", "success");
      }
      await loadMemories();
    } catch (error) {
      JP.toast(error.message, "error");
    }
  }

  function bind() {
    document.querySelectorAll("#memory-tabs .segment").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll("#memory-tabs .segment").forEach((item) => item.classList.remove("active"));
        tab.classList.add("active");
        state.status = tab.dataset.status;
        loadMemories();
      });
    });

    document.addEventListener("click", (event) => {
      const action = event.target.closest("[data-memory-action]");
      if (action) {
        act(action.dataset.memoryId, action.dataset.memoryAction);
      }
    });

    $("#memory-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = $("#memory-text").value.trim();
      if (!text) return;
      const button = event.currentTarget.querySelector("button[type=submit]");
      button.disabled = true;
      try {
        await JP.api("/api/memories", {
          method: "POST",
          body: {
            text,
            kind: $("#memory-kind").value,
            importance: Number($("#memory-importance").value),
            confidence: 0.95,
            explicit: true,
          },
        });
        $("#memory-text").value = "";
        JP.toast("Memory saved.", "success");
        await loadMemories();
      } catch (error) {
        JP.toast(error.message, "error");
      } finally {
        button.disabled = false;
      }
    });

    $("#memory-importance")?.addEventListener("input", (event) => {
      $("#memory-importance-label").textContent = Number(event.currentTarget.value).toFixed(2);
    });

    $("#recall-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const query = $("#recall-query").value.trim();
      if (!query) return;
      const results = $("#recall-results");
      results.innerHTML = '<div class="skeleton-line" style="min-height:54px"></div>';
      try {
        const data = await JP.api(`/api/memories/recall?query=${encodeURIComponent(query)}`);
        const memories = data.memories || [];
        results.innerHTML = memories.length
          ? memories
              .map(
                (memory) => `<div class="recall-item">
                  <div class="recall-score">${memory.similarity !== null && memory.similarity !== undefined ? Number(memory.similarity).toFixed(2) : "—"}</div>
                  <p>${JP.esc(memory.text)}</p>
                  <span class="badge badge-ghost">${JP.esc(memory.kind)}</span>
                </div>`
              )
              .join("")
          : '<p class="muted" style="font-size:12px">No active memories matched. Add one above or mention it in chat.</p>';
      } catch (error) {
        results.innerHTML = `<p style="color:var(--danger);font-size:12px">${JP.esc(error.message)}</p>`;
      }
    });

    document.addEventListener("jobpilot:ready", () => loadMemories());
  }

  function init() {
    bind();
    loadMemories();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
