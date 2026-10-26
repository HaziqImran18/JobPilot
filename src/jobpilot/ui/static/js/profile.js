/* Profile editor: structured, repeatable, and saved as validated Pydantic data. */
(function () {
  "use strict";

  const state = { profile: null, dirty: false };
  const $ = (selector) => document.querySelector(selector);

  function icon(name, size = 15) {
    return `<svg class="icon" width="${size}" height="${size}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
  }

  const splitList = (value) =>
    String(value || "")
      .split(/[,\n]/)
      .map((item) => item.trim())
      .filter(Boolean);

  const numberOr = (value, fallback = 0) => {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  };

  function field(label, name, value, options = {}) {
    const type = options.type || "text";
    const span = options.span ? "span-2" : "";
    if (type === "textarea") {
      return `<label class="field ${span}"><span>${label}</span><textarea data-field="${name}" rows="${options.rows || 3}" placeholder="${JP.esc(options.placeholder || "")}">${JP.esc(value || "")}</textarea></label>`;
    }
    if (type === "select") {
      return `<label class="field ${span}"><span>${label}</span><select data-field="${name}">${options.options
        .map(([key, text]) => `<option value="${JP.esc(key)}" ${String(value) === String(key) ? "selected" : ""}>${JP.esc(text)}</option>`)
        .join("")}</select></label>`;
    }
    return `<label class="field ${span}"><span>${label}</span><input type="${type}" data-field="${name}" value="${JP.esc(value ?? "")}" placeholder="${JP.esc(options.placeholder || "")}" ${options.min !== undefined ? `min="${options.min}"` : ""} ${options.step ? `step="${options.step}"` : ""}></label>`;
  }

  function experienceCard(item, index) {
    return `<div class="repeat-card" data-kind="experience" data-id="${JP.esc(item.id || "")}">
      <div class="repeat-head">
        <h4>Role ${index + 1}</h4>
        <label class="check"><input type="checkbox" data-field="is_current" ${item.is_current ? "checked" : ""}> Current</label>
        <div class="repeat-actions"><button class="icon-btn" type="button" data-remove="experience" title="Remove role">${icon("trash", 15)}</button></div>
      </div>
      <div class="repeat-grid">
        ${field("Company", "company", item.company, { placeholder: "Nexora Labs" })}
        ${field("Title", "title", item.title, { placeholder: "AI Engineer" })}
        ${field("Start", "start_date", item.start_date, { placeholder: "2024-06" })}
        ${field("End", "end_date", item.end_date, { placeholder: "2025-08 or blank" })}
        ${field("Location", "location", item.location, { placeholder: "Islamabad, Pakistan" })}
        ${field("Skills · comma separated", "skills", (item.skills || []).join(", "), { placeholder: "Python, FastAPI, RAG" })}
        ${field("Bullets · one per line", "bullets", (item.bullets || []).join("\n"), { type: "textarea", rows: 5, span: true, placeholder: "Built a FastAPI retrieval service over 12,000 documents…" })}
      </div>
    </div>`;
  }

  function educationCard(item, index) {
    return `<div class="repeat-card" data-kind="education" data-id="${JP.esc(item.id || "")}">
      <div class="repeat-head">
        <h4>Education ${index + 1}</h4>
        <div class="repeat-actions"><button class="icon-btn" type="button" data-remove="education" title="Remove">${icon("trash", 15)}</button></div>
      </div>
      <div class="repeat-grid">
        ${field("Institution", "institution", item.institution, { placeholder: "COMSATS University" })}
        ${field("Degree", "degree", item.degree, { placeholder: "BSc" })}
        ${field("Field of study", "field_of_study", item.field_of_study, { placeholder: "Computer Science" })}
        ${field("Location / details", "details", item.details, { placeholder: "Islamabad · CGPA 3.7" })}
        ${field("Start", "start_date", item.start_date, { placeholder: "2020" })}
        ${field("End", "end_date", item.end_date, { placeholder: "2024" })}
      </div>
    </div>`;
  }

  function projectCard(item, index) {
    return `<div class="repeat-card" data-kind="project" data-id="${JP.esc(item.id || "")}">
      <div class="repeat-head">
        <h4>Project ${index + 1}</h4>
        <div class="repeat-actions"><button class="icon-btn" type="button" data-remove="project" title="Remove">${icon("trash", 15)}</button></div>
      </div>
      <div class="repeat-grid">
        ${field("Name", "name", item.name, { placeholder: "JobPilot" })}
        ${field("URL", "url", item.url, { placeholder: "https://github.com/…" })}
        ${field("Description", "description", item.description, { type: "textarea", rows: 3, span: true })}
        ${field("Skills · comma separated", "skills", (item.skills || []).join(", "), { span: true })}
      </div>
    </div>`;
  }

  function skillRow(skill) {
    const levelOptions = [
      [1, "1 · Familiar"],
      [2, "2 · Working"],
      [3, "3 · Confident"],
      [4, "4 · Advanced"],
      [5, "5 · Expert"],
    ]
      .map(([level, label]) => `<option value="${level}" ${Number(skill.level || 3) === level ? "selected" : ""}>${label}</option>`)
      .join("");
    return `<div class="skill-row" data-kind="skill" data-id="${JP.esc(skill.id || "")}">
      <input type="text" data-field="name" value="${JP.esc(skill.name || "")}" placeholder="e.g. Python" aria-label="Skill name">
      <select data-field="level" aria-label="Skill level">${levelOptions}</select>
      <input type="number" data-field="years" value="${JP.esc(skill.years ?? 0)}" min="0" step="0.5" aria-label="Years used" placeholder="Years">
      <input type="text" data-field="category" class="skill-category" list="skill-categories" value="${JP.esc(skill.category || "general")}" placeholder="category" aria-label="Skill category">
      <button class="icon-btn" type="button" data-remove="skill" title="Remove skill">${icon("trash", 14)}</button>
    </div>`;
  }

  function renderSkills(skills) {
    const list = $("#skill-list");
    if (!list) return;
    list.innerHTML = skills.length
      ? skills.map(skillRow).join("")
      : '<p class="skill-empty">No skills yet. Add the tools you can defend in an interview, with an honest level and years of use.</p>';
  }

  function renderRepeatables(profile) {
    $("#experience-list").innerHTML = (profile.experiences || []).map(experienceCard).join("");
    $("#education-list").innerHTML = (profile.education || []).map(educationCard).join("");
    $("#project-list").innerHTML = (profile.projects || []).map(projectCard).join("");
    renderSkills(profile.skills || []);
  }

  function renderProfile(profile) {
    state.profile = profile;
    const preferences = profile.preferences || {};
    $("#p-full-name").value = profile.full_name || "";
    $("#p-headline").value = profile.headline || "";
    $("#p-email").value = profile.email || "";
    $("#p-phone").value = profile.phone || "";
    $("#p-location").value = profile.location || "";
    $("#p-summary").value = profile.summary || "";
    $("#p-auth").value = preferences.work_authorization || "";
    $("#p-linkedin").value = profile.links?.linkedin || "";
    $("#p-github").value = profile.links?.github || "";
    $("#p-portfolio").value = profile.links?.portfolio || profile.links?.website || "";
    $("#p-target-roles").value = (preferences.target_roles || []).join(", ");
    $("#p-locations").value = (preferences.locations || []).join(", ");
    $("#p-salary-floor").value = preferences.salary_floor ?? "";
    $("#p-currency").value = preferences.currency || "PKR";
    $("#p-salary-period").value = preferences.salary_period || "month";
    $("#p-seniority").value = preferences.seniority || "entry";
    $("#p-remote-preferred").checked = preferences.remote_preferred !== false;
    $("#p-onsite-ok").checked = preferences.onsite_ok !== false;
    $("#p-excluded-companies").value = (preferences.excluded_companies || []).join(", ");
    $("#p-excluded-keywords").value = (preferences.excluded_keywords || []).join(", ");
    $("#p-skills-to-avoid").value = (preferences.skills_to_avoid || []).join(", ");
    $("#p-notes").value = preferences.notes || "";
    renderRepeatables(profile);
    updateFactCounts();
    setDirty(false);
  }

  function updateFactCounts() {
    const experiences = document.querySelectorAll('#experience-list [data-kind="experience"]');
    const skills = document.querySelectorAll('#skill-list [data-kind="skill"]');
    let bullets = 0;
    experiences.forEach((card) => {
      const value = card.querySelector('[data-field="bullets"]')?.value || "";
      bullets += value.split("\n").filter((line) => line.trim()).length;
    });
    $("#fact-experiences").textContent = experiences.length;
    $("#fact-bullets").textContent = bullets;
    $("#fact-skills").textContent = skills.length;
  }

  function setDirty(dirty) {
    state.dirty = dirty;
    $("#save-bar").hidden = !dirty;
  }

  function collect() {
    const profile = state.profile || {};
    const links = { ...(profile.links || {}) };
    const linkMap = {
      linkedin: $("#p-linkedin").value.trim(),
      github: $("#p-github").value.trim(),
      portfolio: $("#p-portfolio").value.trim(),
    };
    Object.entries(linkMap).forEach(([key, value]) => {
      if (value) links[key] = value;
      else delete links[key];
    });

    const experiences = [...document.querySelectorAll('#experience-list [data-kind="experience"]')]
      .map((card) => {
        const get = (name) => card.querySelector(`[data-field="${name}"]`);
        const isCurrent = get("is_current").checked;
        return {
          id: card.dataset.id || "",
          company: get("company").value.trim(),
          title: get("title").value.trim(),
          start_date: get("start_date").value.trim(),
          end_date: isCurrent ? "" : get("end_date").value.trim(),
          location: get("location").value.trim(),
          is_current: isCurrent,
          bullets: get("bullets").value.split("\n").map((line) => line.trim()).filter(Boolean),
          skills: splitList(get("skills").value),
        };
      })
      .filter((item) => item.company || item.title);

    const education = [...document.querySelectorAll('#education-list [data-kind="education"]')]
      .map((card) => {
        const get = (name) => card.querySelector(`[data-field="${name}"]`);
        return {
          id: card.dataset.id || "",
          institution: get("institution").value.trim(),
          degree: get("degree").value.trim(),
          field_of_study: get("field_of_study").value.trim(),
          start_date: get("start_date").value.trim(),
          end_date: get("end_date").value.trim(),
          details: get("details").value.trim(),
        };
      })
      .filter((item) => item.institution);

    const projects = [...document.querySelectorAll('#project-list [data-kind="project"]')]
      .map((card) => {
        const get = (name) => card.querySelector(`[data-field="${name}"]`);
        return {
          id: card.dataset.id || "",
          name: get("name").value.trim(),
          url: get("url").value.trim(),
          description: get("description").value.trim(),
          skills: splitList(get("skills").value),
        };
      })
      .filter((item) => item.name);

    const skills = [...document.querySelectorAll('#skill-list [data-kind="skill"]')]
      .map((row) => {
        const get = (name) => row.querySelector(`[data-field="${name}"]`);
        return {
          id: row.dataset.id || "",
          name: get("name").value.trim(),
          level: Math.max(1, Math.min(5, Math.round(numberOr(get("level").value, 3)))),
          years: Math.max(0, numberOr(get("years").value, 0)),
          category: get("category").value.trim() || "general",
        };
      })
      .filter((item) => item.name);

    const salaryFloor = $("#p-salary-floor").value.trim();

    return {
      id: profile.id || 1,
      full_name: $("#p-full-name").value.trim(),
      headline: $("#p-headline").value.trim(),
      email: $("#p-email").value.trim(),
      phone: $("#p-phone").value.trim(),
      location: $("#p-location").value.trim(),
      summary: $("#p-summary").value.trim(),
      links,
      preferences: {
        target_roles: splitList($("#p-target-roles").value),
        locations: splitList($("#p-locations").value),
        remote_preferred: $("#p-remote-preferred").checked,
        onsite_ok: $("#p-onsite-ok").checked,
        salary_floor: salaryFloor ? Number(salaryFloor) : null,
        currency: ($("#p-currency").value.trim() || "PKR").toUpperCase(),
        salary_period: $("#p-salary-period").value,
        seniority: $("#p-seniority").value,
        excluded_companies: splitList($("#p-excluded-companies").value),
        excluded_keywords: splitList($("#p-excluded-keywords").value),
        skills_to_avoid: splitList($("#p-skills-to-avoid").value),
        work_authorization: $("#p-auth").value.trim(),
        notes: $("#p-notes").value.trim(),
      },
      experiences,
      education,
      projects,
      skills,
    };
  }

  async function save(event) {
    event?.preventDefault();
    const payload = collect();
    if (!payload.full_name) {
      JP.toast("A full name is required.", "error");
      $("#p-full-name").focus();
      return;
    }
    const buttons = document.querySelectorAll("#save-profile, .save-bar button");
    buttons.forEach((button) => (button.disabled = true));
    try {
      const data = await JP.api("/api/profile", { method: "PUT", body: payload });
      JP.toast("Profile saved. Scores will refresh against the new version.", "success");
      renderProfile(data.profile);
      $("#profile-version").textContent = `v${(data.version || "").slice(0, 8)}`;
      $("#profile-saved-at").textContent = `Saved ${JP.formatDate(data.profile.updated_at)} · version ${(data.version || "").slice(0, 8)}`;
      JP.loadBootstrap();
    } catch (error) {
      JP.toast(error.message, "error");
    } finally {
      buttons.forEach((button) => (button.disabled = false));
    }
  }

  function addItem(kind) {
    const profile = state.profile || {};
    if (kind === "experience") {
      profile.experiences = [...(profile.experiences || []), { company: "", title: "", start_date: "", end_date: "", location: "", bullets: [], skills: [], is_current: false }];
      $("#experience-list").insertAdjacentHTML("beforeend", experienceCard(profile.experiences[profile.experiences.length - 1], profile.experiences.length - 1));
    } else if (kind === "education") {
      profile.education = [...(profile.education || []), { institution: "", degree: "", field_of_study: "", start_date: "", end_date: "", details: "" }];
      $("#education-list").insertAdjacentHTML("beforeend", educationCard(profile.education[profile.education.length - 1], profile.education.length - 1));
    } else if (kind === "project") {
      profile.projects = [...(profile.projects || []), { name: "", description: "", url: "", skills: [] }];
      $("#project-list").insertAdjacentHTML("beforeend", projectCard(profile.projects[profile.projects.length - 1], profile.projects.length - 1));
    } else if (kind === "skill") {
      $("#skill-list")?.querySelector(".skill-empty")?.remove();
      $("#skill-list").insertAdjacentHTML("beforeend", skillRow({ name: "", level: 3, years: 1, category: "general" }));
    }
    setDirty(true);
    updateFactCounts();
  }

  function bind() {
    $("#add-experience")?.addEventListener("click", () => addItem("experience"));
    $("#add-education")?.addEventListener("click", () => addItem("education"));
    $("#add-project")?.addEventListener("click", () => addItem("project"));
    $("#add-skill")?.addEventListener("click", () => addItem("skill"));

    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-remove]");
      if (!button) return;
      const kind = button.dataset.remove;
      if (kind === "skill") button.closest(".skill-row")?.remove();
      else button.closest(".repeat-card")?.remove();
      if (kind === "skill" && !document.querySelector('#skill-list [data-kind="skill"]')) {
        renderSkills([]);
      }
      setDirty(true);
      updateFactCounts();
    });

    $("#profile-form")?.addEventListener("submit", save);
    $("#profile-form")?.addEventListener("input", () => setDirty(true));
    $("#profile-form")?.addEventListener("change", () => setDirty(true));
    $("#profile-form")?.addEventListener("input", (event) => {
      if (event.target.closest("#experience-list")) updateFactCounts();
    });

    window.addEventListener("beforeunload", (event) => {
      if (!state.dirty) return;
      event.preventDefault();
      event.returnValue = "";
    });
  }

  async function init() {
    bind();
    try {
      const data = await JP.api("/api/profile");
      renderProfile(data.profile);
      $("#profile-version").textContent = `v${(data.version || "").slice(0, 8)}`;
      if (data.profile.updated_at) {
        $("#profile-saved-at").textContent = `Saved ${JP.formatDate(data.profile.updated_at)} · version ${(data.version || "").slice(0, 8)}`;
      }
    } catch (error) {
      JP.toast(error.message, "error");
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
