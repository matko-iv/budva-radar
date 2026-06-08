/* inlined terrain-budva.js */
// Budva terrain for the weather sandbox. Prefers the REAL baked elevation model
// (terrain-data.js: window.TERRAIN_DEM, from AWS terrarium SRTM/Copernicus tiles)
// and only falls back to a coarse hand-built heightmap when that data is absent
// (e.g. the Node unit test, or terrain-data.js failed to load).
//
// UMD: browser window.TERRAIN_BUDVA + Node. i=east, j=north; Budva at the box
// centre. build(N, L) -> { height: Float32Array(N*N), sea: Uint8Array(N*N) }.
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TERRAIN_BUDVA = factory();
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function gauss(x, s) { var t = x / s; return Math.exp(-0.5 * t * t); }
  function smoothstep(x, a, b) { var t = Math.min(1, Math.max(0, (x - a) / (b - a))); return t * t * (3 - 2 * t); }

  // --- REAL DEM path -------------------------------------------------------
  // Decode the base64 little-endian Int16 heightmap shipped in terrain-data.js.
  var _demCache = null;
  function decodeDem() {
    if (_demCache !== null) return _demCache;
    var glob = (typeof self !== 'undefined') ? self : (typeof window !== 'undefined' ? window : {});
    var dem = glob.TERRAIN_DEM;
    if (!dem || !dem.height_b64 || typeof atob !== 'function') { _demCache = false; return false; }
    try {
      var bin = atob(dem.height_b64);
      var bytes = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      _demCache = { n: dem.n, l: dem.l, height: new Int16Array(bytes.buffer) };
    } catch (e) { _demCache = false; }
    return _demCache;
  }

  // Bilinearly resample the M x M DEM onto the requested N x N box. Both grids use
  // the same (idx+0.5)/count cell-centre convention over the SAME geographic box,
  // so sim cell i maps to DEM coordinate (i+0.5)*M/N - 0.5.
  function buildFromDem(dem, N, L) {
    var M = dem.n, H = dem.height;
    var height = new Float32Array(N * N), sea = new Uint8Array(N * N);
    var sxScale = M / N, syScale = M / N;
    for (var j = 0; j < N; j++) {
      var gy = (j + 0.5) * syScale - 0.5;
      var j0 = Math.floor(gy); var fy = gy - j0;
      var j0c = j0 < 0 ? 0 : (j0 > M - 1 ? M - 1 : j0);
      var j1c = j0 + 1 < 0 ? 0 : (j0 + 1 > M - 1 ? M - 1 : j0 + 1);
      for (var i = 0; i < N; i++) {
        var gx = (i + 0.5) * sxScale - 0.5;
        var i0 = Math.floor(gx); var fx = gx - i0;
        var i0c = i0 < 0 ? 0 : (i0 > M - 1 ? M - 1 : i0);
        var i1c = i0 + 1 < 0 ? 0 : (i0 + 1 > M - 1 ? M - 1 : i0 + 1);
        var a = H[j0c * M + i0c], b = H[j0c * M + i1c];
        var c = H[j1c * M + i0c], d = H[j1c * M + i1c];
        var h = (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy;
        var k = j * N + i;
        if (h <= 0) { height[k] = 0; sea[k] = 1; }
        else { height[k] = h; sea[k] = 0; }
      }
    }
    return { height: height, sea: sea };
  }

  // --- procedural fallback (coarse, geographically-flavoured) --------------
  // Sea to the S/SW, mountains (Lovcen) rising to the N/NW. Only used when the
  // real DEM isn't available.
  function buildProcedural(N, L) {
    var height = new Float32Array(N * N), sea = new Uint8Array(N * N);
    var dxkm = (L / N) / 1000, half = L / 2000;     // km
    for (var j = 0; j < N; j++) for (var i = 0; i < N; i++) {
      var xe = (i + 0.5) * dxkm - half;             // km east of Budva
      var yn = (j + 0.5) * dxkm - half;             // km north of Budva
      var coast = -0.4 + 0.12 * xe;
      var inland = yn - coast;                      // km north of the shoreline
      var k = j * N + i, h, isSea;
      if (inland <= 0) { h = 0; isSea = 1; }
      else {
        isSea = 0;
        var base = 320 * inland;                    // coastal slope ~320 m/km
        var ridge = 1500 * gauss(xe + 1.2, 2.4) * smoothstep(inland, 1.2, 4.0);
        h = Math.min(1850, base + ridge);
      }
      height[k] = h; sea[k] = isSea;
    }
    return { height: height, sea: sea };
  }

  // Build height (m) + sea mask over an L-metre square box, N x N.
  function build(N, L) {
    var dem = decodeDem();
    // Only use the DEM when it covers the same box extent the sim is asking for.
    if (dem && Math.abs(dem.l - L) < 1.0) return buildFromDem(dem, N, L);
    return buildProcedural(N, L);
  }

  return { build: build, buildProcedural: buildProcedural, usingRealDem: function () { return !!decodeDem(); } };
}));

