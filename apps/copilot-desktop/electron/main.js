import { app, BrowserWindow, ipcMain, net, protocol, session } from "electron";
import path from "path";
import { fileURLToPath, pathToFileURL } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DIST = path.join(__dirname, "..", "dist");

// `--dev` loads the Vite dev server. Otherwise the built UI is served from dist/ over sensei://,
// a privileged scheme, instead of file:// (whose pages send `Origin: null` to the service).
const DEV = process.argv.includes("--dev");
const UI_URL = process.env.SENSEI_UI_URL ?? (DEV ? "http://127.0.0.1:5180" : "sensei://app/index.html");

// Where the SENSEI service listens; the page connects to <SERVICE_URL>/ws/ui.
const SERVICE_URL = serviceOrigin(process.env.SENSEI_SERVICE_URL ?? "ws://127.0.0.1:8000");

// The page's Content-Security-Policy, set as a response header so it follows SERVICE_URL:
// scripts only from the app, WebSockets only to the service, no eval.
const CSP = `default-src 'self'; connect-src 'self' ${SERVICE_URL}; style-src 'self' 'unsafe-inline'`;

/** @param {string} value */
function serviceOrigin(value) {
  const url = new URL(value);
  if (url.protocol !== "ws:" && url.protocol !== "wss:") {
    throw new Error(`SENSEI_SERVICE_URL must be a ws:// or wss:// URL, got ${value}`);
  }
  return url.origin;
}

protocol.registerSchemesAsPrivileged([
  { scheme: "sensei", privileges: { standard: true, secure: true, supportFetchAPI: true } }
]);

function serveDist() {
  protocol.handle("sensei", async (request) => {
    const file = path.normalize(path.join(DIST, decodeURIComponent(new URL(request.url).pathname)));
    if (!file.startsWith(DIST + path.sep)) return new Response("Not found", { status: 404 });
    const response = await net.fetch(pathToFileURL(file).toString());
    const headers = new Headers(response.headers);
    headers.set("Content-Security-Policy", CSP);
    return new Response(response.body, { status: response.status, headers });
  });
}

// An http(s) UI (the Vite dev server) gets the same policy; sensei:// responses get it in serveDist().
function applyCspToDevServer() {
  const ui = new URL(UI_URL);
  if (ui.protocol !== "http:" && ui.protocol !== "https:") return;
  session.defaultSession.webRequest.onHeadersReceived({ urls: [`${ui.protocol}//${ui.host}/*`] }, (details, callback) => {
    callback({ responseHeaders: { ...details.responseHeaders, "Content-Security-Policy": [CSP] } });
  });
}

/** @param {string} url */
function isUiUrl(url) {
  // compare scheme + host: URL.origin is "null" for every custom scheme
  const target = new URL(url);
  const ui = new URL(UI_URL);
  return target.protocol === ui.protocol && target.host === ui.host;
}

function createWindow() {
  const win = new BrowserWindow({
    width: 980,
    height: 720,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  });

  // The window only ever shows the SENSEI UI: no popups, no navigating elsewhere.
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("will-navigate", (event, url) => {
    if (!isUiUrl(url)) event.preventDefault();
  });

  load(win);
}

// The Vite dev server may still be starting; retry until it answers.
/** @param {BrowserWindow} win */
function load(win) {
  win.loadURL(UI_URL).catch(() => {
    if (!win.isDestroyed()) setTimeout(() => load(win), 500);
  });
}

// The page gets the service address and the optional token over IPC, so neither is baked into the
// bundle and the token never appears in a URL.
ipcMain.handle("sensei:config", (event) => {
  // senderFrame is null if the frame navigated away or was destroyed meanwhile
  if (!event.senderFrame || !isUiUrl(event.senderFrame.url)) throw new Error("sensei:config is only available to the SENSEI UI");
  return { token: process.env.SENSEI_TOKEN || null, serviceUrl: SERVICE_URL };
});

app.whenReady().then(() => {
  serveDist();
  applyCspToDevServer();
  createWindow();
});
