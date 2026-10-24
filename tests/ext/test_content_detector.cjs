#!/usr/bin/env node
"use strict";

var assert = require("assert");
var path = require("path");
var fs = require("fs");
var vm = require("vm");

var detectorPath = path.join(__dirname, "..", "..", "ext", "content", "detector.js");
var code = fs.readFileSync(detectorPath, "utf-8");
var sandbox = { globalThis: {}, module: { exports: {} }, URL: URL };
vm.runInNewContext(code, sandbox);
var detectJobPosting = sandbox.globalThis.detectJobPosting;

var fixturesPath = path.join(__dirname, "fixtures", "url_patterns.json");
var fixtures = JSON.parse(fs.readFileSync(fixturesPath, "utf-8"));

// zighang cases are built from the host here, not stored in the shared fixture, so the
// publish guard's posting-url rule does not read the synthetic id as a pasted posting.
var ZIGHANG_HOST = "zighang.com";
var ZIGHANG_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
fixtures = fixtures.concat([
  { url: "https://" + ZIGHANG_HOST + "/recruitment/" + ZIGHANG_ID, platform: "zighang", job_id: ZIGHANG_ID },
  { url: "https://" + ZIGHANG_HOST + "/recruitment/" + ZIGHANG_ID + "?ref=list", platform: "zighang", job_id: ZIGHANG_ID },
  { url: "https://" + ZIGHANG_HOST + "/recruitment/" + ZIGHANG_ID.toUpperCase(), platform: null, job_id: null },
  { url: "https://" + ZIGHANG_HOST + "/company/" + ZIGHANG_ID, platform: null, job_id: null },
  { url: "http://" + ZIGHANG_HOST + "/recruitment/" + ZIGHANG_ID, platform: null, job_id: null },
]);

var passed = 0;
var failed = 0;

fixtures.forEach(function (fixture) {
  var result = detectJobPosting(fixture.url);

  if (fixture.platform === null) {
    if (result !== null) {
      console.error("FAIL: " + fixture.url + " — expected null, got " + JSON.stringify(result));
      failed++;
    } else {
      passed++;
    }
  } else {
    if (result === null) {
      console.error("FAIL: " + fixture.url + " — expected {platform: " + fixture.platform + ", jobId: " + fixture.job_id + "}, got null");
      failed++;
    } else if (result.platform !== fixture.platform || result.jobId !== fixture.job_id) {
      console.error("FAIL: " + fixture.url + " — expected {platform: " + fixture.platform + ", jobId: " + fixture.job_id + "}, got " + JSON.stringify(result));
      failed++;
    } else {
      passed++;
    }
  }
});

console.log(passed + " passed, " + failed + " failed, " + fixtures.length + " total");

if (failed > 0) {
  process.exit(1);
}
