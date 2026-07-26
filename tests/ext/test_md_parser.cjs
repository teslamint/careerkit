"use strict";

var assert = require("assert");
var path = require("path");
var fs = require("fs");
var vm = require("vm");

var parserPath = path.join(__dirname, "..", "..", "ext", "sidepanel", "md-parser.js");
var code = fs.readFileSync(parserPath, "utf-8");
var sandbox = { globalThis: {}, module: { exports: {} } };
vm.runInNewContext(code, sandbox);
var parse = sandbox.globalThis.parseScreeningMarkdown;

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

// --- Normal path ---

test("4-column matching table", function () {
  var md = [
    "## 이력/경험 매칭",
    "",
    "| 요건 | 구분 | 대조 | 근거 |",
    "|------|------|------|------|",
    "| 백엔드 5년 | 필수 | 충족 | 15년차 |",
    "| Java 경험 | 필수 | 충족 | Spring Boot |",
  ].join("\n");
  var result = parse(md);
  assert.strictEqual(result.hasParsedContent, true);
  assert.strictEqual(result.sections.length, 1);
  var table = result.sections[0].elements[0];
  assert.strictEqual(table.type, "table");
  assert.deepEqual(table.headers, ["요건", "구분", "대조", "근거"]);
  assert.strictEqual(table.rows.length, 2);
  assert.deepEqual(table.rows[0], ["백엔드 5년", "필수", "충족", "15년차"]);
});

test("3-column screening result table", function () {
  var md = [
    "## 스크리닝 결과",
    "",
    "| 평가 항목 | 결과 | 근거 |",
    "|------|:---:|------|",
    "| 포지션 도메인 | 적합 | 백엔드 중심 |",
  ].join("\n");
  var result = parse(md);
  var table = result.sections[0].elements[0];
  assert.strictEqual(table.type, "table");
  assert.strictEqual(table.headers.length, 3);
  assert.strictEqual(table.rows.length, 1);
});

test("2-column basic info table", function () {
  var md = [
    "## 기본 정보",
    "",
    "| 항목 | 내용 |",
    "|------|------|",
    "| 회사명 | 페이히어 |",
    "| 포지션 | 서버 엔지니어 |",
  ].join("\n");
  var result = parse(md);
  var table = result.sections[0].elements[0];
  assert.strictEqual(table.type, "table");
  assert.strictEqual(table.headers.length, 2);
  assert.strictEqual(table.rows.length, 2);
});

test("dynamic heading names — sections split by any ## heading", function () {
  var md = [
    "## 한 줄 요약",
    "요약 텍스트",
    "## 팀 소개",
    "팀 설명",
    "## 6대 최종 판단",
    "판단 내용",
  ].join("\n");
  var result = parse(md);
  assert.strictEqual(result.sections.length, 3);
  assert.strictEqual(result.sections[0].heading, "한 줄 요약");
  assert.strictEqual(result.sections[1].heading, "팀 소개");
  assert.strictEqual(result.sections[2].heading, "6대 최종 판단");
  assert.strictEqual(result.hasParsedContent, true);
});

test("verdict block with prefix match — emoji and bold variants", function () {
  var md1 = "### 최종 판정: 지원 보류";
  var r1 = parse(md1);
  assert.strictEqual(r1.sections[0].elements[0].type, "verdict");
  assert.strictEqual(r1.sections[0].elements[0].text, "최종 판정: 지원 보류");

  var md2 = "### 최종 판정 🔴 지원 비추천";
  var r2 = parse(md2);
  assert.strictEqual(r2.sections[0].elements[0].type, "verdict");

  var md3 = "### 최종 판정(결론)";
  var r3 = parse(md3);
  assert.strictEqual(r3.sections[0].elements[0].type, "verdict");
});

test("list items parsed as list type", function () {
  var md = [
    "## 핵심 근거",
    "",
    "- 백엔드 도메인 적합",
    "- 기술 매칭 강함",
    "- 역할 범위 넓음",
  ].join("\n");
  var result = parse(md);
  var list = result.sections[0].elements[0];
  assert.strictEqual(list.type, "list");
  assert.strictEqual(list.items.length, 3);
  assert.strictEqual(list.items[0], "백엔드 도메인 적합");
});

test("mixed content — text paragraphs between tables and lists", function () {
  var md = [
    "## 최종 판정",
    "",
    "### 최종 판정: 지원 보류",
    "",
    "백엔드 도메인 자체는 적합하다.",
    "",
    "- 첫 번째 근거",
    "- 두 번째 근거",
  ].join("\n");
  var result = parse(md);
  var els = result.sections[0].elements;
  assert.strictEqual(els[0].type, "verdict");
  assert.strictEqual(els[1].type, "text");
  assert.strictEqual(els[2].type, "list");
});

// --- Edge cases ---

test("empty markdown returns fallback", function () {
  var r1 = parse("");
  assert.strictEqual(r1.hasParsedContent, false);
  assert.strictEqual(r1.sections.length, 0);

  var r2 = parse(null);
  assert.strictEqual(r2.hasParsedContent, false);

  var r3 = parse(undefined);
  assert.strictEqual(r3.hasParsedContent, false);
});

test("no headings + no tables = fallback", function () {
  var md = "그냥 텍스트만 있는 문서\n\n추가 텍스트";
  var result = parse(md);
  assert.strictEqual(result.hasParsedContent, false);
  assert.strictEqual(result.sections.length, 1);
  assert.strictEqual(result.sections[0].heading, null);
});

test("table-only document without headings", function () {
  var md = [
    "| A | B |",
    "|---|---|",
    "| 1 | 2 |",
  ].join("\n");
  var result = parse(md);
  assert.strictEqual(result.hasParsedContent, true);
  assert.strictEqual(result.sections[0].heading, null);
  assert.strictEqual(result.sections[0].elements[0].type, "table");
});

test("column count mismatch — mismatched rows rendered as text after table", function () {
  var md = [
    "## 테스트",
    "| A | B | C |",
    "|---|---|---|",
    "| 1 | 2 | 3 |",
    "| x | y |",
    "| 4 | 5 | 6 |",
  ].join("\n");
  var result = parse(md);
  var els = result.sections[0].elements;
  assert.strictEqual(els[0].type, "table");
  assert.strictEqual(els[0].rows.length, 2);
  assert.strictEqual(els[1].type, "text");
  assert.ok(els[1].content.indexOf("x") >= 0);
});

test("column mismatch >= 50% triggers table fallback to text", function () {
  var md = [
    "## 테스트",
    "| A | B | C |",
    "|---|---|---|",
    "| x | y |",
    "| p | q |",
  ].join("\n");
  var result = parse(md);
  var el = result.sections[0].elements[0];
  assert.strictEqual(el.type, "text");
});

test("duplicate ## headings create separate sections", function () {
  var md = [
    "## 결과",
    "첫 번째",
    "## 결과",
    "두 번째",
  ].join("\n");
  var result = parse(md);
  assert.strictEqual(result.sections.length, 2);
  assert.strictEqual(result.sections[0].heading, "결과");
  assert.strictEqual(result.sections[1].heading, "결과");
});

test("headings without content create empty sections", function () {
  var md = [
    "## 빈 섹션",
    "## 다음 섹션",
    "내용",
  ].join("\n");
  var result = parse(md);
  assert.strictEqual(result.sections.length, 2);
  assert.strictEqual(result.sections[0].elements.length, 0);
});

// --- Security ---

test("script tags in cells rendered as literal text", function () {
  var md = [
    "## 테스트",
    "| A | B |",
    "|---|---|",
    '| <script>alert(1)</script> | normal |',
  ].join("\n");
  var result = parse(md);
  var table = result.sections[0].elements[0];
  assert.strictEqual(table.rows[0][0], "<script>alert(1)</script>");
});

test("img onerror in cells preserved as text", function () {
  var md = [
    "## 테스트",
    "| A |",
    "|---|",
    '| <img onerror="alert(1)"> |',
  ].join("\n");
  var result = parse(md);
  var table = result.sections[0].elements[0];
  assert.ok(table.rows[0][0].indexOf("<img") >= 0);
});

// --- Summary ---

console.log("\n" + passed + " passed, " + failed + " failed");
if (failed > 0) process.exit(1);
