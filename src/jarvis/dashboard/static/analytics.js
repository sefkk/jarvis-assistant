/* Analytics charts: fetch /analytics/data once and draw every chart with Chart.js (vendored). The page has
   the tables already; this file only paints the canvases and sets the widths of the stage bars. */
(function () {
  "use strict";

  // Colors come from the design tokens (tokens.css), resolved through a hidden probe so light-dark() pairs follow
  // the current theme; every chart is redrawn when the theme changes.
  var PALETTE = [];
  var T = {};
  var PART_ORDER = ["persona", "rules", "profile", "memory", "tool_definitions", "conversation_history", "tool_results", "user_input", "thinking"];
  var STAGE_ORDER = ["stt", "ttft", "api", "tools", "first_audio"];
  var charts = {};
  var data = null;

  function money(v) { return v >= 1 ? "$" + v.toFixed(2) : "$" + v.toFixed(3); }
  function ms(v) { return v >= 1000 ? (v / 1000).toFixed(1) + " s" : Math.round(v) + " ms"; }
  function sum(values) { return values.reduce(function (a, b) { return a + (b || 0); }, 0); }
  function shortDay(day) { return day.slice(5); }

  function resolve(name) {
    var probe = document.createElement("span");
    probe.hidden = true;
    probe.style.color = "var(" + name + ")";
    document.body.appendChild(probe);
    var color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  }
  function alpha(color, a) {
    var m = String(color).match(/[\d.]+/g);
    if (!m || m.length < 3) { return color; }
    return "rgba(" + m[0] + ", " + m[1] + ", " + m[2] + ", " + a + ")";
  }
  function readTheme() {
    PALETTE = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(function (i) { return resolve("--chart-" + i); });
    var root = getComputedStyle(document.documentElement);
    T = {
      text: resolve("--chart-text"), strong: resolve("--text"), grid: resolve("--chart-grid"), tooltip: resolve("--chart-tooltip"),
      edge: resolve("--control-border"), neutral: resolve("--neutral"), font: root.getPropertyValue("--font-sans").trim() || "system-ui"
    };
    if (window.Chart) {
      Chart.defaults.font.family = T.font;
      Chart.defaults.font.size = 12;
      Chart.defaults.color = T.text;
      Chart.defaults.animation.duration = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 250;
    }
  }
  // A soft vertical gradient under a line: the series color fading to nothing.
  function areaFill(color) {
    return function (ctx) {
      var area = ctx.chart.chartArea;
      if (!area) { return alpha(color, 0.12); }
      var g = ctx.chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
      g.addColorStop(0, alpha(color, 0.24));
      g.addColorStop(1, alpha(color, 0));
      return g;
    };
  }
  function line(label, values, color, extra) {
    return Object.assign({ label: label, data: values, borderColor: color, backgroundColor: color, borderWidth: 2, tension: 0.35,
      pointRadius: 0, pointHoverRadius: 4, pointHoverBorderWidth: 2, pointHoverBorderColor: T.tooltip, fill: false, spanGaps: true }, extra || {});
  }
  function bars(label, values, color, extra) {
    return Object.assign({ label: label, data: values, backgroundColor: color, hoverBackgroundColor: alpha(color, 0.8), borderRadius: 6,
      borderSkipped: false, maxBarThickness: 28 }, extra || {});
  }

  function canvasFor(name) { return document.querySelector('canvas[data-chart="' + name + '"]'); }

  function draw(name, config, empty) {
    var canvas = canvasFor(name);
    if (!canvas || !window.Chart) { return; }
    var note = canvas.parentElement.querySelector(".chart-empty");
    var loading = canvas.parentElement.querySelector(".chart-loading");
    if (loading) { loading.remove(); }
    if (charts[name]) { charts[name].destroy(); delete charts[name]; }
    if (empty) {
      canvas.hidden = true;
      if (note) { note.hidden = false; }
      return;
    }
    canvas.hidden = false;
    if (note) { note.hidden = true; }
    var o = config.options = config.options || {};
    o.responsive = true;
    o.maintainAspectRatio = false;
    o.color = T.text;
    o.interaction = Object.assign({ mode: "index", intersect: false }, o.interaction || {});
    o.plugins = o.plugins || {};
    o.plugins.legend = Object.assign({ labels: { color: T.text, usePointStyle: true, pointStyle: "circle", boxWidth: 7, boxHeight: 7, padding: 14 } }, o.plugins.legend || {});
    o.plugins.tooltip = Object.assign({
      backgroundColor: T.tooltip, titleColor: T.strong, bodyColor: T.text, borderColor: T.edge, borderWidth: 1, cornerRadius: 10,
      padding: 10, boxPadding: 4, usePointStyle: true, titleFont: { weight: "600" }, caretSize: 0
    }, o.plugins.tooltip || {});
    if (o.scales) {
      Object.keys(o.scales).forEach(function (axis) {
        var sc = o.scales[axis];
        sc.border = Object.assign({ display: false }, sc.border || {});
        sc.ticks = Object.assign({ color: T.text, padding: 8 }, sc.ticks || {});
        sc.grid = Object.assign(axis === "x" ? { display: false } : { color: T.grid, drawTicks: false }, sc.grid || {});
      });
    }
    charts[name] = new Chart(canvas.getContext("2d"), config);
  }

  function doughnut(name, labels, values, formatter) {
    var total = sum(values);
    draw(name, {
      type: "doughnut",
      data: { labels: labels, datasets: [{ data: values, backgroundColor: PALETTE.slice(0, labels.length), borderWidth: 0, borderRadius: 4, spacing: 2, hoverOffset: 4 }] },
      options: { cutout: "70%", interaction: { mode: "nearest", intersect: true }, plugins: { legend: { position: "right" }, tooltip: { callbacks: { label: function (ctx) {
        var v = ctx.parsed; var pct = total ? Math.round(v / total * 100) : 0;
        return " " + ctx.label + ": " + (formatter ? formatter(v) : v) + " (" + pct + "%)";
      } } } } }
    }, total <= 0);
  }

  function lineOrBar(name, type, labels, datasets, opts) {
    var hasData = datasets.some(function (d) { return d.data.some(function (v) { return v !== null && v !== undefined && v !== 0; }); });
    var options = Object.assign({ scales: { x: { stacked: !!opts.stacked }, y: { stacked: !!opts.stacked, beginAtZero: true } } }, opts.options || {});
    if (opts.yFormat) { options.scales.y.ticks = { callback: function (v) { return opts.yFormat(v); } }; }
    if (opts.tooltip) { options.plugins = Object.assign(options.plugins || {}, { tooltip: { callbacks: { label: function (ctx) { return " " + ctx.dataset.label + ": " + opts.tooltip(ctx.parsed.y); } } } }); }
    if (opts.stacked) {  // stacked bars: only the top of the stack is rounded
      datasets.forEach(function (d) { if (d.borderRadius) { d.borderRadius = 0; d.borderSkipped = "bottom"; } });
      var last = datasets[datasets.length - 1];
      if (last && type === "bar") { last.borderRadius = { topLeft: 6, topRight: 6 }; }
    }
    draw(name, { type: type, data: { labels: labels, datasets: datasets }, options: options }, !hasData);
  }

  function renderDailyCost(d) {
    var days = d.days.map(shortDay);
    var budget = document.querySelector("[data-budget]");
    if (budget) { budget.textContent = d.budget > 0 ? money(d.budget) : "–"; }
    var datasets = [
      line("Anthropic", d.anthropic, PALETTE[0], { fill: "origin", backgroundColor: areaFill(PALETTE[0]) }),
      line("ElevenLabs", d.elevenlabs, PALETTE[1]),
      line("Google Maps", d.maps, PALETTE[2])
    ];
    if (d.budget > 0) {
      datasets.push(line("Daily budget", d.days.map(function () { return d.budget; }), PALETTE[4], { borderDash: [6, 4], borderWidth: 1.5, pointHoverRadius: 0 }));
    }
    lineOrBar("daily_cost", "line", days, datasets, { yFormat: money, tooltip: money });
  }

  function renderModelUsage(metric) {
    var rows = data.model_usage || [];
    var formatter = metric === "cost" ? money : function (v) { return String(v); };
    doughnut("model_usage", rows.map(function (r) { return r.label; }), rows.map(function (r) { return r[metric]; }), formatter);
  }

  function renderComposition(c) {
    var labels = PART_ORDER.map(function (p) { return c.labels[p]; });
    doughnut("composition", labels, PART_ORDER.map(function (p) { return c.parts[p] || 0; }));
    var datasets = PART_ORDER.map(function (p, i) {
      return bars(c.labels[p], c.series[p], PALETTE[i % PALETTE.length], { stack: "parts" });
    });
    lineOrBar("composition_time", "bar", c.days.map(shortDay), datasets, { stacked: true });
  }

  function renderCache(c) {
    var days = c.days.map(shortDay);
    lineOrBar("cache_hit", "line", days, [line("Cache hit rate", c.hit_rate, PALETTE[0], { fill: "origin", backgroundColor: areaFill(PALETTE[0]) })],
      { yFormat: function (v) { return v + " %"; }, tooltip: function (v) { return v + " %"; }, options: { scales: { x: {}, y: { beginAtZero: true, max: 100 } } } });
    lineOrBar("cache_saved", "bar", days, [bars("Tokens saved by cache", c.saved_tokens, PALETTE[1])], { options: { plugins: { legend: { display: false } }, scales: { x: {}, y: { beginAtZero: true } } } });
  }

  function renderLatency(l) {
    var days = l.days.map(shortDay);
    lineOrBar("latency", "line", days, [
      line("p50", l.p50, PALETTE[0], { fill: "origin", backgroundColor: areaFill(PALETTE[0]) }),
      line("p90", l.p90, PALETTE[2]),
      line("p95", l.p95, PALETTE[4])
    ], { yFormat: ms, tooltip: ms });
    var datasets = STAGE_ORDER.map(function (key, i) {
      return bars(l.stage_labels[key], l.stages[key], PALETTE[i], { stack: "stages" });
    });
    lineOrBar("latency_stage", "bar", days, datasets, { stacked: true, yFormat: ms, tooltip: ms });
  }

  function renderErrors(e) {
    var labels = Object.keys(e.by_type);
    lineOrBar("failed_by_type", "bar", labels, [bars("Failed requests", labels.map(function (k) { return e.by_type[k]; }), PALETTE[4])],
      { options: { plugins: { legend: { display: false } }, scales: { x: {}, y: { beginAtZero: true, ticks: { precision: 0 } } } } });
  }

  function renderChannels(c) {
    var labels = Object.keys(c);
    doughnut("channels", labels, labels.map(function (k) { return c[k]; }));
  }

  function renderConfirmations(rows) {
    var labels = rows.map(function (r) { return r.reason; });
    lineOrBar("confirmations", "bar", labels, [
      bars("Approved", rows.map(function (r) { return r.approved; }), PALETTE[1], { stack: "c" }),
      bars("Denied", rows.map(function (r) { return r.denied; }), PALETTE[4], { stack: "c" })
    ], { stacked: true, options: { scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } } } } });
  }

  function renderAll() {
    readTheme();
    renderDailyCost(data.daily_cost);
    var providers = data.cost_by_provider;
    doughnut("cost_by_provider", Object.keys(providers), Object.keys(providers).map(function (k) { return providers[k]; }), money);
    var active = document.querySelector('[data-toggle="model_metric"] .seg-btn.active');
    renderModelUsage(active ? active.getAttribute("data-metric") : "tokens");
    renderComposition(data.composition);
    renderCache(data.cache);
    renderLatency(data.latency);
    renderErrors(data.errors);
    renderChannels(data.channels);
    renderConfirmations(data.confirmations || []);
  }

  function sizeStageBars(root) {
    (root || document).querySelectorAll(".waterfall .seg[data-pct]").forEach(function (seg) {
      seg.style.width = seg.getAttribute("data-pct") + "%";
    });
  }

  function load() {
    var root = document.getElementById("analytics");
    if (!root) { return; }
    sizeStageBars(root);
    fetch(root.getAttribute("data-src"), { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (res) {
        if (res.status === 401) { window.location.reload(); return null; }
        if (res.status === 423) { document.dispatchEvent(new CustomEvent("jarvis:locked")); return null; }
        return res.ok ? res.json() : null;
      })
      .then(function (json) {
        if (!json) { return; }
        data = json;
        renderAll();
      })
      .catch(function () {
        var toast = document.getElementById("toast");
        if (toast) { toast.hidden = false; }
      });
  }

  document.addEventListener("click", function (evt) {
    var button = evt.target.closest('[data-toggle="model_metric"] .seg-btn');
    if (!button || !data) { return; }
    button.parentElement.querySelectorAll(".seg-btn").forEach(function (b) { b.classList.remove("active"); });
    button.classList.add("active");
    renderModelUsage(button.getAttribute("data-metric"));
  });

  // The date range is a segmented radio group; "Custom" shows the two date fields.
  document.addEventListener("change", function (evt) {
    if (evt.target && evt.target.name === "range" && evt.target.type === "radio") {
      var custom = document.querySelector(".custom-range");
      if (custom) { custom.hidden = evt.target.value !== "custom"; }
    }
  });

  document.addEventListener("htmx:afterSwap", function (evt) { sizeStageBars(evt.target); });
  document.addEventListener("jarvis:theme", function () { if (data) { renderAll(); } });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
