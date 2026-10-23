// Sandboxed preload (must be CommonJS): exposes only the service config to the page.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("sensei", {
  config: () => ipcRenderer.invoke("sensei:config")
});
