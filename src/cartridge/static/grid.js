// Filter chips, search, the box-art grid with infinite scroll, and its empty states (spec 10).

import { getJSON } from "./api.js";
import { bus, clear, el } from "./dom.js";
import { deviceByName, getDevices, getSelected, syncNow } from "./devices.js";

const PAGE_SIZE = 120;
const SEARCH_DELAY_MS = 250;
const README_URL = "https://github.com/SDNick484/cartridge#readme";
const REASONS = { not_on_device: "not on this device", core_missing: "core not installed" };

const ui = {};
let remote = false;
let synced = true;
let onOpen = () => {};
let view = "all"; // all | favorites | recent | system:<id>
let query = "";
let onlyDevice = true;
let count = 0;
let total = null;
let loading = false;
let generation = 0;
let observer = null;
let searchTimer = null;

export function initGrid(elements, { isRemote, hasSynced, open }) {
  Object.assign(ui, elements);
  remote = isRemote;
  synced = hasSynced;
  onOpen = open;
  ui.onlyBox.addEventListener("change", () => {
    onlyDevice = ui.onlyBox.checked;
    reload();
  });
  ui.search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      query = ui.search.value.trim();
      reload();
    }, SEARCH_DELAY_MS);
  });
  observer = new IntersectionObserver((entries) => entries.some((e) => e.isIntersecting) && loadMore(), {
    rootMargin: "600px",
  });
  observer.observe(ui.sentinel);
  bus.addEventListener("selection-changed", () => {
    renderToggle();
    reload();
  });
  bus.addEventListener("devices-changed", renderToggle);
  bus.addEventListener("favorite-changed", () => view === "favorites" && reload());
  renderToggle();
}

export function setSynced(value) {
  synced = value;
}

export async function loadChips() {
  let systems = [];
  try {
    systems = await getJSON("/api/systems");
  } catch {
    // The grid still works without system chips.
  }
  const chips = [
    ["all", "A–Z"],
    ["favorites", "Favorites"],
    ["recent", "Recently played"],
    ...systems.map((s) => [`system:${s.id}`, s.name]),
  ];
  ui.chips.replaceChildren(
    ...chips.map(([key, label]) =>
      el("button", {
        type: "button",
        class: "chip",
        text: label,
        dataset: { key },
        "aria-pressed": String(key === view),
        on: { click: () => chooseView(key) },
      }),
    ),
  );
}

function chooseView(key) {
  view = key;
  for (const chip of ui.chips.children) chip.setAttribute("aria-pressed", String(chip.dataset.key === key));
  reload();
}

function renderToggle() {
  const device = getSelected();
  ui.onlyWrap.hidden = !device;
  if (device) ui.onlyText.textContent = `Only games for ${device.label}`;
  ui.onlyBox.checked = onlyDevice;
}

// -- loading ------------------------------------------------------------------------------------

export function reload() {
  generation += 1;
  count = 0;
  total = null;
  loading = false;
  clear(ui.grid);
  clear(ui.empty);
  ui.empty.hidden = true;
  loadMore();
}

function gamesPath() {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(count) });
  if (view.startsWith("system:")) params.set("system", view.slice("system:".length));
  if (view === "favorites") params.set("favorites", "1");
  if (query) params.set("q", query);
  const device = getSelected();
  if (device) {
    params.set("device", device.name);
    params.set("only_device", onlyDevice ? "1" : "0");
  }
  return `/api/games?${params}`;
}

/** The recently played list is small and unpaged: resolve each entry to a game and show them all. */
async function recentGames() {
  const device = getSelected();
  const rows = await getJSON("/api/recent");
  const ids = [];
  for (const row of rows) {
    if (!row.game || ids.includes(row.game)) continue;
    if (device && onlyDevice && row.device !== device.name) continue;
    ids.push(row.game);
  }
  const games = await Promise.all(ids.map((id) => getJSON(`/api/games/${encodeURIComponent(id)}`).catch(() => null)));
  const needle = query.toLowerCase();
  return games.filter((g) => g && g.title.toLowerCase().includes(needle));
}

async function loadMore() {
  if (loading || (total !== null && count >= total)) return;
  loading = true;
  const mine = generation;
  let games;
  try {
    if (view === "recent") {
      games = await recentGames();
      total = games.length;
    } else {
      const page = await getJSON(gamesPath());
      games = page.games;
      total = page.total;
    }
  } catch (error) {
    if (mine !== generation) return;
    loading = false;
    showMessage("Couldn't load games", error.message ?? "Try again in a moment.");
    return;
  }
  if (mine !== generation) return; // a newer filter replaced this request
  loading = false;
  count += games.length;
  ui.grid.append(...games.map(tile));
  if (count === 0) showEmpty();
  if (games.length === 0) total = count; // never loop on an empty page
  observer.unobserve(ui.sentinel); // re-observing fires again if the sentinel is still in view
  observer.observe(ui.sentinel);
}

// -- tiles and empty states ---------------------------------------------------------------------

function unavailableReason(game) {
  const device = getSelected();
  if (!device) return null;
  const entry = game.devices[device.name];
  if (!entry) return REASONS.not_on_device;
  if (entry.available) return null;
  return REASONS[entry.reason] ?? "not available";
}

function tile(game) {
  const reason = unavailableReason(game);
  return el(
    "button",
    {
      type: "button",
      class: reason ? "tile unavailable" : "tile",
      dataset: { id: game.id },
      on: { click: () => onOpen(game) },
    },
    el("span", { class: "frame" }, el("img", { src: `/art/${encodeURIComponent(game.id)}`, loading: "lazy", alt: "" })),
    el("span", { class: "tile-title", text: game.title }),
    el("span", { class: "tile-sub", text: game.system_name }),
    reason ? el("span", { class: "tile-reason", text: reason }) : null,
  );
}

function showMessage(heading, text, ...extra) {
  ui.empty.replaceChildren(el("h2", { text: heading }), el("p", { text }), ...extra);
  ui.empty.hidden = false;
}

function showEmpty() {
  const plain = view === "all" && !query;
  if (!synced && plain) {
    const buttons = remote ? [] : getDevices().map(syncButton);
    showMessage(
      "Nothing has been indexed yet",
      remote
        ? "No games have been synced yet; sync from home first."
        : "Cartridge lists what Zaparoo has indexed on each device. Open Zaparoo's own web UI and update its media index on the device, then press Sync now here.",
      el("p", {}, el("a", { href: README_URL, text: "Read the README for the full setup", rel: "noreferrer" })),
      el("div", { class: "actions" }, ...buttons),
    );
    return;
  }
  const hint = {
    favorites: "Star a game in its sheet to see it here.",
    recent: "Games you launch will show up here.",
  }[view];
  showMessage("No games match", hint ?? "Try another search, pick another chip, or turn off the device filter.");
}

function syncButton(device) {
  const many = getDevices().length > 1;
  return el("button", {
    type: "button",
    class: "btn primary",
    text: many ? `Sync ${device.label}` : "Sync now",
    on: { click: (event) => syncNow(deviceByName(device.name), event.currentTarget) },
  });
}
