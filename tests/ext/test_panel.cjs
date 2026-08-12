"use strict";

var assert = require("assert");
var path = require("path");
var fs = require("fs");
var vm = require("vm");

var panelPath = path.join(__dirname, "..", "..", "ext", "sidepanel", "panel.js");
var code = fs.readFileSync(panelPath, "utf-8");

function createElement(tagName) {
  var node = {
    tagName: tagName || "div",
    className: "",
    textContent: "",
    id: "",
    hidden: false,
    title: "",
    disabled: false,
    attributes: {},
    style: { cursor: "", setProperty: function () {} },
    children: [],
    parentNode: null,
    appendChild: function (child) {
      if (!child) return child;
      child.parentNode = this;
      this.children.push(child);
      return child;
    },
    removeChild: function (child) {
      var index = this.children.indexOf(child);
      if (index >= 0) {
        this.children.splice(index, 1);
        child.parentNode = null;
      }
    },
    insertBefore: function (child, before) {
      var index = this.children.indexOf(before);
      child.parentNode = this;
      if (index < 0) this.children.push(child);
      else this.children.splice(index, 0, child);
      return child;
    },
    setAttribute: function (name, value) {
      this.attributes[name] = String(value);
      if (name === "id") this.id = String(value);
      if (name === "class") this.className = String(value);
    },
    getAttribute: function (name) {
      if (name === "id") return this.id || null;
      if (name === "class") return this.className || null;
      return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    },
    addEventListener: function () {},
    closest: function () { return null; },
    classList: { toggle: function () {} }
  };
  Object.defineProperty(node, "firstChild", {
    get: function () {
      return this.children[0] || null;
    }
  });
  node.querySelectorAll = function (selector) {
    return queryAll(node, selector);
  };
  node.querySelector = function (selector) {
    var matches = queryAll(node, selector);
    return matches[0] || null;
  };
  return node;
}

function matchesSelector(node, selector) {
  if (!selector) return false;
  var classMatch = selector.match(/\.([a-zA-Z0-9_-]+)/);
  if (classMatch) {
    var classes = (node.className || "").split(/\s+/).filter(Boolean);
    if (classes.indexOf(classMatch[1]) < 0) return false;
  }
  var attrMatch = selector.match(/\[([^=]+)=\"([^\"]+)\"\]/);
  if (attrMatch && node.getAttribute(attrMatch[1]) !== attrMatch[2]) return false;
  var tagMatch = selector.match(/^[a-zA-Z]+/);
  if (tagMatch && node.tagName.toLowerCase() !== tagMatch[0].toLowerCase()) return false;
  return true;
}

function queryAll(root, selector) {
  var matches = [];
  function walk(node) {
    if (node !== root && matchesSelector(node, selector)) matches.push(node);
    for (var i = 0; i < node.children.length; i++) {
      walk(node.children[i]);
    }
  }
  walk(root);
  return matches;
}

function textTree(node) {
  var parts = [];
  function walk(current) {
    if (current.textContent) parts.push(current.textContent);
    for (var i = 0; i < current.children.length; i++) {
      walk(current.children[i]);
    }
  }
  walk(node);
  return parts.join("\n");
}

function createHarness(options) {
  options = options || {};
  var content = createElement("div");
  content.id = "content";
  var runtimeListener = null;
  var sendMessageLog = [];
  var requestHandlers = options.requestHandlers || {};
  var activeTab = options.activeTab || null;

  var documentMock = {
    body: createElement("body"),
    getElementById: function (id) {
      if (id === "content") return content;
      if (content.id === id) return content;
      return queryAll(content, "#" + id)[0] || null;
    },
    createElement: function (tag) { return createElement(tag); },
    createDocumentFragment: function () { return createElement("fragment"); }
  };

  var chromeMock = {
    runtime: {
      id: "test",
      lastError: null,
      onMessage: { addListener: function (listener) { runtimeListener = listener; } },
      sendMessage: function (request, callback) {
        sendMessageLog.push(request);
        var handler = requestHandlers[request.action];
        var response = handler ? handler(request, sendMessageLog) : { status: "ok", data: null };
        if (callback) callback(response);
      }
    },
    tabs: {
      onActivated: { addListener: function () {} },
      onUpdated: { addListener: function () {} },
      query: function (_query, callback) { callback(activeTab ? [activeTab] : []); }
    },
    sidePanel: { setPanelBehavior: function () {} },
    alarms: { create: function () {}, onAlarm: { addListener: function () {} } },
    action: { getBadgeText: function () {}, setBadgeBackgroundColor: function () {}, setBadgeText: function () {} },
    notifications: { create: function () {} },
    storage: { session: { get: function () { return Promise.resolve({}); }, set: function () { return Promise.resolve(); } } }
  };

  var sandbox = {
    globalThis: {},
    module: { exports: {} },
    document: documentMock,
    chrome: chromeMock,
    detectJobPosting: function (url) {
      if (!url) return null;
      return { platform: "wanted", jobId: "123" };
    },
    parseScreeningMarkdown: function (markdown) {
      return { sections: [], hasParsedContent: false, raw: markdown };
    },
    setTimeout: function () {},
    clearTimeout: function () {},
    setInterval: function () {},
    clearInterval: function () {},
    navigator: { clipboard: null },
    Promise: Promise
  };

  vm.runInNewContext(code, sandbox);

  return {
    api: sandbox.module.exports,
    globalApi: sandbox.globalThis,
    content: content,
    sendMessageLog: sendMessageLog,
    runtimeListener: function () { return runtimeListener; },
    textTree: function () { return textTree(content); }
  };
}

var helperHarness = createHarness();
var shouldOfferRescreen = helperHarness.globalApi.shouldOfferRescreen;
var buildTabBar = helperHarness.globalApi.buildTabBar;
var helperApi = helperHarness.api;

var passed = 0;
var failed = 0;

function test(name, fn) {
  try {
    fn();
    passed++;
  } catch (e) {
    failed++;
    console.error("FAIL: " + name);
    console.error("  " + e.message);
  }
}

function makeDetailResponse() {
  return {
    status: "ok",
    data: {
      record: {
        company: "Acme",
        position: "Backend Engineer",
        screening_verdict: "recommended",
        verdict_capped: false,
        screening_provider: "openai"
      },
      screening_markdown: "# Screening",
      jd_markdown: "# JD",
      is_fallback: false
    }
  };
}

function makeNoVerdictDetailResponse() {
  return {
    status: "ok",
    data: {
      record: {
        company: "Acme",
        position: "Backend Engineer",
        screening_verdict: null,
        verdict_capped: false,
        screening_provider: null
      },
      screening_markdown: null,
      jd_markdown: "# JD",
      is_fallback: false
    }
  };
}

// --- shouldOfferRescreen ---

test("returns false for null record", function () {
  assert.strictEqual(shouldOfferRescreen(null, false), false);
});

test("returns false for record without verdict", function () {
  assert.strictEqual(shouldOfferRescreen({ screening_verdict: null }, false), false);
});

test("returns 'prominent' for capped record", function () {
  assert.strictEqual(
    shouldOfferRescreen({ screening_verdict: "hold", verdict_capped: true }, false),
    "prominent"
  );
});

test("returns 'prominent' for fallback document", function () {
  assert.strictEqual(
    shouldOfferRescreen({ screening_verdict: "hold", verdict_capped: false }, true),
    "prominent"
  );
});

test("returns 'icon' for normal screened record", function () {
  assert.strictEqual(
    shouldOfferRescreen({ screening_verdict: "recommended", verdict_capped: false }, false),
    "icon"
  );
});

test("returns 'icon' for not_recommended without fallback or capped", function () {
  assert.strictEqual(
    shouldOfferRescreen({ screening_verdict: "not_recommended" }, false),
    "icon"
  );
});

test("capped takes priority over fallback", function () {
  assert.strictEqual(
    shouldOfferRescreen({ screening_verdict: "hold", verdict_capped: true }, true),
    "prominent"
  );
});

test("isFallback undefined treated as falsy", function () {
  assert.strictEqual(
    shouldOfferRescreen({ screening_verdict: "hold", verdict_capped: false }, undefined),
    "icon"
  );
});

// --- buildTabBar ---

test("buildTabBar creates 3 buttons", function () {
  var bar = buildTabBar(true, true);
  var buttons = bar.children.filter(function (c) { return c.className && c.className.indexOf("tab-btn") >= 0; });
  assert.strictEqual(buttons.length, 3);
});

test("buildTabBar disables company tab when hasCompanyInfo is false", function () {
  var bar = buildTabBar(true, false);
  var buttons = bar.children.filter(function (c) { return c.className && c.className.indexOf("tab-btn") >= 0; });
  var companyBtn = buttons[2];
  assert.strictEqual(companyBtn.textContent, "회사 정보");
  assert.strictEqual(companyBtn.disabled, true);
});

test("buildTabBar enables company tab when hasCompanyInfo is true", function () {
  var bar = buildTabBar(true, true);
  var buttons = bar.children.filter(function (c) { return c.className && c.className.indexOf("tab-btn") >= 0; });
  var companyBtn = buttons[2];
  assert.strictEqual(companyBtn.textContent, "회사 정보");
  assert.strictEqual(!!companyBtn.disabled, false);
});

test("buildTabBar disables jd tab when hasJd is false", function () {
  var bar = buildTabBar(false, true);
  var buttons = bar.children.filter(function (c) { return c.className && c.className.indexOf("tab-btn") >= 0; });
  var jdBtn = buttons[1];
  assert.strictEqual(jdBtn.disabled, true);
  var companyBtn = buttons[2];
  assert.strictEqual(!!companyBtn.disabled, false);
});

// --- helper coverage ---

test("progress stage labels render company-info stage before screening stage", function () {
  assert.strictEqual(helperApi.stageTextForProgress({ stage: "company_info", state: "checking" }), "회사정보 확인 중...");
  assert.strictEqual(helperApi.stageTextForProgress({ stage: "company_info", state: "enriching" }), "회사정보 보강 중...");
  assert.strictEqual(helperApi.stageTextForProgress({ stage: "screening", state: "running" }), "스크리닝 중...");
});

test("buildCompanyInfoNoticeData returns ready notice with completeness", function () {
  assert.strictEqual(
    JSON.stringify(helperApi.buildCompanyInfoNoticeData({ status: "ready", completeness: 88, warning_code: null, persisted: true })),
    JSON.stringify({
      tone: "ready",
      title: "회사 정보 준비 완료",
      body: "완성도 88%",
      showCompleteness: true,
      shouldRefresh: true
    })
  );
});

test("buildCompanyInfoNoticeData returns warning notice without failure tone for below-threshold result", function () {
  assert.strictEqual(
    JSON.stringify(helperApi.buildCompanyInfoNoticeData({ status: "warning", completeness: 62, warning_code: "below_threshold", persisted: true })),
    JSON.stringify({
      tone: "warning",
      title: "회사 정보 보강 필요",
      body: "완성도 62%",
      showCompleteness: true,
      shouldRefresh: false
    })
  );
});

test("buildCompanyInfoNoticeData returns missing warning without invented score", function () {
  assert.strictEqual(
    JSON.stringify(helperApi.buildCompanyInfoNoticeData({ status: "warning", completeness: null, warning_code: "missing", persisted: false })),
    JSON.stringify({
      tone: "warning",
      title: "회사 정보가 아직 없습니다",
      body: "플랫폼 정보만으로 스크리닝을 계속했습니다.",
      showCompleteness: false,
      shouldRefresh: false
    })
  );
});

test("shouldIgnoreMessageForUrl requires exact current-url equality", function () {
  assert.strictEqual(helperApi.shouldIgnoreMessageForUrl("https://current.example/job/1", "https://other.example/job/2"), true);
  assert.strictEqual(helperApi.shouldIgnoreMessageForUrl("https://current.example/job/1", "https://current.example/job/1"), false);
  assert.strictEqual(helperApi.shouldIgnoreMessageForUrl("https://current.example/job/1", null), true);
});

// --- runtime listener behavior ---

test("runtime progress handler updates stage only for current-url messages", function () {
  var harness = createHarness();
  var api = harness.api;
  api.__setState({ url: "https://current.example/job/1", detected: { platform: "wanted", jobId: "1" } });
  api.__renderCollecting("대기 중...");
  var listener = harness.runtimeListener();

  listener({ action: "screening_progress", url: "https://current.example/job/1", stage: "company_info", state: "checking" });
  assert.ok(harness.textTree().indexOf("회사정보 확인 중...") >= 0);

  listener({ action: "screening_progress", url: "https://other.example/job/2", stage: "screening", state: "running" });
  assert.strictEqual(harness.textTree().indexOf("스크리닝 중..."), -1);

  listener({ action: "screening_progress", stage: "screening", state: "running" });
  assert.strictEqual(harness.textTree().indexOf("스크리닝 중..."), -1);
});

test("poll fallback does not overwrite accepted native company-info stage and later screening progress advances it", function () {
  var harness = createHarness({
    requestHandlers: {
      get_detail: function () { return makeNoVerdictDetailResponse(); }
    }
  });
  var api = harness.api;
  api.__setState({ url: "https://current.example/job/1", detected: { platform: "wanted", jobId: "1" } });
  api.__renderCollecting("대기 중...");
  var listener = harness.runtimeListener();

  listener({ action: "screening_progress", url: "https://current.example/job/1", stage: "company_info", state: "checking" });
  assert.ok(harness.textTree().indexOf("회사정보 확인 중...") >= 0);

  api.__pollOnce();
  assert.ok(harness.textTree().indexOf("회사정보 확인 중...") >= 0);
  assert.strictEqual(harness.textTree().indexOf("스크리닝 중..."), -1);

  listener({ action: "screening_progress", url: "https://current.example/job/1", stage: "screening", state: "running" });
  assert.ok(harness.textTree().indexOf("스크리닝 중...") >= 0);
  assert.strictEqual(api.__getState().progressStageText, "스크리닝 중...");
});

test("runtime completion handler ignores stale and missing urls", function () {
  var harness = createHarness({
    requestHandlers: {
      get_detail: function () { return makeDetailResponse(); },
      get_company_info: function () { return { status: "ok", data: { company_info_markdown: "# Company" } }; }
    }
  });
  var api = harness.api;
  api.__setState({ url: "https://current.example/job/1", detected: { platform: "wanted", jobId: "1" } });
  var listener = harness.runtimeListener();

  listener({ action: "screening_complete", data: { company_info: { status: "ready", attempted: true, persisted: true, completeness: 100, warning_code: null } } });
  listener({ action: "screening_complete", url: "https://other.example/job/2", data: { company_info: { status: "ready", attempted: true, persisted: true, completeness: 100, warning_code: null } } });

  assert.strictEqual(harness.sendMessageLog.length, 0);
  assert.strictEqual(api.__getState().companyInfoResult, null);
});

test("runtime failed handler ignores stale and missing urls", function () {
  var harness = createHarness();
  var api = harness.api;
  api.__setState({
    url: "https://current.example/job/1",
    detected: { platform: "wanted", jobId: "1" },
    rescreenMode: true,
    lastData: { record: { company: "Acme", position: "Backend Engineer", screening_verdict: "recommended" }, screening_markdown: "# before", jd_markdown: "# jd", is_fallback: false }
  });
  var listener = harness.runtimeListener();

  listener({ action: "screening_failed", message: "ignored" });
  listener({ action: "screening_failed", url: "https://other.example/job/2", message: "ignored" });

  assert.strictEqual(api.__getState().rescreenMode, true);
  assert.strictEqual(harness.textTree().indexOf("ignored"), -1);
});

test("runtime ready completion stores result and triggers detail and company refresh for current url", function () {
  var harness = createHarness({
    requestHandlers: {
      get_detail: function () { return makeDetailResponse(); },
      get_company_info: function () { return { status: "ok", data: { company_info_markdown: "# Company Markdown" } }; }
    }
  });
  var api = harness.api;
  api.__setState({ url: "https://current.example/job/1", detected: { platform: "wanted", jobId: "1" } });
  var listener = harness.runtimeListener();

  listener({
    action: "screening_complete",
    url: "https://current.example/job/1",
    data: {
      company_info: { status: "ready", attempted: true, persisted: true, completeness: 100, warning_code: null }
    }
  });

  assert.strictEqual(harness.sendMessageLog[0].action, "get_detail");
  assert.strictEqual(harness.sendMessageLog[1].action, "get_company_info");
  assert.strictEqual(api.__getState().companyInfoResult.status, "ready");
  assert.strictEqual(api.__getState().lastData.company_info_markdown, "# Company Markdown");
  assert.ok(harness.textTree().indexOf("회사 정보 준비 완료") >= 0);
  assert.ok(harness.textTree().indexOf("Acme") >= 0);
});

test("runtime warning completion keeps screening verdict visible without failure styling", function () {
  var harness = createHarness({
    requestHandlers: {
      get_detail: function () { return makeDetailResponse(); },
      get_company_info: function () { return { status: "ok", data: { company_info_markdown: "# Company Markdown" } }; }
    }
  });
  var api = harness.api;
  api.__setState({ url: "https://current.example/job/1", detected: { platform: "wanted", jobId: "1" } });
  var listener = harness.runtimeListener();

  listener({
    action: "screening_complete",
    url: "https://current.example/job/1",
    data: {
      company_info: { status: "warning", attempted: true, persisted: true, completeness: 62, warning_code: "below_threshold" }
    }
  });

  assert.strictEqual(api.__getState().companyInfoResult.warning_code, "below_threshold");
  assert.ok(harness.textTree().indexOf("회사 정보 보강 필요") >= 0);
  assert.ok(harness.textTree().indexOf("완성도 62%") >= 0);
  assert.ok(harness.textTree().indexOf("Acme") >= 0);
  assert.strictEqual(harness.textTree().indexOf("실패"), -1);
});

test("runtime completion requires current url and company_info to trigger refresh", function () {
  var harness = createHarness({
    requestHandlers: {
      get_detail: function () { return makeDetailResponse(); },
      get_company_info: function () { return { status: "ok", data: { company_info_markdown: "# Company Markdown" } }; }
    }
  });
  var api = harness.api;
  api.__setState({ url: "https://current.example/job/1", detected: { platform: "wanted", jobId: "1" } });
  var listener = harness.runtimeListener();

  listener({
    action: "screening_complete",
    url: "https://current.example/job/1",
    data: { screening_verdict: "recommended", verdict_capped: false }
  });

  assert.strictEqual(harness.sendMessageLog.length, 0);
  assert.strictEqual(api.__getState().companyInfoResult, null);
});

console.log(passed + "/" + (passed + failed) + " passed");
if (failed > 0) process.exit(1);
