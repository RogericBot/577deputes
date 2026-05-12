/* anqp — bouton « Partager cette page ».
 *
 * Fichier externe (et pas un <script> inline) parce que le CSP nginx
 * impose script-src 'self' : l'inline serait bloqué.
 *
 * Sur mobile (API Web Share dispo) → ouvre le menu de partage natif du
 * téléphone avec le titre + l'URL de la page courante.
 * Sur ordinateur → copie l'URL dans le presse-papier et affiche
 * brièvement « Lien copié ! » ; fallback sur une fenêtre prompt() si le
 * presse-papier n'est pas disponible.
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

  btn.addEventListener("click", function () {
    var url = location.href;
    var title = document.title;
    if (navigator.share) {
      navigator.share({ title: title, url: url }).catch(function () { /* annulé : rien à faire */ });
    } else if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(
        function () { flash("Lien copié !"); },
        function () { window.prompt("Copiez le lien :", url); }
      );
    } else {
      window.prompt("Copiez le lien :", url);
    }
  });
})();
