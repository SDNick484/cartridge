// Entry point: wires the modules together and keeps them current from the SSE stream.

import { events, getJSON } from "./api.js";
import { registerSheet } from "./dom.js";
import { initDevices, setDevices, updateDevice } from "./devices.js";
import { initFocus } from "./focus.js";
import { initGrid, loadChips, reload, setSynced } from "./grid.js";
import { initSheet, openGame } from "./sheet.js";

const $ = (id) => document.getElementById(id);

async function loadConfig() {
  try {
    return await getJSON("/api/config");
  } catch {
    return { mode: "lan", synced: true };
  }
}

async function refreshDevices() {
  try {
    setDevices(await getJSON("/api/devices"));
  } catch {
    // The stream's next hello replaces the list anyway.
  }
}

async function refreshSynced() {
  const config = await loadConfig();
  setSynced(Boolean(config.synced));
}

async function main() {
  const config = await loadConfig();
  const isRemote = config.mode === "remote";
  document.body.classList.toggle("remote", isRemote);

  initFocus($("tv-toggle"));
  for (const dialog of document.querySelectorAll("dialog")) {
    registerSheet(dialog);
    for (const button of dialog.querySelectorAll("[data-close]")) button.addEventListener("click", () => dialog.close());
  }

  initDevices({ pillButton: $("device-pill"), deviceSheet: $("device-sheet"), deviceList: $("device-list"), isRemote });
  await refreshDevices();

  initSheet(
    {
      dialog: $("game-sheet"),
      art: $("sheet-art"),
      title: $("sheet-title"),
      system: $("sheet-system"),
      devices: $("sheet-devices"),
      star: $("star"),
      play: $("play"),
      reason: $("play-reason"),
      remoteNote: $("remote-note"),
      error: $("sheet-error"),
    },
    { isRemote },
  );
  initGrid(
    {
      grid: $("grid"),
      chips: $("chips"),
      sentinel: $("sentinel"),
      empty: $("empty"),
      onlyWrap: $("only-wrap"),
      onlyBox: $("only-device"),
      onlyText: $("only-text"),
      search: $("search"),
    },
    { isRemote, hasSynced: Boolean(config.synced), open: openGame },
  );
  await loadChips();
  reload();

  let wasDown = false;
  events(
    (type, data) => {
      if (type === "hello") {
        setDevices(data);
        if (wasDown) refreshSynced().then(reload);
        wasDown = false;
      } else if (type === "device") {
        updateDevice(data);
      } else {
        // sync.started / sync.finished: the device card's sync state changed; a finished run may add games.
        refreshDevices();
        if (type === "sync.finished") refreshSynced().then(reload);
      }
    },
    (up) => {
      $("conn").hidden = up;
      if (!up) wasDown = true;
    },
  );
}

main();
