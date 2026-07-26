"use strict";

// Background service worker: owns the single Native Messaging connection to
// the local careerkit Python backend and routes messages between content
// scripts / the side panel and that connection.
//
// Protocol notes:
// - Content script / side panel -> background: chrome.runtime.sendMessage({action, ...})
// - Background -> native host: forwards the request object as-is over the port
// - Native host -> background: either a response to the oldest in-flight
//   request (FIFO — the host answers requests in the order it received them)
//   or an unsolicited push message shaped {type: "screening_complete" | "screening_failed", tracking_id, ...}

var NATIVE_HOST_NAME = "com.careerkit.host";
var KEEPALIVE_ALARM_NAME = "keepalive";
var KEEPALIVE_PERIOD_MINUTES = 0.5;

var nativePort = null;
var nextRequestId = 1;
var pendingRequests = new Map(); // request_id -> {resolve, reject}

function connectNativeHost() {
  if (nativePort) return nativePort;

  rehydratePendingScreenings();

  try {
    nativePort = chrome.runtime.connectNative(NATIVE_HOST_NAME);
  } catch (err) {
    nativePort = null;
    return null;
  }

  nativePort.onMessage.addListener(handleNativeMessage);
  nativePort.onDisconnect.addListener(handleNativeDisconnect);

  return nativePort;
}

function handleNativeDisconnect() {
  nativePort = null;
  var lastError = chrome.runtime.lastError;
  var message = lastError && lastError.message ? lastError.message : "Native host not connected";

  pendingRequests.forEach(function (pending) {
    pending.reject(new Error(message));
  });
  pendingRequests.clear();

  sweepPendingScreeningsAsOrphans("Native host disconnected");
}

function handleNativeMessage(message) {
  if (message && message.type === "screening_complete") {
    onScreeningComplete(message);
    return;
  }
  if (message && message.type === "screening_failed") {
    onScreeningFailed(message);
    return;
  }

  if (message && message.request_id != null && pendingRequests.has(message.request_id)) {
    var pending = pendingRequests.get(message.request_id);
    pendingRequests.delete(message.request_id);
    pending.resolve(message);
  } else {
    var oldest = pendingRequests.entries().next();
    if (!oldest.done) {
      pendingRequests.delete(oldest.value[0]);
      oldest.value[1].resolve(message);
    }
  }
}

function sendToNativeHost(request) {
  return new Promise(function (resolve, reject) {
    var port = connectNativeHost();
    if (!port) {
      reject(new Error("Native host not connected"));
      return;
    }

    var id = nextRequestId++;
    request.request_id = id;
    pendingRequests.set(id, { resolve: resolve, reject: reject });
    try {
      port.postMessage(request);
    } catch (err) {
      pendingRequests.delete(id);
      reject(err);
    }
  });
}

// --- pending_screenings tracking (local Map, flushed to session storage) ---

var pendingScreenings = new Map();

function flushPendingScreenings() {
  var obj = {};
  pendingScreenings.forEach(function (entry, id) { obj[id] = entry; });
  chrome.storage.session.set({ pending_screenings: obj }).catch(function () {});
}

function setPendingScreening(trackingId, entry) {
  entry.createdAt = Date.now();
  pendingScreenings.set(trackingId, entry);
  flushPendingScreenings();
  return Promise.resolve();
}

function popPendingScreening(trackingId) {
  var entry = pendingScreenings.get(trackingId);
  pendingScreenings.delete(trackingId);
  flushPendingScreenings();
  return Promise.resolve(entry);
}

function sweepPendingScreeningsAsOrphans(reason) {
  if (pendingScreenings.size === 0) return;
  var tabFallbacks = [];
  pendingScreenings.forEach(function (entry, trackingId) {
    var payload = { action: "screening_failed", message: reason, url: entry.url };
    if (entry.tabId) {
      notifyTab(entry.tabId, payload);
    } else {
      tabFallbacks.push(payload);
    }
  });
  pendingScreenings.clear();
  flushPendingScreenings();
  if (tabFallbacks.length > 0) {
    chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
      if (tabs && tabs[0]) {
        tabFallbacks.forEach(function (payload) { notifyTab(tabs[0].id, payload); });
      }
    });
  }
}

function rehydratePendingScreenings() {
  return chrome.storage.session.get("pending_screenings").then(function (result) {
    var stored = result.pending_screenings || {};
    var keys = Object.keys(stored);
    if (keys.length === 0) return;
    keys.forEach(function (id) {
      var entry = stored[id];
      var payload = { action: "screening_failed", message: "Native host disconnected (recovered)", url: entry.url };
      if (entry.tabId) {
        notifyTab(entry.tabId, payload);
      }
    });
    chrome.storage.session.set({ pending_screenings: {} }).catch(function () {});
  });
}

// --- screening_complete / screening_failed push handling ---

function notifyTab(tabId, message) {
  if (typeof tabId !== "number") return;
  chrome.tabs.sendMessage(tabId, message, function () {
    void chrome.runtime.lastError; // tab/content script may be gone; ignore
  });
}

function incrementBadgeCount() {
  chrome.action.getBadgeText({}).then(function (current) {
    var count = (parseInt(current, 10) || 0) + 1;
    chrome.action.setBadgeBackgroundColor({ color: "#4A90D9" });
    chrome.action.setBadgeText({ text: String(count) });
  });
}

function onScreeningComplete(message) {
  var trackingId = message.tracking_id;
  var data = message.data || {};

  popPendingScreening(trackingId).then(function (pending) {
    var label = data.verdict_label || data.verdict || "스크리닝 완료";
    var title = data.company ? data.company + " — " + data.position : "CareerKit";

    chrome.notifications.create("careerkit-" + trackingId, {
      type: "basic",
      iconUrl: chrome.runtime.getURL("icons/icon-128.png"),
      title: title,
      message: label,
    });

    incrementBadgeCount();

    var payload = { action: "screening_complete", data: data, url: pending && pending.url };
    chrome.runtime.sendMessage(payload, function () {
      void chrome.runtime.lastError;
    });
    if (pending && pending.tabId) {
      notifyTab(pending.tabId, payload);
    } else {
      chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
        if (tabs && tabs[0]) notifyTab(tabs[0].id, payload);
      });
    }
  });
}

function onScreeningFailed(message) {
  var trackingId = message.tracking_id;

  popPendingScreening(trackingId).then(function (pending) {
    var payload = { action: "screening_failed", message: message.message, url: pending && pending.url };
    chrome.runtime.sendMessage(payload, function () {
      void chrome.runtime.lastError;
    });
    if (pending && pending.tabId) {
      notifyTab(pending.tabId, payload);
    } else {
      chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
        if (tabs && tabs[0]) notifyTab(tabs[0].id, payload);
      });
    }
  });
}

// --- message routing from content scripts / side panel ---

function handleAction(request, sender) {
  return sendToNativeHost(request).then(function (response) {
    if (
      (request.action === "collect" || request.action === "rescreen") &&
      response &&
      response.status === "accepted" &&
      response.tracking_id
    ) {
      var tabId = sender && sender.tab ? sender.tab.id : null;
      return setPendingScreening(response.tracking_id, { url: request.url, tabId: tabId }).then(
        function () {
          return response;
        }
      );
    }
    return response;
  });
}

chrome.runtime.onMessage.addListener(function (request, sender, sendResponse) {
  if (!sender || sender.id !== chrome.runtime.id) {
    sendResponse({ status: "error", message: "unauthorized" });
    return false;
  }
  if (!request || typeof request.action !== "string") {
    sendResponse({ status: "error", message: "invalid request" });
    return false;
  }

  handleAction(request, sender)
    .then(function (response) {
      sendResponse(response);
    })
    .catch(function (err) {
      sendResponse({ status: "error", message: err && err.message ? err.message : "Native host not connected" });
    });

  return true; // keep sendResponse channel open for the async response above
});

// --- lifecycle: install/startup + keepalive ---

chrome.runtime.onInstalled.addListener(function () {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(function () {});
  chrome.alarms.create(KEEPALIVE_ALARM_NAME, { periodInMinutes: KEEPALIVE_PERIOD_MINUTES });
});

chrome.runtime.onStartup.addListener(function () {
  chrome.alarms.create(KEEPALIVE_ALARM_NAME, { periodInMinutes: KEEPALIVE_PERIOD_MINUTES });
});

chrome.alarms.onAlarm.addListener(function (alarm) {
  if (alarm.name === KEEPALIVE_ALARM_NAME) {
    connectNativeHost();
  }
});
