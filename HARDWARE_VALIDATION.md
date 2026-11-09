# Hardware validation

Cartridge is built and tested against a simulated Zaparoo Core (`cartridge simulate`). The claims below came from Zaparoo's documentation, not from a real MiSTer. Run each check against a real device and record the result in the README's assumptions table (change `unverified` to `verified`, or fix the code and this list if the claim is wrong). Each claim is cited in the code as `# ASSUMPTION <id>`; `grep -rn "ASSUMPTION <id>" src` shows where it matters.

Setup: a MiSTer with Zaparoo Core installed and its media database built (README, "Install Zaparoo Core on a MiSTer"), its address in `devices.yaml` (use `192.0.2.10` below as a stand-in), Cartridge running, and a WebSocket client such as `websocat` on the same network. Run `cartridge check` first: it should print `<device>: online`.

## `Z-LEGACY`: unauthenticated LAN access

Claim: MiSTer Core admits unauthenticated plaintext LAN clients for every allow-listed method, including `media.image`.

1. With Zaparoo's API key and encryption unconfigured, run `websocat ws://192.0.2.10:7497/api/v0.1`.
2. Send `{"jsonrpc":"2.0","id":"1","method":"version"}`, then the same with `"method":"media"`, `"systems"`, `"launchers"`, `"media.search"` (params `{"query":"a"}`), `"media.active"`, `"media.history"`.
3. In Cartridge, open the device, run a sync, launch a game, return to menu.

Expected: every call returns a `result`, never an authentication or permission error, and the Cartridge steps work with no credentials configured. If Core refuses, `auth: paired` is needed (out of scope for v1).

## `Z-ENDPOINT`: endpoint, ids and ping

Claim: `ws://<host>:7497/api/v0.1`, string ids, and the text `ping` is answered with `pong`.

1. Connect with `websocat ws://192.0.2.10:7497/api/v0.1`.
2. Send the plain text `ping`.
3. Send `{"jsonrpc":"2.0","id":"abc","method":"version"}`.

Expected: `pong` for the first; for the second a response whose `id` is the string `"abc"`. The Cartridge card shows the device online and stays online for several minutes (it pings periodically).

## `Z-SEARCH-FIELDS`: media.search result shape

Claim: `media.search` results carry `mediaId`, `name`, `systemId` (or `system.id`) and `path`, and maybe `relativePath`.

1. Send `{"jsonrpc":"2.0","id":"1","method":"media.search","params":{"query":"a","maxResults":5}}`.
2. Inspect the objects in the result.

Expected: each carries `mediaId`, `name`, a system identifier (as `systemId` or `system.id`) and `path`. Note whether `relativePath` is present. After a sync the Cartridge grid lists the library with correct titles and systems, with nothing missing.

## `C-RELPATH`: relative paths across devices

Claim: `relativePath` is relative to the system folder and identical across devices that use one share.

1. For the same game on two MiSTers that share one library, run `media.search` on each and compare `relativePath` and `path`.
2. In Cartridge, find the game and open its sheet.

Expected: `relativePath` is the path inside the system folder (no `/media/fat/games/<System>/` prefix) and the same on both devices, and Cartridge shows one game available on both. If it is missing or differs, set `strip_prefixes` per device (README, "Device config") and check again.

## `Z-RUN-WAITS`: run returns after the launch

Claim: `run` returns after the launch has happened, within 30 s.

1. Send `{"jsonrpc":"2.0","id":"1","method":"run","params":{"text":"<a game path from media.search>"}}` and time the reply.
2. Launch a game from Cartridge and time until the UI reports success.

Expected: the reply arrives after the game starts, in under 30 s, and Cartridge reports the launch only once the game is running (the device card shows it as now playing).

## `Z-STOP-MENU`: stop returns to the menu

Claim: `stop` returns the MiSTer to the menu, and `media.stopped` follows (or `media.active` becomes empty).

1. Launch a game, keep a `websocat` session open, and send `{"jsonrpc":"2.0","id":"1","method":"stop"}`.
2. Watch for a `media.stopped` notification, and send `media.active` afterwards.
3. In Cartridge, use **Return to menu**, and launch a game onto a busy device with replace.

Expected: the MiSTer shows its menu, `media.stopped` arrives within about 5 s (or `media.active` is empty), and Cartridge's card returns to idle and a replacing launch starts the new game.

## `Z-HISTORY-PATH`: history paths match indexed paths

Claim: `media.history.mediaPath` and `media.active.mediaPath` equal the path the index reports.

1. Play a game, then send `media.history` and `media.active`.
2. Compare each `mediaPath` with the `path` of the same game from `media.search`.

Expected: byte-for-byte equal. In Cartridge the game appears under **Recently played** with the right title, and its device card shows it as now playing. If the paths differ, recently played will be empty or wrong.

## `Z-INDEXING`: indexing completion

Claim: `media.indexing` signals completion in a detectable way (a notification with `indexing` false).

1. Keep a `websocat` session open and start a re-index from Zaparoo's web UI (`http://192.0.2.10:7497/app/`).
2. Watch the notifications until it finishes.

Expected: `media.indexing` notifications arrive while it runs, and a final one has `indexing: false`. Cartridge then syncs that device by itself, and new games appear without pressing Sync now.

## `Z-IMAGE`: media.image inline

Claim: `media.image` with inline delivery returns base64 data and a `contentType`, and returns an error or an empty result when the game has no art.

1. For a game with art on the share, send `{"jsonrpc":"2.0","id":"1","method":"media.image","params":{"mediaId":<id>,"imageTypes":["boxart","cover"],"maxSize":512,"delivery":"inline"}}`.
2. Repeat for a game without art.

Expected: the first returns base64 data and a `contentType` such as `image/png`, and Cartridge shows it as the cover. The second returns an error or empty data, and Cartridge falls back to libretro-thumbnails (`ART_ONLINE=1`) or a generated tile.

## `Z-ENC-ERROR`: encryption-required rejection

Claim: when Core requires encryption, it rejects plaintext requests with error code `-32002`.

1. On the MiSTer, turn encryption on in Zaparoo Core's configuration and restart Core.
2. Send `{"jsonrpc":"2.0","id":"1","method":"version"}` from `websocat`.
3. Look at the device card in Cartridge.
4. Set `encryption = false` again and restart Core.

Expected: an error response with code `-32002`; the card shows "needs pairing (Zaparoo encryption is on)", and goes back online after step 4.

## `L-THUMB-URL`: libretro-thumbnails URL rule

Claim: art lives at `https://thumbnails.libretro.com/<Playlist Name>/Named_Boxarts/<Name>.png`, where `<Name>` is the title with `` & * / : ` < > ? \ | " `` replaced by `_`.

1. Pick games from at least three systems, one with a reserved character in its title (for example one containing `:` or `&`).
2. Build the URL by hand from the playlist name (for example `Nintendo - Super Nintendo Entertainment System`) and the title, and request it with `curl -I`.
3. Run Cartridge with `ART_ONLINE=1` and look at those games' covers.

Expected: `200 image/png` for titles the repository has, and the same covers appear in Cartridge. A title whose reserved characters were replaced with `_` resolves; if it doesn't, the replacement rule in `src/cartridge/art.py` is wrong. A miss is a generated tile, not an error.
