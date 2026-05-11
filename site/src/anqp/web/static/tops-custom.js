// Constructeur de tops — rebuild client-side de la dropdown métrique
// quand l'utilisateur change d'entité, sans recharger la page.
//
// Le CSP nginx interdit l'inline JS (script-src 'self'), donc tout passe
// par ce fichier statique + addEventListener (pas d'attribut onchange).
(function () {
  const island = document.getElementById('metrics-catalog');
  if (!island) return;

  let CATALOG;
  try {
    CATALOG = JSON.parse(island.textContent);
  } catch (e) {
    console.error('tops-custom: JSON parse failed', e);
    return;
  }

  function onEntityChange(entity) {
    const select = document.getElementById('metric-select');
    const counter = document.getElementById('metric-count');
    if (!select) return;
    const metrics = CATALOG[entity] || [];

    select.innerHTML = '';

    let currentGroup = null, lastCat = null;
    metrics.forEach(m => {
      if (m.category !== lastCat) {
        currentGroup = document.createElement('optgroup');
        currentGroup.label = m.category;
        select.appendChild(currentGroup);
        lastCat = m.category;
      }
      const opt = document.createElement('option');
      opt.value = m.key;
      opt.textContent = m.label;
      opt.title = m.description;
      currentGroup.appendChild(opt);
    });

    if (counter) counter.textContent = `(${metrics.length} disponibles)`;
    if (metrics.length) select.value = metrics[0].key;

    // Met en évidence le bouton "Générer" pour signaler que les filtres
    // n'ont pas encore été rechargés pour la nouvelle entité.
    const submitBtn = document.querySelector('.custom-top-actions button[type=submit]');
    if (submitBtn) {
      submitBtn.classList.add('btn-pulse');
      submitBtn.textContent = 'Générer →';
    }
  }

  const entitySelect = document.getElementById('entity-select');
  if (entitySelect) {
    entitySelect.addEventListener('change', e => onEntityChange(e.target.value));
  }

  // Click-to-copy sur l'input du lien partageable
  const shareInput = document.getElementById('share-url-input');
  if (shareInput) {
    shareInput.addEventListener('click', () => {
      shareInput.select();
      try {
        navigator.clipboard.writeText(shareInput.value);
      } catch (e) {
        document.execCommand('copy');
      }
    });
  }

  // Exposé en global pour debug console
  window.onEntityChange = onEntityChange;
})();
