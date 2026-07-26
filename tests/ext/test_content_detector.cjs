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
