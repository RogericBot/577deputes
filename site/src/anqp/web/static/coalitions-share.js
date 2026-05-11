// Active les boutons d'export PNG sur tous les blocs .sharable de /coalitions
// (matrice de cohésion, ternaire, blocs, scrutins). Chaque bloc est composé
// uniquement de SVG / HTML simple sans photos, donc l'export foreignObject
// de share.js fonctionne correctement.
(function () {
  function init() {
    if (typeof window.__attachShareButtons === "function") {
      window.__attachShareButtons(document);
    } else {
      // share.js n'est pas encore chargé (defer ordering) → réessayer
      setTimeout(init, 50);
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
