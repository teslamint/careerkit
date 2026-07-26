(function () {
  "use strict";

  function splitTableRow(line) {
    var trimmed = line.trim();
    if (trimmed.charAt(0) === "|") trimmed = trimmed.substring(1);
    if (trimmed.charAt(trimmed.length - 1) === "|") trimmed = trimmed.substring(0, trimmed.length - 1);
    return trimmed.split("|").map(function (cell) { return cell.trim(); });
  }

  function isSeparatorRow(cells) {
    return cells.every(function (cell) { return /^[-:]+$/.test(cell); });
  }

  function isTableRow(line) {
    var t = line.trim();
    return t.length > 1 && t.charAt(0) === "|" && t.charAt(t.length - 1) === "|";
  }

  function flushTable(headerCells, dataRows) {
    if (!headerCells || headerCells.length === 0) return null;
    var colCount = headerCells.length;
    var validRows = [];
    var mismatchedRows = [];
    for (var i = 0; i < dataRows.length; i++) {
      if (dataRows[i].length === colCount) {
        validRows.push(dataRows[i]);
      } else {
        mismatchedRows.push(dataRows[i]);
      }
    }
    var total = dataRows.length;
    if (total > 0 && mismatchedRows.length / total >= 0.5) {
      return null;
    }
    return { type: "table", headers: headerCells, rows: validRows, mismatchedRows: mismatchedRows };
  }

  function parseScreeningMarkdown(markdown) {
    if (!markdown || typeof markdown !== "string") {
      return { sections: [], hasParsedContent: false };
    }

    var lines = markdown.split("\n");
    var sections = [];
    var currentHeading = null;
    var currentElements = [];
    var headingCount = 0;
    var tableCount = 0;

    var tableState = null; // null | "header" | "separator" | "data"
    var tableHeaderCells = null;
    var tableDataRows = [];

    function finishTable() {
      if (tableHeaderCells) {
        var table = flushTable(tableHeaderCells, tableDataRows);
        if (table) {
          currentElements.push(table);
          tableCount++;
          for (var j = 0; j < table.mismatchedRows.length; j++) {
            currentElements.push({ type: "text", content: "| " + table.mismatchedRows[j].join(" | ") + " |" });
          }
        } else {
          var fallback = [];
          fallback.push("| " + tableHeaderCells.join(" | ") + " |");
          for (var i = 0; i < tableDataRows.length; i++) {
            fallback.push("| " + tableDataRows[i].join(" | ") + " |");
          }
          currentElements.push({ type: "text", content: fallback.join("\n") });
        }
      }
      tableState = null;
      tableHeaderCells = null;
      tableDataRows = [];
    }

    function finishSection() {
      if (currentHeading !== null || currentElements.length > 0) {
        sections.push({ heading: currentHeading, elements: currentElements });
      }
      currentHeading = null;
      currentElements = [];
    }

    var listItems = [];

    function flushList() {
      if (listItems.length > 0) {
        currentElements.push({ type: "list", items: listItems.slice() });
        listItems = [];
      }
    }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];

      if (line.indexOf("## ") === 0) {
        finishTable();
        flushList();
        finishSection();
        currentHeading = line.substring(3).trim();
        headingCount++;
        continue;
      }

      if (line.indexOf("### ") === 0 && line.substring(4).trim().indexOf("최종 판정") === 0) {
        finishTable();
        flushList();
        var verdictText = line.substring(4).trim();
        currentElements.push({ type: "verdict", text: verdictText });
        continue;
      }

      if (isTableRow(line)) {
        flushList();
        var cells = splitTableRow(line);
        if (tableState === null) {
          tableHeaderCells = cells;
          tableState = "header";
        } else if (tableState === "header") {
          if (isSeparatorRow(cells)) {
            tableState = "data";
          } else {
            finishTable();
            tableHeaderCells = cells;
            tableState = "header";
          }
        } else {
          if (isSeparatorRow(cells)) {
            continue;
          }
          tableDataRows.push(cells);
        }
        continue;
      }

      if (tableState !== null) {
        finishTable();
      }

      if (line.indexOf("- ") === 0) {
        listItems.push(line.substring(2).trim());
        continue;
      }

      if (listItems.length > 0) {
        flushList();
      }

      var trimmedLine = line.trim();
      if (trimmedLine === "") continue;

      currentElements.push({ type: "text", content: trimmedLine });
    }

    finishTable();
    flushList();
    finishSection();

    return {
      sections: sections,
      hasParsedContent: headingCount > 0 || tableCount > 0
    };
  }

  if (typeof globalThis !== "undefined") {
    globalThis.parseScreeningMarkdown = parseScreeningMarkdown;
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { parseScreeningMarkdown: parseScreeningMarkdown };
  }
})();
