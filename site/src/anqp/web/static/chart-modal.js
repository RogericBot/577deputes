/* 577députés — chart modal + client-side SVG renderer.
 *
 * Any element with `data-chart='[…json…]'` becomes a clickable trigger
 * that opens a centred modal showing a horizontal bar chart of the
 * data. Optional attrs :
 *   data-chart-title    title shown above the chart
 *   data-chart-subtitle one-liner, shown below the title (units, source)
 *   data-chart-unit     text appended to each value ("questions", "%", "j")
 * The modal is .sharable so the existing share.js attaches PNG export
 * buttons to it for free.
 */
(function () {
  const MAX_LABEL = 60;

  function escapeXml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function fmtInt(n) {
    if (n === null || n === undefined || n === "") return "—";
    const x = Number(n);
    if (!isFinite(x)) return String(n);
    return x.toLocaleString("fr-FR");
  }

  function buildHBarChart(items, opts) {
    const o = Object.assign({
      width: 1080,
      barH: 40,
      gap: 10,
      labelWidth: 380,
      paddingTop: 16,
      paddingBottom: 32,
      title: "",
      subtitle: "",
      unit: "",
    }, opts || {});
    const n = items.length;
    const valuesNum = items.map(it => Number(it.value) || 0);
    const vmax = Math.max(1, ...valuesNum);
    const headerH = (o.title || o.subtitle) ? 70 : 0;
    const innerH = n * (o.barH + o.gap) + o.paddingTop + o.paddingBottom;
    const totalH = headerH + innerH;
    const barAreaW = o.width - o.labelWidth - 80;

    let svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${o.width} ${totalH}" width="100%" height="${totalH}" class="hbar-chart-modal" role="img">`;
    if (o.title) {
      svg += `<text x="32" y="34" class="chart-title">${escapeXml(o.title)}</text>`;
    }
    if (o.subtitle) {
      svg += `<text x="32" y="58" class="chart-subtitle">${escapeXml(o.subtitle)}</text>`;
    }
    items.forEach((it, i) => {
      const y = headerH + o.paddingTop + i * (o.barH + o.gap);
      const v = valuesNum[i];
      const bw = Math.max(2, (v / vmax) * barAreaW);
      const color = it.color || "#4f46e5";
      const label = String(it.label || "");
      const truncLabel = label.length > MAX_LABEL ? label.slice(0, MAX_LABEL - 1) + "…" : label;
      const rank = i + 1;
      const valStr = fmtInt(v) + (o.unit ? ` ${o.unit}` : "");
      svg += `<g class="hbar-row">
        <text x="32" y="${y + o.barH * 0.7}" class="hbar-rank">${rank}.</text>
        <text x="68" y="${y + o.barH * 0.7}" class="hbar-label">${escapeXml(truncLabel)}</text>
        <rect x="${o.labelWidth}" y="${y}" width="${bw}" height="${o.barH}" fill="${color}" rx="6"/>
        <text x="${o.labelWidth + bw + 8}" y="${y + o.barH * 0.7}" class="hbar-value">${escapeXml(valStr)}</text>
      </g>`;
    });
    svg += `</svg>`;
    return svg;
  }

  function openModal({ title, subtitle, unit, data }) {
    document.querySelectorAll(".chart-modal-backdrop").forEach(n => n.remove());
    const back = document.createElement("div");
    back.className = "chart-modal-backdrop";
    const modal = document.createElement("div");
    modal.className = "chart-modal sharable";
    modal.setAttribute("data-share-title", title || "Graphique");
    const close = document.createElement("button");
    close.className = "chart-modal-close";
    close.type = "button";
    close.setAttribute("aria-label", "Fermer");
    close.textContent = "×";
    modal.appendChild(close);

    const inner = document.createElement("div");
    inner.className = "chart-modal-inner";
    inner.innerHTML = buildHBarChart(data, {
      title: title || "",
      subtitle: subtitle || "",
      unit: unit || "",
    });
    modal.appendChild(inner);

    back.appendChild(modal);
    document.body.appendChild(back);

    const dismiss = () => back.remove();
    close.addEventListener("click", dismiss);
    back.addEventListener("click", e => { if (e.target === back) dismiss(); });
    document.addEventListener("keydown", function escHandler(e) {
      if (e.key === "Escape") {
        dismiss();
        document.removeEventListener("keydown", escHandler);
      }
    });

    // Attach PNG export buttons scoped to the modal only.
    if (typeof window.__attachShareButtons === "function") {
      window.__attachShareButtons(modal);
    }
  }

  function buildLineChart(payload, opts) {
    const o = Object.assign({
      width: 1200, height: 600, paddingX: 80, paddingY: 90,
      title: "", subtitle: "",
    }, opts || {});
    const series = payload.series || [];
    const xKeys = payload.x_keys || [];
    if (!series.length || !xKeys.length) return "";

    const allValues = [];
    series.forEach(s => (s.values || []).forEach(v => v != null && allValues.push(Number(v))));
    const vmax = allValues.length ? Math.max(...allValues) : 1;
    const innerW = o.width - 2 * o.paddingX;
    const innerH = o.height - 2 * o.paddingY;
    const stepX = innerW / Math.max(1, xKeys.length - 1);

    let svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${o.width} ${o.height}" width="100%" height="${o.height}" class="hbar-chart-modal" role="img">`;
    if (o.title) svg += `<text x="32" y="38" class="chart-title">${escapeXml(o.title)}</text>`;
    if (o.subtitle) svg += `<text x="32" y="62" class="chart-subtitle">${escapeXml(o.subtitle)}</text>`;

    // axes
    const x0 = o.paddingX;
    const y0 = o.height - o.paddingY;
    svg += `<line x1="${x0}" y1="${y0}" x2="${x0 + innerW}" y2="${y0}" stroke="#cbd5e1" stroke-width="1"/>`;
    svg += `<line x1="${x0}" y1="${o.paddingY}" x2="${x0}" y2="${y0}" stroke="#cbd5e1" stroke-width="1"/>`;

    // horizontal grid lines + y labels (5 levels)
    for (let i = 0; i <= 4; i++) {
      const yy = o.paddingY + (innerH * i / 4);
      const vv = vmax * (1 - i / 4);
      svg += `<line x1="${x0}" y1="${yy}" x2="${x0 + innerW}" y2="${yy}" stroke="#e2e8f0" stroke-width="0.6" stroke-dasharray="3 3"/>`;
      svg += `<text x="${x0 - 8}" y="${yy + 5}" text-anchor="end" font-size="13" fill="#64748b">${fmtInt(Math.round(vv))}</text>`;
    }

    // series
    series.forEach(s => {
      const color = s.color || "#3b82f6";
      const pts = (s.values || []).map((v, i) => {
        const x = x0 + i * stepX;
        const y = y0 - ((Number(v) || 0) / vmax) * innerH;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      });
      svg += `<polyline points="${pts.join(" ")}" fill="none" stroke="${color}" stroke-width="3"/>`;
      (s.values || []).forEach((v, i) => {
        const x = x0 + i * stepX;
        const y = y0 - ((Number(v) || 0) / vmax) * innerH;
        svg += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="4" fill="${color}"/>`;
      });
    });

    // x-axis labels (every Nth to avoid crowding)
    const labelStep = Math.ceil(xKeys.length / 12);
    xKeys.forEach((k, i) => {
      if (i % labelStep === 0 || i === xKeys.length - 1) {
        const x = x0 + i * stepX;
        svg += `<text x="${x}" y="${y0 + 22}" text-anchor="middle" font-size="13" fill="#64748b">${escapeXml(String(k))}</text>`;
      }
    });

    // legend (top, after subtitle)
    const legendY = o.subtitle ? 84 : 60;
    series.forEach((s, i) => {
      const lx = 32 + i * 220;
      svg += `<rect x="${lx}" y="${legendY - 12}" width="14" height="14" fill="${s.color || '#3b82f6'}" rx="2"/>`;
      svg += `<text x="${lx + 22}" y="${legendY}" font-size="14" fill="#0f172a">${escapeXml(s.name || ("série " + (i+1)))}</text>`;
    });

    svg += `</svg>`;
    return svg;
  }

  function openLineModal({ title, subtitle, payload }) {
    document.querySelectorAll(".chart-modal-backdrop").forEach(n => n.remove());
    const back = document.createElement("div");
    back.className = "chart-modal-backdrop";
    const modal = document.createElement("div");
    modal.className = "chart-modal sharable";
    modal.setAttribute("data-share-title", title || "Graphique");
    const close = document.createElement("button");
    close.className = "chart-modal-close";
    close.type = "button";
    close.setAttribute("aria-label", "Fermer");
    close.textContent = "×";
    modal.appendChild(close);
    const inner = document.createElement("div");
    inner.className = "chart-modal-inner";
    inner.innerHTML = buildLineChart(payload, { title: title || "", subtitle: subtitle || "" });
    modal.appendChild(inner);
    back.appendChild(modal);
    document.body.appendChild(back);
    const dismiss = () => back.remove();
    close.addEventListener("click", dismiss);
    back.addEventListener("click", e => { if (e.target === back) dismiss(); });
    document.addEventListener("keydown", function escHandler(e) {
      if (e.key === "Escape") { dismiss(); document.removeEventListener("keydown", escHandler); }
    });
    if (typeof window.__attachShareButtons === "function") {
      window.__attachShareButtons(modal);
    }
  }

  document.addEventListener("click", e => {
    // Line chart trigger: data-chart-line='{"series":[…],"x_keys":[…]}'
    const lineTrigger = e.target.closest("[data-chart-line]");
    if (lineTrigger) {
      e.preventDefault();
      let payload;
      try { payload = JSON.parse(lineTrigger.dataset.chartLine); }
      catch (err) { console.error("invalid line chart JSON", err); return; }
      if (!payload || !payload.series || !payload.series.length) return;
      openLineModal({
        title: lineTrigger.dataset.chartTitle,
        subtitle: lineTrigger.dataset.chartSubtitle,
        payload,
      });
      return;
    }
    // Hbar trigger (existing)
    const trigger = e.target.closest("[data-chart]");
    if (!trigger) return;
    e.preventDefault();
    let data;
    try { data = JSON.parse(trigger.dataset.chart); }
    catch (err) { console.error("invalid chart JSON", err); return; }
    if (!Array.isArray(data) || data.length === 0) return;
    openModal({
      title: trigger.dataset.chartTitle,
      subtitle: trigger.dataset.chartSubtitle,
      unit: trigger.dataset.chartUnit,
      data,
    });
  });
})();
