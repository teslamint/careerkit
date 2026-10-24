(function () {
  "use strict";

  if (typeof detectJobPosting !== "function") return;
  if (typeof chrome === "undefined" || !chrome.runtime || !chrome.runtime.sendMessage) return;

  var CURRENT_URL = null;
  var host = null;
  var shadow = null;
  var container = null;
  var initialized = false;
  var pollInterval = null;
  var checkInterval = null;

  function isAlive() {
    try {
      return chrome.runtime && !!chrome.runtime.id;
    } catch (e) {
      return false;
    }
  }

  function cleanup() {
    if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
    if (checkInterval) { clearInterval(checkInterval); checkInterval = null; }
    if (host && host.parentNode) host.parentNode.removeChild(host);
  }

  function safeSend(msg, callback) {
    if (!isAlive()) { cleanup(); return; }
    try {
      chrome.runtime.sendMessage(msg, function (response) {
        if (!isAlive()) { cleanup(); return; }
        if (chrome.runtime.lastError) {
          if (callback) callback(null, chrome.runtime.lastError.message);
          return;
        }
        if (callback) callback(response, null);
      });
    } catch (e) {
      cleanup();
    }
  }

  var VERDICT_CONFIG = {
    recommended: { color: "#22C55E", icon: "🟢", label: "추천" },
    hold: { color: "#F59E0B", icon: "🟡", label: "보류" },
    hold_capped: { color: "#F59E0B", icon: "🟡⏳", label: "대기" },
    not_recommended: { color: "#EF4444", icon: "🔴", label: "비추천" },
    none: { color: "#9CA3AF", icon: "⚪", label: "미스크리닝" }
  };

  // Not a verdict: a record of why detailed screening was skipped.
  var SET_ASIDE_CONFIG = { color: "#6B7280", icon: "⏭", label: "사전 필터 제외 기록" };

  var STYLE_TEXT = [
    ":host { all: initial; }",
    ".ck-container { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }",
    ".ck-pill {",
    "  display: inline-flex;",
    "  align-items: center;",
    "  gap: 6px;",
    "  height: 40px;",
    "  padding: 0 14px;",
    "  border-radius: 20px;",
    "  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);",
    "  font-size: 13px;",
    "  font-weight: 600;",
    "  line-height: 1;",
    "  white-space: nowrap;",
    "  cursor: default;",
    "  box-sizing: border-box;",
    "  border: none;",
    "}",
    ".ck-badge {",
    "  background: var(--ck-color, #9CA3AF);",
    "  color: #FFFFFF;",
    "}",
    ".ck-icon { font-size: 14px; }",
    ".ck-collect-btn {",
    "  background: #3B82F6;",
    "  color: #FFFFFF;",
    "  cursor: pointer;",
    "  font: inherit;",
    "  transition: background 0.15s ease;",
    "}",
    ".ck-collect-btn:hover { background: #2563EB; }",
    ".ck-collect-btn:active { background: #1D4ED8; }",
    ".ck-loading { background: #6B7280; color: #FFFFFF; }",
    ".ck-error { background: #DC2626; color: #FFFFFF; }",
    ".ck-spinner {",
    "  width: 14px;",
    "  height: 14px;",
    "  border-radius: 50%;",
    "  border: 2px solid rgba(255, 255, 255, 0.35);",
    "  border-top-color: #FFFFFF;",
    "  animation: ck-spin 0.7s linear infinite;",
    "  box-sizing: border-box;",
    "}",
    "@keyframes ck-spin {",
    "  from { transform: rotate(0deg); }",
    "  to { transform: rotate(360deg); }",
    "}",
    "@media (prefers-color-scheme: dark) {",
    "  .ck-pill { box-shadow: 0 2px 10px rgba(0, 0, 0, 0.5); }",
    "  .ck-collect-btn { background: #3B82F6; }",
    "  .ck-collect-btn:hover { background: #60A5FA; }",
    "}"
  ].join("\n");

  function ensureHost() {
    if (initialized) return;
    initialized = true;
    host = document.createElement("div");
    host.style.cssText =
      "all: initial; position: fixed; z-index: 2147483647; right: 20px; bottom: 20px;";
    shadow = host.attachShadow({ mode: "closed" });

    var styleEl = document.createElement("style");
    styleEl.textContent = STYLE_TEXT;
    shadow.appendChild(styleEl);

    container = document.createElement("div");
    container.className = "ck-container";
    shadow.appendChild(container);

    (document.documentElement || document.body).appendChild(host);
  }

  function render(node) {
    if (!container) return;
    container.textContent = "";
    container.appendChild(node);
  }

  function buildBadge(config) {
    var pill = document.createElement("div");
    pill.className = "ck-pill ck-badge";
    pill.style.setProperty("--ck-color", config.color);

    var icon = document.createElement("span");
    icon.className = "ck-icon";
    icon.textContent = config.icon;
    pill.appendChild(icon);

    var label = document.createElement("span");
    label.className = "ck-label";
    label.textContent = config.label;
    pill.appendChild(label);

    return pill;
  }

  function buildCollectButton() {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ck-pill ck-collect-btn";
    btn.textContent = "수집";
    btn.addEventListener("click", function () { startCollectAndPoll(CURRENT_URL); });
    return btn;
  }

  function buildLoading(label) {
    var pill = document.createElement("div");
    pill.className = "ck-pill ck-loading";

    var spinner = document.createElement("span");
    spinner.className = "ck-spinner";
    pill.appendChild(spinner);

    var text = document.createElement("span");
    text.className = "ck-label";
    text.textContent = label;
    pill.appendChild(text);

    return pill;
  }

  function buildError(message) {
    var pill = document.createElement("div");
    pill.className = "ck-pill ck-error";

    var icon = document.createElement("span");
    icon.className = "ck-icon";
    icon.textContent = "⚠️";
    pill.appendChild(icon);

    var text = document.createElement("span");
    text.className = "ck-label";
    text.textContent = message || "연결 오류";
    pill.appendChild(text);

    return pill;
  }

  function verdictConfigFor(data) {
    if (!data || !data.screening_verdict) return VERDICT_CONFIG.none;
    if (data.screening_verdict === "hold" && data.verdict_capped === true) {
      return VERDICT_CONFIG.hold_capped;
    }
    return VERDICT_CONFIG[data.screening_verdict] || VERDICT_CONFIG.none;
  }

  function isDecided(data) {
    return !!(data && (data.screening_verdict || data.prescreen_reason));
  }

  function stopPolling() {
    if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
  }

  function pollForVerdict(url) {
    stopPolling();
    var attempts = 0;
    var maxAttempts = 120;
    pollInterval = setInterval(function () {
      if (!isAlive()) { stopPolling(); return; }
      attempts++;
      if (attempts > maxAttempts) {
        stopPolling();
        render(buildError("시간 초과"));
        return;
      }
      safeSend({ action: "lookup", url: url }, function (response, err) {
        if (url !== CURRENT_URL) { stopPolling(); return; }
        if (err || !response || response.status !== "ok" || !response.data) return;
        if (isDecided(response.data)) {
          stopPolling();
          renderFromRecord(response.data);
        }
      });
    }, 3000);
  }

  // Matches the host's page cap (careerkit_host.py:_MAX_PAGE_BYTES), which keeps the whole
  // request under the host's 1 MB message cap (_MAX_MESSAGE_BYTES).
  var MAX_PAGE_BYTES = 900000;

  // 직행 disallows automated access, so its posting is read from the scripts of the
  // page the user already loaded and nothing is fetched. A client-side navigation keeps
  // the first page's scripts, so a page without the posting id needs a reload.
  function captureZighangPage(jobId) {
    var parts = [];
    var scripts = document.querySelectorAll("script");
    for (var i = 0; i < scripts.length; i++) {
      var text = scripts[i].textContent || "";
      if (text.indexOf("self.__next_f.push") >= 0) {
        parts.push("<script>" + text + "</script>");
      }
    }
    var page = parts.join("\n");
    if (page.indexOf(jobId) < 0) return { error: "새로고침 후 다시 시도" };
    if (new TextEncoder().encode(JSON.stringify(page)).length > MAX_PAGE_BYTES) {
      return { error: "페이지가 너무 큽니다" };
    }
    return { page: page };
  }

  function startCollectAndPoll(url) {
    var message = { action: "collect", url: url };
    var detection = detectJobPosting(url);
    if (detection && detection.platform === "zighang") {
      var captured = captureZighangPage(detection.jobId);
      if (captured.error) {
        stopPolling();
        render(buildError(captured.error));
        return;
      }
      message.page = captured.page;
    }
    render(buildLoading("스크리닝 중"));
    pollForVerdict(url);
    safeSend(message, function (response, err) {
      if (url !== CURRENT_URL) return;
      if (err) { stopPolling(); return; }
      if (response && response.status === "error") {
        stopPolling();
        render(buildError(response.message));
        return;
      }
      if (response && isDecided(response.data)) {
        stopPolling();
        renderFromRecord(response.data);
        return;
      }
    });
  }

  function buildScreenButton() {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ck-pill ck-collect-btn";
    btn.style.background = "#9CA3AF";
    btn.textContent = "⚪ 스크리닝";
    btn.addEventListener("click", function () {
      startCollectAndPoll(CURRENT_URL);
    });
    return btn;
  }

  function renderFromRecord(data) {
    if (!data) {
      render(buildCollectButton());
      return;
    }
    if (!data.screening_verdict) {
      if (data.prescreen_reason) {
        render(buildBadge(SET_ASIDE_CONFIG));
        return;
      }
      render(buildScreenButton());
      return;
    }
    render(buildBadge(verdictConfigFor(data)));
  }

  function checkUrl() {
    if (!isAlive()) { cleanup(); return; }
    var url = location.href;
    if (url === CURRENT_URL) return;

    var detection = detectJobPosting(url);
    CURRENT_URL = url;
    stopPolling();

    if (!detection) {
      if (host && host.parentNode) {
        host.style.display = "none";
      }
      return;
    }

    ensureHost();
    host.style.display = "";
    render(buildLoading("확인 중"));
    var requestedUrl = CURRENT_URL;
    safeSend({ action: "lookup", url: CURRENT_URL }, function (response, err) {
      if (requestedUrl !== CURRENT_URL) return;
      if (err) { render(buildError()); return; }
      if (!response || response.status !== "ok") {
        render(buildError(response && response.message));
        return;
      }
      renderFromRecord(response.data);
    });
  }

  try {
    chrome.runtime.onMessage.addListener(function (message) {
      if (!isAlive() || !message) return;
      if (!message.url || message.url !== CURRENT_URL) return;

      if (message.action === "screening_complete") {
        stopPolling();
        renderFromRecord(message.data);
      } else if (message.action === "screening_failed") {
        stopPolling();
        render(buildError(message.message));
      }
    });
  } catch (e) {
    // context already invalidated at load time
  }

  checkUrl();
  checkInterval = setInterval(checkUrl, 800);
})();
