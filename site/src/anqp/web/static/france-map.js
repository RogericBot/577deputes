/* 577députés — interactive France map.
 *
 * Two responsibilities :
 *   1. Pan / zoom : mouse wheel zooms (centre = cursor), drag pans.
 *      "+" / "−" / "Reset" buttons exposed for accessibility.
 *   2. Rich hover card : a floating panel shows the deputy's details when
 *      a circonscription is hovered. Data is pulled from data-* attributes
 *      on the path elements (set server-side in cartography.py).
 *
 * Pure vanilla, no dependency.
 */
(function () {
  const svg = document.querySelector(".france-map");
  if (!svg) return;

  // -------------------------------------------------------------------
  // Pan / zoom
  // -------------------------------------------------------------------
  const initial = svg.getAttribute("viewBox").split(/\s+/).map(Number);
  let [vx, vy, vw, vh] = initial;
  const MIN_W = initial[2] / 16;     // max zoom = 16x
  const MAX_W = initial[2] * 1.2;    // a bit of room out

  function applyViewBox() {
    svg.setAttribute("viewBox", `${vx.toFixed(1)} ${vy.toFixed(1)} ${vw.toFixed(1)} ${vh.toFixed(1)}`);
  }

  function zoomAtClient(clientX, clientY, factor) {
    const rect = svg.getBoundingClientRect();
    const mx = vx + ((clientX - rect.left) / rect.width) * vw;
    const my = vy + ((clientY - rect.top) / rect.height) * vh;
    const newW = Math.min(MAX_W, Math.max(MIN_W, vw * factor));
    const realFactor = newW / vw;
    vw = newW;
    vh = vh * realFactor;
    vx = mx - (mx - vx) * realFactor;
    vy = my - (my - vy) * realFactor;
    applyViewBox();
  }

  svg.addEventListener("wheel", e => {
    e.preventDefault();
    zoomAtClient(e.clientX, e.clientY, e.deltaY > 0 ? 1.18 : 1 / 1.18);
  }, { passive: false });

  // Drag-to-pan ; click is blocked ONLY when the user actually moved.
  let panning = false, sx0 = 0, sy0 = 0, vx0 = 0, vy0 = 0, dragDist = 0;
  svg.addEventListener("mousedown", e => {
    if (e.button !== 0) return;
    panning = true; dragDist = 0;
    sx0 = e.clientX; sy0 = e.clientY; vx0 = vx; vy0 = vy;
    svg.style.cursor = "grabbing";
  });
  window.addEventListener("mousemove", e => {
    if (!panning) return;
    const rect = svg.getBoundingClientRect();
    const dx = ((e.clientX - sx0) / rect.width) * vw;
    const dy = ((e.clientY - sy0) / rect.height) * vh;
    dragDist += Math.abs(e.movementX) + Math.abs(e.movementY);
    vx = vx0 - dx; vy = vy0 - dy;
    applyViewBox();
  });
  window.addEventListener("mouseup", () => {
    if (panning) {
      panning = false;
      svg.style.cursor = "";
    }
  });

  // Suppress link-click only if the user truly dragged (>5px cumulated).
  svg.addEventListener("click", e => {
    if (dragDist > 5) {
      e.preventDefault(); e.stopPropagation();
      dragDist = 0;
    }
  }, true);

  // Touch (pinch + drag) — minimal but functional.
  let pinchDist0 = 0, pinchVw = 0;
  svg.addEventListener("touchstart", e => {
    if (e.touches.length === 1) {
      panning = true; sx0 = e.touches[0].clientX; sy0 = e.touches[0].clientY;
      vx0 = vx; vy0 = vy;
    } else if (e.touches.length === 2) {
      panning = false;
      const dx = e.touches[0].clientX - e.touches[1].clientX;
      const dy = e.touches[0].clientY - e.touches[1].clientY;
      pinchDist0 = Math.hypot(dx, dy); pinchVw = vw;
    }
  }, { passive: true });
  svg.addEventListener("touchmove", e => {
    if (e.touches.length === 1 && panning) {
      const rect = svg.getBoundingClientRect();
      const dx = ((e.touches[0].clientX - sx0) / rect.width) * vw;
      const dy = ((e.touches[0].clientY - sy0) / rect.height) * vh;
      vx = vx0 - dx; vy = vy0 - dy;
      applyViewBox();
      e.preventDefault();
    } else if (e.touches.length === 2) {
      const dx = e.touches[0].clientX - e.touches[1].clientX;
      const dy = e.touches[0].clientY - e.touches[1].clientY;
      const dist = Math.hypot(dx, dy);
      if (pinchDist0 > 0) {
        const ratio = pinchDist0 / dist;
        const newW = Math.min(MAX_W, Math.max(MIN_W, pinchVw * ratio));
        const cx = vx + vw / 2, cy = vy + vh / 2;
        const f = newW / vw;
        vw = newW; vh = vh * f;
        vx = cx - vw / 2; vy = cy - vh / 2;
        applyViewBox();
      }
      e.preventDefault();
    }
  }, { passive: false });
  svg.addEventListener("touchend", () => { panning = false; pinchDist0 = 0; });

  // Buttons
  function getCenter() {
    const rect = svg.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  }
  document.querySelectorAll("[data-map-action]").forEach(btn => {
    btn.addEventListener("click", () => {
      const action = btn.dataset.mapAction;
      if (action === "zoom-in") {
        const c = getCenter(); zoomAtClient(c.x, c.y, 1 / 1.4);
      } else if (action === "zoom-out") {
        const c = getCenter(); zoomAtClient(c.x, c.y, 1.4);
      } else if (action === "reset") {
        [vx, vy, vw, vh] = initial; applyViewBox();
      }
    });
  });

  // -------------------------------------------------------------------
  // Hover card
  // -------------------------------------------------------------------
  const card = document.getElementById("map-hover-card");
  function escapeHtml(s) {
    return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function fmtNum(n) {
    if (!n) return null;
    const x = Number(n);
    if (!isFinite(x) || x === 0) return null;
    return x.toLocaleString("fr-FR");
  }
  function buildCardHtml(d) {
    const hasDeputy = !!d.uid;
    const photo = hasDeputy ? `<div class="hover-card-photo" style="background-image:url('/photo/${escapeHtml(d.uid)}')"></div>` : "";
    const name = hasDeputy
      ? `<strong>${escapeHtml(d.nom)}</strong>`
      : `<em>siège vacant ou non rattaché dans la base</em>`;
    const groupe = hasDeputy && d.groupe
      ? `<span class="hover-card-group" style="background:${escapeHtml(d.couleur || '#777')}">${escapeHtml(d.groupe)}</span>`
      : "";
    const groupeLong = hasDeputy && d['groupeLibelle']
      ? `<div class="muted small">${escapeHtml(d['groupeLibelle'])}</div>` : "";
    const pop = fmtNum(d.pop);
    const inscr = fmtNum(d.inscrits);
    const stats = (pop || inscr)
      ? `<div class="hover-card-stats muted small">
           ${pop ? `<div><strong>${pop}</strong> habitants</div>` : ""}
           ${inscr ? `<div><strong>${inscr}</strong> inscrits sur les listes électorales</div>` : ""}
         </div>` : "";
    return `
      <div class="hover-card-head">
        ${photo}
        <div>
          ${name}
          <div class="muted small">${escapeHtml(d.circoName)}</div>
          <div class="muted small">${escapeHtml(d.deptName)}</div>
        </div>
      </div>
      <div class="hover-card-body">
        ${groupe} ${groupeLong}
      </div>
      ${stats}
      ${hasDeputy ? `<div class="hover-card-foot muted small">Cliquez pour voir la fiche complète →</div>` : ""}
    `;
  }
  if (card) {
    svg.querySelectorAll("path").forEach(path => {
      path.addEventListener("mouseenter", () => {
        const d = {
          uid: path.dataset.uid,
          nom: path.dataset.nom,
          groupe: path.dataset.groupe,
          groupeLibelle: path.dataset.groupeLibelle,
          couleur: path.dataset.couleur,
          deptName: path.dataset.deptName,
          circoName: path.dataset.circoName,
          pop: path.dataset.pop,
          inscrits: path.dataset.inscrits,
          votants: path.dataset.votants,
        };
        card.innerHTML = buildCardHtml(d);
        card.classList.add("visible");
      });
    });
    svg.addEventListener("mouseleave", () => card.classList.remove("visible"));
  }

  // -------------------------------------------------------------------
  // Legend → map highlighting (et HTML DOM rows aussi)
  // -------------------------------------------------------------------
  const legendItems = document.querySelectorAll(".legend-item[data-groupe-uid]");
  const allHighlightables = [
    ...svg.querySelectorAll("path[data-groupe-uid]"),
    ...document.querySelectorAll(".dom-row[data-groupe-uid]"),
  ];
  legendItems.forEach(item => {
    const uid = item.dataset.groupeUid;
    if (!uid) return;
    item.addEventListener("mouseenter", () => {
      svg.classList.add("highlight-mode");
      document.querySelector(".dom-grid")?.classList.add("highlight-mode");
      allHighlightables.forEach(el => {
        if (el.dataset.groupeUid === uid) el.classList.add("highlighted");
        else el.classList.add("dimmed");
      });
    });
    item.addEventListener("mouseleave", () => {
      svg.classList.remove("highlight-mode");
      document.querySelector(".dom-grid")?.classList.remove("highlight-mode");
      allHighlightables.forEach(el => {
        el.classList.remove("highlighted", "dimmed");
      });
    });
  });
})();
