// Device state, the selector pill in the header and the sheet listing every device (spec 10).
// The browser's choice is a convenience only: every launch names its device explicitly.

import { ApiFailure, postJSON } from "./api.js";
import { clear, confirmSheet, el, emit, loadPref, openSheet, savePref, toast } from "./dom.js";

const KEY = "cartridge.device";
const STATE_TEXT = {
  online: "Online",
  offline: "Offline",
  connecting: "Connecting",
  needs_pairing: "Needs pairing",
  misconfigured: "Misconfigured",
};

let devices = [];
let selected = null;
let remote = false;
let loaded = false;
let pill = null;
let sheet = null;
let list = null;

export const getDevices = () => devices;
export const getSelected = () => devices.find((d) => d.name === selected) ?? null;
export const deviceByName = (name) => devices.find((d) => d.name === name) ?? null;
export const stateText = (device) => STATE_TEXT[device.state] ?? device.state;

export function initDevices({ pillButton, deviceSheet, deviceList, isRemote }) {
  pill = pillButton;
  sheet = deviceSheet;
  list = deviceList;
  remote = isRemote;
  pill.addEventListener("click", () => openSheet(sheet));
  render();
}

/** Replaces the whole list (the SSE hello). Picks the saved device, or the only one, the first time. */
export function setDevices(next) {
  const before = selected;
  devices = next;
  if (!loaded) {
    loaded = true;
    const saved = loadPref(KEY);
    if (saved && devices.some((d) => d.name === saved)) selected = saved;
    else if (devices.length === 1) selected = devices[0].name;
  }
  if (selected && !devices.some((d) => d.name === selected)) selected = null;
  changed(before);
}

/** Replaces one device card (an SSE device event). */
export function updateDevice(dto) {
  const at = devices.findIndex((d) => d.name === dto.name);
  if (at < 0) devices = [...devices, dto];
  else devices = devices.map((d) => (d.name === dto.name ? dto : d));
  changed(selected);
}

function choose(name) {
  const before = selected;
  selected = name;
  savePref(KEY, name);
  changed(before);
}

function changed(before) {
  render();
  emit("devices-changed");
  if (before !== selected) emit("selection-changed");
}

// -- rendering ----------------------------------------------------------------------------------

function statusLine(device) {
  return el(
    "span",
    { class: "status" },
    el("span", { class: `dot state-${device.state}`, "aria-hidden": "true" }),
    el("span", { text: stateText(device) }),
  );
}

function render() {
  if (!pill) return;
  const device = getSelected();
  clear(pill);
  if (device) {
    pill.append(
      el("span", { class: "pill-main" }, el("span", { class: "pill-label", text: device.label }), statusLine(device)),
      el("span", { class: "pill-model", text: device.model }),
    );
  } else {
    pill.append(el("span", { class: "pill-label", text: "Choose a device" }));
  }
  pill.setAttribute("aria-label", device ? `Device: ${device.label}, ${stateText(device)}` : "Choose a device");
  if (list) {
    list.replaceChildren(...devices.map(card));
    if (devices.length === 0) list.append(el("p", { class: "muted", text: "No devices are configured yet." }));
  }
}

function when(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

function syncLine(sync) {
  if (!sync) return "Not synced yet.";
  if (sync.running) return "Syncing now…";
  const good = sync.last_good_at ? `Last good sync ${when(sync.last_good_at)}` : "No successful sync yet";
  if (sync.status === "failed") return `Last sync failed: ${sync.error ?? "unknown error"}. ${good}.`;
  return `${good}.`;
}

function card(device) {
  const isSelected = device.name === selected;
  const lines = [];
  if (device.message) lines.push(el("p", { class: "device-message", text: device.message }));
  if (device.now_playing) lines.push(el("p", { text: `Now playing: ${device.now_playing.title}` }));
  if (device.last_launched) {
    lines.push(el("p", { class: "muted", text: `Last launched: ${device.last_launched.title}` }));
  }
  lines.push(el("p", { class: "muted", text: syncLine(device.sync) }));

  const actions = [
    isSelected
      ? el("span", { class: "badge", text: "Selected" })
      : el("button", { type: "button", class: "btn primary", text: `Use ${device.label}`, on: { click: () => pick(device) } }),
  ];
  if (!remote) {
    actions.push(
      el("button", {
        type: "button",
        class: "btn",
        text: "Return to menu",
        disabled: device.state !== "online",
        on: { click: () => returnToMenu(device) },
      }),
      el("button", {
        type: "button",
        class: "btn",
        text: "Sync now",
        disabled: Boolean(device.sync?.running),
        on: { click: (event) => syncNow(device, event.currentTarget) },
      }),
    );
  }
  return el(
    "section",
    { class: "device-card", dataset: { device: device.name } },
    el("h3", {}, device.label, " ", el("span", { class: "muted model", text: device.model })),
    statusLine(device),
    ...lines,
    el("div", { class: "actions" }, ...actions),
  );
}

function pick(device) {
  choose(device.name);
  sheet.close();
}

async function returnToMenu(device) {
  const ok = await confirmSheet({
    body: [el("p", { text: `Return ${device.label} to its menu? Unsaved progress will be lost.` })],
    confirmLabel: "Return to menu",
  });
  if (!ok) return;
  try {
    await postJSON(`/api/devices/${encodeURIComponent(device.name)}/menu`);
    toast(`${device.label} is back at its menu.`);
  } catch (error) {
    toast(error instanceof ApiFailure ? error.message : "That didn't work; try again.");
  }
}

export async function syncNow(device, button = null) {
  if (button) button.disabled = true;
  try {
    await postJSON(`/api/devices/${encodeURIComponent(device.name)}/sync`);
    toast(`Syncing ${device.label}…`);
  } catch (error) {
    toast(error instanceof ApiFailure ? error.message : "That didn't work; try again.");
    if (button) button.disabled = false;
  }
}
