/* anqp — bouton « Partager cette page ».
 *
 * Fichier externe (et pas un <script> inline) parce que le CSP nginx
 * impose script-src 'self' : l'inline serait bloqué.
 *
 * Sur appareil tactile (téléphone / tablette, où l'API Web Share ouvre un
 * vrai menu de partage natif utilisable) → on utilise navigator.share().
 * Sur ordinateur → on NE veut PAS déclencher la « feuille de partage »
 * Windows (capricieuse, popup « Essayez à nouveau / Nous n'avons pas pu
 * vous montrer tous les partages possibles ») : on copie directement l'URL
 * dans le presse-papier avec un bref « Lien copié ! » ; fallback prompt()
 * si le presse-papier n'est pas disponible.
 */
(function () {
  var btn = document.getElementById("share-page-btn");
  if (!btn) return;
  var msg = document.getElementById("share-page-msg");

  function flash(text) {
    if (!msg) return;
    msg.textContent = text;
    setTimeout(function () { msg.textContent = ""; }, 2500);
  }

  // Heuristique « appareil tactile » : pointeur grossier (doigt) → mobile/tablette.
  function isTouchDevice() {
    return (
      (window.matchMedia && window.matchMedia("(pointer: coarse)").matches) ||
      ("ontouchstart" in window && (navigator.maxTouchPoints || 0) > 0)
    );
  }

  function copyLink(url) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(
        function () { flash("Lien copié !"); },
        function () { window.prompt("Copiez le lien :", url); }
      );
    } else {
      window.prompt("Copiez le lien :", url);
    }
  }

  btn.addEventListener("click", function () {
    var url = location.href;
    var title = document.title;
    if (navigator.share && isTouchDevice()) {
      navigator.share({ title: title, url: url }).catch(function () {
        /* partage annulé ou échoué → on retombe sur la copie du lien */
        copyLink(url);
      });
    } else {
      copyLink(url);
    }
  });
})();
