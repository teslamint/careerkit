(function () {
  "use strict";

  // Message contract (matches ext/content/badge.js and ext/background/service-worker.js):
  // requests to background are {action, url}; the native host resolves
  // (platform, job_id) from `url` itself (careerkit_host.py:_resolve_key), so
  // we never send platform/job_id directly.

  var PLATFORM_LABELS = {
    wanted: "원티드",
    remember: "리멤버",
    saramin: "사람인",
    groupby: "그룹바이"
  };

  var VERDICT_CONFIG = {
    recommended: { color: "#22C55E", icon: "🟢", label: "추천" },
    hold: { color: "#F59E0B", icon: "🟡", label: "보류" },
    hold_capped: { color: "#F59E0B", icon: "🟡⏳", label: "대기" },
    not_recommended: { color: "#EF4444", icon: "🔴", label: "비추천" },
    none: { color: "#9CA3AF", icon: "⚪", label: "미스크리닝" }
  };

  // Background only pushes screening_complete/screening_failed to the tab's
  // content script (chrome.tabs.sendMessage), not to extension pages like
  // this side panel. The chrome.runtime.onMessage listener below covers that
  // path if it's ever wired up, but polling is the fallback that actually
  // resolves state 3 -> state 4 today.
  var POLL_INTERVAL_MS = 3000;
  var POLL_TIMEOUT_MS = 180000;

  var content = document.getElementById("content");

  var state = {
    url: null,
    detected: null, // {platform, jobId} from detectJobPosting, or null
    rescreenMode: false, // true while rescreen polling is active
    beforeScreening: null, // screening_markdown captured before rescreen
    lastData: null, // last successful get_detail data for re-rendering on failure
    companyInfoResult: null,
    progressStageText: null
  };

  var pollTimer = null;
  var pollTimeoutTimer = null;

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    if (pollTimeoutTimer) {
      clearTimeout(pollTimeoutTimer);
      pollTimeoutTimer = null;
    }
  }

  function clearContent() {
    stopPolling();
    while (content.firstChild) content.removeChild(content.firstChild);
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function renderMessage(icon, title, body) {
    clearContent();
    var wrap = el("div", "state-message");
    if (icon) wrap.appendChild(el("div", "state-icon", icon));
    wrap.appendChild(el("p", "state-title", title));
    if (body) wrap.appendChild(el("p", "state-body", body));
    content.appendChild(wrap);
  }

  function renderNotJobSite() {
    renderMessage("🌐", "채용 사이트에서 사용하세요", "지원 중인 채용 공고 페이지를 열면 여기에 정보가 표시됩니다.");
  }

  function renderConnectionError(message) {
    renderMessage("⚠️", "Native host 연결 불가", message || "careerkit native host가 설치되어 있는지 확인하세요.");
    content.appendChild(
      el("p", "state-hint", "설치 안내: ext/native-host/install.py 실행 후 Chrome을 재시작하세요.")
    );
  }

  function renderNotCollected(detected, errorMessage) {
    clearContent();
    var wrap = el("div", "not-collected");
    wrap.appendChild(el("p", "platform-label", PLATFORM_LABELS[detected.platform] || detected.platform));
    wrap.appendChild(el("p", "job-url", state.url));
    var button = el("button", "collect-button", "공고 수집하기");
    button.type = "button";
    button.addEventListener("click", onCollectClick);
    wrap.appendChild(button);
    if (errorMessage) wrap.appendChild(el("p", "state-error", errorMessage));
    content.appendChild(wrap);
  }

  function renderCollecting(stageText) {
    clearContent();
    var wrap = el("div", "collecting");
    var bar = el("div", "progress-bar");
    bar.appendChild(el("div", "progress-bar-fill"));
    wrap.appendChild(bar);
    var stage = el("p", "progress-stage", stageText);
    stage.id = "progress-stage";
    wrap.appendChild(stage);
    content.appendChild(wrap);
  }

  function setStageText(text) {
    var stageEl = document.getElementById("progress-stage");
    if (stageEl) stageEl.textContent = text;
  }

  function applyProgressStageText(text) {
    if (!text) return;
    state.progressStageText = text;
    setStageText(text);
  }

  function stageTextForProgress(message) {
    if (!message) return null;
    if (message.stage === "company_info" && message.state === "checking") return "회사정보 확인 중...";
    if (message.stage === "company_info" && message.state === "enriching") return "회사정보 보강 중...";
    if (message.stage === "screening" && message.state === "running") return "스크리닝 중...";
    return null;
  }

  function buildCompanyInfoNoticeData(result) {
    if (!result || !result.status) return null;
    if (result.status === "ready") {
      return {
        tone: "ready",
        title: "회사 정보 준비 완료",
        body: "완성도 " + result.completeness + "%",
        showCompleteness: true,
        shouldRefresh: result.persisted === true
      };
    }
    if (result.status === "warning" && result.warning_code === "below_threshold") {
      return {
        tone: "warning",
        title: "회사 정보 보강 필요",
        body: "완성도 " + result.completeness + "%",
        showCompleteness: true,
        shouldRefresh: false
      };
    }
    if (result.status === "warning" && result.warning_code === "missing") {
      return {
        tone: "warning",
        title: "회사 정보가 아직 없습니다",
        body: "플랫폼 정보만으로 스크리닝을 계속했습니다.",
        showCompleteness: false,
        shouldRefresh: false
      };
    }
    return null;
  }

  function shouldIgnoreMessageForUrl(currentUrl, messageUrl) {
    return typeof currentUrl !== "string" || typeof messageUrl !== "string" || messageUrl !== currentUrl;
  }

  function buildCompanyInfoNotice(result) {
    var noticeData = buildCompanyInfoNoticeData(result);
    if (!noticeData) return null;
    var notice = el("div", "company-info-notice company-info-notice-" + noticeData.tone);
    notice.appendChild(el("p", "company-info-notice-title", noticeData.title));
    notice.appendChild(el("p", "company-info-notice-body", noticeData.body));
    return notice;
  }

  function shouldOfferRescreen(record, isFallback) {
    if (!record || !record.screening_verdict) return false;
    if (record.verdict_capped === true) return "prominent";
    if (isFallback === true) return "prominent";
    return "icon";
  }
  if (typeof globalThis !== "undefined") {
    globalThis.shouldOfferRescreen = shouldOfferRescreen;
    globalThis.buildTabBar = buildTabBar;
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { shouldOfferRescreen: shouldOfferRescreen, buildTabBar: buildTabBar };

  function verdictConfigFor(record) {
    if (!record || !record.screening_verdict) return VERDICT_CONFIG.none;
    if (record.screening_verdict === "hold" && record.verdict_capped === true) {
      return VERDICT_CONFIG.hold_capped;
    }
    return VERDICT_CONFIG[record.screening_verdict] || VERDICT_CONFIG.none;
  }

  function buildVerdictBadge(record) {
    var config = verdictConfigFor(record);
    var badge = el("span", "verdict-badge", "");
    badge.style.setProperty("--verdict-color", config.color);
    badge.appendChild(el("span", "verdict-icon", config.icon));
    badge.appendChild(el("span", "verdict-label", config.label));
    return badge;
  }

  function buildTableElement(tableData) {
    var wrapper = el("div", "table-wrap");
    var table = document.createElement("table");
    var thead = document.createElement("thead");
    var headerRow = document.createElement("tr");
    for (var i = 0; i < tableData.headers.length; i++) {
      var th = document.createElement("th");
      th.textContent = tableData.headers[i];
      headerRow.appendChild(th);
    }
    thead.appendChild(headerRow);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    for (var r = 0; r < tableData.rows.length; r++) {
      var tr = document.createElement("tr");
      for (var c = 0; c < tableData.rows[r].length; c++) {
        var td = document.createElement("td");
        td.textContent = tableData.rows[r][c];
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrapper.appendChild(table);
    return wrapper;
  }

  function buildParsedElements(elements) {
    var frag = document.createDocumentFragment();
    for (var i = 0; i < elements.length; i++) {
      var item = elements[i];
      if (item.type === "table") {
        frag.appendChild(buildTableElement(item));
      } else if (item.type === "verdict") {
        frag.appendChild(el("div", "verdict-highlight", item.text));
      } else if (item.type === "list") {
        var ul = document.createElement("ul");
        for (var j = 0; j < item.items.length; j++) {
          ul.appendChild(el("li", null, item.items[j]));
        }
        frag.appendChild(ul);
      } else {
        frag.appendChild(el("p", "parsed-text", item.content));
      }
    }
    return frag;
  }

  function buildSection(section) {
    if (!section.heading) {
      return buildParsedElements(section.elements);
    }
    var details = document.createElement("details");
    var isCollapsed = section.heading.indexOf("핵심 근거") >= 0;
    if (!isCollapsed) details.setAttribute("open", "");
    var summary = document.createElement("summary");
    summary.textContent = section.heading;
    details.appendChild(summary);
    details.appendChild(buildParsedElements(section.elements));
    return details;
  }

  function buildCappedNotice() {
    var notice = el("div", "capped-notice");
    notice.appendChild(el("p", "capped-title", "⚠ 로컬 모델 판정"));
    notice.appendChild(el("p", "capped-body", '“추천” 이상으로 올라갈 수 없습니다. 아래 재스크리닝 버튼으로 더 정확한 판정을 받을 수 있습니다.'));
    return notice;
  }

  function buildRescreenButton(type) {
    if (type === "prominent") {
      var btn = el("button", "rescreen-btn", "재스크리닝");
      btn.type = "button";
      btn.addEventListener("click", onRescreenClick);
      return btn;
    }
    var iconBtn = el("button", "rescreen-icon-btn", "↻");
    iconBtn.type = "button";
    iconBtn.title = "재스크리닝";
    iconBtn.addEventListener("click", onRescreenClick);
    return iconBtn;
  }

  function onRescreenClick() {
    state.rescreenMode = true;
    state.beforeScreening = state.lastData ? state.lastData.screening_markdown : null;
    state.progressStageText = null;
    renderCollecting("재스크리닝 중...");
    chrome.runtime.sendMessage({ action: "rescreen", url: state.url }, function (response) {
      if (chrome.runtime.lastError) {
        state.rescreenMode = false;
        renderConnectionError(chrome.runtime.lastError.message);
        return;
      }
      if (!response || response.status === "error") {
        state.rescreenMode = false;
        if (state.lastData) {
          renderDetail(state.lastData.record, state.lastData.screening_markdown, state.lastData.jd_markdown, state.lastData.is_fallback);
          var errMsg = el("p", "state-error rescreen-error", (response && response.message) || "재스크리닝을 시작하지 못했습니다.");
          var tabBar = content.querySelector(".tab-bar");
          if (tabBar) content.insertBefore(errMsg, tabBar);
          else content.appendChild(errMsg);
        } else {
          renderNotCollected(state.detected, (response && response.message) || "재스크리닝을 시작하지 못했습니다.");
        }
        return;
      }
      startPolling();
    });
  }

  function buildTabBar(hasJd, hasCompanyInfo) {
    var bar = el("div", "tab-bar");
    var screeningBtn = el("button", "tab-btn tab-btn-active", "스크리닝");
    screeningBtn.setAttribute("data-tab", "screening");
    screeningBtn.type = "button";
    bar.appendChild(screeningBtn);

    var jdBtn = el("button", "tab-btn", "JD 원문");
    jdBtn.setAttribute("data-tab", "jd");
    jdBtn.type = "button";
    if (!hasJd) jdBtn.disabled = true;
    bar.appendChild(jdBtn);

    var companyBtn = el("button", "tab-btn", "회사 정보");
    companyBtn.setAttribute("data-tab", "company");
    companyBtn.type = "button";
    if (!hasCompanyInfo) companyBtn.disabled = true;
    bar.appendChild(companyBtn);

    bar.addEventListener("click", function (e) {
      var btn = e.target.closest(".tab-btn");
      if (!btn || btn.disabled) return;
      var tab = btn.getAttribute("data-tab");
      var buttons = bar.querySelectorAll(".tab-btn");
      for (var i = 0; i < buttons.length; i++) {
        buttons[i].classList.toggle("tab-btn-active", buttons[i] === btn);
      }
      var panels = content.querySelectorAll(".tab-content");
      for (var j = 0; j < panels.length; j++) {
        panels[j].hidden = panels[j].getAttribute("data-tab") !== tab;
      }
    });
    return bar;
  }

  function renderDetail(record, screeningMarkdown, jdMarkdown, isFallback) {
    state.lastData = {
      record: record,
      screening_markdown: screeningMarkdown,
      jd_markdown: jdMarkdown,
      company_info_markdown: null,
      is_fallback: isFallback
    };
    clearContent();

    var header = el("div", "detail-header");
    header.appendChild(buildVerdictBadge(record));
    var rescreenType = shouldOfferRescreen(record, isFallback);
    if (rescreenType === "icon") {
      header.appendChild(buildRescreenButton("icon"));
    }
    content.appendChild(header);

    content.appendChild(el("h2", "company-name", record.company));
    content.appendChild(el("p", "position-name", record.position));

    if (record.screening_provider) {
      content.appendChild(el("p", "provider-label", "제공자: " + record.screening_provider));
    }

    if (record.screening_verdict === "hold" && record.verdict_capped === true) {
      content.appendChild(buildCappedNotice());
    }

    var companyInfoNotice = buildCompanyInfoNotice(state.companyInfoResult);
    if (companyInfoNotice) {
      content.appendChild(companyInfoNotice);
    }

    if (rescreenType === "prominent") {
      content.appendChild(buildRescreenButton("prominent"));
    }

    var hasJd = !!jdMarkdown;
    content.appendChild(buildTabBar(hasJd, false));

    var screeningPanel = el("div", "tab-content");
    screeningPanel.setAttribute("data-tab", "screening");
    if (screeningMarkdown) {
      var parsed = parseScreeningMarkdown(screeningMarkdown);
      if (parsed.hasParsedContent) {
        for (var i = 0; i < parsed.sections.length; i++) {
          screeningPanel.appendChild(buildSection(parsed.sections[i]));
        }
      } else {
        var pre = document.createElement("pre");
        pre.className = "screening-text";
        pre.textContent = screeningMarkdown;
        screeningPanel.appendChild(pre);
      }
    } else if (record.screening_verdict) {
      var prescreenMsg = "사전 필터에서 판정되어 상세 스크리닝이 실행되지 않았습니다.";
      if (record.prescreen_reason) {
        var reasonLabels = {
          title_exclude: "제목 제외 키워드 매칭",
          title_include: "제목에 백엔드/서버 키워드 없음",
          closed: "공고 마감",
          prior_application: "동일 회사 기지원"
        };
        var label = reasonLabels[record.prescreen_reason] || record.prescreen_reason;
        if (record.prescreen_reason.indexOf("domain_") === 0) {
          label = "비백엔드 도메인 (" + record.prescreen_reason.substring(7) + ")";
        }
        prescreenMsg += "\n사유: " + label;
      }
      screeningPanel.appendChild(el("p", "state-body prescreen-reason", prescreenMsg));
    } else {
      screeningPanel.appendChild(el("p", "state-body", "스크리닝 결과가 아직 없습니다."));
    }
    content.appendChild(screeningPanel);

    var jdPanel = el("div", "tab-content");
    jdPanel.setAttribute("data-tab", "jd");
    jdPanel.hidden = true;
    if (jdMarkdown) {
      var jdPre = document.createElement("pre");
      jdPre.className = "screening-text";
      jdPre.textContent = jdMarkdown;
      jdPanel.appendChild(jdPre);
    } else {
      jdPanel.appendChild(el("p", "state-body", "JD 원문을 불러올 수 없습니다."));
    }
    content.appendChild(jdPanel);

    var companyPanel = el("div", "tab-content");
    companyPanel.setAttribute("data-tab", "company");
    companyPanel.hidden = true;
    companyPanel.appendChild(el("p", "state-body", "회사 정보를 불러올 수 없습니다."));
    content.appendChild(companyPanel);
    if (!state.companyInfoResult || state.companyInfoResult.status !== "warning" || state.companyInfoResult.warning_code === "below_threshold") {
      fetchCompanyInfo();
    }
  }

  function applyCompanyInfo(markdown) {
    if (!markdown) return;
    if (state.lastData) state.lastData.company_info_markdown = markdown;
    var companyBtn = content.querySelector('.tab-btn[data-tab="company"]');
    if (companyBtn) companyBtn.disabled = false;
    var companyPanel = content.querySelector('.tab-content[data-tab="company"]');
    if (companyPanel) {
      while (companyPanel.firstChild) companyPanel.removeChild(companyPanel.firstChild);
      var parsed = parseScreeningMarkdown(markdown);
      if (parsed.hasParsedContent) {
        for (var i = 0; i < parsed.sections.length; i++) {
          companyPanel.appendChild(buildSection(parsed.sections[i]));
        }
      } else {
        var pre = document.createElement("pre");
        pre.className = "screening-text";
        pre.textContent = markdown;
        companyPanel.appendChild(pre);
      }
    }
  }

  function fetchCompanyInfo() {
    if (!state.detected) return;
    var requestUrl = state.url;
    chrome.runtime.sendMessage({ action: "get_company_info", url: requestUrl }, function (response) {
      if (chrome.runtime.lastError) return;
      if (shouldIgnoreMessageForUrl(state.url, requestUrl)) return;
      if (!response || response.status === "error" || !response.data) return;
      applyCompanyInfo(response.data.company_info_markdown);
    });
  }

  function applyDetailResponse(data) {
    if (!data || !data.record) {
      renderNotCollected(state.detected);
      return;
    }
    stopPolling();
    renderDetail(data.record, data.screening_markdown, data.jd_markdown, data.is_fallback);
  }

  function fetchDetail() {
    if (!state.detected) return;
    chrome.runtime.sendMessage({ action: "get_detail", url: state.url }, function (response) {
      if (chrome.runtime.lastError) {
        renderConnectionError(chrome.runtime.lastError.message);
        return;
      }
      if (!response || response.status === "error") {
        renderConnectionError(response && response.message);
        return;
      }
      applyDetailResponse(response.data);
    });
  }

  function pollOnce() {
    if (!state.detected) {
      stopPolling();
      return;
    }
    chrome.runtime.sendMessage({ action: "get_detail", url: state.url }, function (response) {
      if (chrome.runtime.lastError || !response || response.status === "error") return;
      var data = response.data;
      if (!data || !data.record) return;
      if (state.rescreenMode) {
        var changed = data.screening_markdown !== state.beforeScreening;
        if (!changed && state.lastData) {
          changed = data.record.screening_provider !== state.lastData.record.screening_provider;
        }
        if (changed) {
          stopPolling();
          state.rescreenMode = false;
          state.progressStageText = null;
          renderDetail(data.record, data.screening_markdown, data.jd_markdown, data.is_fallback);
          return;
        }
        if (!state.progressStageText) setStageText("재스크리닝 중...");
      } else {
        if (data.record.screening_verdict) {
          stopPolling();
          state.progressStageText = null;
          renderDetail(data.record, data.screening_markdown, data.jd_markdown, data.is_fallback);
          return;
        }
        if (!state.progressStageText) setStageText("스크리닝 중...");
      }
    });
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
    pollTimeoutTimer = setTimeout(function () {
      stopPolling();
      if (state.rescreenMode) {
        state.rescreenMode = false;
        chrome.runtime.sendMessage({ action: "get_detail", url: state.url }, function (response) {
          if (response && response.data && response.data.record) {
            renderDetail(response.data.record, response.data.screening_markdown, response.data.jd_markdown, response.data.is_fallback);
            var msg = el("p", "state-error rescreen-error", "재스크리닝 시간이 초과되었습니다. 아직 진행 중일 수 있습니다.");
            var tabBar = content.querySelector(".tab-bar");
            if (tabBar) content.insertBefore(msg, tabBar);
            else content.appendChild(msg);
          } else {
            renderNotCollected(state.detected, "재스크리닝 시간이 초과되었습니다.");
          }
        });
      } else {
        renderNotCollected(state.detected, "스크리닝 시간이 초과되었습니다.");
      }
    }, POLL_TIMEOUT_MS);
  }

  function onCollectClick() {
    state.progressStageText = null;
    renderCollecting("추출 중...");
    chrome.runtime.sendMessage({ action: "collect", url: state.url }, function (response) {
      if (chrome.runtime.lastError) {
        renderConnectionError(chrome.runtime.lastError.message);
        return;
      }
      if (!response || response.status === "error") {
        renderNotCollected(state.detected, (response && response.message) || "수집을 시작하지 못했습니다.");
        return;
      }
      if (response.status === "duplicate") {
        fetchDetail();
        return;
      }
      // status === "accepted": screening runs asynchronously in the native
      // host; poll until it lands (see POLL_INTERVAL_MS comment above).
      startPolling();
    });
  }

  function init() {
    state.rescreenMode = false;
    state.beforeScreening = null;
    state.lastData = null;
    state.companyInfoResult = null;
    state.progressStageText = null;
    clearContent();
    content.appendChild(el("p", "state-body", "불러오는 중..."));

    chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
      var tab = tabs && tabs[0];
      if (!tab || !tab.url || typeof detectJobPosting !== "function") {
        renderNotJobSite();
        return;
      }
      state.url = tab.url;
      state.detected = detectJobPosting(tab.url);
      if (!state.detected) {
        renderNotJobSite();
        return;
      }
      chrome.runtime.sendMessage({ action: "get_pending_status", url: state.url }, function (resp) {
        if (chrome.runtime.lastError) { fetchDetail(); return; }
        if (resp && resp.pending) {
          renderCollecting("스크리닝 중...");
          startPolling();
        } else {
          fetchDetail();
        }
      });
    });
  }

  chrome.runtime.onMessage.addListener(function (message) {
    if (!message || !state.detected) return;
    if (shouldIgnoreMessageForUrl(state.url, message.url)) return;

    if (message.action === "screening_progress") {
      var stageText = stageTextForProgress(message);
      if (stageText) applyProgressStageText(stageText);
    } else if (message.action === "screening_complete") {
      var companyInfoResult = message.data && message.data.company_info ? message.data.company_info : null;
      if (!buildCompanyInfoNoticeData(companyInfoResult)) return;
      state.rescreenMode = false;
      state.progressStageText = null;
      state.companyInfoResult = companyInfoResult;
      fetchDetail();
    } else if (message.action === "screening_failed") {
      stopPolling();
      state.progressStageText = null;
      if (state.rescreenMode && state.lastData) {
        state.rescreenMode = false;
        renderDetail(state.lastData.record, state.lastData.screening_markdown, state.lastData.jd_markdown, state.lastData.is_fallback);
        var errMsg = el("p", "state-error rescreen-error", message.message || "재스크리닝에 실패했습니다.");
        var tabBar = content.querySelector(".tab-bar");
        if (tabBar) content.insertBefore(errMsg, tabBar);
        else content.appendChild(errMsg);
      } else {
        renderNotCollected(state.detected, message.message || "스크리닝에 실패했습니다.");
      }
    }
  });

  chrome.tabs.onActivated.addListener(init);

  chrome.tabs.onUpdated.addListener(function (tabId, changeInfo, tab) {
    if (changeInfo.url && tab.active) init();
  });

  init();

  if (typeof globalThis !== "undefined") {
    globalThis.stageTextForProgress = stageTextForProgress;
    globalThis.buildCompanyInfoNoticeData = buildCompanyInfoNoticeData;
    globalThis.shouldIgnoreMessageForUrl = shouldIgnoreMessageForUrl;
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      shouldOfferRescreen: shouldOfferRescreen,
      buildTabBar: buildTabBar,
      stageTextForProgress: stageTextForProgress,
      buildCompanyInfoNoticeData: buildCompanyInfoNoticeData,
      shouldIgnoreMessageForUrl: shouldIgnoreMessageForUrl,
      __setState: function (nextState) {
        Object.keys(nextState || {}).forEach(function (key) {
          state[key] = nextState[key];
        });
      },
      __getState: function () {
        return state;
      },
      __renderCollecting: renderCollecting,
      __pollOnce: pollOnce,
      __getContentRoot: function () {
        return content;
      }
    };
  }
})();
