(function () {
  "use strict";

  var PLATFORM_DEFS = [
    {
      platform: "wanted",
      hosts: ["www.wanted.co.kr", "wanted.co.kr"],
      match: function (p) { var m = p.pathname.match(/^\/wd\/(\d+)(?:[/?#]|$)/); return m && m[1]; }
    },
    {
      platform: "remember",
      hosts: [".rememberapp.co.kr"],
      match: function (p) { var m = p.pathname.match(/^\/job\/(?:posting\/)?(\d+)(?:[/?#]|$)/); return m && m[1]; }
    },
    {
      platform: "groupby",
      hosts: ["groupby.kr", "www.groupby.kr"],
      match: function (p) { var m = p.pathname.match(/^\/positions\/(\d+)(?:[/?#]|$)/); return m && m[1]; }
    },
    {
      platform: "saramin",
      hosts: ["www.saramin.co.kr"],
      match: function (p) { return p.searchParams.get("rec_idx") || null; }
    }
  ];

  var EXCLUDED_HOSTS = ["jumpit.saramin.co.kr"];

  function detectJobPosting(url) {
    if (typeof url !== "string") return null;
    var parsed;
    try { parsed = new URL(url); } catch (e) { return null; }
    if (parsed.protocol !== "https:") return null;

    var hostname = parsed.hostname;

    for (var i = 0; i < EXCLUDED_HOSTS.length; i++) {
      if (hostname === EXCLUDED_HOSTS[i]) return null;
    }

    for (var j = 0; j < PLATFORM_DEFS.length; j++) {
      var def = PLATFORM_DEFS[j];
      var hostMatch = false;
      for (var k = 0; k < def.hosts.length; k++) {
        var h = def.hosts[k];
        if (h.charAt(0) === ".") {
          if (hostname === h.substring(1) || hostname.endsWith(h)) { hostMatch = true; break; }
        } else {
          if (hostname === h) { hostMatch = true; break; }
        }
      }
      if (!hostMatch) continue;
      var jobId = def.match(parsed);
      if (jobId) return { platform: def.platform, jobId: jobId };
    }

    return null;
  }

  if (typeof globalThis !== "undefined") {
    globalThis.detectJobPosting = detectJobPosting;
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { detectJobPosting: detectJobPosting };
  }
})();
