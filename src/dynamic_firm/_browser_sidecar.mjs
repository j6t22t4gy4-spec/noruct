#!/usr/bin/env node
// Private bridge for a bounded, user-managed loopback Chrome DevTools surface.
// It deliberately uses only Node's built-in fetch/WebSocket APIs: Noruct does
// not bundle a browser client or a WebSocket package.

const PROTOCOL = "noruct-local-browser-v2";

function fail(code) {
  process.stdout.write(JSON.stringify({ protocol: PROTOCOL, ok: false, error_code: code }) + "\n");
  process.exitCode = 2;
}

function loopback(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" && ["127.0.0.1", "::1", "localhost"].includes(parsed.hostname)
      && parsed.port && !parsed.username && !parsed.password && !parsed.search && !parsed.hash
      && (parsed.pathname === "" || parsed.pathname === "/");
  } catch { return false; }
}

async function readRequest() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const raw = Buffer.concat(chunks);
  if (raw.length > 8192) throw new Error("INVALID_REQUEST");
  const request = JSON.parse(raw.toString("utf8"));
  if (!request || request.protocol !== PROTOCOL || !["list", "snapshot", "navigate", "click", "type", "screenshot"].includes(request.operation)
      || !loopback(request.cdp_endpoint) || typeof request.timeout_seconds !== "number"
      || request.timeout_seconds <= 0 || request.timeout_seconds > 30
      || !Number.isInteger(request.max_result_bytes) || request.max_result_bytes < 1024 || request.max_result_bytes > 64000) {
    throw new Error("INVALID_REQUEST");
  }
  if (!Number.isInteger(request.max_capture_bytes) || request.max_capture_bytes < 4096 || request.max_capture_bytes > 1000000) throw new Error("INVALID_REQUEST");
  if (request.operation !== "list" && (!Number.isInteger(request.tab_index) || request.tab_index < 1 || request.tab_index > 8)) {
    throw new Error("INVALID_REQUEST");
  }
  if (request.operation === "navigate") {
    try {
      const target = new URL(request.url);
      if (!["http:", "https:"].includes(target.protocol) || target.username || target.password || Buffer.byteLength(request.url) > 2048) throw new Error("INVALID_REQUEST");
    } catch { throw new Error("INVALID_REQUEST"); }
  }
  if (["click", "type"].includes(request.operation) && (typeof request.selector !== "string" || !request.selector.trim() || request.selector.includes("\0") || Buffer.byteLength(request.selector) > 256)) throw new Error("INVALID_REQUEST");
  if (request.operation === "type" && (typeof request.text !== "string" || request.text.includes("\0") || Buffer.byteLength(request.text) > 4096)) throw new Error("INVALID_REQUEST");
  return request;
}

function safeTab(tab) {
  try {
    const url = new URL(tab.url);
    const debuggerUrl = new URL(tab.webSocketDebuggerUrl);
    return ["http:", "https:"].includes(url.protocol)
      && debuggerUrl.protocol === "ws:"
      && ["127.0.0.1", "::1", "localhost"].includes(debuggerUrl.hostname)
      && Boolean(debuggerUrl.port)
      && !debuggerUrl.username && !debuggerUrl.password && !debuggerUrl.search && !debuggerUrl.hash;
  } catch { return false; }
}

async function tabs(request) {
  const response = await fetch(new URL("/json/list", request.cdp_endpoint), { signal: AbortSignal.timeout(Math.ceil(request.timeout_seconds * 1000)) });
  if (!response.ok) throw new Error("BROWSER_NOT_RUNNING");
  const payload = await response.json();
  if (!Array.isArray(payload)) throw new Error("INVALID_BROWSER_RESPONSE");
  return payload.filter((tab) => tab && tab.type === "page" && safeTab(tab)).slice(0, 8);
}

function cdpCommand(url, method, params, timeoutMs) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    const timer = setTimeout(() => { try { ws.close(); } catch {} reject(new Error("BROWSER_TIMEOUT")); }, timeoutMs);
    ws.addEventListener("open", () => ws.send(JSON.stringify({ id: 1, method, params })));
    ws.addEventListener("message", (event) => {
      try {
        const raw = typeof event.data === "string" ? event.data : Buffer.from(event.data).toString("utf8");
        const value = JSON.parse(raw);
        if (value.id !== 1) return;
        clearTimeout(timer); ws.close();
        if (value.error || !value.result) throw new Error("INVALID_BROWSER_RESPONSE");
        resolve(value.result);
      } catch (error) { clearTimeout(timer); try { ws.close(); } catch {} reject(error); }
    });
    ws.addEventListener("error", () => { clearTimeout(timer); reject(new Error("BROWSER_NOT_RUNNING")); });
  });
}

function evaluate(url, expression, timeoutMs) {
  return cdpCommand(url, "Runtime.evaluate", { expression, returnByValue: true, awaitPromise: false }, timeoutMs)
    .then((result) => {
      const value = result.result && result.result.value;
      if (!value || typeof value !== "object") throw new Error("INVALID_BROWSER_RESPONSE");
      return value;
    });
}

try {
  const request = await readRequest();
  const visible = await tabs(request);
  let result;
  if (request.operation === "list") {
    result = { tabs: visible.map((tab, index) => ({ tab_index: index + 1, title: String(tab.title || "").slice(0, 512), url: String(tab.url).slice(0, 2048) })) };
  } else {
    const selected = visible[request.tab_index - 1];
    if (!selected) throw new Error("INVALID_BROWSER_RESPONSE");
    const timeout = Math.ceil(request.timeout_seconds * 1000);
    if (request.operation === "snapshot") {
      const snapshot = await evaluate(selected.webSocketDebuggerUrl, "(() => ({title: document.title || '', url: location.href, text: (document.body && document.body.innerText ? document.body.innerText : '').slice(0, 36000)}))()", timeout);
      if (typeof snapshot.text !== "string") throw new Error("INVALID_BROWSER_RESPONSE");
      result = { tab_index: request.tab_index, title: String(snapshot.title || "").slice(0, 512), url: String(snapshot.url || "").slice(0, 2048), text: snapshot.text };
    } else if (request.operation === "screenshot") {
      const capture = await cdpCommand(selected.webSocketDebuggerUrl, "Page.captureScreenshot", { format: "png", fromSurface: true }, timeout);
      if (!capture || typeof capture.data !== "string" || Buffer.byteLength(capture.data, "ascii") > Math.ceil(request.max_capture_bytes * 4 / 3) + 8) throw new Error("RESULT_TOO_LARGE");
      result = { tab_index: request.tab_index, operation: "screenshot", png_base64: capture.data };
    } else if (request.operation === "navigate") {
      await cdpCommand(selected.webSocketDebuggerUrl, "Page.navigate", { url: request.url }, timeout);
      result = { tab_index: request.tab_index, operation: "navigate", target_url: request.url };
    } else if (request.operation === "click") {
      const value = await evaluate(selected.webSocketDebuggerUrl, `(() => { const element = document.querySelector(${JSON.stringify(request.selector)}); if (!element) return {changed:false, reason:"selector_not_found"}; element.click(); return {changed:true}; })()`, timeout);
      result = { tab_index: request.tab_index, operation: "click", changed: value.changed === true, reason: typeof value.reason === "string" ? value.reason : undefined };
    } else {
      const value = await evaluate(selected.webSocketDebuggerUrl, `(() => { const element = document.querySelector(${JSON.stringify(request.selector)}); if (!element) return {changed:false, reason:"selector_not_found"}; if (!("value" in element)) return {changed:false, reason:"not_editable"}; element.focus(); element.value = ${JSON.stringify(request.text)}; element.dispatchEvent(new Event("input", {bubbles:true})); element.dispatchEvent(new Event("change", {bubbles:true})); return {changed:true}; })()`, timeout);
      result = { tab_index: request.tab_index, operation: "type", changed: value.changed === true, reason: typeof value.reason === "string" ? value.reason : undefined };
    }
  }
  const encoded = JSON.stringify({ protocol: PROTOCOL, ok: true, result });
  const outputLimit = request.operation === "screenshot"
    ? Math.ceil(request.max_capture_bytes * 4 / 3) + 4096
    : request.max_result_bytes;
  if (Buffer.byteLength(encoded) > outputLimit) throw new Error("RESULT_TOO_LARGE");
  process.stdout.write(encoded + "\n");
} catch (error) {
  const code = error && typeof error.message === "string" && /^[A-Z_]+$/.test(error.message) ? error.message : "BROWSER_BRIDGE_FAILURE";
  fail(code);
}
