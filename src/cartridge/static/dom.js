// DOM helpers shared by the UI modules: element builder, modal sheets with focus return, toast, confirm.
// Elements are built with createElement/textContent only; server strings never reach innerHTML (CSP, XSS).

export const bus = new EventTarget();
export const emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, { detail }));

/** el("button", {class: "x", text: "Hi", dataset: {id: 1}, on: {click: fn}, type: "button"}, ...children) */
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "on") for (const [type, fn] of Object.entries(value)) node.addEventListener(type, fn);
    else if (key in node && typeof value !== "string") node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child);
  }
  return node;
}

export const clear = (node) => node.replaceChildren();

// -- preferences --------------------------------------------------------------------------------

// localStorage can be missing or throw (private windows, blocked site data); a preference then lasts the session.
const session = new Map();

export function loadPref(key) {
  try {
    const value = window.localStorage.getItem(key);
    if (value !== null) return value;
  } catch {
    // fall through to the session copy
  }
  return session.get(key) ?? null;
}

export function savePref(key, value) {
  session.set(key, value);
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // session-only
  }
}

// -- sheets (<dialog>) --------------------------------------------------------------------------

const stack = [];

/** Makes a <dialog> close on backdrop click and give focus back to whatever opened it. */
export function registerSheet(dialog) {
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", () => {
    const at = stack.indexOf(dialog);
    if (at >= 0) stack.splice(at, 1);
    const opener = dialog._opener;
    dialog._opener = null;
    if (opener && opener.isConnected) opener.focus();
  });
}

export function openSheet(dialog) {
  if (dialog.open) return;
  dialog._opener = document.activeElement;
  stack.push(dialog);
  dialog.showModal();
}

export function closeTopSheet() {
  const top = stack[stack.length - 1];
  if (top) top.close();
  return Boolean(top);
}

export const hasOpenSheet = () => stack.length > 0;

// -- toast and confirm --------------------------------------------------------------------------

let toastTimer = null;
export function toast(text) {
  const node = document.getElementById("toast");
  node.textContent = text;
  // A manual popover sits in the top layer, so it shows above any sheet that is open (or opens later, after re-show).
  if (node.matches(":popover-open")) node.hidePopover();
  node.showPopover();
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    if (node.matches(":popover-open")) node.hidePopover();
  }, 6000);
}

/** Resolves true when the person picks the confirm button; Escape, Cancel and the backdrop resolve false. */
export function confirmSheet({ body, confirmLabel, cancelLabel = "Cancel" }) {
  const dialog = document.getElementById("confirm-sheet");
  document.getElementById("confirm-body").replaceChildren(...body);
  document.getElementById("confirm-yes").textContent = confirmLabel;
  document.getElementById("confirm-no").textContent = cancelLabel;
  dialog.returnValue = "";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "yes"), { once: true });
    openSheet(dialog);
  });
}
