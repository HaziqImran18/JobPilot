/* Chat page: SSE streaming, approvals, memory proposals and session history. */
(function () {
  "use strict";

  const state = {
    sessionId: new URLSearchParams(window.location.search).get("session") || "",
    sessions: [],
    streaming: false,
    emptyTemplate: null,
    stream: null,
    pendingTitle: "",
  };

  const $ = (selector) => document.querySelector(selector);

  function icon(name, size = 16) {
    return `<svg class="icon" width="${size}" height="${size}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
  }

  function userInitials() {
    return (JP.state.bootstrap && JP.state.bootstrap.profile && JP.state.bootstrap.profile.initials) || "You";
  }

  function scrollToBottom(force = false) {
    const scroller = $("#message-scroll");
    if (!scroller) return;
    const near = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140;
    if (force || near) scroller.scrollTop = scroller.scrollHeight;
  }

  function updateScrollButton() {
    const scroller = $("#message-scroll");
    const button = $("#scroll-bottom");
    if (!scroller || !button) return;
    const away = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight > 220;
    button.hidden = !away;
  }

  /* --------------------------------------------------------------- rendering */

  function modeLabel(mode) {
    if (mode === "live") return "Live agent";
    if (mode === "error") return "Error";
    return "Unavailable";
  }

  function renderChatCards(cards) {
    const list = (cards || []).filter((card) => card && card.id).slice(0, 6);
    if (!list.length) return "";
    return `<div class="chat-cards">${list
      .map((card) => {
        const job = JP.normalizeJob(card);
        const score = job.score;
        return `<div class="chat-job">
          <div class="chat-job-top">
            <div>
              <strong>${JP.esc(job.title)}</strong>
              <small>${JP.esc(job.company || "")}${job.location ? ` · ${JP.esc(job.location)}` : ""}</small>
            </div>
            ${score !== undefined && score !== null ? `<span class="mini-score">${Math.round(score)}</span>` : ""}
          </div>
          <div class="mini-actions">
            <button type="button" data-job-action="open" data-job-id="${JP.esc(job.id)}">View</button>
            <button type="button" data-job-action="tailor" data-job-id="${JP.esc(job.id)}">Tailor</button>
            <button type="button" data-job-action="track" data-job-id="${JP.esc(job.id)}">Track</button>
          </div>
        </div>`;
      })
      .join("")}</div>`;
  }

  function renderMeta(message) {
    const meta = message.meta || {};
    const parts = [`<span>${JP.esc(JP.formatRelative(message.created_at))}</span>`];
    if (meta.mode) {
      parts.push('<span class="dot"></span>');
      parts.push(`<span>${JP.esc(modeLabel(meta.mode))}</span>`);
    }
    if (meta.cost_usd) {
      parts.push('<span class="dot"></span>');
      parts.push(`<span>${JP.formatMoney(meta.cost_usd)}</span>`);
    }
    return `<div class="msg-meta">${parts.join("")}</div>`;
  }

  function renderToolLog(toolCalls) {
    const calls = (toolCalls || []).filter(Boolean);
    if (!calls.length) return "";
    return `<details class="run-log">
      <summary>${icon("activity", 13)} ${calls.length} tool call${calls.length === 1 ? "" : "s"} · ${calls.filter((call) => call.ok).length} succeeded</summary>
      ${calls
        .map(
          (call) => `<div class="run-step ${call.ok ? "ok" : "bad"}">${icon(call.ok ? "check" : "alert", 13)}
            <span><b>${JP.esc(call.name || "")}</b>${call.summary ? ` — ${JP.esc(call.summary)}` : ""}</span></div>`
        )
        .join("")}
    </details>`;
  }

  function messageElement(message) {
    const isUser = message.role === "user";
    const meta = message.meta || {};
    const element = document.createElement("div");
    element.className = `msg ${isUser ? "msg-user" : "msg-assistant"}`;
    const streamedCards = meta.cards || [];
    element.innerHTML = `
      <div class="msg-avatar">${isUser ? JP.esc(userInitials()) : icon("sparkles", 16)}</div>
      <div class="msg-body">
        ${isUser ? "" : renderToolLog(meta.tool_calls)}
        <div class="msg-bubble">${isUser ? JP.esc(message.content).replace(/\n/g, "<br>") : JP.md(message.content)}</div>
        ${isUser ? "" : renderChatCards(streamedCards)}
        ${renderMeta(message)}
      </div>`;
    return element;
  }

  function showEmpty() {
    const list = $("#messages");
    if (!list) return;
    list.innerHTML = "";
    if (state.emptyTemplate) list.appendChild(state.emptyTemplate.cloneNode(true));
  }

  function hideEmpty() {
    const empty = $("#chat-empty");
    if (empty) empty.remove();
  }

  /* --------------------------------------------------------------- sessions */

  function renderSessions() {
    const list = $("#session-list");
    if (!list) return;
    const filter = ($("#session-search")?.value || "").toLowerCase().trim();
    const sessions = state.sessions.filter((session) => {
      if (!filter) return true;
      return `${session.title} ${session.preview || ""}`.toLowerCase().includes(filter);
    });
    if (!sessions.length) {
      list.innerHTML = `<p class="board-empty">${state.sessions.length ? "No conversations match that filter." : "No conversations yet. Start one below."}</p>`;
      return;
    }
    list.innerHTML = sessions
      .map(
        (session) => `<button class="session-item ${session.id === state.sessionId ? "active" : ""}" data-session-id="${JP.esc(session.id)}">
          ${icon("chat", 15)}
          <span class="session-copy">
            <span class="session-title">${JP.esc(session.title || "New conversation")}</span>
            <span class="session-meta">${JP.esc(JP.formatRelative(session.updated_at))} · ${session.message_count || 0} messages</span>
          </span>
        </button>`
      )
      .join("");
  }

  async function loadSessions() {
    try {
      const data = await JP.api("/api/sessions");
      state.sessions = data.sessions || [];
      renderSessions();
    } catch (error) {
      const list = $("#session-list");
      if (list) list.innerHTML = `<p class="board-empty">Could not load conversations.</p>`;
    }
  }

  function setActiveSession(sessionId, session) {
    state.sessionId = sessionId || "";
    const title = $("#chat-title");
    if (title) title.textContent = session?.title || "New conversation";
    const subtitle = $("#chat-subtitle");
    if (subtitle) {
      subtitle.textContent = session?.message_count
        ? `${session.message_count} messages · ${JP.formatRelative(session.updated_at)}`
        : "Ask for a search, a score, a tailored CV, or a tracker update.";
    }
    const deleteButton = $("#delete-chat");
    if (deleteButton) deleteButton.hidden = !sessionId;
    renderSessions();
  }

  async function openSession(sessionId) {
    try {
      const data = await JP.api(`/api/sessions/${encodeURIComponent(sessionId)}/messages`);
      state.sessionId = sessionId;
      const list = $("#messages");
      list.innerHTML = "";
      (data.messages || []).forEach((message) => list.appendChild(messageElement(message)));
      if (!(data.messages || []).length) showEmpty();
      setActiveSession(sessionId, data.session);
      const url = new URL(window.location.href);
      url.searchParams.set("session", sessionId);
      window.history.replaceState({}, "", url);
      window.setTimeout(() => scrollToBottom(true), 40);
    } catch (error) {
      JP.toast(error.message, "error");
      resetChat();
    }
  }

  function resetChat() {
    if (state.streaming) {
      JP.toast("Wait for the current run to finish before starting a new conversation.", "info");
      return;
    }
    state.sessionId = "";
    state.streaming = false;
    $("#chat-title").textContent = "New conversation";
    $("#chat-subtitle").textContent = "Ask for a search, a score, a tailored CV, or a tracker update.";
    $("#delete-chat").hidden = true;
    $("#messages").innerHTML = "";
    showEmpty();
    setActiveSession("");
    const url = new URL(window.location.href);
    url.searchParams.delete("session");
    window.history.replaceState({}, "", url);
    $("#composer")?.focus();
  }

  /* ---------------------------------------------------------------- streaming */

  function addRunStep(text, tone = "active") {
    if (!state.stream) return;
    const steps = state.stream.steps;
    const element = document.createElement("div");
    element.className = `run-step ${tone}`;
    element.innerHTML = `${icon(tone === "bad" ? "alert" : "zap", 13)}<span>${JP.esc(text)}</span>`;
    steps.appendChild(element);
    while (steps.children.length > 7) steps.firstElementChild.remove();
    scrollToBottom();
  }

  function finishRunSteps() {
    if (!state.stream) return;
    state.stream.steps.querySelectorAll(".run-step.active").forEach((step) => {
      step.classList.remove("active");
      step.classList.add("ok");
    });
  }

  function onToolCall(data) {
    if (!state.stream) return;
    const card = document.createElement("details");
    card.className = "tool-card";
    card.open = true;
    card.dataset.toolName = data.name || "";
    card.innerHTML = `
      <summary class="tool-head">
        <span class="spinner"></span>
        <span class="tool-name">${JP.esc(data.name || "tool")}</span>
        <span class="tool-state running">running</span>
      </summary>
      <pre class="tool-preview">${JP.esc(JSON.stringify(data.arguments || {}, null, 2))}</pre>`;
    state.stream.toolStack.appendChild(card);
    state.stream.pending.push({ name: data.name, element: card });
    if (data.requires_approval) addRunStep(`${data.name} is waiting for your approval.`, "active");
    scrollToBottom();
  }

  function onToolResult(data) {
    if (!state.stream) return;
    const pending = [...state.stream.pending].reverse().find((item) => item.name === data.name && !item.done);
    let card = pending?.element;
    if (!card) {
      card = document.createElement("details");
      card.className = "tool-card";
      card.innerHTML = `<summary class="tool-head"><span class="tool-name">${JP.esc(data.name || "tool")}</span><span class="tool-state"></span></summary><pre class="tool-preview"></pre>`;
      state.stream.toolStack.appendChild(card);
    }
    if (pending) pending.done = true;
    card.querySelector(".spinner")?.remove();
    const badge = card.querySelector(".tool-state");
    if (badge) {
      badge.className = `tool-state ${data.ok ? "ok" : "bad"}`;
      badge.innerHTML = `${icon(data.ok ? "check" : "alert", 11)} ${data.ok ? "done" : "failed"}`;
    }
    const preview = card.querySelector(".tool-preview");
    if (preview) {
      const details = [data.summary, data.error, data.preview].filter(Boolean).join("\n\n");
      preview.textContent = JP.truncate(details || "No output.", 1200);
      if (!data.ok) preview.style.color = "var(--danger)";
    }
    (data.cards || []).forEach((job) => {
      if (job && job.id && !state.stream.cards.some((item) => item.id === job.id)) {
        state.stream.cards.push(job);
      }
    });
    scrollToBottom();
  }

  function onApproval(data) {
    if (!state.stream) return;
    const card = document.createElement("div");
    card.className = "approval-card";
    card.innerHTML = `
      <div class="approval-head">${icon("shield", 16)} Approval required</div>
      <p>${JP.esc(data.reason || `Allow ${data.tool} to run?`)}</p>
      <p class="mono" style="font-size:11px;color:var(--muted)">${JP.esc(data.tool || "")} ${JP.esc(JSON.stringify(data.arguments || {}))}</p>
      <div class="approval-actions">
        <button class="btn btn-primary btn-sm" data-approve>Approve</button>
        <button class="btn btn-ghost btn-sm" data-deny>Deny</button>
      </div>`;
    const decide = async (approved, button) => {
      card.querySelectorAll("button").forEach((item) => (item.disabled = true));
      try {
        await JP.api(`/api/approvals/${encodeURIComponent(data.id)}`, {
          method: "POST",
          body: { approved },
        });
        card.querySelector(".approval-actions").innerHTML = `<span class="badge ${approved ? "badge-success" : "badge-danger"}">${approved ? "Approved" : "Denied"}</span>`;
        addRunStep(approved ? "Action approved by you." : "Action denied by you.", approved ? "ok" : "bad");
      } catch (error) {
        JP.toast(error.message, "error");
        button.disabled = false;
      }
    };
    card.querySelector("[data-approve]").addEventListener("click", (event) => decide(true, event.currentTarget));
    card.querySelector("[data-deny]").addEventListener("click", (event) => decide(false, event.currentTarget));
    state.stream.toolStack.appendChild(card);
    addRunStep("Waiting for your approval…", "active");
    scrollToBottom(true);
  }

  function onMemoryProposal(data) {
    if (!state.stream) return;
    const memory = data.memory || {};
    const card = document.createElement("div");
    card.className = "memory-proposal";
    card.innerHTML = `
      <div class="approval-head">${icon("cpu", 16)} Memory proposed</div>
      <p>${JP.esc(memory.text || "")}</p>
      ${data.reason ? `<p class="fine-print">${JP.esc(data.reason)}</p>` : ""}
      <div class="approval-actions">
        <button class="btn btn-secondary btn-sm" data-confirm-memory>Keep it</button>
        <button class="btn btn-ghost btn-sm" data-dismiss-memory>Not now</button>
      </div>`;
    card.querySelector("[data-confirm-memory]").addEventListener("click", async (event) => {
      event.currentTarget.disabled = true;
      try {
        await JP.api(`/api/memories/${encodeURIComponent(memory.id)}/confirm`, { method: "POST", body: {} });
        card.querySelector(".approval-actions").innerHTML = '<span class="badge badge-success">Remembered</span>';
        JP.toast("Memory confirmed.", "success");
      } catch (error) {
        JP.toast(error.message, "error");
      }
    });
    card.querySelector("[data-dismiss-memory]").addEventListener("click", () => card.remove());
    state.stream.toolStack.appendChild(card);
    scrollToBottom();
  }

  function createStreamMessage() {
    hideEmpty();
    const wrapper = document.createElement("div");
    wrapper.className = "msg msg-assistant";
    wrapper.innerHTML = `
      <div class="msg-avatar">${icon("sparkles", 16)}</div>
      <div class="msg-body">
        <details class="run-log" open>
          <summary>${icon("activity", 13)} Working…</summary>
          <div class="run-steps"></div>
        </details>
        <div class="tool-stack"></div>
        <div class="msg-bubble streaming"><span class="typing"><span></span><span></span><span></span></span></div>
        <div class="stream-cards"></div>
        <div class="msg-meta"><span>Just now</span><span class="dot"></span><span class="stream-mode">${JP.esc(modeLabel("live"))}</span></div>
      </div>`;
    $("#messages").appendChild(wrapper);
    const stream = {
      element: wrapper,
      bubble: wrapper.querySelector(".msg-bubble"),
      log: wrapper.querySelector(".run-log"),
      logSummary: wrapper.querySelector(".run-log summary"),
      steps: wrapper.querySelector(".run-steps"),
      toolStack: wrapper.querySelector(".tool-stack"),
      cardsContainer: wrapper.querySelector(".stream-cards"),
      metaMode: wrapper.querySelector(".stream-mode"),
      toolLog: [],
      cards: [],
      pending: [],
      text: "",
      stepsCount: 0,
      renderPending: false,
      mode: "live",
    };
    state.stream = stream;
    window.setTimeout(() => scrollToBottom(true), 30);
    return stream;
  }

  function appendToken(text) {
    if (!state.stream || !text) return;
    state.stream.text += text;
    if (state.stream.bubble.querySelector(".typing")) {
      state.stream.bubble.classList.add("streaming");
      state.stream.bubble.innerHTML = "";
    }
    if (!state.stream.renderPending) {
      state.stream.renderPending = true;
      window.requestAnimationFrame(() => {
        if (!state.stream) return;
        state.stream.bubble.innerHTML = JP.md(state.stream.text);
        state.stream.renderPending = false;
        scrollToBottom();
      });
    }
  }

  function finalizeStream(data) {
    if (!state.stream) return;
    const stream = state.stream;
    if (!stream.text && data.message) stream.text = data.message;
    stream.bubble.classList.remove("streaming");
    stream.bubble.innerHTML = JP.md(stream.text || "No answer was produced.");
    const cards = (data.cards && data.cards.length ? data.cards : stream.cards) || [];
    stream.cardsContainer.innerHTML = renderChatCards(cards);
    stream.metaMode.textContent = modeLabel(data.mode || "live");
    const steps = data.steps || 0;
    const cost = data.cost_usd || 0;
    stream.logSummary.innerHTML = `${icon("activity", 13)} ${steps} step${steps === 1 ? "" : "s"} · ${JP.esc(modeLabel(data.mode || "live"))} · ${JP.formatMoney(cost)}`;
    stream.log.open = false;
    stream.toolStack.querySelectorAll("details").forEach((details) => (details.open = false));
    finishRunSteps();
    if (!stream.toolLog.length) {
      stream.toolStack.hidden = true;
    }
    const budget = data.budget || {};
    const budgetText = document.getElementById("budget-text");
    if (budgetText) budgetText.textContent = JP.formatMoney(budget.daily_spent || 0);
    stream.metaMode.textContent = modeLabel(data.mode || "live");
    JP.loadBootstrap();
    state.stream = null;
  }

  function streamFailed(message) {
    if (!state.stream) {
      JP.toast(message, "error");
      return;
    }
    const stream = state.stream;
    stream.bubble.classList.remove("streaming");
    if (!stream.text) {
      stream.bubble.innerHTML = `<span style="color:var(--danger)">${JP.esc(message)}</span>`;
    }
    stream.log.open = true;
    addRunStep(message, "bad");
    finishRunSteps();
    stream.toolStack.querySelectorAll("details").forEach((details) => (details.open = false));
    state.stream = null;
    JP.toast(message, "error");
  }

  function handleEvent(type, data) {
    if (!state.stream && type !== "done" && type !== "error") return;
    switch (type) {
      case "status":
        addRunStep(data.text || "Working…");
        state.stream && state.stream.stepsCount++;
        break;
      case "tool_call":
        onToolCall(data);
        if (state.stream) {
          state.stream.toolLog.push({ name: data.name, ok: true, summary: "" });
        }
        break;
      case "tool_result":
        onToolResult(data);
        if (state.stream) {
          const entry = [...state.stream.toolLog].reverse().find((item) => item.name === data.name && !item.summarySet);
          if (entry) {
            entry.ok = data.ok;
            entry.summary = data.summary || "";
            entry.summarySet = true;
          }
        }
        break;
      case "approval_required":
        onApproval(data);
        break;
      case "memory_proposal":
        onMemoryProposal(data);
        break;
      case "token":
        appendToken(data.text || "");
        break;
      case "error":
        streamFailed(data.text || "The run failed.");
        break;
      case "done":
        finalizeStream(data);
        state.sessionId = data.session_id || state.sessionId;
        setActiveSession(state.sessionId, {
          title:
            $("#chat-title").textContent === "New conversation"
              ? JP.truncate(state.pendingTitle || "New conversation", 70)
              : $("#chat-title").textContent,
          message_count: 0,
          updated_at: new Date().toISOString(),
        });
        if (state.sessionId) {
          const url = new URL(window.location.href);
          url.searchParams.set("session", state.sessionId);
          window.history.replaceState({}, "", url);
        }
        loadSessions();
        break;
      default:
        break;
    }
  }

  async function sendMessage(raw) {
    const message = String(raw || "").trim();
    if (!message || state.streaming) return;
    state.streaming = true;
    state.pendingTitle = message;
    $("#send-btn").disabled = true;
    $("#live-indicator").hidden = false;
    $("#composer").value = "";
    $("#composer").style.height = "auto";

    hideEmpty();
    const user = {
      role: "user",
      content: message,
      created_at: new Date().toISOString(),
      meta: {},
    };
    $("#messages").appendChild(messageElement(user));
    createStreamMessage();
    scrollToBottom(true);

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-JobPilot": "1" },
        body: JSON.stringify({ message, session_id: state.sessionId }),
      });
      if (!response.ok) {
        let detail = `Request failed (${response.status})`;
        try {
          const payload = await response.json();
          detail = payload.error || detail;
        } catch (error) { /* keep default */ }
        throw new Error(detail);
      }
      if (!response.body) throw new Error("Streaming is not supported by this browser.");

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let boundary = buffer.indexOf("\n\n");
        while (boundary !== -1) {
          const chunk = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          for (const line of chunk.split("\n")) {
            if (!line.startsWith("data:")) continue;
            const payloadText = line.slice(5).trim();
            if (!payloadText) continue;
            let payload;
            try {
              payload = JSON.parse(payloadText);
            } catch (error) {
              continue;
            }
            handleEvent(payload.type, payload.data || {});
          }
          boundary = buffer.indexOf("\n\n");
        }
      }
      if (state.stream) {
        streamFailed("The stream ended before the run finished. Check the System page for the trace.");
      }
    } catch (error) {
      streamFailed(error.message || "Could not reach the agent.");
    } finally {
      state.streaming = false;
      $("#send-btn").disabled = false;
      $("#live-indicator").hidden = true;
      $("#composer").focus();
      updateScrollButton();
    }
  }

  /* ---------------------------------------------------------------- bindings */

  function bind() {
    state.emptyTemplate = $("#chat-empty")?.cloneNode(true) || null;

    $("#composer-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      sendMessage($("#composer").value);
    });

    $("#composer")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage(event.currentTarget.value);
      }
    });

    $("#composer")?.addEventListener("input", (event) => {
      const textarea = event.currentTarget;
      textarea.style.height = "auto";
      textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
    });

    document.addEventListener("click", (event) => {
      const prompt = event.target.closest("[data-prompt]");
      if (prompt) {
        event.preventDefault();
        sendMessage(prompt.dataset.prompt || "");
        return;
      }
      const session = event.target.closest("[data-session-id]");
      if (session) {
        openSession(session.dataset.sessionId);
      }
    });

    $("#session-search")?.addEventListener("input", renderSessions);

    $("#new-chat")?.addEventListener("click", resetChat);
    document.addEventListener("jobpilot:new-chat", resetChat);

    $("#delete-chat")?.addEventListener("click", async () => {
      if (!state.sessionId) return;
      const confirmed = await JP.confirm("Delete this conversation and its trace history?", {
        title: "Delete conversation",
        confirm: "Delete",
        danger: true,
      });
      if (!confirmed) return;
      try {
        await JP.api(`/api/sessions/${encodeURIComponent(state.sessionId)}`, { method: "DELETE" });
        JP.toast("Conversation deleted.", "success");
        resetChat();
        loadSessions();
      } catch (error) {
        JP.toast(error.message, "error");
      }
    });

    $("#toggle-chat-list")?.addEventListener("click", () => $("#chat-list")?.classList.toggle("open"));

    $("#message-scroll")?.addEventListener("scroll", updateScrollButton);
    $("#scroll-bottom")?.addEventListener("click", () => scrollToBottom(true));

    document.addEventListener("jobpilot:tracked", () => loadSessions());
  }

  async function init() {
    bind();
    await loadSessions();
    if (state.sessionId) {
      openSession(state.sessionId);
    } else {
      setActiveSession("");
      showEmpty();
    }
    document.addEventListener("jobpilot:ready", () => renderSessions());
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
