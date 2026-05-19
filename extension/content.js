// HAL content script. Single-file because phase 6 stays vanilla — no
// bundler, no service worker. Injects a floating button, opens a side
// panel, talks to localhost:8000/analyze.

(async () => {
  if (window.__halInjected) return;
  window.__halInjected = true;

  const BACKEND = "http://localhost:8000/analyze";
  const MAX_HISTORY = 50;
  const TF_OPTIONS = ["1m", "5m", "15m", "1h", "4h", "1D", "1W"];
  const TF_DEFAULT = "1h";
  // Per-chart TF preference is persisted under this key prefix.
  const TF_STORAGE_PREFIX = "hal_tf_";
  // Per-chart session id persisted across reloads so multi-turn context
  // survives the panel being closed and reopened.
  const SESSION_STORAGE_PREFIX = "hal_session_";

  let detectedSymbol = null;
  let selectedTimeframe = TF_DEFAULT;
  let isOpen = false;
  let storageKey = null;
  let tfStorageKey = null;
  let sessionStorageKey = null;
  let sessionId = null;

  // ───────────── symbol detection ─────────────
  //
  // TradingView consistently writes ?symbol=EXCHANGE:TICKER on chart URLs.
  // Verified 2026-05-19 across: spot crypto, perp futures (.P suffix),
  // stocks, indices, forex, futures (month codes / continuous contracts).
  // The previous title-regex fallback never fired in practice — removed
  // so a parse miss surfaces a visible error instead of a silent wrong
  // symbol.

  function stripExchange(sym) {
    if (!sym) return sym;
    const i = sym.indexOf(":");
    return i >= 0 ? sym.slice(i + 1) : sym;
  }

  function symbolFromUrl() {
    try {
      const u = new URL(location.href);
      const raw = u.searchParams.get("symbol");
      return raw ? stripExchange(raw) : null;
    } catch {
      return null;
    }
  }

  function detectSymbol() {
    const s = symbolFromUrl();
    detectedSymbol = s || null;
    return detectedSymbol;
  }

  // ───────────── storage ─────────────

  function deriveStorageKeys() {
    const m = location.pathname.match(/\/chart\/([^/]+)/);
    const slug = m ? m[1] : location.pathname.replace(/\W+/g, "_");
    storageKey = "hal_chat_" + slug;
    tfStorageKey = TF_STORAGE_PREFIX + slug;
    sessionStorageKey = SESSION_STORAGE_PREFIX + slug;
  }

  function loadOrCreateSession() {
    if (!chrome?.storage?.local) {
      sessionId = crypto.randomUUID();
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      chrome.storage.local.get(sessionStorageKey, (obj) => {
        const existing = obj[sessionStorageKey];
        if (existing) {
          sessionId = existing;
          resolve();
        } else {
          sessionId = crypto.randomUUID();
          chrome.storage.local.set({ [sessionStorageKey]: sessionId }, resolve);
        }
      });
    });
  }

  function rotateSession() {
    sessionId = crypto.randomUUID();
    if (chrome?.storage?.local) {
      chrome.storage.local.set({ [sessionStorageKey]: sessionId });
    }
  }

  function loadHistory() {
    if (!chrome?.storage?.local) return Promise.resolve([]);
    return new Promise((resolve) => {
      chrome.storage.local.get(storageKey, (obj) => resolve(obj[storageKey] || []));
    });
  }

  function loadTimeframe() {
    if (!chrome?.storage?.local) return Promise.resolve(TF_DEFAULT);
    return new Promise((resolve) => {
      chrome.storage.local.get(tfStorageKey, (obj) =>
        resolve(obj[tfStorageKey] || TF_DEFAULT)
      );
    });
  }

  function saveHistory(messages) {
    if (!chrome?.storage?.local) return;
    chrome.storage.local.set({ [storageKey]: messages.slice(-MAX_HISTORY) });
  }

  function saveTimeframe(tf) {
    if (!chrome?.storage?.local) return;
    chrome.storage.local.set({ [tfStorageKey]: tf });
  }

  function clearHistory() {
    if (!chrome?.storage?.local) return;
    chrome.storage.local.remove(storageKey);
  }

  // ───────────── UI ─────────────
  //
  // Host element + Shadow DOM. Isolates HAL styling from TradingView's CSS
  // (and vice versa) so a TV restyle can't bleed into the panel.

  const host = document.createElement("div");
  host.id = "hal-host";
  document.body.appendChild(host);
  const shadow = host.attachShadow({ mode: "open" });

  // Fetch the panel stylesheet from the extension package and inline it
  // into the shadow root. panel.css is listed under web_accessible_resources
  // so this URL is fetchable from the content script.
  const cssUrl = chrome.runtime.getURL("panel.css");
  const cssText = await fetch(cssUrl).then((r) => r.text());
  const style = document.createElement("style");
  style.textContent = cssText;
  shadow.appendChild(style);

  const fab = document.createElement("button");
  fab.className = "hal-fab";
  fab.textContent = "HAL";
  fab.title = "Open HAL";
  shadow.appendChild(fab);

  const panel = document.createElement("div");
  panel.className = "hal-panel";
  panel.innerHTML = `
    <div class="hal-header">
      <span class="hal-title">HAL</span>
      <span class="hal-context" data-hal="ctx">no chart</span>
      <select class="hal-tf" data-hal="tf" title="Timeframe">
        ${TF_OPTIONS.map((t) => `<option value="${t}">${t}</option>`).join("")}
      </select>
      <button class="hal-icon-btn" data-hal="clear" title="Clear chat">⟲</button>
      <button class="hal-icon-btn" data-hal="close" title="Close">✕</button>
    </div>
    <div class="hal-messages" data-hal="messages">
      <div class="hal-empty">Ask about the current chart.</div>
    </div>
    <div class="hal-footer">
      <textarea class="hal-input" data-hal="input"
        placeholder="What's the price action telling you?" rows="1"></textarea>
      <button class="hal-send" data-hal="send">Send</button>
    </div>
  `;
  shadow.appendChild(panel);

  // TradingView attaches global mouse + keyboard handlers (e.g. typing
  // anywhere opens symbol search). Events propagate out of the shadow
  // boundary unless stopped — handle at the host element so all panel +
  // FAB interactions are insulated. Stopping at host doesn't break the
  // input itself: the event still fires on the input first and only gets
  // killed on the way out.
  const stopEvents = [
    "mousedown", "mouseup", "mousemove", "click", "dblclick", "wheel",
    "keydown", "keypress", "keyup",
  ];
  for (const ev of stopEvents) {
    host.addEventListener(ev, (e) => e.stopPropagation());
  }

  const $ctx = panel.querySelector('[data-hal="ctx"]');
  const $tf = panel.querySelector('[data-hal="tf"]');
  const $messages = panel.querySelector('[data-hal="messages"]');
  const $input = panel.querySelector('[data-hal="input"]');
  const $send = panel.querySelector('[data-hal="send"]');
  const $clear = panel.querySelector('[data-hal="clear"]');
  const $close = panel.querySelector('[data-hal="close"]');

  function refreshContextLabel() {
    const sym = detectSymbol();
    $ctx.textContent = sym ? sym : "no symbol";
  }

  function renderMessages(history) {
    $messages.innerHTML = "";
    if (!history.length) {
      const empty = document.createElement("div");
      empty.className = "hal-empty";
      empty.textContent = "Ask about the current chart.";
      $messages.appendChild(empty);
      return;
    }
    for (const m of history) {
      const div = document.createElement("div");
      div.className = "hal-msg hal-msg-" + m.role;
      div.textContent = m.text;
      $messages.appendChild(div);
    }
    $messages.scrollTop = $messages.scrollHeight;
  }

  function appendMessage(history, role, text) {
    history.push({ role, text, ts: Date.now() });
    return appendBubble(role, text);
  }

  function appendBubble(role, text) {
    const empty = $messages.querySelector(".hal-empty");
    if (empty) empty.remove();
    const div = document.createElement("div");
    div.className = "hal-msg hal-msg-" + role;
    div.textContent = text;
    $messages.appendChild(div);
    $messages.scrollTop = $messages.scrollHeight;
    return div;
  }

  // Build a one-line feature summary like "3 FVGs · 1 OB · 2 structure · 1 sweep".
  function summarizeFeatures(features) {
    if (!features) return "(no features)";
    const parts = [];
    const n = (k) => (features[k] || []).length;
    if (n("fvgs")) parts.push(`${n("fvgs")} FVG${n("fvgs") === 1 ? "" : "s"}`);
    if (n("order_blocks")) parts.push(`${n("order_blocks")} OB${n("order_blocks") === 1 ? "" : "s"}`);
    if (n("structure")) parts.push(`${n("structure")} structure`);
    if (n("liquidity_sweeps")) parts.push(`${n("liquidity_sweeps")} sweep${n("liquidity_sweeps") === 1 ? "" : "s"}`);
    return parts.length ? parts.join(" · ") : "no features detected";
  }

  function appendDebugExpander(meta) {
    const details = document.createElement("details");
    details.className = "hal-debug";
    const summary = document.createElement("summary");
    const f = meta && meta.features;
    summary.textContent = `view detected features (${summarizeFeatures(f)})`;
    details.appendChild(summary);
    const pre = document.createElement("pre");
    pre.className = "hal-debug-json";
    pre.textContent = JSON.stringify(f || {}, null, 2);
    details.appendChild(pre);
    $messages.appendChild(details);
    $messages.scrollTop = $messages.scrollHeight;
  }

  // Parse one SSE frame ("event: ...\ndata: ...") into {event, data}.
  function parseSSE(raw) {
    let event = "message";
    const dataLines = [];
    for (const line of raw.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
    }
    if (!dataLines.length) return null;
    let data;
    try { data = JSON.parse(dataLines.join("\n")); } catch { data = {}; }
    return { event, data };
  }

  let history = [];

  async function openPanel() {
    isOpen = true;
    panel.classList.add("hal-open");
    refreshContextLabel();
    history = await loadHistory();
    renderMessages(history);
    selectedTimeframe = await loadTimeframe();
    $tf.value = selectedTimeframe;
    await loadOrCreateSession();
    $input.focus();
  }

  function closePanel() {
    isOpen = false;
    panel.classList.remove("hal-open");
  }

  fab.addEventListener("click", () => (isOpen ? closePanel() : openPanel()));
  $close.addEventListener("click", closePanel);
  $clear.addEventListener("click", () => {
    history = [];
    clearHistory();
    rotateSession();
    renderMessages(history);
  });
  $tf.addEventListener("change", () => {
    selectedTimeframe = $tf.value;
    saveTimeframe(selectedTimeframe);
  });

  // ───────────── send ─────────────

  let inflightController = null;

  function setSendMode(mode) {
    if (mode === "stop") {
      $send.textContent = "Stop";
      $send.classList.add("hal-stop");
    } else {
      $send.textContent = "Send";
      $send.classList.remove("hal-stop");
    }
  }

  async function send() {
    const text = $input.value.trim();
    if (!text) return;
    const symbol = detectSymbol();
    if (!symbol) {
      appendMessage(history, "error",
        "Couldn't detect a symbol on this page. Open a TradingView chart " +
        "(URL should contain ?symbol=…)."
      );
      saveHistory(history);
      return;
    }
    $input.value = "";
    $input.style.height = "auto";
    inflightController = new AbortController();
    setSendMode("stop");

    appendMessage(history, "user", text);
    saveHistory(history);

    const thinking = document.createElement("div");
    thinking.className = "hal-msg hal-msg-thinking";
    thinking.textContent = "thinking…";
    $messages.appendChild(thinking);
    $messages.scrollTop = $messages.scrollHeight;

    let assistantBubble = null;
    let assistantText = "";
    let sawDone = false;
    let streamErr = null;
    let wasAborted = false;
    let metaPayload = null;

    try {
      const resp = await fetch(BACKEND, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol,
          timeframe: selectedTimeframe,
          query: text,
          session_id: sessionId,
        }),
        signal: inflightController.signal,
      });
      if (!resp.ok) {
        thinking.remove();
        let detail = `Backend ${resp.status}`;
        try {
          const body = await resp.json();
          if (body && body.detail) detail = body.detail;
        } catch {}
        appendMessage(history, "error", detail);
        saveHistory(history);
        return;
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      // Stream loop. Buffer raw text, split on the SSE frame delimiter
      // (\n\n), dispatch each completed frame, keep the trailing partial.
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop();
        for (const raw of frames) {
          const evt = parseSSE(raw);
          if (!evt) continue;
          if (evt.event === "meta") {
            metaPayload = evt.data;
            continue;
          }
          if (evt.event === "token") {
            if (!assistantBubble) {
              thinking.remove();
              assistantBubble = appendBubble("assistant", "");
            }
            assistantText += evt.data.text || "";
            // textContent (not innerHTML) so model output can't inject HTML.
            assistantBubble.textContent = assistantText;
            $messages.scrollTop = $messages.scrollHeight;
            continue;
          }
          if (evt.event === "error") {
            streamErr = evt.data.detail || "stream error";
            continue;
          }
          if (evt.event === "done") {
            sawDone = true;
            continue;
          }
        }
      }
    } catch (err) {
      if (err.name === "AbortError") {
        wasAborted = true;
      } else {
        streamErr = `Could not reach backend at ${BACKEND}. Is uvicorn running? (${err.message})`;
      }
    } finally {
      // Clean up thinking indicator if we never got a token.
      if (thinking.isConnected) thinking.remove();

      if (streamErr) {
        if (assistantBubble) assistantBubble.remove();
        appendMessage(history, "error", streamErr);
      } else if (wasAborted) {
        if (assistantBubble) {
          assistantText += " (stopped)";
          assistantBubble.textContent = assistantText;
          history.push({ role: "assistant", text: assistantText, ts: Date.now() });
        } else {
          appendMessage(history, "error", "(stopped before any response)");
        }
      } else if (assistantBubble) {
        if (!sawDone) {
          // Stream cut off mid-flight (e.g. backend killed). Flag it
          // rather than silently leaving a truncated bubble.
          appendMessage(history, "error", "(stream ended unexpectedly)");
          // Still keep the partial answer in history below.
          assistantText && history.push({ role: "assistant", text: assistantText, ts: Date.now() });
        } else {
          history.push({ role: "assistant", text: assistantText || "(empty response)", ts: Date.now() });
          if (metaPayload) appendDebugExpander(metaPayload);
        }
      } else {
        appendMessage(history, "error", "(no response)");
      }
      saveHistory(history);
      inflightController = null;
      setSendMode("send");
      $input.focus();
    }
  }

  $send.addEventListener("click", () => {
    if (inflightController) {
      inflightController.abort();
    } else {
      send();
    }
  });
  $input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!inflightController) send();
    }
  });
  $input.addEventListener("input", () => {
    $input.style.height = "auto";
    $input.style.height = Math.min($input.scrollHeight, 120) + "px";
  });

  // ───────────── observers ─────────────

  deriveStorageKeys();
  refreshContextLabel();

  const titleEl = document.querySelector("title");
  if (titleEl) {
    new MutationObserver(refreshContextLabel).observe(titleEl, { childList: true });
  }

  // URL changes from client-side nav — re-derive keys, reload history+TF.
  let lastHref = location.href;
  setInterval(async () => {
    if (location.href === lastHref) return;
    lastHref = location.href;
    deriveStorageKeys();
    refreshContextLabel();
    if (isOpen) {
      history = await loadHistory();
      renderMessages(history);
      selectedTimeframe = await loadTimeframe();
      $tf.value = selectedTimeframe;
      await loadOrCreateSession();
    }
  }, 1500);

  console.log("[HAL] content script loaded");
})();
