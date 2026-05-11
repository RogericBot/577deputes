// Décompte en direct du prochain rafraîchissement automatique sur /a-propos.
// La page rend une valeur figée à l'instant de la requête ; ce script
// la décrémente chaque seconde côté navigateur pour donner un signal
// "vivant" et éviter l'impression d'une horloge cassée.
(function () {
  const el = document.getElementById('next-refresh-countdown');
  if (!el) return;
  let seconds = parseInt(el.dataset.seconds || '0', 10);
  if (!Number.isFinite(seconds) || seconds <= 0) return;

  function format(s) {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h > 0) {
      return `${h}h${String(m).padStart(2, '0')}`;
    }
    if (m > 0) {
      return `${m}m${String(sec).padStart(2, '0')}`;
    }
    return `${sec}s`;
  }

  function tick() {
    seconds -= 1;
    if (seconds <= 0) {
      el.textContent = 'quelques secondes';
      return;
    }
    el.textContent = format(seconds);
    setTimeout(tick, 1000);
  }
  setTimeout(tick, 1000);
})();
