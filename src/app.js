/* Bloodlust Optimizer — renderer.
 *
 * Plain DOM, no framework, no bundler, no CDN. The renderer holds no logic that
 * matters: every decision (gating, risk ceiling, undo) is made by the Python
 * engine, and the UI only renders what it is told. That keeps the display
 * honest — a lock in the UI corresponds to a real refusal in the engine, not
 * to a client-side guess.
 */
"use strict";

const invoke = (cmd, args) => window.__TAURI__.core.invoke(cmd, args || {});

const state = {
  families: [],
  actions: [],
  activeFamily: null,
  ceiling: "safe",
  undo: [],
  view: "actions",
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ toasts */

function toast(message, kind = "info", ms = 3600) {
  const host = $("toast-host");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

/* -------------------------------------------------------------- boot / sys */

function setEngine(ok, text) {
  const dot = $("engine-dot");
  dot.className = "dot " + (ok === null ? "" : ok ? "ok" : "bad");
  $("sys-text").textContent = text;
}

function summariseSystem(info) {
  const hw = (info && info.hardware) || {};
  const bits = [];
  if (hw.cpu_name) bits.push(hw.cpu_name.replace(/\(R\)|\(TM\)/g, "").replace(/\s+/g, " ").trim());
  if (hw.discrete_gpu) bits.push(hw.discrete_gpu.name);
  if (hw.ram_mb) bits.push(`${Math.round(hw.ram_mb / 1024)} GB RAM`);
  if (hw.is_elevated === false) bits.push("NOT ELEVATED");
  return bits.join("  ·  ") || "system information unavailable";
}

function renderProvenance(prov) {
  const el = $("provenance");
  if (!prov || !prov.source) { el.textContent = "not loaded"; return; }
  if (prov.source === "remote") {
    const hash = (prov.sha256 || "").slice(0, 12);
    el.innerHTML = `<span class="ok">GitHub</span> · ${escapeHtml(prov.catalog_version || "unversioned")}<br>sha256 ${escapeHtml(hash)}…`;
  } else if (prov.source === "bundled") {
    el.innerHTML = `<span class="warn">bundled</span> · ${escapeHtml(prov.catalog_version || "unversioned")}<br>GitHub unreachable or not yet synced`;
  } else {
    el.innerHTML = `<span class="warn">${escapeHtml(prov.source)}</span>`;
  }
}

/* -------------------------------------------------------------- risk labels */

const RISK_ORDER = ["safe", "moderate", "aggressive", "danger"];

function ceilingBlocks(risk) {
  return RISK_ORDER.indexOf(risk) > RISK_ORDER.indexOf(state.ceiling);
}

/* ------------------------------------------------------------------ families */

function renderFamilies() {
  const host = $("family-list");
  host.innerHTML = "";
  for (const fam of state.families) {
    const count = state.actions.filter((a) => a.family === fam.id).length;
    const el = document.createElement("div");
    el.className = "family" + (state.activeFamily === fam.id ? " on" : "");
    el.innerHTML =
      `<span>${escapeHtml(fam.label)}</span><span class="count">${count}</span>`;
    el.onclick = () => selectFamily(fam.id);
    host.appendChild(el);
  }
}

function selectFamily(id) {
  state.activeFamily = id;
  state.view = "actions";
  showView("actions");
  renderFamilies();
  renderActions();
}

/* ------------------------------------------------------------------- actions */

function renderActions() {
  const fam = state.families.find((f) => f.id === state.activeFamily);
  $("family-title").textContent = fam ? fam.label : "Actions";
  $("family-desc").textContent = fam ? fam.description || "" : "";

  const host = $("action-list");
  host.innerHTML = "";

  const list = state.actions.filter((a) => a.family === state.activeFamily);
  if (!list.length) {
    host.innerHTML = `<p class="muted">No actions in this family.</p>`;
    return;
  }

  for (const action of list) host.appendChild(cardFor(action));

  const available = list.filter((a) => a.available).length;
  $("batch-note").textContent = `${available} of ${list.length} available here`;
}

function cardFor(action) {
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.id = action.id;

  const blocked = !action.available;
  const overCeiling = ceilingBlocks(action.risk);
  if (blocked) card.classList.add("blocked");

  const head = document.createElement("div");
  head.className = "card-head";

  const titleWrap = document.createElement("div");
  titleWrap.className = "card-title";
  titleWrap.innerHTML =
    `${escapeHtml(action.label)}<span class="card-id">${escapeHtml(action.id)}</span>`;

  const pills = document.createElement("div");
  pills.className = "pills";
  pills.appendChild(pill(action.risk, action.risk));
  if (action.needs_reboot) pills.appendChild(pill("reboot required", "reboot"));
  if (action.reversible === false) pills.appendChild(pill("irreversible", "irreversible"));
  if (action.has_undo) pills.appendChild(pill("undo available", "safe"));

  const actions = document.createElement("div");
  actions.className = "card-actions";

  const btnPreview = button("Preview", "btn ghost small", () => preview(action.id, card));
  actions.appendChild(btnPreview);

  if (!blocked && !overCeiling) {
    actions.appendChild(
      button("Apply", "btn small", () => run(action.id, card), action.risk === "danger")
    );
  }
  if (action.has_undo) {
    actions.appendChild(button("Undo", "btn undo small", () => undo(action.id, card)));
  }

  head.append(titleWrap, actions);
  card.appendChild(head);
  card.appendChild(pills);

  if (action.description) {
    const desc = document.createElement("p");
    desc.className = "card-desc";
    desc.textContent = action.description;
    card.appendChild(desc);
  }

  const foot = document.createElement("div");
  foot.className = "card-foot";
  foot.hidden = true;
  card.appendChild(foot);

  if (blocked) {
    showFoot(card, action.reason || "Unavailable on this machine", "blocked-reason");
  } else if (overCeiling) {
    showFoot(
      card,
      `Risk ${action.risk} is above your ${state.ceiling} ceiling. Raise the ceiling to apply it.`,
      "blocked-reason"
    );
  }

  return card;
}

function pill(text, cls) {
  const el = document.createElement("span");
  el.className = `pill ${cls}`;
  el.textContent = text;
  return el;
}

function button(label, cls, onclick, confirmFirst = false) {
  const el = document.createElement("button");
  el.className = cls;
  el.textContent = label;
  el.onclick = async () => {
    if (confirmFirst) {
      const ok = await window.__TAURI__.dialog
        .ask(`Apply "${label}"? This changes system settings.`, {
          title: "Confirm",
          kind: "warning",
        })
        .catch(() => true);
      if (!ok) return;
    }
    el.disabled = true;
    try {
      await onclick();
    } finally {
      el.disabled = false;
    }
  };
  return el;
}

function showFoot(card, text, cls) {
  const foot = card.querySelector(".card-foot");
  if (!foot) return;
  foot.hidden = false;
  foot.className = "card-foot " + (cls || "");
  foot.textContent = text;
}

function renderChanges(card, result) {
  const foot = card.querySelector(".card-foot");
  foot.hidden = false;
  foot.className = "card-foot";
  foot.innerHTML = "";

  const msg = document.createElement("div");
  msg.textContent = result.message || "";
  foot.appendChild(msg);

  const changes = result.changes || [];
  if (changes.length) {
    const ul = document.createElement("ul");
    ul.className = "changes";
    for (const c of changes.slice(0, 12)) {
      const li = document.createElement("li");
      li.innerHTML =
        `<span class="target">${escapeHtml(c.target || "")}</span> ` +
        `<span class="detail">${escapeHtml(c.detail || "")}</span>`;
      ul.appendChild(li);
    }
    if (changes.length > 12) {
      const li = document.createElement("li");
      li.className = "detail";
      li.textContent = `… and ${changes.length - 12} more`;
      ul.appendChild(li);
    }
    foot.appendChild(ul);
  }

  for (const w of result.warnings || []) {
    const warn = document.createElement("div");
    warn.className = "card-foot blocked-reason";
    warn.textContent = "⚠ " + w;
    foot.appendChild(warn);
  }

  if (result.status === "preview") {
    const note = document.createElement("div");
    note.className = "card-foot";
    note.style.color = "var(--info)";
    note.textContent = "Preview only — nothing was changed.";
    foot.appendChild(note);
  }
}

/* ------------------------------------------------------------------ commands */

async function run(id, card) {
  try {
    const result = await invoke("action_run", { id });
    renderChanges(card, result);
    card.classList.toggle("applied", result.status === "applied");
    toast(
      result.status === "skipped"
        ? `${id}: already in the desired state`
        : `${id}: ${result.message || "applied"}`,
      result.ok ? "ok" : "err"
    );
    if (result.needs_reboot) toast(`${id} needs a reboot to take effect`, "warn", 6000);
    await refreshUndo();
  } catch (e) {
    showFoot(card, String(e), "err-reason");
    toast(String(e), "err");
  }
}

async function preview(id, card) {
  try {
    const result = await invoke("action_preview", { id });
    renderChanges(card, result);
  } catch (e) {
    showFoot(card, String(e), "err-reason");
    toast(String(e), "err");
  }
}

async function undo(id, card) {
  try {
    const result = await invoke("undo_action", { id });
    card.classList.remove("applied");
    showFoot(card, result.message || "reverted", "ok-reason");
    toast(`${id}: reverted`, "ok");
    await refreshUndo();
    renderActions();
  } catch (e) {
    showFoot(card, String(e), "err-reason");
    toast(String(e), "err");
  }
}

/* -------------------------------------------------------------------- batch */

async function runAll(previewMode) {
  const ids = state.actions
    .filter(
      (a) =>
        a.family === state.activeFamily &&
        a.available &&
        a.default_enabled &&
        !ceilingBlocks(a.risk)
    )
    .map((a) => a.id);

  if (!ids.length) {
    toast("Nothing available to run in this family", "warn");
    return;
  }

  if (!previewMode) {
    const ok = await window.__TAURI__.dialog
      .ask(`Apply ${ids.length} action(s) in this family?`, {
        title: "Confirm batch",
        kind: "warning",
      })
      .catch(() => true);
    if (!ok) return;
  }

  try {
    const result = await invoke("batch_run", {
      mode: state.ceiling === "safe" ? "safe" : "aggressive",
      ids,
      preview: previewMode,
    });
    openDialog(
      previewMode ? "Preview results" : "Batch results",
      batchRows(result)
    );
    toast(
      `${result.applied} applied, ${result.skipped} skipped, ${result.failed} failed`,
      result.failed ? "warn" : "ok"
    );
    await refreshUndo();
    renderActions();
  } catch (e) {
    toast(String(e), "err");
  }
}

function batchRows(result) {
  const rows = [];
  for (const r of result.results || []) {
    rows.push({
      k: r.action_id,
      v: r.ok ? (r.status || "applied") : `failed: ${r.error || "?"}`,
      cls: r.ok ? "" : "err",
    });
  }
  return rows;
}

/* --------------------------------------------------------------------- undo */

async function refreshUndo() {
  try {
    const list = await invoke("undo_list");
    state.undo = Array.isArray(list) ? list : [];
  } catch {
    state.undo = [];
  }
  const badge = $("undo-count");
  badge.hidden = state.undo.length === 0;
  badge.textContent = String(state.undo.length);
  if (state.view === "undo") renderUndo();
}

function renderUndo() {
  const host = $("undo-list");
  host.innerHTML = "";

  if (!state.undo.length) {
    host.innerHTML = `<p class="muted">Nothing to revert. Applied changes appear here.</p>`;
    return;
  }

  for (const entry of state.undo) {
    const card = document.createElement("div");
    card.className = "card";

    const head = document.createElement("div");
    head.className = "card-head";

    const title = document.createElement("div");
    title.className = "card-title";
    title.innerHTML =
      `${escapeHtml(entry.label || entry.action_id)}` +
      `<span class="card-id">${escapeHtml(entry.action_id)} · ${entry.entries} journaled value(s)</span>`;

    const actions = document.createElement("div");
    actions.className = "card-actions";
    actions.appendChild(
      button("Revert", "btn undo small", async () => {
        try {
          const r = await invoke("undo_action", { id: entry.action_id });
          toast(`${entry.action_id}: reverted`, "ok");
          await refreshUndo();
          renderActions();
        } catch (e) {
          toast(String(e), "err");
        }
      })
    );

    head.append(title, actions);
    card.appendChild(head);
    host.appendChild(card);
  }
}

/* -------------------------------------------------------------------- views */

function showView(name) {
  state.view = name;
  for (const v of ["actions", "undo", "about", "log", "contact"]) {
    $("view-" + v).hidden = v !== name;
  }
  if (name === "undo") renderUndo();
  if (name === "about") renderAbout();
  if (name === "log") renderLog();
}

const TELEGRAM_LINK = "https://t.me/Alpha0NemesiSS";

async function copyTelegramLink(btn) {
  try {
    await navigator.clipboard.writeText(TELEGRAM_LINK);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = TELEGRAM_LINK;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch {}
    ta.remove();
  }
  const original = btn.innerHTML;
  btn.textContent = "Copied!";
  btn.disabled = true;
  setTimeout(() => {
    btn.innerHTML = original;
    btn.disabled = false;
  }, 1800);
  toast("Link copied: " + TELEGRAM_LINK, "ok");
}

function openTelegram() {
  // External navigation is not allowed from the webview; the default browser
  // is the right place for Telegram. If the platform refuses, the handle and
  // the QR remain on screen.
  try {
    const win = window.open(TELEGRAM_LINK, "_blank", "noopener");
    if (win) return;
  } catch {}
  toast("Couldn't open the browser — scan the QR or type t.me/Alpha0NemesiSS", "warn", 6000);
}

async function renderAbout() {
  const host = $("about-body");
  try {
    const info = await invoke("system_info");
    const about = await invoke("about");
    const hw = (info && info.hardware) || {};

    const rows = [
      ["Application", `${about.name} v${about.version}`],
      ["Developer", about.developer],
      ["Protocol", `v${about.protocol}`],
      ["Catalog schema", `v${about.catalog_schema}`],
      ["Repository", about.repo],
      ["Operating system", `${hw.os_name || "?"} ${hw.os_version || ""}`],
      ["CPU", `${hw.cpu_name || "?"} (${hw.cpu_cores || "?"} cores / ${hw.cpu_threads || "?"} threads)`],
      ["GPU", hw.discrete_gpu ? hw.discrete_gpu.name : "none detected"],
      ["RAM", hw.ram_mb ? `${(hw.ram_mb / 1024).toFixed(1)} GB` : "?"],
      ["Administrator", hw.is_elevated ? "yes" : "no — HKLM writes will fail"],
      ["Hypervisor active", hw.hypervisor_present === null ? "unknown" : hw.hypervisor_present ? "yes" : "no"],
    ];

    host.innerHTML =
      `<table><tbody>` +
      rows
        .map(
          ([k, v]) =>
            `<tr><th>${escapeHtml(k)}</th><td>${escapeHtml(String(v))}</td></tr>`
        )
        .join("") +
      `</tbody></table>

      <h3>How this works</h3>
      <p>Every action is described in a catalog fetched from the project's GitHub repository.
      The catalog is <b>data, not code</b>: it can only select and configure actions that are
      already compiled into this build. A catalog from GitHub cannot make the app do anything
      new, and it is rejected outright if it tries.</p>

      <h3>Undo</h3>
      <p>Before a value is changed, the previous value is recorded in a journal. Reverting an
      action replays that journal, so applying an action twice and reverting once still returns
      you to the original state.</p>

      <h3>What this cannot fix</h3>
      <p>An emulator adds ARM-to-x86 translation and a frame of queueing that no setting removes.
      If your hardware is already the limit, these tweaks will not create headroom that is not
      there. They remove overhead; they do not add performance.</p>`;
  } catch (e) {
    host.textContent = String(e);
  }
}

async function renderLog() {
  try {
    const entries = await invoke("log_tail", { limit: 250 });
    $("log-body").textContent = (entries || [])
      .map((e) => `${new Date(e.ts * 1000).toLocaleTimeString()}  ${e.level.padEnd(7)} ${e.message}`)
      .join("\n") || "no log entries yet";
  } catch (e) {
    $("log-body").textContent = String(e);
  }
}

/* ------------------------------------------------------------------- dialog */

function openDialog(title, rows) {
  $("dlg-title").textContent = title;
  const body = $("dlg-body");
  body.innerHTML = "";
  for (const r of rows) {
    const el = document.createElement("div");
    el.className = "row";
    el.innerHTML = `<b>${escapeHtml(r.k)}</b> — ${escapeHtml(String(r.v))}`;
    if (r.cls === "err") el.style.color = "var(--danger)";
    body.appendChild(el);
  }
  $("result-dialog").showModal();
}

/* -------------------------------------------------------------------- utils */

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

/* --------------------------------------------------------------------- init */

async function main() {
  $("btn-refresh-catalog").onclick = async () => {
    setEngine(null, "syncing catalog…");
    try {
      const r = await invoke("refresh_catalog");
      toast(`Catalog synced: ${r.actions} actions`, "ok");
      await afterCatalogChange();
    } catch (e) {
      toast(`Catalog sync failed: ${e}`, "warn", 6000);
      setEngine(true, $("sys-text").textContent);
    }
  };

  $("btn-undo").onclick = () => showView("undo");
  $("btn-run-all").onclick = () => runAll(false);
  $("btn-preview-all").onclick = () => runAll(true);
  $("btn-undo-all").onclick = async () => {
    try {
      const r = await invoke("undo_all");
      toast(`Reverted ${r.restored}, failed ${r.failed}`, r.failed ? "warn" : "ok");
      await refreshUndo();
      renderActions();
    } catch (e) {
      toast(String(e), "err");
    }
  };
  $("lnk-about").onclick = (e) => { e.preventDefault(); showView("about"); };
  $("lnk-log").onclick = (e) => { e.preventDefault(); showView("log"); };
  $("btn-contact").onclick = (e) => { e.preventDefault(); showView("contact"); };
  $("lnk-contact").onclick = (e) => { e.preventDefault(); showView("contact"); };
  $("btn-copy-telegram").onclick = () => copyTelegramLink($("btn-copy-telegram"));
  $("btn-open-telegram").onclick = openTelegram;
  $("dlg-close").onclick = () => $("result-dialog").close();

  for (const btn of document.querySelectorAll(".risk-opt")) {
    btn.onclick = async () => {
      const ceiling = btn.dataset.ceiling;
      try {
        await invoke("risk_set_ceiling", { ceiling });
        state.ceiling = ceiling;
        for (const b of document.querySelectorAll(".risk-opt")) {
          b.classList.toggle("on", b.dataset.ceiling === ceiling);
        }
        renderActions();
      } catch (e) {
        toast(String(e), "err");
      }
    };
  }

  // ---- boot ----
  try {
    $("boot-status").textContent = "starting the tuning engine…";
    const boot = await invoke("bootstrap");

    if (boot && boot.catalog_refresh && boot.catalog_refresh.ok === false) {
      toast(
        `Using the bundled catalog (${boot.catalog_refresh.error})`,
        "warn",
        7000
      );
    }

    await afterCatalogChange(boot);

    const info = boot.system || (await invoke("system_info"));
    setEngine(true, summariseSystem(info));

    $("boot").remove();
    $("app").hidden = false;

    if (info && info.hardware && info.hardware.is_elevated === false) {
      toast(
        "Not running as administrator — system-level tweaks are locked",
        "warn",
        8000
      );
    }
  } catch (e) {
    setEngine(false, String(e));
    $("boot-status").innerHTML =
      `could not start the tuning engine<br><br><code style="color:var(--danger)">${escapeHtml(String(e))}</code>`;
  }
}

async function afterCatalogChange() {
  // Three independent reads; none of them can be derived from another.
  const [families, actions, prov] = await Promise.all([
    invoke("catalog_families"),
    invoke("action_list"),
    invoke("catalog_provenance"),
  ]);

  state.families = Array.isArray(families) ? families : [];
  state.actions = Array.isArray(actions) ? actions : [];

  // Defensive fallback: if the engine returned no families but did return
  // actions, show the actions grouped by their raw family id rather than an
  // empty sidebar.
  if (!state.families.length && state.actions.length) {
    const seen = new Map();
    for (const a of state.actions) {
      if (!seen.has(a.family)) {
        seen.set(a.family, { id: a.family, label: a.family, description: "" });
      }
    }
    state.families = [...seen.values()];
  }

  if (!state.activeFamily || !state.families.some((f) => f.id === state.activeFamily)) {
    state.activeFamily = state.families.length ? state.families[0].id : null;
  }

  renderProvenance(prov.active || {});
  renderFamilies();
  renderActions();
  await refreshUndo();
}

main();
