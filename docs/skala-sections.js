// Shared section renderers so index.html and radar-map.html show the SAME
// elements + layout + sentences for the Budva overview (radar image previews,
// per-source detail tables, and the per-radar summary lines). Depends on
// skala-text.js (SKALA.interpret) for the wording.
(function (global) {
  'use strict';

  // Closest actual WET PIXEL near the point, from the precomputed rings. This is
  // the LOCAL intensity at/around the location — the same thing radar-map.html's
  // live pixel scan reports. We display THIS, not approaching.closest_rain_*,
  // because the pipeline (interpret.py) sets closest_rain_intensity_dbz to the
  // dominant CELL's peak max_dbz (the storm core, possibly far away). That peak
  // is much higher than the local value and made Budva read a far stronger dBZ
  // than any other clicked location.
  function closestWet(src) {
    var rings = (src && src.rings) || [];
    var best = null;
    for (var i = 0; i < rings.length; i++) {
      var r = rings[i];
      if (r.closest_wet_km != null && (best == null || r.closest_wet_km < best.km)) {
        best = { km: r.closest_wet_km, cardinal: r.closest_wet_bearing_cardinal, dbz: r.closest_wet_dbz };
      }
    }
    return best;
  }

  // Build the normalized facts SKALA.interpret() expects from one source's
  // precomputed ring/approach data (same shape on both pages).
  function factsFromSource(src, loc) {
    const app = (src && src.approaching) || {};
    const rings = (src && src.rings) || [];
    const motion = (src && src.motion) || {};
    // Cells beyond the outermost sampling ring (config.SAMPLE_RADII_KM max =
    // 150 km) are NOT monitored — a storm 200 km away is not a local approaching
    // or severe event. The pipeline's `dominant` cell + `is_approaching` are NOT
    // distance-bounded (it once flagged a 68 dBZ cell 209 km away as "severe
    // approaching"), so we bound them here.
    const VICINITY_MAX_KM = 150;
    // The dominant tracked storm cell the nowcast is following — may be a big cell
    // BEHIND a lighter closest one. Used ONLY to flag SEVERE, and only when it is
    // actually within the monitored vicinity.
    const dom = (app.nowcast_details && app.nowcast_details.dominant) || null;
    const domInRange = !!(dom && dom.dist_km != null && dom.dist_km <= VICINITY_MAX_KM);
    const threat = domInRange ? {
      dbz: dom.max_dbz, km: dom.dist_km, cardinal: dom.bearing_cardinal,
      eta: dom.eta_minutes, label: dom.intensity_label,
    } : (app.closest_rain_km != null && app.closest_rain_km <= VICINITY_MAX_KM ? {
      dbz: app.closest_rain_intensity_dbz, km: app.closest_rain_km,
      cardinal: app.closest_rain_bearing_cardinal, eta: app.eta_minutes,
      label: app.closest_rain_intensity_label,
    } : null);
    // Displayed km/intensity/dBZ = the LOCAL closest wet pixel (see closestWet),
    // so Budva reads the same magnitude as any other point. The dominant cell
    // (threat) is reserved for the SEVERE decision + its narrative.
    const cw = closestWet(src);
    // "Approaching" only counts if the driving cell is within the monitored
    // vicinity: the dominant cell is in range, OR there is a wet pixel in the
    // rings. Never approaching solely because of a cell beyond the rings.
    const approaching = !!app.is_approaching && (domInRange || !!cw);
    return {
      locationName: loc,
      rainAtLocation: !!app.rain_at_location,
      approaching: approaching,
      anyRain: !!app.any_rain_within_radii,
      anyWet: rings.some(r => (r.n_wet || 0) > 0),
      anyEcho: rings.some(r => (r.n_echo || 0) > 0),
      km: cw ? cw.km : app.closest_rain_km,
      cardinal: cw ? cw.cardinal : app.closest_rain_bearing_cardinal,
      dbz: cw ? cw.dbz : app.closest_rain_intensity_dbz,
      motionCardinal: app.motion_direction_cardinal || motion.direction_cardinal,
      eta: app.eta_minutes,
      threat: threat,
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
    // PREFERRED: the canonical verdict precomputed ONCE by the pipeline
    // (radar/verdict.py) — the same object the forecast page renders, so every
    // surface shows literally the same conclusion. The JS computation below
    // remains only as a fallback for data.js produced by older pipelines.
    var v = sum.budva_verdict;
    if (v && v.state && v.headline) {
      return { state: v.state, headline: v.headline, narrative: v.narrative,
               meta: v.style || (global.SKALA && global.SKALA.STATE_META[v.state]) || {} };
    }
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
        // PREFERRED: pipeline-precomputed per-source verdict (radar/verdict.py)
        // so the wording is decided in exactly one place; JS fallback for old data.
        const r = (info.verdict && info.verdict.state) ? info.verdict
          : ((global.SKALA && global.SKALA.interpret)
            ? global.SKALA.interpret(factsFromSource(info, locName))
            : { state: '?', narrative: '' });
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

  // Stale-image technical-fault check. Fires ONLY when EVERY available source
  // (DHMZ AND OPERA) has a frame older than thresholdMin (default 30) — if at
  // least one source is still fresh the page has usable radar, so no fault. The
  // pipeline/images not updating means the verdict (built from those frames)
  // can't be trusted. Returns a plain one-line message (no emoji/decoration)
  // naming the stale sources, or null otherwise. Frame timestamps carry no
  // timezone, so they parse as LOCAL time — the same convention the "Generated:"
  // line uses; the HH:MM shown is sliced from the string so display is tz-proof.
  function stalenessNotice(data, thresholdMin) {
    thresholdMin = thresholdMin || 30;
    var sources = (data && data.sources) || {};
    var now = Date.now();
    var stale = [];
    var total = 0;
    for (var sid in sources) {
      if (!Object.prototype.hasOwnProperty.call(sources, sid)) continue;
      total++;
      var info = sources[sid];
      var name = sid.toUpperCase();
      var ts = (info && info.frame_timestamp) ? new Date(info.frame_timestamp) : null;
      var ageMin = ts ? (now - ts.getTime()) / 60000 : NaN;
      if (!info || info.ok === false || !ts || isNaN(ageMin)) {
        stale.push('Posljednja ' + name + ' slika: nedostupna');
      } else if (ageMin > thresholdMin) {
        var hhmm = String(info.frame_timestamp).slice(11, 16); // "HH:MM"
        stale.push('Posljednja ' + name + ' slika: ' + hhmm
          + ' (prije ' + Math.round(ageMin) + ' min)');
      }
    }
    // Only a real fault when ALL sources are stale; a single fresh source clears it.
    if (total === 0 || stale.length < total) return null;
    return { message: 'Tehnička greška — nema novih radarskih slika. '
      + stale.join('. ') + '.' };
  }

  global.SKALA_SECTIONS = {
    factsFromSource: factsFromSource,
    budvaHeadline: budvaHeadline,
    renderSummaryLines: renderSummaryLines,
    renderImages: renderImages,
    renderSources: renderSources,
    stalenessNotice: stalenessNotice,
  };
})(window);
