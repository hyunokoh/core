/* =========================================================================
   zkCEX market-data WebSocket client.

   Vanilla ES module — wraps the built-in WebSocket with:
     * auto-reconnect (exponential backoff, capped at 30s),
     * client-side desired-subscription tracking (resubscribe on reconnect),
     * a queue for SUBSCRIBE/UNSUBSCRIBE issued before the socket is open,
     * a `ws-status` CustomEvent on `window` whenever the state changes.

   Default URL is `/ws/stream`, served same-origin via the homepage proxy.

   Usage:

     import { MarketWS } from "./ws-client.js";
     const ws = new MarketWS({ onMessage: (m) => console.log(m) });
     ws.open();
     ws.subscribe(["depth@ETHUSDT", "trade@ETHUSDT"]);

   Each delivered message has the Binance shape: { stream, data }.
   ========================================================================= */

const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS  = 30_000;

export class MarketWS {
  constructor({ url = "/ws/stream", onMessage, onError } = {}) {
    this.url = url;
    this.onMessage = typeof onMessage === "function" ? onMessage : () => {};
    this.onError   = typeof onError   === "function" ? onError   : () => {};

    /** @type {WebSocket|null} */
    this._ws = null;
    /** Streams the caller actually wants, regardless of socket state. */
    this._desired = new Set();
    /** Queued frames (raw JSON strings) to send once the socket opens. */
    this._sendQueue = [];
    /** Increments per (re)connect; used to tag pending RPC ids. */
    this._connectAttempt = 0;
    this._reconnectDelay = RECONNECT_BASE_MS;
    this._reconnectTimer = null;
    this._closedByUser = false;
    this._rpcId = 1;
    this._lastStatus = null;
  }

  // -------- public API --------

  open() {
    this._closedByUser = false;
    this._connect();
  }

  close() {
    this._closedByUser = true;
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    if (this._ws) {
      try { this._ws.close(1000, "client_close"); } catch { /* ignore */ }
    }
    this._setStatus("closed");
  }

  /** Subscribe to one or more streams. Idempotent. */
  subscribe(streams) {
    const list = (Array.isArray(streams) ? streams : [streams]).filter(Boolean);
    const fresh = list.filter(s => !this._desired.has(s));
    list.forEach(s => this._desired.add(s));
    if (!fresh.length) return;
    this._send({ method: "SUBSCRIBE", params: fresh, id: this._rpcId++ });
  }

  /** Unsubscribe (or remove desired tracking + send UNSUBSCRIBE if open). */
  unsubscribe(streams) {
    const list = (Array.isArray(streams) ? streams : [streams]).filter(Boolean);
    const tracked = list.filter(s => this._desired.has(s));
    tracked.forEach(s => this._desired.delete(s));
    if (!tracked.length) return;
    this._send({ method: "UNSUBSCRIBE", params: tracked, id: this._rpcId++ });
  }

  /** Currently desired streams (caller-visible, not server-confirmed). */
  desired() { return [...this._desired]; }

  /** Last status string surfaced via the ws-status event. */
  status() { return this._lastStatus; }

  // -------- internals --------

  _send(obj) {
    const text = JSON.stringify(obj);
    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      try {
        this._ws.send(text);
      } catch (e) {
        this._sendQueue.push(text);
      }
    } else {
      // Will be flushed on open. Cap the queue defensively.
      this._sendQueue.push(text);
      if (this._sendQueue.length > 256) this._sendQueue.shift();
    }
  }

  _flushSendQueue() {
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
    while (this._sendQueue.length) {
      const text = this._sendQueue.shift();
      try { this._ws.send(text); }
      catch (e) {
        // socket dropped; requeue and bail.
        this._sendQueue.unshift(text);
        return;
      }
    }
  }

  _connect() {
    if (this._closedByUser) return;
    this._connectAttempt += 1;
    this._setStatus("connecting");
    const proto = (typeof location !== "undefined" && location.protocol === "https:")
      ? "wss" : "ws";
    const host  = (typeof location !== "undefined" && location.host) || "127.0.0.1";
    const url = /^wss?:/i.test(this.url) ? this.url : `${proto}://${host}${this.url}`;

    let ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      this.onError(e);
      this._scheduleReconnect();
      return;
    }
    this._ws = ws;

    ws.addEventListener("open", () => {
      this._setStatus("open");
      this._reconnectDelay = RECONNECT_BASE_MS;
      // Resubscribe to everything we want.
      const want = [...this._desired];
      // Keep send queue but prepend a fresh SUBSCRIBE so the server starts
      // streaming from a known baseline.
      this._sendQueue.length = 0;
      if (want.length) {
        this._sendQueue.push(JSON.stringify({
          method: "SUBSCRIBE", params: want, id: this._rpcId++,
        }));
      }
      this._flushSendQueue();
    });

    ws.addEventListener("message", (ev) => {
      let parsed;
      try { parsed = JSON.parse(ev.data); }
      catch (_e) { return; }
      // RPC reply (has `id`, no `stream`) -> ignore unless it's an error.
      if (parsed && parsed.error && parsed.id !== undefined) {
        // surface as console-only signal so callers that want to react can
        // listen to the ws-status event.
        try { this.onError(parsed); } catch { /* ignore */ }
        return;
      }
      if (parsed && typeof parsed.stream === "string") {
        try { this.onMessage(parsed); } catch (e) { this.onError(e); }
      }
    });

    ws.addEventListener("error", (e) => {
      this._setStatus("error");
      try { this.onError(e); } catch { /* ignore */ }
    });

    ws.addEventListener("close", () => {
      this._ws = null;
      if (!this._closedByUser) {
        this._setStatus("closed");
        this._scheduleReconnect();
      }
    });
  }

  _scheduleReconnect() {
    if (this._closedByUser) return;
    const delay = Math.min(this._reconnectDelay, RECONNECT_MAX_MS);
    // Add up to 30% jitter so a herd of tabs doesn't reconnect in lockstep.
    const jitter = delay * (Math.random() * 0.3);
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      this._connect();
    }, delay + jitter);
    this._reconnectDelay = Math.min(this._reconnectDelay * 2, RECONNECT_MAX_MS);
  }

  _setStatus(s) {
    if (this._lastStatus === s) return;
    this._lastStatus = s;
    if (typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("ws-status", { detail: { status: s, url: this.url } }));
    }
  }
}

export default MarketWS;
