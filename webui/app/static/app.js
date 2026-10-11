// Small progressive enhancements; every page works without this file.
(function () {
  "use strict";

  // Dim the fields of a store that is switched off.
  document.querySelectorAll("[data-store-toggle]").forEach(function (box) {
    box.addEventListener("change", function () {
      var set = box.closest("fieldset");
      if (set) set.classList.toggle("off", !box.checked);
    });
  });

  // Show a hidden value (the VNC password) on request.
  document.querySelectorAll("[data-reveal]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var el = document.getElementById(btn.getAttribute("data-reveal"));
      if (el) { el.classList.toggle("hidden"); }
    });
  });

  // Ask before destructive actions.
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // Keep the status badge fresh on the overview page.
  var badge = document.querySelector("[data-poll]");
  if (badge && window.fetch) {
    var url = badge.getAttribute("data-poll");
    var last = badge.textContent;
    setInterval(function () {
      fetch(url, { credentials: "same-origin" }).then(function (r) { return r.ok ? r.json() : null; })
        .then(function (s) {
          if (!s || !s.label) return;
          badge.textContent = s.label;
          badge.className = "badge " + s.state;
          var next = document.getElementById("next-run");
          if (next) next.textContent = s.next_run;
          // A run just finished: reload once to show the new games and log.
          if (last !== s.label && (s.state === "ok") && /Bezig|wachtrij/.test(last)) {
            window.location.reload();
          }
          last = s.label;
        }).catch(function () {});
    }, 10000);
  }
})();
