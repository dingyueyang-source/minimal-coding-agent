"use strict";
// All server/user/model text is rendered via textContent / DOM text nodes only.

const PHASES = [
  ["sandbox_starting", "Sandbox startup"],
  ["agent_running", "Agent execution"],
  ["retrieving", "Result retrieval"],
  ["cleaning", "Sandbox cleanup"],
  ["complete", "Complete"],
];
const TERMINAL = new Set(["succeeded", "failed", "timeout"]);
const POLL_MS = 500;
const FETCH_TIMEOUT_MS = 5000;
const MAX_RETRIES = 3;
const REQUEST_LIMIT = 8192;
const STORE_KEY = "mca-task-id";

const $ = (id) => document.getElementById(id);
let taskId = null;
let lastRevision = -1;
let failures = 0;
let pollTimer = null;
let polling = false;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function store(id) {
  try { id ? sessionStorage.setItem(STORE_KEY, id) : sessionStorage.removeItem(STORE_KEY); } catch (_) { /* optional */ }
}

function stored() {
  try { return sessionStorage.getItem(STORE_KEY); } catch (_) { return null; }
}

function notice(text, kind) {
  const box = $("notice");
  box.textContent = text || "";
  box.className = "notice " + (kind || "");
  box.hidden = !text;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0");
}

async function fetchWithTimeout(url, options) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    return await fetch(url, { ...options, signal: ctrl.signal, cache: "no-store" });
  } finally {
    clearTimeout(timer);
  }
}

// ---------- input ----------
function checkInput() {
  const value = $("request").value;
  const bytes = new TextEncoder().encode(value).length;
  let hint = "";
  if (!value.trim()) hint = "Enter a task first.";
  else if (bytes > REQUEST_LIMIT) hint = `Task is ${bytes} bytes; the limit is ${REQUEST_LIMIT} UTF-8 bytes.`;
  $("input-hint").textContent = hint;
  $("input-hint").className = hint ? "warn-text" : "muted";
  return !hint;
}

async function run() {
  if (!checkInput() || polling) return;
  $("run").disabled = true;
  notice("Submitting…");
  let response;
  try {
    response = await fetchWithTimeout("/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request: $("request").value }),
    });
  } catch (_) {
    // Never resubmit automatically: the server may or may not have accepted it.
    notice("Submission status unknown: the request was sent but no response arrived. It was not retried automatically.", "error");
    $("run").disabled = false;
    return;
  }
  let body = null;
  try { body = await response.json(); } catch (_) { /* handled below */ }
  if (response.status !== 202 || !body || !body.task_id) {
    const err = body && body.error ? `${body.error.code}: ${body.error.message}` : `HTTP ${response.status}`;
    const hint = { 429: " All sandbox slots are busy; try again shortly.", 503: " The backend is not ready (startup checks or cleanup pending)." }[response.status] || "";
    notice(`Task was not accepted — ${err}.${hint}`, "error");
    $("run").disabled = false;
    return;
  }
  notice("");
  startTracking(body.task_id);
}

// ---------- polling ----------
function startTracking(id) {
  taskId = id;
  lastRevision = -1;
  failures = 0;
  store(id);
  $("task").hidden = false;
  $("resume").hidden = true;
  $("run").disabled = true;
  resetView(id);
  schedule(0);
}

function schedule(delay) {
  polling = true;
  clearTimeout(pollTimer);
  pollTimer = setTimeout(poll, delay);
}

function stopPolling() {
  polling = false;
  clearTimeout(pollTimer);
}

async function poll() {
  const id = taskId;
  let response;
  try {
    response = await fetchWithTimeout(`/tasks/${encodeURIComponent(id)}`, { method: "GET" });
  } catch (_) {
    return pollFailed("network error");
  }
  if (id !== taskId) return;
  if (response.status === 404) {
    stopPolling();
    store(null);
    notice("Task not found: it does not exist, has expired, or the backend restarted.", "error");
    $("run").disabled = false;
    return;
  }
  if (!response.ok) return pollFailed(`HTTP ${response.status}`);
  let snap;
  try { snap = await response.json(); } catch (_) { return pollFailed("invalid response"); }
  failures = 0;
  notice("");
  if (typeof snap.revision === "number" && snap.revision >= lastRevision) {
    lastRevision = snap.revision;
    render(snap);
  }
  if (TERMINAL.has(snap.status)) {
    stopPolling();
    $("run").disabled = false;
    health(); // slot count changes when a task finishes; refresh now instead of on the next tick
    if (snap.cleanup && snap.cleanup.status === "failed") {
      $("resume").textContent = "Refresh task (check background cleanup)";
      $("resume").hidden = false;
    }
  } else {
    schedule(POLL_MS);
  }
}

function pollFailed(reason) {
  failures += 1;
  if (failures > MAX_RETRIES) {
    stopPolling();
    notice(`Connection lost (${reason}). The task may still be running on the server. Polling paused.`, "error");
    $("resume").textContent = "Resume checking this task";
    $("resume").hidden = false;
    $("run").disabled = false;
    return;
  }
  notice(`Connection problem (${reason}); retrying ${failures}/${MAX_RETRIES}. The task may still be running.`, "warn");
  schedule(POLL_MS * 2);
}

function resume() {
  if (!taskId) return;
  failures = 0;
  $("resume").hidden = true;
  $("run").disabled = true;
  notice("Checking task…");
  schedule(0); // GET only — never resubmits.
}

// ---------- rendering ----------
function resetView(id) {
  $("task-id").textContent = id;
  for (const key of ["container", "cleanup", "created", "finished"]) $(key).textContent = "—";
  $("status").textContent = "submitted";
  $("status").className = "badge running";
  $("error").hidden = true;
  $("cleanup-error").hidden = true;
  $("partial").hidden = true;
  $("files").replaceChildren(el("p", "muted", "No files read back yet."));
  $("events").replaceChildren();
  $("agent-message").textContent = "—";
  renderPhases({ phase: "sandbox_starting", status: "running", events: [] });
}

function render(s) {
  $("status").textContent = s.status === "running" ? `running · ${s.phase}` : s.status;
  $("status").className = "badge " + s.status;
  $("container").textContent = s.container_id || "not created yet";
  const cleanup = (s.cleanup && s.cleanup.status) || "—";
  $("cleanup").textContent = cleanup;
  $("cleanup").className = "cleanup-" + cleanup;
  $("created").textContent = fmtTime(s.created_at);
  $("finished").textContent = fmtTime(s.finished_at);
  renderPhases(s);

  if (s.error) {
    $("error").hidden = false;
    $("error-code").textContent = s.error.code;
    $("error-message").textContent = s.error.message;
    $("error-phase").textContent = `Failed during: ${s.failed_phase || s.error.phase || "unknown"}`
      + (s.execution_status && s.execution_status !== s.status ? ` · execution: ${s.execution_status}` : "");
  } else {
    $("error").hidden = true;
  }
  if (s.cleanup && s.cleanup.error) {
    $("cleanup-error").hidden = false;
    $("cleanup-error").textContent = `Cleanup problem — ${s.cleanup.error.code}: ${s.cleanup.error.message}`;
  } else {
    $("cleanup-error").hidden = true;
  }

  renderFiles(s);
  $("agent-message").textContent = s.agent_message || "—";
  const events = (s.events || []).map((e) => {
    const li = el("li", /REJECTED|FAILED|TIMEOUT|ERROR|EXCEEDED|INVALID|UNKNOWN/.test(e.code) ? "bad" : "");
    li.append(el("time", null, fmtTime(e.at)), el("code", null, e.code), el("span", null, e.message));
    return li;
  });
  $("events").replaceChildren(...events);
}

function renderPhases(s) {
  const reached = {};
  for (const e of s.events || []) {
    if (e.code === "TASK_ACCEPTED") reached.sandbox_starting = e.at;
    if (e.code === "PHASE_CHANGED") reached[e.message] = reached[e.message] || e.at;
  }
  const current = PHASES.findIndex(([key]) => key === s.phase);
  const failedAt = PHASES.findIndex(([key]) => key === s.failed_phase);
  const items = PHASES.map(([key, label], i) => {
    let state = "pending";
    if (i < current || (s.phase === "complete" && i === current)) state = "done";
    else if (i === current) state = "active";
    if (i === failedAt) state = s.status === "timeout" || s.execution_status === "timeout" ? "timeout" : "failed";
    else if (failedAt >= 0 && i > failedAt && i < 3 && !reached[key]) state = "skipped";
    if (key === "cleaning" && s.cleanup && s.cleanup.status === "failed") state = "failed";
    if (key === "complete" && s.phase === "complete" && s.status !== "succeeded") state = s.status;
    const li = el("li", "phase " + state);
    li.append(el("span", "dot"), el("span", "label", label), el("span", "state", state),
      el("time", null, reached[key] ? fmtTime(reached[key]) : ""));
    return li;
  });
  $("phases").replaceChildren(...items);
}

function renderFiles(s) {
  const files = s.files || [];
  const incomplete = files.length > 0 && s.status !== "succeeded" && s.status !== "running";
  $("partial").hidden = !incomplete && !files.some((f) => f.truncated);
  if (!files.length) {
    const msg = s.status === "running" ? "No files read back yet." : "No files were read back from the sandbox.";
    $("files").replaceChildren(el("p", "muted", msg));
    return;
  }
  $("files").replaceChildren(...files.map((f) => {
    const box = el("article", "file");
    const head = el("header");
    head.append(el("code", "path", f.path), el("span", "muted",
      `${f.size_bytes} bytes · read back from sandbox${f.truncated ? " · TRUNCATED preview" : ""}${f.decode_replaced ? " · invalid UTF-8 replaced" : ""}`));
    const pre = el("pre", "content", f.content);
    if (f.content === "") pre.append(el("span", "muted", "(empty file)"));
    box.append(head, pre);
    return box;
  }));
}

// ---------- health ----------
let healthTimer = null;
async function health() {
  clearTimeout(healthTimer);
  try {
    const r = await fetchWithTimeout("/health", { method: "GET" });
    const h = await r.json();
    const box = $("health");
    if (h.ready) {
      box.textContent = `Backend ready · model: ${h.model} · slots in use ${h.occupied}/${h.capacity}`;
      box.className = "health ok";
    } else if (h.startup_checks === "running") {
      box.textContent = "Backend starting: running Docker and sandbox smoke checks…";
      box.className = "health warn";
    } else {
      box.textContent = `Backend not accepting tasks (${h.startup_error || h.status}). `
        + (h.startup_error ? "Startup checks failed; is Docker running and the sandbox image built?" : "Cleanup recovery pending.");
      box.className = "health bad";
    }
  } catch (_) {
    $("health").textContent = "Backend unreachable.";
    $("health").className = "health bad";
  }
  clearTimeout(healthTimer);
  healthTimer = setTimeout(health, 5000);
}

document.addEventListener("DOMContentLoaded", () => {
  $("run").addEventListener("click", run);
  $("resume").addEventListener("click", resume);
  $("request").addEventListener("input", checkInput);
  $("request").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) run(); });
  health();
  const previous = stored();
  if (previous) startTracking(previous);
});
