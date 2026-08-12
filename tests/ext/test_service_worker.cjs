"use strict";

var assert = require("assert");
var fs = require("fs");
var path = require("path");
var vm = require("vm");

var workerPath = path.join(__dirname, "..", "..", "ext", "background", "service-worker.js");
var code = fs.readFileSync(workerPath, "utf-8");

function createChromeMock(events) {
  return {
    runtime: {
      id: "test-runtime",
      lastError: null,
      onMessage: { addListener: function () {} },
      onInstalled: { addListener: function () {} },
      onStartup: { addListener: function () {} },
      connectNative: function () { throw new Error("not connected"); },
      getURL: function (value) { return "chrome-extension://" + value; },
      sendMessage: function (payload, callback) {
        events.runtimeMessages.push(payload);
        if (callback) callback();
      }
    },
    tabs: {
      sendMessage: function (tabId, payload, callback) {
        events.tabMessages.push({ tabId: tabId, payload: payload });
        if (callback) callback();
      },
      query: function (_query, callback) {
        events.tabQueries.push(true);
        callback([]);
      }
    },
    notifications: {
      create: function (id, payload) {
        events.notifications.push({ id: id, payload: payload });
      },
      clear: function () {},
      onClicked: { addListener: function () {} }
    },
    action: {
      getBadgeText: function () { return Promise.resolve(""); },
      setBadgeBackgroundColor: function () { return Promise.resolve(); },
      setBadgeText: function () { return Promise.resolve(); }
    },
    sidePanel: {
      setPanelBehavior: function () { return Promise.resolve(); }
    },
    alarms: {
      create: function () {},
      onAlarm: { addListener: function () {} }
    },
    storage: {
      session: {
        set: function (payload) {
          events.storageSets.push(payload);
          return Promise.resolve();
        },
        get: function () { return Promise.resolve({ pending_screenings: {} }); }
      }
    }
  };
}

function loadHarness() {
  var events = {
    runtimeMessages: [],
    tabMessages: [],
    tabQueries: [],
    notifications: [],
    storageSets: []
  };
  var sandbox = {
    console: console,
    Promise: Promise,
    Date: Date,
    Map: Map,
    setTimeout: function () {},
    clearTimeout: function () {},
    setInterval: function () {},
    clearInterval: function () {},
    module: { exports: {} },
    globalThis: {},
    chrome: createChromeMock(events)
  };
  vm.runInNewContext(code, sandbox, { filename: workerPath });
  return { api: sandbox.module.exports, events: events };
}

var passed = 0;
var failed = 0;
var tests = [];

function test(name, fn) {
  tests.push({ name: name, fn: fn });
}

test("valid progress routes without resolving unrelated pending request", function () {
  var harness = loadHarness();
  var api = harness.api;
  var resolved = false;
  api.__setPendingRequest(91, function () { resolved = true; });
  api.__setPendingScreening("track-1", { url: "https://example.com/jobs/1", tabId: 7 });

  api.handleNativeMessage({
    type: "screening_progress",
    tracking_id: "track-1",
    stage: "company_info",
    state: "checking"
  });

  assert.strictEqual(resolved, false);
  assert.strictEqual(api.__hasPendingRequest(91), true);
  assert.strictEqual(api.__hasPendingScreening("track-1"), true);
  assert.strictEqual(JSON.stringify(harness.events.runtimeMessages), JSON.stringify([
    {
      action: "screening_progress",
      tracking_id: "track-1",
      stage: "company_info",
      state: "checking",
      url: "https://example.com/jobs/1"
    }
  ]));
  assert.strictEqual(JSON.stringify(harness.events.tabMessages), JSON.stringify([
    {
      tabId: 7,
      payload: {
        action: "screening_progress",
        tracking_id: "track-1",
        stage: "company_info",
        state: "checking",
        url: "https://example.com/jobs/1"
      }
    }
  ]));
});

test("unknown or invalid progress pushes are ignored without active-tab fallback", function () {
  var harness = loadHarness();
  var api = harness.api;
  api.__setPendingScreening("track-1", { url: "https://example.com/jobs/1", tabId: 7 });

  var invalidMessages = [
    null,
    { type: "screening_progress", tracking_id: "missing", stage: "company_info", state: "checking" },
    { type: "screening_progress", tracking_id: 1, stage: "company_info", state: "checking" },
    { type: "screening_progress", tracking_id: "track-1", stage: "unknown", state: "checking" },
    { type: "screening_progress", tracking_id: "track-1", stage: "company_info", state: "done" },
    { type: "screening_progress", tracking_id: "track-1", stage: "screening", state: "enriching" },
    { type: "screening_progress", tracking_id: "track-1", stage: "company_info", state: "checking", provider: "private" }
  ];

  invalidMessages.forEach(function (message) {
    api.handleNativeMessage(message);
  });

  assert.strictEqual(harness.events.runtimeMessages.length, 0);
  assert.strictEqual(harness.events.tabMessages.length, 0);
  assert.strictEqual(harness.events.tabQueries.length, 0);
  assert.strictEqual(api.__hasPendingScreening("track-1"), true);
});

test("valid progress retains pending tracking and forwards correlated url", function () {
  var harness = loadHarness();
  var api = harness.api;
  api.__setPendingScreening("track-2", { url: "https://example.com/jobs/2", tabId: 12 });

  api.handleNativeMessage({
    type: "screening_progress",
    tracking_id: "track-2",
    stage: "screening",
    state: "running"
  });

  assert.strictEqual(api.__getPendingScreening("track-2").url, "https://example.com/jobs/2");
  assert.strictEqual(harness.events.runtimeMessages[0].url, "https://example.com/jobs/2");
});

test("invalid company-info completion combinations are ignored", function () {
  var harness = loadHarness();
  var api = harness.api;
  api.__setPendingScreening("track-3", { url: "https://example.com/jobs/3", tabId: 3 });

  var invalidCompletions = [
    {
      type: "screening_complete",
      tracking_id: "track-3",
      data: { company_info: { status: "warning", attempted: true, persisted: false, completeness: 55, warning_code: "missing" } }
    },
    {
      type: "screening_complete",
      tracking_id: "track-3",
      data: { company_info: { status: "warning", attempted: true, persisted: false, completeness: null, warning_code: "below_threshold" } }
    },
    {
      type: "screening_complete",
      tracking_id: "track-3",
      data: { company_info: { status: "ready", attempted: true, persisted: true, completeness: null, warning_code: null } }
    },
    {
      type: "screening_complete",
      tracking_id: "track-3",
      data: {
        company_info: {
          status: "ready",
          attempted: true,
          persisted: true,
          completeness: 100,
          warning_code: null,
          company_info_markdown: "# private"
        }
      }
    }
  ];

  invalidCompletions.forEach(function (message) {
    api.handleNativeMessage(message);
    assert.strictEqual(api.__hasPendingScreening("track-3"), true);
  });

  assert.strictEqual(harness.events.runtimeMessages.length, 0);
  assert.strictEqual(harness.events.tabMessages.length, 0);
  assert.strictEqual(harness.events.tabQueries.length, 0);
});

test("stale completion is ignored without removing pending requests or using fallback", function () {
  var harness = loadHarness();
  var api = harness.api;
  api.__setPendingRequest(17, function () {});

  api.handleNativeMessage({
    type: "screening_complete",
    tracking_id: "unknown-track",
    data: {
      verdict: "recommended",
      company_info: { status: "ready", attempted: true, persisted: true, completeness: 100, warning_code: null }
    }
  });

  assert.strictEqual(api.__hasPendingRequest(17), true);
  assert.strictEqual(harness.events.runtimeMessages.length, 0);
  assert.strictEqual(harness.events.tabMessages.length, 0);
  assert.strictEqual(harness.events.tabQueries.length, 0);
});

test("missing company_info completion is ignored", function () {
  var harness = loadHarness();
  var api = harness.api;
  api.__setPendingScreening("track-4", { url: "https://example.com/jobs/4", tabId: 4 });

  api.handleNativeMessage({
    type: "screening_complete",
    tracking_id: "track-4",
    data: { screening_verdict: "recommended", verdict_capped: false }
  });

  assert.strictEqual(api.__hasPendingScreening("track-4"), true);
  assert.strictEqual(harness.events.runtimeMessages.length, 0);
  assert.strictEqual(harness.events.tabMessages.length, 0);
});

test("valid completion strips extra top-level private fields and forwards allowlisted data only", function () {
  var harness = loadHarness();
  var api = harness.api;
  api.__setPendingScreening("track-5", { url: "https://example.com/jobs/5", tabId: 5 });

  api.handleNativeMessage({
    type: "screening_complete",
    tracking_id: "track-5",
    data: {
      company: "Acme",
      position: "Backend Engineer",
      verdict_label: "지원 추천",
      screening_verdict: "recommended",
      verdict_capped: false,
      screening_provider: "secret-provider",
      jd_markdown: "# JD",
      company_info_path: "/private/company.md",
      source_url: "https://private.example/source",
      company_info: {
        status: "ready",
        attempted: true,
        persisted: true,
        completeness: 100,
        warning_code: null
      }
    }
  });

  return Promise.resolve().then(function () {
    assert.strictEqual(api.__hasPendingScreening("track-5"), false);
    assert.strictEqual(JSON.stringify(harness.events.runtimeMessages), JSON.stringify([
      {
        action: "screening_complete",
        data: {
          company: "Acme",
          position: "Backend Engineer",
          verdict_label: "지원 추천",
          screening_verdict: "recommended",
          verdict_capped: false,
          company_info: {
            status: "ready",
            attempted: true,
            persisted: true,
            completeness: 100,
            warning_code: null
          }
        },
        url: "https://example.com/jobs/5"
      }
    ]));
    assert.strictEqual(harness.events.runtimeMessages[0].data.screening_provider, undefined);
    assert.strictEqual(harness.events.runtimeMessages[0].data.jd_markdown, undefined);
    assert.strictEqual(harness.events.runtimeMessages[0].data.company_info_path, undefined);
    assert.strictEqual(harness.events.runtimeMessages[0].data.source_url, undefined);
    assert.strictEqual(harness.events.notifications.length, 1);
    assert.strictEqual(harness.events.notifications[0].payload.title, "Acme — Backend Engineer");
    assert.strictEqual(harness.events.notifications[0].payload.message, "지원 추천");
  });
});

(async function () {
  for (var i = 0; i < tests.length; i++) {
    try {
      await tests[i].fn();
      passed++;
    } catch (error) {
      failed++;
      console.error("FAIL: " + tests[i].name);
      console.error("  " + error.message);
    }
  }
  console.log(passed + "/" + (passed + failed) + " passed");
  if (failed > 0) process.exit(1);
})();
