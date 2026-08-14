"use strict";

var assert = require("assert");
var path = require("path");
var fs = require("fs");
var vm = require("vm");

var badgePath = path.join(__dirname, "..", "..", "ext", "content", "badge.js");
var code = fs.readFileSync(badgePath, "utf-8");

function createStyle() {
  return {
    cssText: "",
    display: "",
    background: "",
    setProperty: function (name, value) { this[name] = value; }
  };
}

function createElement(tagName) {
  var node = {
    tagName: tagName || "div",
    className: "",
    type: "",
    style: createStyle(),
    children: [],
    parentNode: null,
    listeners: {},
    shadowRoot: null,
    _text: "",
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
    addEventListener: function (name, fn) {
      if (!this.listeners[name]) this.listeners[name] = [];
      this.listeners[name].push(fn);
    },
    dispatch: function (name) {
      var fns = this.listeners[name] || [];
      for (var i = 0; i < fns.length; i++) fns[i]();
    },
    attachShadow: function () {
      this.shadowRoot = createElement("#shadow-root");
      return this.shadowRoot;
    }
  };
  // render() clears the previous node with `container.textContent = ""`, so the
  // setter must drop children the way a real DOM node does.
  Object.defineProperty(node, "textContent", {
    get: function () { return this._text; },
    set: function (value) {
      this._text = value === null || value === undefined ? "" : String(value);
      this.children.length = 0;
    }
  });
  return node;
}

function textTree(node) {
  if (!node) return "";
  var parts = [];
  function walk(current) {
    if (current.textContent) parts.push(current.textContent);
    for (var i = 0; i < current.children.length; i++) walk(current.children[i]);
  }
  walk(node);
  return parts.join("\n");
}

function findAll(node, predicate) {
  var found = [];
  function walk(current) {
    if (predicate(current)) found.push(current);
    for (var i = 0; i < current.children.length; i++) walk(current.children[i]);
  }
  if (node) walk(node);
  return found;
}

function createHarness(options) {
  options = options || {};
  var url = options.url || "https://jobs.example.test/postings/1";
  var handlers = options.handlers || {};
  var sendMessageLog = [];
  var runtimeListener = null;
  var intervals = {};
  var nextIntervalId = 1;

  var documentMock = {
    documentElement: createElement("html"),
    body: createElement("body"),
    createElement: function (tag) { return createElement(tag); }
  };

  var chromeMock = {
    runtime: {
      id: "test",
      lastError: null,
      onMessage: { addListener: function (listener) { runtimeListener = listener; } },
      sendMessage: function (message, callback) {
        sendMessageLog.push(message);
        var handler = handlers[message.action];
        var response = handler ? handler(message, sendMessageLog) : { status: "ok", data: null };
        if (callback) callback(response);
      }
    }
  };

  var sandbox = {
    document: documentMock,
    location: { href: url },
    chrome: chromeMock,
    detectJobPosting: function (href) {
      if (!href) return null;
      return { platform: "example", jobId: "1" };
    },
    // Interval ids start at 1: badge.js gates cleanup on truthiness.
    setInterval: function (fn, ms) {
      var id = nextIntervalId++;
      intervals[id] = { fn: fn, ms: ms };
      return id;
    },
    clearInterval: function (id) { delete intervals[id]; }
  };

  vm.runInNewContext(code, sandbox);

  function container() {
    var host = documentMock.documentElement.children[0];
    if (!host || !host.shadowRoot) return null;
    // shadowRoot children: [style, .ck-container]
    return host.shadowRoot.children[1] || null;
  }

  function intervalsWithMs(ms) {
    var ids = [];
    for (var id in intervals) {
      if (Object.prototype.hasOwnProperty.call(intervals, id) && intervals[id].ms === ms) ids.push(id);
    }
    return ids;
  }

  return {
    container: container,
    text: function () { return textTree(container()); },
    buttons: function () {
      return findAll(container(), function (n) { return n.tagName === "button"; });
    },
    click: function (label) {
      var buttons = findAll(container(), function (n) {
        return n.tagName === "button" && (label === undefined || n.textContent === label);
      });
      assert.ok(buttons.length > 0, "no button matching " + label);
      buttons[0].dispatch("click");
    },
    tickPoll: function (times) {
      for (var i = 0; i < (times || 1); i++) {
        var ids = intervalsWithMs(3000);
        for (var j = 0; j < ids.length; j++) {
          if (intervals[ids[j]]) intervals[ids[j]].fn();
        }
      }
    },
    pollCount: function () { return intervalsWithMs(3000).length; },
    runtimeListener: function () { return runtimeListener; },
    sendMessageLog: sendMessageLog
  };
}

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

var SET_ASIDE_LABEL = "사전 필터 제외 기록";

function okResponse(data) {
  return { status: "ok", data: data };
}

function setAsideRecord() {
  return { screening_verdict: null, verdict_capped: false, prescreen_reason: "title_exclude" };
}

function unknownRecord() {
  return { screening_verdict: null, verdict_capped: false, prescreen_reason: null };
}

// --- renderFromRecord ---

test("verdict-bearing record renders its badge exactly as before", function () {
  var harness = createHarness({
    handlers: {
      lookup: function () { return okResponse({ screening_verdict: "recommended", verdict_capped: false }); }
    }
  });
  var text = harness.text();
  assert.ok(text.indexOf("🟢") >= 0, text);
  assert.ok(text.indexOf("추천") >= 0, text);
  assert.strictEqual(harness.buttons().length, 0);
});

test("set-aside record renders the recorded state, not the screen button", function () {
  var harness = createHarness({
    handlers: { lookup: function () { return okResponse(setAsideRecord()); } }
  });
  var text = harness.text();
  assert.ok(text.indexOf(SET_ASIDE_LABEL) >= 0, text);
  assert.strictEqual(text.indexOf("⚪ 스크리닝"), -1, text);
  assert.strictEqual(harness.buttons().length, 0);
});

test("record with neither verdict nor reason still offers the screen button", function () {
  var harness = createHarness({
    handlers: { lookup: function () { return okResponse(unknownRecord()); } }
  });
  assert.ok(harness.text().indexOf("⚪ 스크리닝") >= 0, harness.text());
});

// --- pollForVerdict ---

test("polling stops on a pre-screen reason and renders the recorded state", function () {
  var stored = null;
  var harness = createHarness({
    handlers: {
      lookup: function () { return okResponse(stored); },
      collect: function () { return { status: "accepted", data: null }; }
    }
  });
  harness.click("수집");
  assert.strictEqual(harness.pollCount(), 1);

  stored = setAsideRecord();
  harness.tickPoll(1);

  assert.ok(harness.text().indexOf(SET_ASIDE_LABEL) >= 0, harness.text());
  assert.strictEqual(harness.pollCount(), 0);

  // Well past maxAttempts: a stopped poll cannot reach the timeout error.
  harness.tickPoll(130);
  assert.ok(harness.text().indexOf(SET_ASIDE_LABEL) >= 0, harness.text());
  assert.strictEqual(harness.text().indexOf("시간 초과"), -1, harness.text());
});

test("record with neither verdict nor reason still polls to the timeout error", function () {
  var stored = null;
  var harness = createHarness({
    handlers: {
      lookup: function () { return okResponse(stored); },
      collect: function () { return { status: "accepted", data: null }; }
    }
  });
  harness.click("수집");

  // A record exists, but it carries no decision of either kind: still unknown.
  stored = unknownRecord();
  harness.tickPoll(120);
  assert.strictEqual(harness.text().indexOf("시간 초과"), -1, harness.text());

  harness.tickPoll(1);
  assert.ok(harness.text().indexOf("시간 초과") >= 0, harness.text());
  assert.strictEqual(harness.pollCount(), 0);
});

// --- startCollectAndPoll ---

test("duplicate collect reply carrying a reason renders the recorded state", function () {
  var stored = null;
  var harness = createHarness({
    handlers: {
      lookup: function () { return okResponse(stored); },
      collect: function () {
        stored = setAsideRecord();
        return { status: "duplicate", action: "collect", data: setAsideRecord() };
      }
    }
  });
  harness.click("수집");

  assert.ok(harness.text().indexOf(SET_ASIDE_LABEL) >= 0, harness.text());
  assert.strictEqual(harness.text().indexOf("스크리닝 중"), -1, harness.text());

  harness.tickPoll(130);
  assert.ok(harness.text().indexOf(SET_ASIDE_LABEL) >= 0, harness.text());
  assert.strictEqual(harness.text().indexOf("시간 초과"), -1, harness.text());
});

// --- runtime listener (host -> service worker -> content script) ---

// This test claims the live push path, so its fixture must be what the service
// worker actually emits — not a record shape hand-written here. An earlier
// version injected `prescreen_reason` directly and so passed while the real
// COMPLETION_DATA_ALLOWLIST was dropping that field: the fixture masked the
// defect it was written to catch. The payload below is therefore built the way
// the host builds it and then run through the real `sanitizeCompletionData`.

function loadServiceWorker() {
  var workerPath = path.join(__dirname, "..", "..", "ext", "background", "service-worker.js");
  var noop = function () {};
  var chromeMock = {
    runtime: {
      connectNative: function () { return null; },
      getURL: function (value) { return value; },
      sendMessage: noop,
      onMessage: { addListener: noop },
      onStartup: { addListener: noop },
      onInstalled: { addListener: noop },
      lastError: null
    },
    storage: {
      session: {
        get: function () { return Promise.resolve({}); },
        set: function () { return Promise.resolve(); }
      }
    },
    alarms: { create: noop, onAlarm: { addListener: noop } },
    notifications: { create: noop, onClicked: { addListener: noop }, onClosed: { addListener: noop } },
    action: { setBadgeText: noop, setBadgeBackgroundColor: noop },
    tabs: { query: noop, sendMessage: noop },
    sidePanel: { setPanelBehavior: function () { return Promise.resolve(); } }
  };
  var sandbox = {
    console: console,
    Promise: Promise,
    Date: Date,
    Map: Map,
    setTimeout: noop,
    clearTimeout: noop,
    setInterval: noop,
    clearInterval: noop,
    module: { exports: {} },
    globalThis: {},
    chrome: chromeMock
  };
  vm.runInNewContext(fs.readFileSync(workerPath, "utf-8"), sandbox, { filename: workerPath });
  return sandbox.module.exports;
}

// `careerkit_host.py` sends `record.to_dict()` plus `verdict_label` and
// `company_info`. A set-aside record carries a null verdict and a reason.
function hostSetAsidePush() {
  return {
    url: "https://jobs.example.test/postings/1",
    platform: "example",
    job_id: "1",
    company: "Acme",
    position: "Backend Engineer",
    screening_verdict: null,
    verdict_capped: false,
    prescreen_reason: "title_exclude",
    verdict_label: SET_ASIDE_LABEL,
    company_info: {
      status: "ready",
      attempted: true,
      persisted: true,
      completeness: 100,
      warning_code: null
    }
  };
}

test("screening_complete for a set-aside record renders the recorded state", function () {
  var worker = loadServiceWorker();
  var sanitized = worker.sanitizeCompletionData(hostSetAsidePush());

  // Named first so a red run reports the strip, not only the wrong badge.
  assert.ok(sanitized, "service worker rejected the host's set-aside payload");
  assert.strictEqual(
    sanitized.prescreen_reason,
    "title_exclude",
    "service worker dropped prescreen_reason: " + JSON.stringify(sanitized)
  );

  var harness = createHarness({
    handlers: { lookup: function () { return okResponse(null); } }
  });
  var listener = harness.runtimeListener();

  listener({
    action: "screening_complete",
    url: "https://jobs.example.test/postings/1",
    data: sanitized
  });

  var text = harness.text();
  assert.strictEqual(text, "⏭\n" + SET_ASIDE_LABEL, text);
  assert.strictEqual(harness.buttons().length, 0);
});

console.log(passed + "/" + (passed + failed) + " passed");
if (failed > 0) process.exit(1);
