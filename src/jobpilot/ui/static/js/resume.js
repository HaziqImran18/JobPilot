/* Resume upload, review and profile import. */
(function () {
  "use strict";

  const state = { resumes: [], activeId: "", busy: false };
  const $ = (selector) => document.querySelector(selector);

  function icon(name, size = 15) {
    return `<svg class="icon" width="${size}" height="${size}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
  }

  function bytesLabel(bytes) {
    const value = Number(bytes || 0);
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} KB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }

  function statusTone(status) {
    if (status === "ready") return "ok";
    if (status === "error") return "bad";
    return "warn";
  }

  async function upload(file) {
    if (!file || state.busy) return;
    state.busy = true;
    const drop = $("#resume-drop");
    drop?.classList.add("busy");
    const form = new FormData();
    form.append("file", file);
    form.append("activate", "1");
    try {
      const response = await fetch("/api/resumes", {
        method: "POST",
        headers: { "X-JobPilot": "1" },
        body: form,
      });
      const text = await response.text();
      let data = {};
      try {
        data = text ? JSON.parse(text) : {};
      } catch (error) {
        data = { error: text.slice(0, 300) };
      }
      if (!response.ok) throw new Error(data.error || `Upload failed (${response.status})`);
      const resume = data.resume || {};
      if (resume.status === "error") {
        JP.toast(resume.error || "The file was stored but no text could be extracted.", "error");
      } else {
        JP.toast(`Extracted ${resume.characters || 0} characters from ${resume.filename}.`, "success");
      }
      await load();
      openReview(resume.id);
    } catch (error) {
      JP.toast(error.message, "error");
    } finally {
      state.busy = false;
      drop?.classList.remove("busy");
      const input = $("#resume-input");
      if (input) input.value = "";
    }
  }

  function renderList() {
    const holder = $("#resume-list");
    if (!holder) return;
    if (!state.resumes.length) {
      holder.innerHTML = '<p class="resume-empty">No resume uploaded yet. Your profile can still be edited by hand.</p>';
    } else {
      holder.innerHTML = state.resumes
        .map((resume) => {
          const active = resume.is_active || resume.id === state.activeId;
          return `<div class="resume-item ${active ? "active" : ""}" data-resume-id="${JP.esc(resume.id)}">
            <span class="resume-icon">${icon("file", 17)}</span>
            <div class="resume-copy">
              <strong>${JP.esc(resume.filename)}</strong>
              <small>${JP.esc(bytesLabel(resume.size_bytes))} · ${JP.esc(JP.formatRelative(resume.created_at))} · ${JP.esc(resume.status)}</small>
            </div>
            ${active ? '<span class="badge badge-mode">Active</span>' : ""}
            <div class="resume-actions">
              <button class="btn btn-ghost btn-xs" type="button" data-resume="review">Review</button>
              ${active ? "" : '<button class="btn btn-ghost btn-xs" type="button" data-resume="activate">Use</button>'}
              <a class="icon-btn" href="/api/resumes/${encodeURIComponent(resume.id)}/file" target="_blank" rel="noopener" title="View file">${icon("external", 14)}</a>
              <button class="icon-btn" type="button" data-resume="delete" title="Delete resume">${icon("trash", 14)}</button>
            </div>
          </div>`;
        })
        .join("");
    }
    const badge = $("#resume-active-badge");
    if (badge) badge.hidden = !state.resumes.some((item) => item.is_active || item.id === state.activeId);
  }

  async function load() {
    try {
      const data = await JP.api("/api/resumes");
      state.resumes = data.resumes || [];
      state.activeId = data.active_id || "";
      renderList();
    } catch (error) {
      JP.toast(error.message, "error");
    }
  }

  function fieldRow(label, key, value, options = {}) {
    const type = options.type || "text";
    const span = options.span ? "field-span" : "";
    if (type === "textarea") {
      return `<label class="field ${span}"><span>${JP.esc(label)}</span><textarea data-extract="${JP.esc(key)}" rows="${options.rows || 3}" placeholder="${JP.esc(options.placeholder || "")}">${JP.esc(value || "")}</textarea></label>`;
    }
    return `<label class="field ${span}"><span>${JP.esc(label)}</span><input type="${type}" data-extract="${JP.esc(key)}" value="${JP.esc(value || "")}" placeholder="${JP.esc(options.placeholder || "")}"></label>`;
  }

  async function openReview(resumeId) {
    if (!resumeId) return;
    let resume;
    try {
      const data = await JP.api(`/api/resumes/${encodeURIComponent(resumeId)}`);
      resume = data.resume;
    } catch (error) {
      JP.toast(error.message, "error");
      return;
    }
    const extracted = resume.extracted || {};
    const links = extracted.links || {};
    const skills = Array.isArray(extracted.skills) ? extracted.skills.join(", ") : "";
    const body = `<div class="resume-review">
      <div class="resume-extract-note">${icon("sparkles", 16)}<div><strong>Review the extraction.</strong> Edit anything that is wrong, then apply only the fields you trust. Applying merges into your profile and never overwrites experience, education or projects.</div></div>
      <div class="resume-review-grid">
        ${fieldRow("Full name", "full_name", extracted.full_name)}
        ${fieldRow("Headline", "headline", extracted.headline, { placeholder: "AI Engineer building production LLM systems" })}
        ${fieldRow("Email", "email", extracted.email, { type: "email" })}
        ${fieldRow("Phone", "phone", extracted.phone)}
        ${fieldRow("Location", "location", extracted.location, { placeholder: "Lahore, Pakistan" })}
        ${fieldRow("LinkedIn", "links.linkedin", links.linkedin, { type: "url" })}
        ${fieldRow("GitHub", "links.github", links.github, { type: "url" })}
        ${fieldRow("Website", "links.website", links.website, { type: "url" })}
        ${fieldRow("Skills · comma separated", "skills", skills, { span: true })}
        ${fieldRow("Summary", "summary", extracted.summary, { type: "textarea", span: true, rows: 4 })}
      </div>
      <details>
        <summary class="fine-print">Show extracted text</summary>
        <div class="resume-text">${JP.esc(resume.text || "No text was extracted.")}</div>
      </details>
    </div>`;
    const footer = `<button class="btn btn-ghost" data-modal-close type="button">Close</button>
      <button class="btn btn-secondary" id="resume-save" type="button">Save review</button>
      <button class="btn btn-primary" id="resume-apply" type="button">${icon("check", 15)} Apply to profile</button>`;
    const modal = JP.modal({
      title: resume.filename || "Resume",
      subtitle: "Review extraction before it reaches your profile",
      body,
      footer,
      wide: true,
    });

    const collect = () => {
      const fields = {};
      modal.element.querySelectorAll("[data-extract]").forEach((input) => {
        const key = input.dataset.extract;
        const value = input.value.trim();
        if (key.startsWith("links.")) {
          fields.links = fields.links || {};
          if (value) fields.links[key.split(".")[1]] = value;
        } else if (key === "skills") {
          fields.skills = value
            .split(",")
            .map((item) => item.trim())
            .filter(Boolean);
        } else {
          fields[key] = value;
        }
      });
      return fields;
    };

    const saveReview = async (button) => {
      const original = button.innerHTML;
      button.disabled = true;
      button.innerHTML = '<span class="spinner"></span> Saving…';
      try {
        await JP.api(`/api/resumes/${encodeURIComponent(resumeId)}`, {
          method: "PUT",
          body: { fields: collect() },
        });
        JP.toast("Review saved.", "success");
        await load();
      } catch (error) {
        JP.toast(error.message, "error");
      } finally {
        button.disabled = false;
        button.innerHTML = original;
      }
    };

    modal.element.querySelector("#resume-save").addEventListener("click", (event) => {
      saveReview(event.currentTarget);
    });
    modal.element.querySelector("#resume-apply").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const original = button.innerHTML;
      button.disabled = true;
      button.innerHTML = '<span class="spinner"></span> Applying…';
      try {
        await JP.api(`/api/resumes/${encodeURIComponent(resumeId)}`, {
          method: "PUT",
          body: { fields: collect() },
        });
        const fields = collect();
        const names = Object.keys(fields).filter((key) => {
          if (key === "links") return Object.keys(fields.links || {}).length > 0;
          if (key === "skills") return (fields.skills || []).length > 0;
          return Boolean(fields[key]);
        });
        const data = await JP.api(`/api/resumes/${encodeURIComponent(resumeId)}/apply`, {
          method: "POST",
          body: { fields: names },
        });
        JP.closeModal();
        JP.toast(`Applied ${(data.applied || []).length} field${(data.applied || []).length === 1 ? "" : "s"} to your profile.`, "success");
        if (window.JobPilotProfile) await window.JobPilotProfile.reload();
        await load();
      } catch (error) {
        JP.toast(error.message, "error");
        button.disabled = false;
        button.innerHTML = original;
      }
    });
  }

  async function activate(resumeId) {
    try {
      await JP.api(`/api/resumes/${encodeURIComponent(resumeId)}/activate`, { method: "POST" });
      JP.toast("Active resume updated.", "success");
      await load();
    } catch (error) {
      JP.toast(error.message, "error");
    }
  }

  async function remove(resumeId, filename) {
    const confirmed = await JP.confirm(`Delete “${filename}”? The file is removed from this account.`, {
      title: "Delete resume",
      confirm: "Delete",
      danger: true,
    });
    if (!confirmed) return;
    try {
      await JP.api(`/api/resumes/${encodeURIComponent(resumeId)}`, { method: "DELETE" });
      JP.toast("Resume deleted.", "success");
      await load();
    } catch (error) {
      JP.toast(error.message, "error");
    }
  }

  function bind() {
    const input = $("#resume-input");
    input?.addEventListener("change", () => upload(input.files && input.files[0]));
    const drop = $("#resume-drop");
    if (drop) {
      ["dragenter", "dragover"].forEach((name) => {
        drop.addEventListener(name, (event) => {
          event.preventDefault();
          drop.classList.add("dragging");
        });
      });
      ["dragleave", "drop"].forEach((name) => {
        drop.addEventListener(name, (event) => {
          event.preventDefault();
          drop.classList.remove("dragging");
        });
      });
      drop.addEventListener("drop", (event) => {
        const file = event.dataTransfer?.files?.[0];
        if (file) upload(file);
      });
    }
    $("#resume-list")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-resume]");
      if (!button) return;
      const item = button.closest("[data-resume-id]");
      const resumeId = item?.dataset.resumeId;
      if (!resumeId) return;
      const action = button.dataset.resume;
      if (action === "review") openReview(resumeId);
      if (action === "activate") activate(resumeId);
      if (action === "delete") {
        const resume = state.resumes.find((entry) => entry.id === resumeId);
        remove(resumeId, resume?.filename || "this resume");
      }
    });
  }

  async function init() {
    if (!$("#resume-panel")) return;
    bind();
    await load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
