"use strict";

var assert = require("assert");
var path = require("path");
var fs = require("fs");
var vm = require("vm");

var panelPath = path.join(__dirname, "..", "..", "ext", "sidepanel", "panel.js");
var code = fs.readFileSync(panelPath, "utf-8");

function mockElement() {
  return {
    className: "", textContent: "", id: "", hidden: false, title: "",
    style: { cursor: "", setProperty: function () {} },
    children: [],
    appendChild: function (c) { this.children.push(c); return c; },
    removeChild: function (c) { var i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); },
    setAttribute: function () {},
    getAttribute: function () { return null; },
    addEventListener: function () {},
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    closest: function () { return null; },
    classList: { toggle: function () {} },
    get firstChild() { return this.children[0] || null; }
  };
}

var sandbox = {
  globalThis: {},
  module: { exports: {} },
  document: {
    getElementById: function () { return mockElement(); },
    createElement: function () { return mockElement(); },
    createDocumentFragment: function () { return mockElement(); },
    body: mockElement()
  },
  chrome: {
    runtime: { onMessage: { addListener: function () {} }, id: "test", sendMessage: function () {} },
    tabs: { onActivated: { addListener: function () {} }, onUpdated: { addListener: function () {} }, query: function (q, cb) { if (cb) cb([]); } },
    sidePanel: { setPanelBehavior: function () {} },
    alarms: { create: function () {}, onAlarm: { addListener: function () {} } },
    action: { getBadgeText: function () {}, setBadgeBackgroundColor: function () {}, setBadgeText: function () {} },
    notifications: { create: function () {} },
    storage: { session: { get: function () { return Promise.resolve({}); }, set: function () { return Promise.resolve(); } } }
  },
  detectJobPosting: function () { return null; },
  parseScreeningMarkdown: function () { return { sections: [], hasParsedContent: false }; },
  setTimeout: function () {},
  clearTimeout: function () {},
  setInterval: function () {},
  clearInterval: function () {},
  navigator: { clipboard: null },
  Promise: Promise
};
vm.runInNewContext(code, sandbox);

var shouldOfferRescreen = sandbox.globalThis.shouldOfferRescreen;

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

test("returns 'icon' for not_recommended without fallback/capped", function () {
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

var buildTabBar = sandbox.globalThis.buildTabBar;

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

console.log(passed + "/" + (passed + failed) + " passed");
if (failed > 0) process.exit(1);
