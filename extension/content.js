// HAL content script. Single-file because phase 6 stays vanilla — no
// bundler, no service worker. Injects a floating button, opens a side
// panel, talks to localhost:8000/analyze.

(() => {
  if (window.__halInjected) return;
  window.__halInjected = true;

  const BACKEND = "http://localhost:8000/analyze";
  const MAX_HISTORY = 50;
  const TF_OPTIONS = ["1m", "5m", "15m", "1h", "4h", "1D", "1W"];
  const TF_DEFAULT = "1h";
  // Per-chart TF preference is persisted under this key prefix.
  const TF_STORAGE_PREFIX = "hal_tf_";

  let detectedSymbol = null;
  let selectedTimeframe = TF_DEFAULT;
  let isOpen = false;
  let storageKey = null;
  let tfStorageKey = null;

  // ───────────── symbol detection ─────────────
  //
  // We rely on the URL: TradingView consistently writes ?symbol=EXCHANGE:TICKER
  // when a chart is loaded. The page title shows price/percent only, no TF.
  // Title is a weak fallback in case ?symbol is missing (rare).

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

  function symbolFromTitle() {
    // Observed format: "BTCUSDT 78,206.45 ▼ −1.15% Unnamed"
    // Take the first word if it looks like a ticker.
    const m = document.title.match(/^([A-Z0-9.:_-]{2,16})\s/);
    return m ? stripExchange(m[1]) : null;
  }

  function detectSymbol() {
    const s = symbolFromUrl() || symbolFromTitle();
    if (s) detectedSymbol = s;
    return detectedSymbol;
  }

  // ───────────── storage ─────────────

  function deriveStorageKeys() {
    const m = location.pathname.match(/\/chart\/([^/]+)/);
    const slug = m ? m[1] : location.pathname.replace(/\W+/g, "_");
    storageKey = "hal_chat_" + slug;
    tfStorageKey = TF_STORAGE_PREFIX + slug;
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

  const fab = document.createElement("button");
  fab.className = "hal-fab";
  fab.textContent = "HAL";
  fab.title = "Open HAL";
  document.body.appendChild(fab);

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
  document.body.appendChild(panel);

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
    const empty = $messages.querySelector(".hal-empty");
    if (empty) empty.remove();
    const div = document.createElement("div");
    div.className = "hal-msg hal-msg-" + role;
    div.textContent = text;
    $messages.appendChild(div);
    $messages.scrollTop = $messages.scrollHeight;
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
    renderMessages(history);
  });
  $tf.addEventListener("change", () => {
    selectedTimeframe = $tf.value;
    saveTimeframe(selectedTimeframe);
  });

  // ───────────── send ─────────────

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
    $send.disabled = true;

    appendMessage(history, "user", text);
    saveHistory(history);

    const thinking = document.createElement("div");
    thinking.className = "hal-msg hal-msg-thinking";
    thinking.textContent = "thinking…";
    $messages.appendChild(thinking);
    $messages.scrollTop = $messages.scrollHeight;

    try {
      const resp = await fetch(BACKEND, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol, timeframe: selectedTimeframe, query: text }),
      });
      const data = await resp.json().catch(() => ({}));
      thinking.remove();
      if (!resp.ok) {
        appendMessage(history, "error", data.detail || `Backend ${resp.status}`);
      } else {
        appendMessage(history, "assistant", data.answer || "(empty response)");
      }
    } catch (err) {
      thinking.remove();
      appendMessage(history, "error",
        `Could not reach backend at ${BACKEND}. Is uvicorn running? (${err.message})`
      );
    } finally {
      saveHistory(history);
      $send.disabled = false;
      $input.focus();
    }
  }

  $send.addEventListener("click", send);
  $input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
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
    }
  }, 1500);

  console.log("[HAL] content script loaded");
})();
