// RenegadeVillage desktop shell: starts the Python server (unless one is already
// running) and shows the chat in its own window instead of a browser tab.
const { app, BrowserWindow, dialog, shell } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const CONFIG = process.env.RV_CONFIG || path.join(ROOT, "config.json");
const PYTHON = process.env.RV_PYTHON || (process.platform === "win32" ? "python" : "python3");

let server = null; // child process we started (null if we reused a running server)
let serverLog = "";
let win = null;

function readPort() {
  const cfg = JSON.parse(fs.readFileSync(CONFIG, "utf8"));
  return cfg.port || 8642;
}

async function isUp(url) {
  try {
    const r = await fetch(`${url}/api/state`, { signal: AbortSignal.timeout(1500) });
    return r.ok;
  } catch {
    return false;
  }
}

function startServer() {
  server = spawn(PYTHON, [path.join(ROOT, "run.py"), "--no-browser", "--config", CONFIG], {
    cwd: ROOT,
    windowsHide: true,
    env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" },
  });
  const keep = (d) => { serverLog = (serverLog + d.toString()).slice(-4000); };
  server.stdout.on("data", keep);
  server.stderr.on("data", keep);
  server.on("error", (e) => { serverLog += `\n${e.message}`; });
}

async function waitForServer(url, timeoutMs = 30000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (await isUp(url)) return true;
    if (server && server.exitCode !== null) return false;
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

const LOADING = `data:text/html;charset=utf-8,${encodeURIComponent(`<!doctype html>
<html><body style="margin:0;height:100vh;display:grid;place-items:center;background:#313338;color:#949ba4;
font-family:'Segoe UI',system-ui,sans-serif"><div>Waking up the village…</div></body></html>`)}`;

function fail(title, detail) {
  dialog.showErrorBox(title, detail);
  app.quit();
}

async function main() {
  if (!fs.existsSync(CONFIG)) {
    return fail("RenegadeVillage isn't set up yet",
      `No config found at:\n${CONFIG}\n\nRun this once in the RenegadeVillage folder, then open the app again:\n\n  python install.py`);
  }
  const url = `http://127.0.0.1:${readPort()}`;

  win = new BrowserWindow({
    width: 1280, height: 820, minWidth: 420, minHeight: 480,
    title: "RenegadeVillage", backgroundColor: "#313338", autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, sandbox: true },
  });
  win.loadURL(LOADING);

  // Links in messages open in the real browser; the window only ever shows the village.
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    if (/^https?:/.test(target)) shell.openExternal(target);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (e, target) => {
    if (!target.startsWith(url)) { e.preventDefault(); shell.openExternal(target); }
  });

  if (!(await isUp(url))) {
    startServer();
    if (!(await waitForServer(url))) {
      return fail("Couldn't start the village",
        `The Python server didn't come up on ${url}.\n\nPython command: ${PYTHON} (set RV_PYTHON to change it)\n\n${serverLog.trim() || "(no output)"}`);
    }
  }
  win.loadURL(url);
  win.on("closed", () => { win = null; });
}

function stopServer() {
  if (server && server.exitCode === null) server.kill();
  server = null;
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (win) { if (win.isMinimized()) win.restore(); win.focus(); }
  });
  app.whenReady().then(main);
  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", stopServer);
  process.on("exit", stopServer);
}
