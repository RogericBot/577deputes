// Bascule de législature : navigue vers /legislature/{n}?next=<page courante>.
// Externalisé pour respecter le CSP `script-src 'self'` (pas d'inline JS).
(function () {
  var sel = document.getElementById("leg-switcher");
  if (!sel) return;
  sel.addEventListener("change", function () {
    var next = window.location.pathname + window.location.search;
    window.location.href = "/legislature/" + encodeURIComponent(this.value)
      + "?next=" + encodeURIComponent(next);
  });
})();
