// The game sheet and the launch / replace flow (spec 10, 9.8).

import { ApiFailure, del, getJSON, newRequestId, postJSON } from "./api.js";
import { bus, confirmSheet, el, emit, openSheet, toast } from "./dom.js";
import { deviceByName, getSelected, stateText } from "./devices.js";
import { REASONS } from "./grid.js";

const ui = {};
let remote = false;
let current = null;
let busy = false;
let favoriteChanged = false;

export function initSheet(elements, { isRemote }) {
  Object.assign(ui, elements);
  remote = isRemote;
  ui.play.addEventListener("click", play);
  ui.star.addEventListener("click", toggleStar);
  ui.dialog.addEventListener("close", () => {
    current = null;
    if (favoriteChanged) emit("favorite-changed");
    favoriteChanged = false;
  });
  bus.addEventListener("devices-changed", () => current && render());
  bus.addEventListener("selection-changed", () => current && render());
}

export async function openGame(game) {
  current = game;
  setError("");
  render();
  openSheet(ui.dialog);
  try {
    const fresh = await getJSON(`/api/games/${encodeURIComponent(game.id)}`);
    if (current && current.id === game.id) {
      current = fresh;
      render();
    }
  } catch {
    // The list's copy is good enough.
  }
}

function setError(text) {
  ui.error.textContent = text;
  ui.error.hidden = !text;
}

/** Why Play is unavailable right now, or null when it is not. */
function blocker(game) {
  const device = getSelected();
  if (!device) return "Choose a device to play on.";
  if (device.state !== "online") {
    return device.state === "offline"
      ? `${device.label} is offline.`
      : `${device.label} isn't ready (${stateText(device).toLowerCase()}).`;
  }
  const entry = game.devices[device.name];
  if (!entry) return `${game.title} isn't on ${device.label}.`;
  if (entry.available) return null;
  switch (entry.reason) {
    case "not_on_device":
      return `${game.title} isn't on ${device.label}.`;
    case "core_missing":
      return `${device.label} doesn't have the core for ${game.system_name}.`;
    default:
      return `${game.title} isn't available on ${device.label}.`;
  }
}

function render() {
  const game = current;
  ui.art.src = `/art/${encodeURIComponent(game.id)}`;
  ui.title.textContent = game.title;
  ui.system.textContent = game.system_name;
  ui.devices.replaceChildren(
    ...Object.entries(game.devices).map(([name, entry]) => {
      const device = deviceByName(name);
      const status = entry.available ? "ready to play" : (REASONS[entry.reason] ?? "not available");
      return el("li", { text: `${device ? device.label : name}: ${status}` });
    }),
  );
  if (Object.keys(game.devices).length === 0) ui.devices.append(el("li", { text: "Not on any device right now." }));

  ui.remoteNote.hidden = !remote;
  ui.play.hidden = remote;
  ui.star.hidden = remote;
  ui.reason.hidden = remote;
  if (remote) return;

  ui.star.setAttribute("aria-pressed", String(game.favorite));
  ui.star.textContent = game.favorite ? "★ Favorite" : "☆ Add to favorites";
  const device = getSelected();
  const why = blocker(game);
  if (busy && device) {
    ui.play.textContent = `Starting on ${device.label}…`;
    ui.play.disabled = true;
    ui.reason.textContent = `Starting on ${device.label}…`;
    return;
  }
  ui.play.textContent = device ? `Play on ${device.label}` : "Play";
  ui.play.disabled = Boolean(why);
  ui.reason.textContent = why ?? "";
}

async function toggleStar() {
  const game = current;
  const want = !game.favorite;
  try {
    await (want ? postJSON(`/api/favorites/${encodeURIComponent(game.id)}`) : del(`/api/favorites/${encodeURIComponent(game.id)}`));
    favoriteChanged = true;
    if (current && current.id === game.id) {
      current = { ...current, favorite: want };
      render();
    }
  } catch (error) {
    setError(error instanceof ApiFailure ? error.message : "That didn't work; try again.");
  }
}

function launchBody(game, device, replace, requestId) {
  return { device: device.name, game: game.id, replace, request_id: requestId };
}

async function play() {
  const game = current;
  const device = getSelected();
  if (!game || !device || busy) return;
  busy = true;
  setError("");
  render();
  const requestId = newRequestId(); // fresh per tap; reused only for the confirm resend below
  try {
    try {
      await postJSON("/api/launch", launchBody(game, device, false, requestId));
    } catch (error) {
      if (!(error instanceof ApiFailure && error.status === 409 && error.code === "playing")) throw error;
      const running = error.body?.now_playing?.title ?? "Another game";
      const yes = await confirmSheet({
        body: [el("p", {}, el("em", { text: running }), ` is running on ${device.label}. Replace it? Unsaved progress will be lost.`)],
        confirmLabel: "Replace",
      });
      if (!yes) return;
      await postJSON("/api/launch", launchBody(game, device, true, requestId));
    }
    ui.dialog.close();
    toast(`Started ${game.title} on ${device.label}.`);
  } catch (error) {
    setError(error instanceof ApiFailure ? error.message : "That didn't work; try again.");
  } finally {
    busy = false;
    if (current) render();
  }
}
