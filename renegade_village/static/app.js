"use strict";

const $ = (s) => document.querySelector(s);
const state = {
  channels: [], agents: [], running: false, user: "you", goal: "",
  current: localGet("rv.channel") || "general",
  unread: new Set(), filesPath: ".", oldest: null, loadingOlder: false,
};

function localGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem(k, v); } catch {} }

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const initials = (name) => (name.match(/[A-Za-z0-9]+/g) || ["?"]).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
const agentBy = (name) => state.agents.find((a) => a.name === name);
const fileUrl = (p) => `/api/file?path=${encodeURIComponent(p)}`;
const isImage = (p) => /\.(png|jpe?g|gif|webp|svg)$/i.test(p);
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const fmtDay = (ts) => {
  const d = new Date(ts * 1000), today = new Date();
  return d.toDateString() === today.toDateString() ? `Today at ${fmtTime(ts)}` : `${d.toLocaleDateString()} ${fmtTime(ts)}`;
};

function renderMarkdown(text) {
  let html;
  if (window.marked && window.DOMPurify) {
    html = DOMPurify.sanitize(marked.parse(text, { breaks: true, gfm: true }));
  } else {
    html = esc(text).replace(/\n/g, "<br>");
  }
  // highlight @mentions of known names (outside of tags)
  const names = [...state.agents.map((a) => a.name), state.user];
  for (const n of names) {
    const re = new RegExp(`(^|[^\\w@>])@(${n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})\\b(?![^<]*>)`, "gi");
    html = html.replace(re, `$1<span class="mention">@$2</span>`);
  }
  return html;
}

function avatarEl(name, kind, size) {
  const d = document.createElement("div");
  d.className = "avatar" + (kind === "human" ? " human" : "");
  const a = agentBy(name);
  if (a) d.style.background = a.color;
  if (kind === "system") d.style.background = "#4e5058";
  d.textContent = kind === "system" ? "RV" : initials(name);
  if (size) { d.style.width = d.style.height = size; }
  return d;
}

/* ---------- channels ---------- */
function renderChannels() {
  const ul = $("#channels");
  ul.innerHTML = "";
  const active = new Set(state.agents.filter((a) => a.status !== "idle" && a.channel).map((a) => a.channel));
  for (const c of state.channels) {
    const li = document.createElement("li");
    li.className = (c.name === state.current ? "active " : "") + (state.unread.has(c.name) ? "unread" : "");
    li.innerHTML = `<span class="hash">#</span><span>${esc(c.name)}</span>`;
    if (active.has(c.name)) li.insertAdjacentHTML("beforeend", `<span class="activity" title="someone is typing"></span>`);
    li.onclick = () => { selectChannel(c.name); $(".sidebar").classList.remove("open"); };
    ul.appendChild(li);
  }
}

async function selectChannel(name) {
  if (!state.channels.find((c) => c.name === name)) name = "general";
  state.current = name;
  localSet("rv.channel", name);
  state.unread.delete(name);
  const ch = state.channels.find((c) => c.name === name) || { topic: "" };
  $("#chName").textContent = name;
  $("#chTopic").textContent = ch.topic || "";
  $("#input").placeholder = `Message #${name}`;
  renderChannels();
  renderTyping();
  const msgs = await api(`/api/channels/${name}/messages?limit=60`);
  const box = $("#messages");
  box.innerHTML = "";
  box.appendChild(welcomeEl(name, ch.topic, msgs.length < 60));
  state.oldest = msgs.length ? msgs[0].id : null;
  let prev = null;
  for (const m of msgs) { box.appendChild(messageEl(m, prev)); prev = m; }
  box.scrollTop = box.scrollHeight;
}

function welcomeEl(name, topic, atStart) {
  const d = document.createElement("div");
  d.className = "welcome";
  d.id = "welcome";
  if (atStart) {
    d.innerHTML = `<div class="big-hash">#</div><h2>Welcome to #${esc(name)}!</h2><p>${esc(topic || "This is the start of the channel.")}</p>`;
  }
  return d;
}

async function loadOlder() {
  const box = $("#messages");
  if (state.loadingOlder || !state.oldest || box.scrollTop > 40) return;
  state.loadingOlder = true;
  const msgs = await api(`/api/channels/${state.current}/messages?limit=60&before=${state.oldest}`);
  const h = box.scrollHeight;
  const first = $("#welcome").nextSibling;
  let prev = null;
  const frag = document.createDocumentFragment();
  for (const m of msgs) { frag.appendChild(messageEl(m, prev)); prev = m; }
  box.insertBefore(frag, first);
  if (msgs.length < 60) $("#welcome").replaceWith(welcomeEl(state.current, (state.channels.find((c) => c.name === state.current) || {}).topic, true));
  state.oldest = msgs.length ? msgs[0].id : null;
  box.scrollTop = box.scrollHeight - h;
  state.loadingOlder = false;
}

/* ---------- messages ---------- */
function messageEl(m, prev) {
  const grouped = prev && prev.author === m.author && m.created_at - prev.created_at < 300 && m.author_kind !== "system";
  const el = document.createElement("div");
  el.className = `msg ${m.author_kind}` + (grouped ? "" : " head");
  el.dataset.id = m.id;
  el.dataset.author = m.author;
  el.dataset.ts = m.created_at;
  const gutter = document.createElement("div");
  gutter.className = "gutter";
  if (grouped) gutter.innerHTML = `<span class="time">${fmtTime(m.created_at)}</span>`;
  else gutter.appendChild(avatarEl(m.author, m.author_kind));
  const body = document.createElement("div");
  body.className = "body";
  if (!grouped) {
    const a = agentBy(m.author);
    const color = a ? a.color : "var(--text-strong)";
    const tag = a ? `<span class="model" title="${esc(a.model)}">${esc(a.model.split(":")[0])}</span>` : "";
    body.innerHTML = `<div class="meta"><span class="author" style="color:${color}">${esc(m.author_kind === "system" ? "Village" : m.author)}</span>${tag}<span class="ts">${fmtDay(m.created_at)}</span></div>`;
  }
  const content = document.createElement("div");
  content.className = "content";
  content.innerHTML = renderMarkdown(m.content);
  body.appendChild(content);
  if (m.attachments && m.attachments.length) {
    const att = document.createElement("div");
    att.className = "attachments";
    for (const p of m.attachments) att.appendChild(attachmentEl(p));
    body.appendChild(att);
  }
  el.append(gutter, body);
  return el;
}

function attachmentEl(p) {
  if (isImage(p)) {
    const img = document.createElement("img");
    img.src = fileUrl(p) + `&t=${Date.now()}`;
    img.alt = p;
    img.onclick = () => openFile(p);
    return img;
  }
  const card = document.createElement("div");
  card.className = "file-card";
  const ext = (p.split(".").pop() || "file").slice(0, 4).toUpperCase();
  card.innerHTML = `<span class="ext">${esc(ext)}</span><span class="name">${esc(p)}</span>`;
  card.onclick = () => openFile(p);
  return card;
}

function appendMessage(m) {
  if (m.channel !== state.current) {
    state.unread.add(m.channel);
    renderChannels();
    return;
  }
  const box = $("#messages");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  const last = box.lastElementChild;
  const prev = last && last.dataset.author ? { author: last.dataset.author, created_at: +last.dataset.ts, author_kind: "" } : null;
  box.appendChild(messageEl(m, prev));
  if (nearBottom || m.author_kind === "human") box.scrollTop = box.scrollHeight;
}

/* ---------- villagers ---------- */
function renderMembers() {
  $("#memberCount").textContent = `VILLAGERS — ${state.agents.length}`;
  const ul = $("#members");
  ul.innerHTML = "";
  for (const a of state.agents) {
    const li = document.createElement("li");
    const av = avatarEl(a.name, "agent");
    const dotCls = a.status === "idle" ? "" : a.status.includes("python") ? "running" : "thinking";
    av.insertAdjacentHTML("beforeend", `<span class="dot ${dotCls}"></span>`);
    const sub = a.status === "idle" ? a.model : `${a.status}${a.channel ? " in #" + a.channel : ""}…`;
    li.innerHTML = `<div class="m-text"><div class="m-name" style="color:${a.color}">${esc(a.name)}</div><div class="m-sub" title="${esc(a.persona || a.model)}">${esc(sub)}</div></div>`;
    li.prepend(av);
    ul.appendChild(li);
  }
  const me = { name: state.user };
  $("#meAvatar").textContent = initials(me.name);
  $("#meName").textContent = me.name;
}

function renderTyping() {
  const busy = state.agents.filter((a) => a.status !== "idle" && a.channel === state.current);
  const t = $("#typing");
  if (!busy.length) { t.innerHTML = ""; return; }
  const names = busy.map((a) => `<b>${esc(a.name)}</b>`).join(", ");
  const verb = busy.some((a) => a.status.includes("python")) ? "running Python" : "typing";
  t.innerHTML = `${names} ${busy.length > 1 ? "are" : "is"} ${verb}…`;
}

function renderVillageState() {
  const b = $("#toggleBtn");
  b.textContent = state.running ? "Pause" : "Start";
  b.classList.toggle("running", state.running);
  $("#villageState").textContent = state.running ? "village running" : "paused · @mention to ask one";
}

/* ---------- shared folder ---------- */
async function loadFiles(fresh = []) {
  let items;
  try { items = await api(`/api/files?path=${encodeURIComponent(state.filesPath)}`); }
  catch { state.filesPath = "."; items = await api(`/api/files?path=.`); }
  $("#filesPath").textContent = state.filesPath === "." ? "SHARED FOLDER" : state.filesPath.toUpperCase();
  $("#filesUp").hidden = state.filesPath === ".";
  const ul = $("#files");
  ul.innerHTML = items.length ? "" : `<li class="muted">Empty. Villagers' files will appear here.</li>`;
  for (const f of items) {
    const li = document.createElement("li");
    if (fresh.some((p) => p === f.path || p.startsWith(f.path + "/"))) li.classList.add("fresh");
    const size = f.is_dir ? "" : f.size < 1024 ? `${f.size} B` : `${(f.size / 1024).toFixed(1)} KB`;
    li.innerHTML = `<span class="kind">${f.is_dir ? "▸" : "·"}</span><span>${esc(f.name)}</span><span class="size">${size}</span>`;
    li.onclick = () => f.is_dir ? (state.filesPath = f.path, loadFiles()) : openFile(f.path);
    ul.appendChild(li);
  }
}

async function openFile(p) {
  $("#viewerTitle").textContent = p;
  const body = $("#viewerBody");
  if (isImage(p)) {
    body.innerHTML = `<img src="${fileUrl(p)}&t=${Date.now()}" alt="${esc(p)}">`;
  } else {
    const r = await fetch(fileUrl(p));
    const text = await r.text();
    body.innerHTML = /\.md$/i.test(p)
      ? `<div class="content">${renderMarkdown(text)}</div>`
      : `<div class="content"><pre><code>${esc(text)}</code></pre></div>`;
  }
  $("#viewer").showModal();
}

/* ---------- live events ---------- */
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (e) => handle(JSON.parse(e.data));
  ws.onclose = () => setTimeout(connect, 1500);
}

function applySnapshot(s) {
  state.channels = s.channels;
  state.agents = s.agents.map((a) => ({ ...a, channel: null }));
  state.running = s.running;
  state.user = s.user_name;
  state.goal = s.goal;
  $("#folderPath").textContent = s.shared_folder;
  renderMembers(); renderVillageState();
}

function handle(ev) {
  switch (ev.type) {
    case "hello":
      applySnapshot(ev.state);
      selectChannel(state.current);
      loadFiles();
      break;
    case "message":
      appendMessage(ev.message);
      break;
    case "channels":
      state.channels = ev.channels;
      renderChannels();
      break;
    case "status": {
      const a = agentBy(ev.agent);
      if (a) { a.status = ev.status; if (ev.channel || ev.status === "idle") a.channel = ev.channel; }
      renderMembers(); renderTyping(); renderChannels();
      break;
    }
    case "village":
      state.running = ev.running;
      renderVillageState();
      break;
    case "files":
      loadFiles(ev.paths || []);
      break;
    case "error":
      toast(`${ev.agent}: ${ev.error}`);
      break;
  }
}

function toast(text) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = text;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), 8000);
}

/* ---------- wiring ---------- */
$("#composer").addEventListener("submit", (e) => e.preventDefault());
$("#input").addEventListener("keydown", async (e) => {
  if (e.key !== "Enter" || e.shiftKey) return;
  e.preventDefault();
  const text = e.target.value.trim();
  if (!text) return;
  e.target.value = "";
  autosize();
  try { await api(`/api/channels/${state.current}/messages`, { method: "POST", body: JSON.stringify({ content: text }) }); }
  catch (err) { toast(err.message); e.target.value = text; }
});
function autosize() { const i = $("#input"); i.style.height = "auto"; i.style.height = i.scrollHeight + "px"; }
$("#input").addEventListener("input", autosize);
$("#messages").addEventListener("scroll", loadOlder);

$("#toggleBtn").onclick = () => api(`/api/village/${state.running ? "pause" : "start"}`, { method: "POST" }).catch((e) => toast(e.message));

document.querySelectorAll(".tab").forEach((b) => b.onclick = () => {
  const panel = $(".panel");
  const same = b.classList.contains("active");
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
  $("#membersPanel").hidden = b.dataset.panel !== "members";
  $("#filesPanel").hidden = b.dataset.panel !== "files";
  panel.classList.toggle("open", !(same && panel.classList.contains("open")));
  if (b.dataset.panel === "files") loadFiles();
});
$("#filesUp").onclick = () => {
  const parts = state.filesPath.split("/"); parts.pop();
  state.filesPath = parts.join("/") || "."; loadFiles();
};
$("#menuBtn").onclick = () => $(".sidebar").classList.toggle("open");

document.querySelectorAll("[data-close]").forEach((b) => b.onclick = () => b.closest("dialog").close());
$("#goalBtn").onclick = () => { $("#goalInput").value = state.goal; $("#goalDialog").showModal(); };
$("#goalForm").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value !== "save") return;
  const r = await api("/api/village/goal", { method: "PUT", body: JSON.stringify({ goal: $("#goalInput").value }) });
  state.goal = r.goal;
});
$("#newChannelBtn").onclick = () => { $("#channelName").value = ""; $("#channelTopic").value = ""; $("#channelDialog").showModal(); };
$("#channelForm").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value !== "save") return;
  try {
    const r = await api("/api/channels", { method: "POST", body: JSON.stringify({ name: $("#channelName").value, topic: $("#channelTopic").value }) });
    if (!state.channels.find((c) => c.name === r.name)) state.channels.push({ name: r.name, topic: $("#channelTopic").value });
    selectChannel(r.name);
  } catch (err) { toast(err.message); }
});

connect();
