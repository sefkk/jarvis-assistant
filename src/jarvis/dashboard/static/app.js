/* Jarvis Dashboard: the little client-side glue. No eval anywhere (CSP: script-src 'self' only):
   HTMX runs with allowEval off, Alpine is the CSP build, and this file registers the Alpine data and
   the HTMX hooks (CSRF header, confirm dialog, session expiry, error toast). */
(function () {
  "use strict";

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  // Alpine (CSP build): components are registered here, templates reference them by name.
  document.addEventListener("alpine:init", function () {
    if (!window.Alpine) { return; }
    window.Alpine.data("drawer", function () {
      return {
        open: false,
        show: function () { this.open = true; },
        hide: function () { this.open = false; },
        toggle: function () { this.open = !this.open; },
        get shellClass() { return this.open ? "drawer-open" : ""; },
        get hiddenAttr() { return this.open ? "false" : "true"; }
      };
    });
  });

  // --- session lock: only clicks, typing, scrolling and navigation count as activity; polling does not ---
  function meta(name, fallback) {
    var el = document.querySelector('meta[name="' + name + '"]');
    return el ? el.getAttribute("content") : fallback;
  }
  var lock = {
    seconds: parseInt(meta("lock-seconds", "180"), 10) || 180,
    mode: meta("unlock-mode", "click"),
    lastActivity: Date.now(),
    lastReport: 0,
    locked: document.body.classList.contains("locked"),
    bannerShown: false,
    userEvent: false
  };
  window.jarvisDashboard = window.jarvisDashboard || {};
  window.jarvisDashboard.lock = lock;

  function postJson(path, body) {
    return fetch(path, { method: "POST", credentials: "same-origin",
      headers: { "X-CSRF-Token": csrfToken(), "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(body || {}) });
  }
  function reportActivity(force) {
    var now = Date.now();
    if (lock.locked) { return; }
    if (!force && now - lock.lastReport < 20000) { return; }
    lock.lastReport = now;
    postJson("/session/activity").then(function (res) { if (res.status === 423) { enterLock(); } }).catch(function () {});
  }
  function noteActivity() {
    if (lock.locked) { return; }
    lock.lastActivity = Date.now();
    lock.userEvent = true;
    if (lock.bannerShown) { hideBanner(); }
    reportActivity(false);
  }
  ["click", "keydown", "input", "scroll", "wheel", "pointerdown", "touchstart"].forEach(function (name) {
    document.addEventListener(name, noteActivity, { capture: true, passive: true });
  });
  function showBanner() { var b = document.getElementById("expire-banner"); if (b) { b.hidden = false; } lock.bannerShown = true; }
  function hideBanner() { var b = document.getElementById("expire-banner"); if (b) { b.hidden = true; } lock.bannerShown = false; }
  function enterLock() {
    if (lock.locked) { return; }
    lock.locked = true;
    hideBanner();
    document.body.classList.add("locked");
    var panel = document.getElementById("lock-panel");
    if (panel) { panel.hidden = false; var err = panel.querySelector(".lock-error"); if (err) { err.hidden = true; } }
  }
  function leaveLock() {
    lock.locked = false;
    lock.lastActivity = Date.now();
    lock.lastReport = Date.now();
    document.body.classList.remove("locked");
    var panel = document.getElementById("lock-panel");
    if (panel) { panel.hidden = true; }
    if (document.body.classList.contains("locked-page")) { window.location.reload(); }
  }
  function tick() {
    if (lock.locked) { return; }
    var idle = (Date.now() - lock.lastActivity) / 1000;
    var seconds = window.jarvisDashboard.lock.seconds;
    if (idle >= seconds) { enterLock(); return; }
    if (idle >= seconds - 60 && !lock.bannerShown) { showBanner(); }
  }
  setInterval(tick, 1000);
  document.addEventListener("click", function (evt) {
    if (evt.target.closest("[data-stay]")) { noteActivity(); reportActivity(true); hideBanner(); return; }
    var unlockButton = evt.target.closest("[data-unlock]");
    if (!unlockButton) { return; }
    var panel = unlockButton.closest("#lock-panel");
    var err = panel ? panel.querySelector(".lock-error") : null;
    unlockButton.disabled = true;
    postJson("/unlock").then(function (res) {
      if (res.status === 401) { window.location.reload(); return null; }
      return res.json().catch(function () { return { ok: false }; });
    }).then(function (data) {
      unlockButton.disabled = false;
      if (!data) { return; }
      if (data.ok) { leaveLock(); } else if (err) { err.hidden = false; }
    }).catch(function () { unlockButton.disabled = false; if (err) { err.hidden = false; } });
  }, true);
  // While locked no HTMX request leaves the page (the server would answer 423 anyway); a 423 locks the page.
  document.addEventListener("htmx:beforeRequest", function (evt) { if (lock.locked) { evt.preventDefault(); } });
  document.addEventListener("jarvis:locked", enterLock);
  document.addEventListener("htmx:responseError", function (evt) {
    if (evt.detail.xhr && evt.detail.xhr.status === 423) { enterLock(); }
  });

  // Every HTMX request carries the session's CSRF token (the server rejects state changes without it); a request
  // that follows a user action also says so, so the server's lock clock restarts (polls do not).
  document.addEventListener("htmx:configRequest", function (evt) {
    evt.detail.headers["X-CSRF-Token"] = csrfToken();
    var trigger = evt.detail.triggeringEvent;
    if (trigger && trigger.isTrusted && trigger.type !== "load" && trigger.type !== "revealed" && !/^every /.test(String(evt.detail.elt.getAttribute("hx-trigger") || ""))) {
      evt.detail.headers["X-Activity"] = "1";
    }
  });

  // hx-confirm opens our own dialog instead of window.confirm.
  var pending = null;
  document.addEventListener("htmx:confirm", function (evt) {
    if (!evt.detail.question) { return; }
    var dialog = document.getElementById("confirm-dialog");
    if (!dialog || typeof dialog.showModal !== "function") { return; }  // falls back to window.confirm
    evt.preventDefault();
    document.getElementById("confirm-text").textContent = evt.detail.question;
    pending = evt.detail;
    dialog.showModal();
  });
  document.addEventListener("click", function (evt) {
    var button = evt.target.closest("[data-confirm]");
    if (button) {
      var dialog = document.getElementById("confirm-dialog");
      if (dialog && dialog.open) { dialog.close(); }
      if (button.getAttribute("data-confirm") === "yes" && pending) { pending.issueRequest(true); }
      pending = null;
      return;
    }
    if (evt.target.closest("[data-reload]")) { window.location.reload(); }
  });
  document.addEventListener("close", function (evt) {
    if (evt.target && evt.target.id === "confirm-dialog") { pending = null; }
  }, true);

  // A thin bar at the top while any HTMX request is in flight (the loading state for every page).
  var inflight = 0;
  function loadingBar(show) {
    var bar = document.getElementById("loading-bar");
    if (bar) { bar.hidden = !show; }
  }
  document.addEventListener("htmx:beforeRequest", function () { inflight += 1; loadingBar(true); });
  document.addEventListener("htmx:afterRequest", function () { inflight = Math.max(0, inflight - 1); if (!inflight) { loadingBar(false); } });

  // A 401 means the session expired: reload shows the expired page. Other failures show the toast.
  function showToast() {
    var toast = document.getElementById("toast");
    if (toast) { toast.hidden = false; }
  }
  document.addEventListener("htmx:responseError", function (evt) {
    var status = evt.detail.xhr ? evt.detail.xhr.status : 0;
    if (status === 401) { window.location.reload(); return; }
    if (status === 423) { return; }
    showToast();
  });
  document.addEventListener("htmx:sendError", showToast);

  // After an action from the "Pause for" menu, fold the menu again.
  document.addEventListener("htmx:afterSwap", function (evt) {
    var verb = evt.detail.requestConfig && evt.detail.requestConfig.verb;
    if (verb && verb !== "get") {
      document.querySelectorAll("details.menu[open]").forEach(function (menu) { menu.removeAttribute("open"); });
    }
  });

  // Stage bars (Analytics requests, turn timeline): widths come from data-pct, set here because CSP forbids
  // style attributes in the HTML.
  function sizeStageBars(root) {
    (root || document).querySelectorAll(".waterfall .seg[data-pct]").forEach(function (seg) {
      seg.style.width = seg.getAttribute("data-pct") + "%";
    });
  }
  // Sliders paint their filled part from --pct (the track is styled in app.css). The variable goes on the parent:
  // htmx copies the style attribute of elements with an id while it settles a swap, which CSP would block.
  function fillRange(input) {
    var min = parseFloat(input.min || "0"), max = parseFloat(input.max || "100"), value = parseFloat(input.value);
    var pct = max > min ? ((value - min) / (max - min)) * 100 : 0;
    if (input.parentElement && !input.parentElement.id) {
      input.parentElement.style.setProperty("--pct", Math.max(0, Math.min(100, pct)) + "%");
    }
  }
  function sizeRanges(root) {
    (root || document).querySelectorAll('input[type="range"]').forEach(fillRange);
  }
  document.addEventListener("htmx:afterSwap", function (evt) { sizeStageBars(evt.target); sizeRanges(document); });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { sizeStageBars(document); sizeRanges(document); });
  } else {
    sizeStageBars(document);
    sizeRanges(document);
  }

  // Settings: the section list marks the section in view and keeps that link visible inside its own scroll.
  function sectionSpy() {
    var nav = document.querySelector(".section-nav");
    if (!nav || !("IntersectionObserver" in window)) { return; }
    var links = {};
    nav.querySelectorAll('a[href^="#"]').forEach(function (a) { links[a.getAttribute("href").slice(1)] = a; });
    var visible = {};
    function mark() {
      var best = null;
      document.querySelectorAll(".settings-section").forEach(function (section) {
        if (!best && visible[section.id]) { best = section.id; }
      });
      if (!best) { return; }
      Object.keys(links).forEach(function (id) { links[id].classList.toggle("active", id === best); });
      var link = links[best];
      if (link && (link.offsetTop < nav.scrollTop || link.offsetTop + link.offsetHeight > nav.scrollTop + nav.clientHeight)) {
        nav.scrollTop = link.offsetTop - nav.clientHeight / 2;
      }
    }
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) { visible[entry.target.id] = entry.isIntersecting; });
      mark();
    }, { rootMargin: "-10% 0px -55% 0px" });
    document.querySelectorAll(".settings-section").forEach(function (section) { observer.observe(section); });
  }
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", sectionSpy); } else { sectionSpy(); }

  // Leaving with staged changes warns (the Save button carries the count; it is swapped in after every staging).
  window.jarvisDashboard.hasUnsavedChanges = false;
  function syncUnsaved() {
    var button = document.getElementById("save-btn");
    window.jarvisDashboard.hasUnsavedChanges = !!(button && parseInt(button.getAttribute("data-count") || "0", 10) > 0);
  }
  document.addEventListener("htmx:afterSettle", syncUnsaved);
  document.addEventListener("htmx:oobAfterSwap", syncUnsaved);
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", syncUnsaved); } else { syncUnsaved(); }
  // The slider shows its value while dragging.
  document.addEventListener("input", function (evt) {
    if (evt.target && evt.target.type === "range") {
      fillRange(evt.target);
      var out = evt.target.parentElement.querySelector("output.range-value");
      if (out) { out.textContent = evt.target.value; }
    }
  });
  window.addEventListener("beforeunload", function (evt) {
    if (window.jarvisDashboard.hasUnsavedChanges) {
      evt.preventDefault();
      evt.returnValue = "";
    }
  });
})();
