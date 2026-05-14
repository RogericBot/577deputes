// Déroulants de la barre de nav (« Données ▾ » / « Plus ▾ »).
// Externalisé pour respecter CSP `script-src 'self'`. Pas d'inline JS.
(function () {
  var groups = document.querySelectorAll(".nav-group");
  if (!groups.length) return;

  function closeAll(except) {
    groups.forEach(function (g) {
      if (g === except) return;
      var btn = g.querySelector(".nav-group-toggle");
      var menu = g.querySelector(".nav-group-menu");
      if (btn) btn.setAttribute("aria-expanded", "false");
      if (menu) menu.setAttribute("hidden", "");
    });
  }

  groups.forEach(function (g) {
    var btn = g.querySelector(".nav-group-toggle");
    var menu = g.querySelector(".nav-group-menu");
    if (!btn || !menu) return;

    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      var isOpen = btn.getAttribute("aria-expanded") === "true";
      closeAll(g);
      if (isOpen) {
        btn.setAttribute("aria-expanded", "false");
        menu.setAttribute("hidden", "");
      } else {
        btn.setAttribute("aria-expanded", "true");
        menu.removeAttribute("hidden");
      }
    });

    // Clics à l'intérieur du menu ne ferment pas (laisse la navigation se faire).
    menu.addEventListener("click", function (ev) {
      ev.stopPropagation();
    });
  });

  // Clic hors d'un groupe → ferme tout.
  document.addEventListener("click", function () {
    closeAll(null);
  });

  // Échap → ferme tout (et redonne le focus au bouton qui était ouvert).
  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape") return;
    var openBtn = null;
    groups.forEach(function (g) {
      var btn = g.querySelector(".nav-group-toggle");
      if (btn && btn.getAttribute("aria-expanded") === "true") openBtn = btn;
    });
    closeAll(null);
    if (openBtn) openBtn.focus();
  });
})();
