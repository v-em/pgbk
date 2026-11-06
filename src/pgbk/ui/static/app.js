"use strict";
const root = document.querySelector("#app");
let session = null, profiles = [], timer = null, generation = 0;
const reasons = {
  COPY_REMOVED_OR_UNAVAILABLE: "Copy removed or unavailable",
  RECOVERY_NOT_STORED: "Recovery instructions were not stored for this backup",
  RECOVERY_INTEGRITY_ERROR: "Integrity error: the stored recovery note could not be verified.",
  RECOVERY_TEMPORARILY_UNAVAILABLE: "Recovery instructions are temporarily unavailable.",
  AUTHENTICATION_UNAVAILABLE: "Authentication temporarily unavailable. Check the Vault connection.",
  SESSION_ROTATED: "Administrator credentials changed. Sign in again.",
  INVALID_CREDENTIALS: "Invalid username or password.",
  LAUNCH_OUTCOME_UNKNOWN: "Outcome unknown. Inspect Jobs and catalog events before intentionally starting a new request."
};
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) if (child !== null && child !== undefined) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
const button = (text, action, cls = "") => el("button", {type: "button", class: cls, onclick: action}, text);
const text = value => value === null || value === undefined || value === "" ? "Unavailable" : String(value);
const date = value => value ? new Date(value).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "long"}) : "Unavailable";
const size = value => value === undefined ? "Unavailable" : value < 1024 ? `${value} B` : value < 1048576 ? `${(value / 1024).toFixed(1)} KiB` : value < 1073741824 ? `${(value / 1048576).toFixed(1)} MiB` : `${(value / 1073741824).toFixed(2)} GiB`;
const duration = value => value === undefined || value === null ? "Unavailable" : `${Math.floor(value / 60)}m ${Math.floor(value % 60)}s`;
function badge(value) {
  const lower = String(value || "unknown").toLowerCase();
  return el("span", {class: "badge " + (/failed|denied|overdue|error/.test(lower) ? "bad" : /pending|busy|paused|running|unknown/.test(lower) ? "warn" : /not-run|unavailable|removed/.test(lower) ? "neutral" : "")}, text(value));
}
function link(label, path, cls = "link") { return el("a", {href: path, class: cls, onclick: event => { if (!event.ctrlKey && !event.metaKey) {event.preventDefault(); navigate(path);} }}, label); }
function backupPath(b) { return `/backups/${encodeURIComponent(b.profileId)}/${encodeURIComponent(b.sourceId)}/${encodeURIComponent(b.runId)}`; }
function backupApi(b) { return `/profiles/${encodeURIComponent(b.profileId)}/sources/${encodeURIComponent(b.sourceId)}/backups/${encodeURIComponent(b.runId)}`; }
function jobPath(j) { return `/jobs/${encodeURIComponent(j.namespace)}/${encodeURIComponent(j.uid || j.jobUid)}`; }
function kv(entries) {return el("dl", {class: "kv"}, entries.flatMap(([label, value]) => [el("dt", {}, label), el("dd", {}, value instanceof Node ? value : text(value))]));}
function errorBox(error) {return el("div", {class: "banner error", role: "alert"}, reasons[error.code] || error.message || "Temporarily unavailable", error.code ? `\n${error.code}` : "", error.requestId ? ` · Request ${error.requestId}` : "");}
async function api(path, options = {}) {
  let response;
  try { response = await fetch("/api/v1" + path, {credentials: "same-origin", cache: "no-store", ...options, headers: {"Content-Type": "application/json", ...(session ? {"X-CSRF-Token": session.csrfToken} : {}), ...(options.headers || {})}}); }
  catch (_) { throw {message: "Connection unavailable. Retry when the connection returns."}; }
  const data = await response.json();
  if (!response.ok) {
    const error = {code: data.reasonCode, requestId: data.requestId};
    if (response.status === 401 && path !== "/session") {session = null; login(error);}
    throw error;
  }
  return data;
}
function login(error) {
  clearTimeout(timer);
  const username = el("input", {name: "username", autocomplete: "username", required: "", maxlength: "256"});
  const password = el("input", {name: "password", type: "password", autocomplete: "current-password", required: ""});
  const messages = el("div", {"aria-live": "polite"}, error ? errorBox(error) : null);
  const submit = el("button", {type: "submit", class: "primary"}, "Sign in →");
  const form = el("form", {class: "login-form", onsubmit: async event => {
    event.preventDefault(); submit.disabled = true; messages.replaceChildren();
    try {session = await api("/session", {method: "POST", body: JSON.stringify({username: username.value, password: password.value})}); password.value = ""; await start();}
    catch (e) {password.value = ""; messages.replaceChildren(errorBox(e)); submit.disabled = false;}
  }}, el("div", {class: "eyebrow"}, "Administrator access"), el("h1", {}, "Welcome to pgbk"), el("p", {class: "subtitle"}, "Sign in to your backup installation."), messages,
    el("label", {}, "Username", username), el("label", {}, "Password", password), submit,
    el("p", {class: "login-foot"}, "Your installation administrator manages access through Vault. Sessions expire automatically."));
  root.replaceChildren(el("div", {class: "login-shell"}, el("aside", {class: "login-aside"}, brand(), el("div", {}, el("h1", {}, "Every backup.\nA clear path back."), el("p", {}, "Published copies, scheduled protection and the Jobs behind them. One place to see where you stand.")), el("div", {class: "eyebrow"}, "PostgreSQL backups for Kubernetes")), el("main", {class: "login-main"}, form)));
}
function brand() {return el("div", {class: "brand"}, el("span", {class: "brand-mark", "aria-hidden": "true"}, "▤"), "pgbk");}
function layout(section) {
  const content = el("main", {class: "content", id: "content"});
  root.replaceChildren(el("div", {class: "shell"}, el("aside", {class: "sidebar"}, brand(), el("div", {class: "brand-caption"}, "Backup console"),
    el("nav", {class: "nav", "aria-label": "Main navigation"}, [ ["Backups", "▤", "/backups"], ["Schedules", "◷", "/schedules"], ["Jobs", "⌘", "/jobs"] ].map(([label, icon, path]) => {
      const a = link("", path, ""); if (section === label) a.setAttribute("aria-current", "page"); a.append(el("span", {class: "nav-icon", "aria-hidden": "true"}, icon), label); return a;
    })), el("div", {class: "side-bottom"}, el("div", {}, el("span", {class: "live-dot"}), "Installation console"), el("div", {class: "muted"}, "pgbk web UI v1 · 0.3.3"))),
    el("div", {class: "main"}, el("header", {class: "topbar"}, el("div", {}, el("div", {class: "eyebrow"}, "Kubernetes / Installation"), el("div", {class: "installation mono"}, session.installationId || "pgbk")),
      el("div", {class: "account"}, el("span", {}, `${session.subject} · Administrator`), button("Sign out", async () => {try {await api("/session", {method: "DELETE"}); session = null; login();} catch (e) {content.prepend(errorBox(e));}}))), content)));
  return content;
}
function heading(title, subtitle, action) {return el("div", {class: "heading"}, el("div", {}, el("h1", {}, title), el("p", {class: "subtitle"}, subtitle)), action);}
function freshness(page) {
  const box = el("div", {});
  if (page.partial) box.append(el("div", {class: "banner"}, "Partial results. The initial scan is in progress or a data source is unavailable. Healthy sources remain visible."));
  for (const error of page.partialErrors || []) box.append(errorBox({code: error.reasonCode, message: `${error.profileId || error.namespace || "Source"}: ${error.reasonCode}`}));
  return box;
}
function table(headers, rows, empty = "No records in this view.") {
  return el("div", {class: "table-scroll"}, rows.length ? el("table", {}, el("thead", {}, el("tr", {}, headers.map(h => el("th", {scope: "col"}, h)))), el("tbody", {}, rows.map(row => el("tr", {}, row.map(cell => el("td", {}, cell)))))) : el("div", {class: "empty"}, empty));
}
function filters(kind, apply) {
  const form = el("form", {class: "toolbar", onsubmit: event => {event.preventDefault(); apply(new URLSearchParams(new FormData(form)));}});
  function select(label, name, values) {form.append(el("label", {}, label, el("select", {name}, values.map(([value, title]) => el("option", {value}, title)))));}
  select("Profile", "profile", [["", "All profiles"], ...profiles.map(p => [p.profileId, p.profileId])]);
  if (kind === "backups") {
    select("Source", "source", [["", "All sources"], ...[...new Set(profiles.map(p => p.sourceId))].map(s => [s, s])]);
    select("Kind", "kind", [["", "All kinds"], ["logical", "Logical"], ["physical-full", "Physical full"]]);
    for (const [name, label] of [["from", "Completed after"], ["to", "Completed before"]]) form.append(el("label", {}, label, el("input", {type: "datetime-local", name})));
  } else {
    select("Operation", "operation", [["backup", "Backups"], ["all", "All operations"], ["verify", "Verify"], ["retention", "Retention"], ["restore-logical", "Restore logical"], ["restore-physical", "Restore physical"]]);
    select("Status", "status", [["", "All statuses"], ...["Pending", "Running", "Complete", "Failed", "Suspended", "Job removed", "Unknown"].map(s => [s, s])]);
  }
  form.append(el("button", {type: "submit"}, "Apply filters"));
  return form;
}
function stats(items, kind, page) {
  const entries = kind === "backups" ? [["Published copies", items.length, page.nextCursor ? "More copies on the next page" : "In the current view"], ["Stored size", size(items.reduce((sum, b) => sum + (b.bytes || 0), 0)), "Published artifacts in this view"], ["Latest copy", items[0] ? date(items[0].completedAt) : "—", "Completion time · local time zone"]] : [["Profiles", items.length, "Explicitly allowed for this installation"], ["Scheduled", items.filter(i => i.status === "Scheduled").length, "Active backup schedules"], ["Paused", items.filter(i => i.suspend).length, "Manual runs may still be available"]];
  return el("div", {class: "stats"}, entries.map(([label, value, note]) => el("div", {class: "stat"}, el("div", {class: "eyebrow"}, label), el("div", {class: "stat-value"}, value), el("div", {class: "stat-note"}, note))));
}
async function listing(kind) {
  const content = layout(kind === "backups" ? "Backups" : "Jobs");
  const myGeneration = generation;
  let query = new URLSearchParams(), items = [], page;
  const results = el("div", {"aria-live": "polite"});
  const statBox = el("div");
  content.append(heading(kind === "backups" ? "Backups" : "Jobs", kind === "backups" ? "Published copies and everything you need to recover." : "Execution status, worker progress and published results.", button("↻  Refresh", () => load())), statBox);
  const panel = el("section", {class: "panel table-panel"}, filters(kind, params => {query = params; for (const key of ["from", "to"]) if (query.get(key)) query.set(key, new Date(query.get(key)).toISOString()); load();}), results);
  content.append(panel);
  async function load(more = false) {
    try {
      const params = new URLSearchParams(query); if (more && page.nextCursor) params.set("cursor", page.nextCursor);
      const result = await api(`/${kind}?${params}`); if (myGeneration !== generation) return;
      page = result; items = more ? [...items, ...page.items] : page.items;
      if (kind === "backups") statBox.replaceChildren(stats(items, kind, page));
      const rows = kind === "backups" ? items.map(b => [el("div", {}, link(b.runId, backupPath(b)), el("span", {class: "secondary"}, date(b.completedAt))), el("div", {}, b.profileId, el("span", {class: "secondary"}, b.sourceId)), badge(b.kind), text(b.trigger), size(b.bytes), badge(b.restoreVerification?.status || "not-run"), link("View →", backupPath(b))]) : items.map(j => [el("div", {}, link(j.name || j.uid, jobPath(j)), el("span", {class: "secondary mono"}, j.namespace)), el("div", {}, j.profileId, el("span", {class: "secondary"}, j.sourceId)), text(j.operation), text(j.trigger), date(j.startedAt || j.createdAt), duration(j.durationSeconds), el("div", {}, badge(j.status), j.worker ? el("span", {class: "secondary"}, `${j.worker.phase} · ${j.worker.reasonCode || ""}`) : null)]);
      results.replaceChildren(freshness(page), table(kind === "backups" ? ["Copy / Completed", "Profile / Source", "Kind", "Trigger", "Size", "Restore verification", ""] : ["Namespace / Job", "Profile / Source", "Operation", "Trigger", "Started", "Duration", "Status / Worker"], rows,
        kind === "backups" ? "No published copies yet. A copy appears after its manifest and publication marker pass validation." : "No Jobs in this view. Live Jobs are limited by Kubernetes history and TTL settings."),
        el("div", {class: "table-footer"}, el("span", {}, `${items.length} records · Last successful refresh: ${date(page.refreshTime)}`), page.nextCursor ? button("Load more", () => load(true)) : el("span", {}, "End of results")));
      if (page.diagnostics?.length) results.append(el("details", {class: "panel diagnostics"}, el("summary", {}, `${page.diagnostics.length} catalog diagnostics`), el("pre", {}, JSON.stringify(page.diagnostics, null, 2))));
    } catch (e) {if (myGeneration === generation) results.prepend(errorBox(e));}
  }
  await load();
  function poll() {timer = setTimeout(async () => {if (generation !== myGeneration) return; if (!page?.nextCursor && items.length <= 25) await load(); poll();}, kind === "jobs" ? 10000 : 30000);}
  poll();
}
async function schedules() {
  const content = layout("Schedules");
  content.append(heading("Schedules", "Backup schedules read directly from your installation’s CronJobs.", button("↻  Refresh", route)));
  try {
    const page = await api("/schedules?limit=100");
    while (page.nextCursor) {const next = await api("/schedules?limit=100&cursor=" + encodeURIComponent(page.nextCursor)); page.items.push(...next.items); page.nextCursor = next.nextCursor;}
    content.append(stats(page.items, "schedules", page), freshness(page), el("section", {class: "panel table-panel"}, table(["Profile / Source", "Kind", "Schedule", "Time zone", "Last scheduled", "Next occurrence", "Status", ""], page.items.map(p => [el("div", {}, el("strong", {}, p.profileId), el("span", {class: "secondary"}, p.sourceId)), badge(p.kind), el("span", {class: "mono"}, text(p.schedule)), text(p.timeZone), date(p.lastScheduleTime), p.suspend ? "—" : date(p.nextOccurrence), el("div", {}, badge(p.status), p.reasonCode ? el("span", {class: "secondary"}, p.reasonCode) : null), (() => {const b = button("Run once", () => launch(p)); b.disabled = !p.launchAvailable; return b;})()])), el("div", {class: "table-footer"}, "A one-off run preserves the next scheduled occurrence.", date(page.refreshTime))));
  } catch (e) {content.append(errorBox(e));}
}
async function launch(profile) {
  const overlay = el("div", {class: "dialog-overlay"});
  const dialog = el("section", {class: "dialog", role: "dialog", "aria-modal": "true", "aria-labelledby": "launch-title"}, el("h2", {id: "launch-title"}, "Run a one-off backup"));
  const messages = el("div", {"aria-live": "polite"});
  const cancel = button("Cancel", () => overlay.remove());
  const accept = button("Start backup →", submit, "primary"); accept.disabled = true;
  dialog.append(el("p", {class: "subtitle"}, "Create a Job from the current backup template. The worker uses its existing coordination leases."), messages, el("div", {class: "dialog-actions"}, cancel, accept)); overlay.append(dialog); document.body.append(overlay);
  let token;
  try { token = await api(`/profiles/${encodeURIComponent(profile.profileId)}/launch-token`); dialog.insertBefore(kv([["Profile", token.profileId], ["Source", token.sourceId], ["Kind", token.kind], ...(token.kind === "logical" ? [["Globals policy", token.globals], ["Maintenance database", token.maintenanceDatabase]] : [])]), messages); accept.disabled = false; accept.focus(); }
  catch (e) {messages.append(errorBox(e));}
  async function submit() {
    accept.disabled = true; cancel.disabled = true; messages.replaceChildren();
    try {
      const job = await api(`/profiles/${encodeURIComponent(profile.profileId)}/backups`, {method: "POST", headers: {"Idempotency-Key": token.key}, body: "{}"});
      if (job.jobUid) {overlay.remove(); navigate(jobPath(job));}
      else {messages.append(errorBox({code: "LAUNCH_OUTCOME_UNKNOWN"}), link("Inspect Jobs and events →", "/jobs")); accept.textContent = "Check original request"; accept.disabled = false; cancel.disabled = false;}
    } catch (e) {messages.append(errorBox(e)); accept.textContent = "Retry original request"; accept.disabled = false; cancel.disabled = false;}
  }
}
function inspectArtifact(artifact) {
  document.querySelector(".drawer")?.remove();
  const drawer = el("aside", {class: "drawer", role: "dialog", "aria-label": "Artifact JSON entry"});
  drawer.append(button("Close ×", () => drawer.remove()), el("h2", {}, artifact.filename), el("p", {class: "subtitle"}, "Stored artifacts[] entry · SHA-256 from manifest"),
    kv([["Database", artifact.database], ["Scope", artifact.scope], ["Compression", artifact.compression ? JSON.stringify(artifact.compression) : null], ["Transfer", artifact.transferMode], ["Selection", artifact.selection ? "Recorded below" : "Unavailable"], ["Archive inventory", artifact.archiveInventory ? "Recorded below" : "Unavailable"]]), el("pre", {}, JSON.stringify(artifact, null, 2)));
  document.body.append(drawer);
}
async function backupDetail(profileId, sourceId, runId) {
  const b = {profileId, sourceId, runId}, content = layout("Backups");
  content.append(link("← Back to backups", "/backups", "back"), heading(runId, `${profileId} / ${sourceId}`));
  try {
    const detail = await api(backupApi(b));
    const tabs = el("div", {class: "tabs", role: "tablist", "aria-label": "Backup details"});
    const body = el("div", {role: "tabpanel"});
    content.append(el("div", {class: "banner"}, "Manifest verified against the publication marker. Artifact hashes are SHA-256 from manifest."), tabs, body);
    const names = ["Summary", "Artifacts", "Metadata", "Recovery order", "Restore verification"];
    const buttons = names.map((name, index) => {const btn = button(name, () => tab(index), "tab"); btn.setAttribute("role", "tab"); tabs.append(btn); return btn;});
    async function tab(index) {
      buttons.forEach((btn, i) => btn.setAttribute("aria-selected", i === index)); body.replaceChildren();
      if (index === 0) {
        body.append(el("div", {class: "detail-grid"}, el("section", {class: "panel"}, el("h2", {}, "Copy summary"), kv([["Run ID", detail.runId], ["Profile", detail.profileId], ["Source", detail.sourceId], ["Kind", detail.kind], ["PostgreSQL major", detail.serverMajor], ["Worker image", detail.workerImage], ["Files", detail.fileCount], ["Total size", `${detail.bytes} bytes · ${size(detail.bytes)}`]])),
          el("section", {class: "panel"}, el("h2", {}, "Timeline & execution"), kv([["Started", date(detail.startedAt)], ["Completed", date(detail.completedAt)], ["Duration", duration(detail.durationSeconds)], ["Snapshot time", date(detail.snapshotAt)], ["Snapshot basis", detail.snapshotTimeBasis], ["Publication", badge(detail.artifactStatus)], ["Trigger", detail.trigger], ["Job", detail.job ? link(detail.job.name || detail.job.uid, jobPath(detail.job)) : "Unavailable"]]))));
        if (detail.databaseSnapshots?.length) body.append(el("section", {class: "panel"}, el("h2", {}, "Per-database snapshot times"), table(["Database", "Snapshot time", "Estimation basis"], detail.databaseSnapshots.map(d => [d.database, date(d.snapshotAt), d.snapshotTimeBasis]))));
      }
      if (index === 1) body.append(el("section", {class: "panel table-panel"}, table(["File / Purpose", "Format", "Database / Scope", "Size in bytes", "SHA-256 from manifest"], detail.artifacts.map(a => [el("div", {}, button(a.filename, () => inspectArtifact(a), "link"), el("span", {class: "secondary mono"}, `${a.fileId} · ${a.purpose || "Unavailable"}`)), a.format, el("div", {}, text(a.database), el("span", {class: "secondary"}, text(a.scope))), a.size, el("span", {class: "mono"}, a.sha256)]))), el("p", {class: "safe-note"}, "Open a file to inspect its recorded compression, transfer, selection, archive inventory and prerequisites when available."));
      if (index === 2) {
        const source = el("select", {"aria-label": "Metadata record"}, ["manifest", "complete", "verificationRecords"].map(key => el("option", {value: key}, key === "verificationRecords" ? "Verification records" : `${key}.json`)));
        const search = el("input", {class: "metadata-search", type: "search", placeholder: "Search JSON keys…", "aria-label": "Search JSON keys"});
        const output = el("pre");
        function searchKeys(value, needle) {if (Array.isArray(value)) return value.map(v => searchKeys(v, needle)).filter(v => v !== undefined); if (value && typeof value === "object") {const pairs = Object.entries(value).flatMap(([key, item]) => {if (key.toLowerCase().includes(needle)) return [[key, item]]; const found = searchKeys(item, needle); return found && Object.keys(found).length ? [[key, found]] : [];}); return Object.fromEntries(pairs);} return undefined;}
        function update() {const value = detail[source.value]; output.textContent = JSON.stringify(search.value ? searchKeys(value, search.value.toLowerCase()) : value, null, 2) || "Unavailable";}
        source.addEventListener("change", update); search.addEventListener("input", update); update();
        body.append(el("section", {class: "panel"}, el("div", {class: "toolbar"}, source, search), output, el("p", {class: "safe-note"}, detail.redactions)));
      }
      if (index === 3) {
        const note = el("div", {"aria-live": "polite"});
        const language = el("select", {"aria-label": "Recovery note language"}, el("option", {value: "en"}, "English · stored original"), el("option", {value: "ru"}, "Русский · сохранённый перевод"));
        body.append(el("section", {class: "panel"}, el("div", {class: "split-title"}, el("h2", {}, "Recovery order"), language), el("p", {class: "subtitle"}, "Instructions published with this copy. Open artifact details alongside the note."), el("div", {class: "toolbar"}, detail.artifacts.map(a => button(a.filename, () => inspectArtifact(a)))), note));
        async function loadNote() {note.replaceChildren(el("p", {class: "subtitle"}, "Verifying stored note…")); const selected = language.value; try {const result = await api(`${backupApi(b)}/recovery/${selected}`); if (language.value !== selected) return; note.replaceChildren(el("p", {class: "safe-note mono"}, `${result.filename} · SHA-256 ${result.sha256}`), el("pre", {class: "recovery-note"}, result.text));} catch (e) {note.replaceChildren(errorBox(e));}}
        language.addEventListener("change", loadNote); await loadNote();
      }
      if (index === 4) {const v = detail.restoreVerification || {}; body.append(el("section", {class: "panel"}, el("h2", {}, "Restore verification"), kv([["Publication", badge(detail.artifactStatus)], ["Verification", badge(v.status)], ["Required", text(v.required)], ["Due time", date(v.dueAt)], ["Reason", v.reasonCode], ["Active attempt", v.activeAttempt?.verificationId], ["Most recent completed attempt", v.latestAttempt?.verificationId], ["Completed at", date(v.latestAttempt?.finishedAt)]]), el("pre", {}, JSON.stringify(v, null, 2))));}
    }
    await tab(0);
  } catch (e) {content.append(errorBox(e));}
}
async function jobDetail(namespace, uid) {
  const content = layout("Jobs"), area = el("div", {"aria-live": "polite"}), mine = generation;
  content.append(link("← Back to jobs", "/jobs", "back"), heading("Job details", `${namespace} / ${uid}`), area);
  async function update() {
    try {
      const j = await api(`/jobs/${encodeURIComponent(namespace)}/${encodeURIComponent(uid)}`); if (generation !== mine) return;
      area.replaceChildren(freshness(j), el("div", {class: "detail-grid"}, el("section", {class: "panel"}, el("h2", {}, "Kubernetes execution"), kv([["Name", j.name], ["Namespace", j.namespace], ["UID", j.uid], ["Profile", j.profileId], ["Source", j.sourceId], ["Operation", j.operation], ["Trigger", j.trigger], ["Status", badge(j.status)], ["Created", date(j.createdAt)], ["Started", date(j.startedAt)], ["Duration", duration(j.durationSeconds)], ["Last successful read", date(j.lastSuccessfulRead || j.refreshTime)]])),
        el("section", {class: "panel"}, el("h2", {}, "Worker & publication"), j.worker ? kv([["Phase", badge(j.worker.phase)], ["Recorded at", date(j.worker.at)], ["Reason code", j.worker.reasonCode], ["Failed phase", j.worker.failedPhase], ["Underlying cause", j.worker.reasonCode === "TOOL_FAILED" ? "Unknown" : j.worker.reasonCode === "ACCESS_DENIED" ? "Worker PostgreSQL access denied" : "Unavailable"]]) : el("p", {class: "subtitle"}, "No worker event recorded yet. Kubernetes status is shown independently."),
          j.worker?.reasonCode === "ACCESS_DENIED" ? el("p", {class: "failure-help"}, "Check the configured maintenance database and backup role permissions. ", el("a", {href: "https://github.com/v-em/pgbk/blob/main/docs/en/operations.md#globals-export-failures", target: "_blank", rel: "noopener noreferrer"}, "Globals export prerequisites →")) : null,
          j.backup ? link("Open backup →", backupPath(j.backup)) : el("p", {class: "safe-note"}, "No validated published copy is correlated with this Job UID."))));
      if (["Pending", "Running", "Suspended"].includes(j.status)) timer = setTimeout(update, 5000);
    } catch (e) {if (generation === mine) {area.prepend(errorBox(e)); timer = setTimeout(update, 10000);}}
  }
  await update();
}
function navigate(path) {history.pushState({}, "", path); route();}
async function route() {
  generation++; clearTimeout(timer); document.querySelector(".drawer")?.remove(); if (!session) return login();
  const parts = location.pathname.split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "backups" && parts.length === 4) return backupDetail(...parts.slice(1));
  if (parts[0] === "jobs" && parts.length === 3) return jobDetail(...parts.slice(1));
  if (parts[0] === "schedules") return schedules();
  return listing(parts[0] === "jobs" ? "jobs" : "backups");
}
async function start() {
  try {session = await api("/session"); profiles = []; let cursor = null; do {const page = await api("/profiles?limit=100" + (cursor ? "&cursor=" + encodeURIComponent(cursor) : "")); profiles.push(...page.items); cursor = page.nextCursor;} while (cursor); await route();}
  catch (error) {login(error.code === "SESSION_REQUIRED" ? null : error);}
}
window.addEventListener("popstate", route);
start();
