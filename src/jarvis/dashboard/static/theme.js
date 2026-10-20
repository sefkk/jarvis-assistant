/* Jarvis Dashboard: the theme choice (system, light, dark). Loaded without defer so the page paints in the chosen
   theme at once; the sidebar toggle calls window.jarvisTheme.set and other scripts listen for "jarvis:theme". */
(function () {
  "use strict";
  var KEY = "jarvis-theme";
  var root = document.documentElement;

  function stored() {
    try { return localStorage.getItem(KEY) || "system"; } catch (e) { return "system"; }
  }
  function apply(choice) {
    if (choice === "light" || choice === "dark") { root.setAttribute("data-theme", choice); } else { root.removeAttribute("data-theme"); }
  }
  function sync() {
    var choice = stored();
    document.querySelectorAll("[data-theme-choice]").forEach(function (button) {
      button.setAttribute("aria-pressed", button.getAttribute("data-theme-choice") === choice ? "true" : "false");
    });
  }
  function set(choice) {
    try { if (choice === "system") { localStorage.removeItem(KEY); } else { localStorage.setItem(KEY, choice); } } catch (e) { /* private mode */ }
    apply(choice);
    sync();
    document.dispatchEvent(new CustomEvent("jarvis:theme"));
  }

  apply(stored());
  window.jarvisTheme = { set: set, choice: stored };
  document.addEventListener("click", function (evt) {
    var button = evt.target.closest ? evt.target.closest("[data-theme-choice]") : null;
    if (button) { set(button.getAttribute("data-theme-choice")); }
  });
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
      if (stored() === "system") { document.dispatchEvent(new CustomEvent("jarvis:theme")); }
    });
  }
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", sync); } else { sync(); }
})();
