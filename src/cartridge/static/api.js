// Thin fetch/EventSource layer. Every failure surfaces as ApiFailure with the server's {code, message}.

export class ApiFailure extends Error {
  constructor(status, code, message, body = null) {
    super(message);
    this.name = "ApiFailure";
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

const NETWORK_MESSAGE = "Can't reach Cartridge; check your connection and try again.";

async function request(method, path, body) {
  const init = { method, headers: {} };
  if (method !== "GET") {
    init.headers["Content-Type"] = "application/json";
    init.headers["X-Cartridge"] = "1";
    if (body !== undefined) init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiFailure(0, "network", NETWORK_MESSAGE);
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const code = data && typeof data.code === "string" ? data.code : "error";
    const message = data && typeof data.message === "string" ? data.message : "Something went wrong; try again.";
    throw new ApiFailure(response.status, code, message, data);
  }
  return data;
}

export const getJSON = (path) => request("GET", path);
export const postJSON = (path, body = {}) => request("POST", path, body);
export const del = (path) => request("DELETE", path, {});

const EVENT_TYPES = ["hello", "device", "sync.started", "sync.finished"];
const BACKOFF_START_MS = 1000;
const BACKOFF_MAX_MS = 30000;

/** Opens /api/events and reconnects with 1 s -> 30 s backoff. Returns a function that closes it. */
export function events(onEvent, onState = () => {}) {
  let source = null;
  let delay = BACKOFF_START_MS;
  let timer = null;
  let closed = false;

  function connect() {
    source = new EventSource("/api/events");
    source.onopen = () => {
      delay = BACKOFF_START_MS;
      onState(true);
    };
    for (const type of EVENT_TYPES) {
      source.addEventListener(type, (event) => {
        try {
          onEvent(type, JSON.parse(event.data));
        } catch {
          // A malformed frame is skipped; the next hello resyncs everything.
        }
      });
    }
    source.onerror = () => {
      source.close();
      onState(false);
      if (closed) return;
      timer = setTimeout(connect, delay);
      delay = Math.min(delay * 2, BACKOFF_MAX_MS);
    };
  }

  connect();
  return () => {
    closed = true;
    clearTimeout(timer);
    if (source) source.close();
  };
}

/** A canonical lowercase UUIDv4; crypto.randomUUID needs a secure context, which a plain-http LAN page isn't. */
export function newRequestId() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID().toLowerCase();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
