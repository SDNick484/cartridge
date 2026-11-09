// Entry point: wires the modules together and keeps them current from the SSE stream.

import { events, getJSON } from "./api.js";
import { registerSheet, toast } from "./dom.js";
import { initDevices, setDevices, updateDevice } from "./devices.js";
import { initFocus } from "./focus.js";
import { initGrid, loadChips, refresh, reload, setSynced } from "./grid.js";
import { initSheet, openGame } from "./sheet.js";

const $ = (id) => document.getElementById(id);

async function loadConfig() {
  try {
    return await getJSON("/api/config");
  } catch {
    // Without a config we can't tell which app this is, so assume the safe one: browse only, no launching.
    toast("Couldn't reach Cartridge, so launching is turned off. Reload to try again.");
    return { mode: "remote", synced: true };
  }
}

async function refreshDevices() {
  try {
    setDevices(await getJSON("/api/devices"));
  } catch {
    // The stream's next hello replaces the list anyway.
  }
}

/** True when the catalog just went from never-synced to synced. */
async function syncedFlipped() {
  try {
    return setSynced(Boolean((await getJSON("/api/config")).synced));
  } catch {
    return false;
  }
}

async function main() {
  const config = await loadConfig();
  const isRemote = config.mode === "remote";

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
    async (type, data) => {
      if (type === "hello") {
        setDevices(data);
        // After a reconnect the grid stays put unless the empty "never synced" state became stale.
        if (wasDown && (await syncedFlipped())) reload();
        wasDown = false;
      } else if (type === "device") {
        updateDevice(data);
      } else {
        // sync.started / sync.finished: the device card's sync state changed.
        refreshDevices();
        if (type !== "sync.finished") return;
        if (await syncedFlipped()) reload();
        else if (data.added > 0 || data.missing > 0) refresh(); // only a visible change touches the grid
      }
    },
    (up) => {
      $("conn").hidden = up;
      if (!up) wasDown = true;
    },
  );
}

main();
