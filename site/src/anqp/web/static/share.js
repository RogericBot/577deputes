/* anqp — convert any .sharable section into a downloadable PNG.
 *
 * Strategy : we draw an HTML/SVG snapshot using canvas. There is no
 * 100% pure HTML-to-canvas in a browser without bundled libraries, so
 * we only rasterise SVG charts and tables-of-numbers (the most useful
 * elements). For everything else we fall back to "screenshot the
 * <section>" via SVG <foreignObject> which respects CSS in modern
 * browsers (Chrome / Firefox / Edge). No external dependency.
 *
 * Each .sharable element gets a small "Télécharger l'image" button.
 */
(function () {
  const FORMATS = {
    "Carré 1080×1080": { w: 1080, h: 1080, name: "carre" },
    "Paysage 1200×628": { w: 1200, h: 628, name: "paysage" },
  };
  const FOOTER_TEXT_DEFAULT = "577députés — données : data.assemblee-nationale.fr";

  function escapeXml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function buildSvg(node, w, h, title, footer) {
    // Wrap the rendered HTML in <foreignObject>. We need to inline ALL
    // computed styles for cross-browser fidelity — we use the document's
    // stylesheet text instead, which is the local /static/style.css.
    const styles = Array.from(document.styleSheets)
      .map(s => {
        try {
          return Array.from(s.cssRules).map(r => r.cssText).join("\n");
        } catch (e) { return ""; }
      })
      .join("\n");
    const cloned = node.cloneNode(true);
    // Remove buttons that should NOT appear in the screenshot.
    cloned.querySelectorAll(".share-bar").forEach(el => el.remove());
    const html = cloned.outerHTML;
    const titleHtml = title ? `<div class="share-title">${escapeXml(title)}</div>` : "";

    return `
<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
  <foreignObject width="100%" height="100%">
    <div xmlns="http://www.w3.org/1999/xhtml" style="background:#fff;width:${w}px;height:${h}px;display:flex;flex-direction:column;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;">
      <style>${styles}
        .share-frame { padding: 28px 32px; flex: 1; overflow: hidden; }
        .share-title { font-size: 22px; font-weight: 700; margin-bottom: 16px; color: #0f172a; }
        .share-footer {
          padding: 14px 32px; background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 50%, #db2777 100%);
          color: white; font-size: 14px; display: flex; justify-content: space-between;
          align-items: center;
        }
        .share-footer strong { font-weight: 700; letter-spacing: .04em; text-transform: uppercase; }
      </style>
      <div class="share-frame">
        ${titleHtml}
        ${html}
      </div>
      <div class="share-footer">
        <strong>577députés</strong>
        <span>${escapeXml(footer)}</span>
      </div>
    </div>
  </foreignObject>
</svg>`;
  }

  function svgToPng(svgText, w, h, fileName) {
    const blob = new Blob([svgText], { type: "image/svg+xml;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = w * 2;     // 2x for retina sharpness
      canvas.height = h * 2;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.scale(2, 2);
      ctx.drawImage(img, 0, 0, w, h);
      URL.revokeObjectURL(url);
      canvas.toBlob(b => {
        const a = document.createElement("a");
        a.href = URL.createObjectURL(b);
        a.download = fileName;
        document.body.appendChild(a); a.click(); a.remove();
      }, "image/png");
    };
    img.onerror = err => {
      console.error("PNG export failed", err);
      alert("L'export PNG a échoué (probablement une image distante non-CORS). " +
            "Réessayez sans photos ou utilisez la capture d'écran du navigateur.");
    };
    img.src = url;
  }

  function attachShareButtons(root) {
    const baseUrl = window.location.origin + window.location.pathname;
    const scope = root || document;
    scope.querySelectorAll(".sharable").forEach(node => {
      if (node.querySelector(":scope > .share-bar")) return;
      const title = node.dataset.shareTitle || (node.querySelector("h1,h2,h3")?.textContent || "").trim();
      const bar = document.createElement("div");
      bar.className = "share-bar";
      Object.entries(FORMATS).forEach(([label, fmt]) => {
        const btn = document.createElement("button");
        btn.type = "button"; btn.className = "btn-share";
        btn.textContent = label;
        btn.onclick = (e) => {
          e.preventDefault(); e.stopPropagation();
          const svg = buildSvg(node, fmt.w, fmt.h, title, FOOTER_TEXT_DEFAULT + "  ·  " + baseUrl);
          const fileName = "577deputes-" + fmt.name + "-" + (title || "export").toLowerCase()
            .replace(/[^a-z0-9]+/g, "-").slice(0, 60) + ".png";
          svgToPng(svg, fmt.w, fmt.h, fileName);
        };
        bar.appendChild(btn);
      });
      node.insertBefore(bar, node.firstChild);
    });
  }

  // Note: we DO NOT auto-attach on page load. Rasterising a full card
  // with images + CSS via SVG foreignObject is unreliable across
  // browsers. The PNG export is now ONLY exposed inside the chart modal
  // (which renders a clean SVG and is reliably exportable).
  window.__attachShareButtons = attachShareButtons;
})();
