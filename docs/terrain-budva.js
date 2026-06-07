// Coarse, geographically-flavoured Budva terrain for the weather sandbox.
// UMD: browser window.TERRAIN_BUDVA + Node. i=east, j=north; Budva at the box
// centre, on the shoreline. Sea to the S/SW, mountains (Lovćen) rising to the
// N/NW. APPROXIMATE hand-built heightmap — swap for a real DEM (SRTM) later.
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TERRAIN_BUDVA = factory();
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function gauss(x, s) { var t = x / s; return Math.exp(-0.5 * t * t); }
  function smoothstep(x, a, b) { var t = Math.min(1, Math.max(0, (x - a) / (b - a))); return t * t * (3 - 2 * t); }

  // Build height (m) + sea mask over an L-metre square box, N×N.
  function build(N, L) {
    var height = new Float32Array(N * N), sea = new Uint8Array(N * N);
    var dxkm = (L / N) / 1000, half = L / 2000;     // km
    for (var j = 0; j < N; j++) for (var i = 0; i < N; i++) {
      var xe = (i + 0.5) * dxkm - half;             // km east of Budva
      var yn = (j + 0.5) * dxkm - half;             // km north of Budva
      // shoreline: gently tilted ~E-W line just south of Budva; sea to the south
      var coast = -0.4 + 0.12 * xe;
      var inland = yn - coast;                      // km north of the shoreline
      var k = j * N + i, h, isSea;
      if (inland <= 0) { h = 0; isSea = 1; }
      else {
        isSea = 0;
        var base = 320 * inland;                    // coastal slope ~320 m/km
        // Lovćen massif: a high ridge to the N, biased NW
        var ridge = 1500 * gauss(xe + 1.2, 2.4) * smoothstep(inland, 1.2, 4.0);
        h = Math.min(1850, base + ridge);
      }
      height[k] = h; sea[k] = isSea;
    }
    return { height: height, sea: sea };
  }

  return { build: build };
}));
