// Shared section renderers so index.html and radar-map.html show the SAME
// elements + layout + sentences for the Budva overview (radar image previews,
// per-source detail tables, and the per-radar summary lines). Depends on
// skala-text.js (SKALA.interpret) for the wording.
(function (global) {
  'use strict';

  // Build the normalized facts SKALA.interpret() expects from one source's
  // precomputed ring/approach data (same shape on both pages).
  function factsFromSource(src, loc) {
    const app = (src && src.approaching) || {};
    const rings = (src && src.rings) || [];
    const motion = (src && src.motion) || {};
    return {
      locationName: loc,
      rainAtLocation: !!app.rain_at_location,
      approaching: !!app.is_approaching,
      anyRain: !!app.any_rain_within_radii,
      anyWet: rings.some(r => (r.n_wet || 0) > 0),
      anyEcho: rings.some(r => (r.n_echo || 0) > 0),
      km: app.closest_rain_km,
      cardinal: app.closest_rain_bearing_cardinal,
      dbz: app.closest_rain_intensity_dbz,
      motionCardinal: app.motion_direction_cardinal || motion.direction_cardinal,
      eta: app.eta_minutes,
    };
  }

  // THE single headline both pages show for Budva: the DHMZ probabilistic
  // nowcast (the high-res local radar's cell-tracking forecast) run through the
  // shared interpreter — the SAME data the per-source DHMZ line uses, so the
  // headline and the detail can never contradict each other. Deliberately NOT
  // the OPERA-blended composite summary (whose coarse nowcast over-triggers
  // "approaching"), and NOT single-frame motion geometry (whose 15 km range gate
  // dismisses a severe cell that is farther out but barreling straight in).
  // index.html uses this for its banner; radar-map.html uses it for the Budva
  // location so the two pages can never disagree.
  function budvaHeadline(data) {
    data = data || {};
    var sources = data.sources || {};
    var sum = data.summary || {};
    var loc = (data.location && data.location.name) || 'this location';
    var srcId = (sources.dhmz && sources.dhmz.ok)
      ? 'dhmz' : (sum.scenario_source || Object.keys(sources)[0]);
    return global.SKALA.interpret(factsFromSource(sources[srcId], loc));
  }

  // Per-radar summary lines (one coherent sentence per source) into a <ul>.
  function renderSummaryLines(data, listEl) {
    if (!listEl) return;
    const locName = (data.location && data.location.name) || 'this location';
    listEl.innerHTML = '';
    for (const [sid, info] of Object.entries(data.sources || {})) {
      const li = document.createElement('li');
      if (!info || !info.ok) {
        li.textContent = sid.toUpperCase() + ': unavailable';
      } else {
        const r = (global.SKALA && global.SKALA.interpret)
          ? global.SKALA.interpret(factsFromSource(info, locName))
          : { state: '?', narrative: '' };
        li.textContent = sid.toUpperCase() + ': [' + r.state + '] ' + r.narrative;
      }
      listEl.appendChild(li);
    }
  }

  // Radar image previews with Budva marker + concentric sampling-ring overlay.
  function renderImages(data, gridEl) {
    if (!gridEl) return;
    gridEl.innerHTML = '';
    const radii = data.radii_km || [10, 25, 50, 100, 150];
    for (const [src, info] of Object.entries(data.sources || {})) {
      if (!info.ok || !info.budva_pixel || !info.image_size) continue;
      const fileName = src === 'dhmz' ? 'latest_dhmz.png' : 'latest_opera.gif';
      const [imgW, imgH] = info.image_size;
      const bx = info.budva_pixel.x, by = info.budva_pixel.y;
      const pxPerKm = info.px_per_km || 1.0;
      let svgInner = '';
      radii.forEach(km => {
        const r = km * pxPerKm;
        svgInner += `<circle cx="${bx}" cy="${by}" r="${r}" fill="none" stroke="#ffffff" stroke-width="${Math.max(1, imgW / 600)}" stroke-dasharray="6 4" opacity="0.55"/>`;
        const labelY = by - r - 2;
        svgInner += `<text x="${bx}" y="${labelY}" fill="#ffffff" stroke="#000000" stroke-width="0.4" font-size="${Math.max(9, imgW / 65)}" text-anchor="middle" font-family="sans-serif">${km}km</text>`;
      });
      svgInner += `<circle cx="${bx}" cy="${by}" r="${Math.max(2.5, imgW / 250)}" fill="#d32f2f" stroke="#ffffff" stroke-width="1"/>`;
      const wrap = document.createElement('div');
      wrap.className = 'image-wrap';
      wrap.innerHTML = `
        <h3>${src.toUpperCase()} — ${info.frame_timestamp || ''}</h3>
        <div class="radar-canvas">
          <img src="${fileName}?t=${Date.now()}" alt="${src} radar">
          <svg class="overlay" viewBox="0 0 ${imgW} ${imgH}" preserveAspectRatio="none">${svgInner}</svg>
        </div>`;
      gridEl.appendChild(wrap);
    }
  }

  // Per-source detailed ring table (raw per-ring data, NOT a competing
  // interpretation). Includes a beam-confidence column when present.
  function renderSources(data, gridEl) {
    if (!gridEl) return;
    gridEl.innerHTML = '';
    for (const [src, info] of Object.entries(data.sources || {})) {
      const div = document.createElement('div');
      div.className = 'source-card';
      let html = '<h3>' + src.toUpperCase() + '</h3>';
      if (!info || !info.ok) {
        html += '<p class="err">Unavailable: ' + ((info && info.reason) || '?') + '</p>';
        div.innerHTML = html;
        gridEl.appendChild(div);
        continue;
      }
      html += '<p class="ts">Frame: ' + (info.frame_timestamp || '?') + '</p>';
      if (info.motion) {
        const dir = info.motion.direction_cardinal || '?';
        const spd = info.motion.speed_kmh != null ? info.motion.speed_kmh + ' km/h' : '?';
        const conf = (info.motion.confidence || 0).toFixed(2);
        html += '<p>Motion: ' + dir + ' @ ' + spd + ' (conf ' + conf + ')</p>';
      }
      html += '<table><thead><tr>'
        + '<th title="Ring radius in km">R (km)</th>'
        + '<th title="Speckle-filtered count of pixels >= 20 dBZ (rain) / threshold">rain px</th>'
        + '<th title="Total echo >= 5 dBZ (rain + sub-rain trace)">echo px</th>'
        + '<th>max dBZ</th>'
        + '<th title="Persistence: was the same ring wet in the previous scan too?">conf</th>'
        + '<th>bearing</th>'
        + '</tr></thead><tbody>';
      (info.rings || []).forEach(ring => {
        const nWet = ring.n_wet || 0;
        const nWetThreshold = ring.min_wet_threshold || 5;
        const nEcho = ring.n_echo || 0;
        const confirmed = !!ring.confirmed;
        const hasRain = nWet >= nWetThreshold && confirmed;
        const hasCandidate = nWet >= nWetThreshold && !confirmed;
        const bg = hasRain ? '#fff8e1' : (hasCandidate ? '#f3f3f3' : '');
        html += '<tr' + (bg ? ' style="background:' + bg + '"' : '') + '>'
          + '<td>' + ring.radius_km + '</td>'
          + '<td>' + nWet + ' / ' + nWetThreshold + '</td>'
          + '<td>' + nEcho + '</td>'
          + '<td>' + (ring.max_dbz != null ? ring.max_dbz : '–') + '</td>'
          + '<td>' + (confirmed ? '✓' : (ring.persistence_scans === 1 ? '?' : '–')) + '</td>'
          + '<td>' + (ring.strongest_bearing_cardinal || '–') + '</td>'
          + '</tr>';
      });
      html += '</tbody></table>';
      div.innerHTML = html;
      gridEl.appendChild(div);
    }
  }

  global.SKALA_SECTIONS = {
    factsFromSource: factsFromSource,
    budvaHeadline: budvaHeadline,
    renderSummaryLines: renderSummaryLines,
    renderImages: renderImages,
    renderSources: renderSources,
  };
})(window);
