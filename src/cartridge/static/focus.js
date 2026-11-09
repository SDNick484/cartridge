// TV mode: larger tiles, a visible focus ring, spatial arrow-key navigation and Backspace to close a sheet.
// Every control is a real button or input, so Tab and Enter work with no pointer at all.

import { closeTopSheet, hasOpenSheet, loadPref, savePref } from "./dom.js";

const KEY = "cartridge.tv";
const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';
const CROSS_WEIGHT = 2; // a candidate off to the side counts for less than one straight ahead
const DIRECTIONS = {
  ArrowRight: [1, 0],
  ArrowLeft: [-1, 0],
  ArrowDown: [0, 1],
  ArrowUp: [0, -1],
};

let tv = false;

export function initFocus(toggle) {
  const requested = new URLSearchParams(window.location.search).get("tv") === "1";
  if (requested) savePref(KEY, "1");
  setTv(requested || loadPref(KEY) === "1", toggle);
  toggle.addEventListener("click", () => {
    setTv(!tv, toggle);
    savePref(KEY, tv ? "1" : "0");
  });
  document.addEventListener("keydown", onKeyDown);
}

function setTv(on, toggle) {
  tv = on;
  document.body.classList.toggle("tv", on);
  toggle.setAttribute("aria-pressed", String(on));
}

const isTextField = (node) => node instanceof HTMLInputElement && ["text", "search"].includes(node.type);

function onKeyDown(event) {
  if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
  if (event.key === "Backspace") {
    if (!isTextField(event.target) && hasOpenSheet()) {
      closeTopSheet();
      event.preventDefault();
    }
    return;
  }
  const direction = DIRECTIONS[event.key];
  if (!tv || !direction) return;
  if (isTextField(event.target) && direction[1] === 0) return; // Left/Right move the caret there
  if (moveFocus(direction)) event.preventDefault();
}

function visibleFocusables(scope) {
  return [...scope.querySelectorAll(FOCUSABLE)].filter((node) => {
    const box = node.getBoundingClientRect();
    return box.width > 0 && box.height > 0 && getComputedStyle(node).visibility !== "hidden";
  });
}

const center = (box) => [box.left + box.width / 2, box.top + box.height / 2];

function moveFocus([dx, dy]) {
  const sheets = [...document.querySelectorAll("dialog[open]")];
  const scope = sheets.length ? sheets[sheets.length - 1] : document;
  const nodes = visibleFocusables(scope);
  if (nodes.length === 0) return false;
  const from = document.activeElement;
  if (!nodes.includes(from)) {
    nodes[0].focus();
    return true;
  }
  const [fx, fy] = center(from.getBoundingClientRect());
  let best = null;
  let bestScore = Infinity;
  for (const node of nodes) {
    if (node === from) continue;
    const [cx, cy] = center(node.getBoundingClientRect());
    const ahead = (cx - fx) * dx + (cy - fy) * dy;
    if (ahead <= 1) continue;
    const across = Math.abs((cx - fx) * dy) + Math.abs((cy - fy) * dx);
    const score = ahead + across * CROSS_WEIGHT;
    if (score < bestScore) {
      best = node;
      bestScore = score;
    }
  }
  if (!best) return false;
  best.focus();
  return true;
}
