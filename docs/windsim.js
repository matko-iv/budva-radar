// Budva weather sandbox — CPU physics core (UMD: browser window.WINDSIM + Node).
//
// Plan-view (top-down map) single-layer moist atmosphere on an N×N collocated
// grid over an L-km box centred on Budva. CPU Float32Array so the physics is
// unit-testable in Node (see tests/test_windsim.js). Real-ish equations, toy
// scale — NOT a forecast.
//
// Fields (all length N*N, index idx(i,j)=j*N+i; i=east x, j=north y):
//   u,v   horizontal wind (m/s)            p     pressure correction (proj.)
//   T     temperature perturbation (K)     qv    water vapour      (g/kg)
//   qc    cloud water (g/kg)               qr    rain water        (g/kg)
// Terrain: hgt (m), solid (1=blocked), sea (1=water surface).
//
// Per step(dt): forcing (nudge to global wind + surface heating) → semi-Lagrangian
// advection of every field → moist column physics (condensation+latent heat,
// autoconversion, rain fallout/evap) → pressure projection to a THERMALLY-FORCED
// target divergence (warm/condensing columns converge => sea breeze + convection),
// with terrain as a no-penetration obstacle.
//
// This file (Stage A) implements the wind + terrain + projection core and the
// field scaffolding; moisture/thermics terms are present and individually
// testable. Tuned for stability over realism; constants in C are easy to change.
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.WINDSIM = factory();
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var C = {
    WIND_NUDGE: 0.5,        // 1/s, relax open-air wind toward the global wind
    PROJ_ITERS: 40,         // red-black Gauss-Seidel sweeps for the pressure solve
    SOLID_HEIGHT_M: 600,    // terrain above this is a solid obstacle
    // moist + thermal coupling
    Lv_over_cp: 2.5,        // K per g/kg condensed (latent heating, scaled)
    QVS0: 12,               // saturation vapour (g/kg) at the base temperature
    QC_AUTOCONV: 0.5,       // g/kg cloud water above which it rains
    AUTOCONV_RATE: 0.1,     // 1/s
    RAIN_FALL: 0.2,         // 1/s rain removed (falls out of the column -> surface)
    RAIN_EVAP: 0.02,        // 1/s rain re-evaporates when subsaturated
    COND_RATE: 0.5,         // 1/s relaxation of supersaturation into cloud
    THERMAL_DIV: 0.01,      // 1/(s·K) convergence per K of buoyancy
    BUOY_FROM_CLOUD: 0.3,   // extra buoyancy (K) per g/kg cloud (latent feedback)
    // surface energy (Stage C): solar in [0,1] heats land >> sea; radiative cooling
    LAND_HEAT: 2.0,         // K/s at full sun over land
    SEA_HEAT: 0.2,          // K/s at full sun over sea (high heat capacity)
    RADCOOL: 0.05,          // 1/s relaxation of T back toward 0 (longwave cooling)
  };

  function createSim(opts) {
    opts = opts || {};
    var N = opts.N || 128;
    var L = opts.L || 10000;          // metres (domain edge length)
    var dx = L / N;                   // metres per cell
    var n2 = N * N;

    var F = function () { return new Float32Array(n2); };
    var u = F(), v = F(), u0 = F(), v0 = F();
    var p = F(), div = F();
    var T = F(), qv = F(), qc = F(), qr = F();
    var tmp = F();
    var hgt = F(), solid = new Uint8Array(n2), sea = new Uint8Array(n2);

    var Ux = 0, Uy = 0;               // global (large-scale) wind, m/s
    var rainAccum = F();              // surface rain accumulation (diagnostic)

    function idx(i, j) { return j * N + i; }
    function clampi(x, lo, hi) { return x < lo ? lo : (x > hi ? hi : x); }

    if (opts.terrain) setTerrain(opts.terrain);

    function setTerrain(t) {
      // t: { height: Float32Array(n2), sea?: Uint8Array(n2) } OR a function (i,j)->m
      for (var j = 0; j < N; j++) for (var i = 0; i < N; i++) {
        var k = idx(i, j);
        var h = typeof t === 'function' ? t(i, j) : (t.height ? t.height[k] : 0);
        hgt[k] = h;
        solid[k] = h > C.SOLID_HEIGHT_M ? 1 : 0;
        sea[k] = t.sea ? t.sea[k] : (h <= 0 ? 1 : 0);
      }
    }

    function setWind(dirDeg, speedMs) {
      // meteorological compass: dir = where wind comes FROM. Vector points TO.
      var to = (dirDeg + 180) * Math.PI / 180;
      Ux = speedMs * Math.sin(to);    // east component
      Uy = speedMs * Math.cos(to);    // north component
    }

    // bilinear sample of a field at grid coords (gx,gy) in cell units
    function sampleField(f, gx, gy) {
      gx = clampi(gx, 0, N - 1.001); gy = clampi(gy, 0, N - 1.001);
      var i0 = gx | 0, j0 = gy | 0, i1 = i0 + 1, j1 = j0 + 1;
      var fx = gx - i0, fy = gy - j0;
      var a = f[idx(i0, j0)], b = f[idx(i1, j0)], c = f[idx(i0, j1)], d = f[idx(i1, j1)];
      return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy;
    }

    // velocity (m/s) at world km-offset from Budva (east,north). For consumers.
    function sampleVel(xKm, yKm) {
      var gx = (xKm * 1000 + L / 2) / dx - 0.5;
      var gy = (yKm * 1000 + L / 2) / dx - 0.5;
      return [sampleField(u, gx, gy), sampleField(v, gx, gy)];
    }

    // --- step pieces -------------------------------------------------------
    function addForces(dt) {
      var a = C.WIND_NUDGE * dt;
      for (var k = 0; k < n2; k++) {
        if (solid[k]) { u[k] = 0; v[k] = 0; continue; }
        u[k] += a * (Ux - u[k]);
        v[k] += a * (Uy - v[k]);
      }
    }

    function advect(field, dst, dt) {
      // semi-Lagrangian backtrace in cell units (dt seconds, velocity m/s)
      var s = dt / dx;
      for (var j = 0; j < N; j++) for (var i = 0; i < N; i++) {
        var k = idx(i, j);
        if (solid[k]) { dst[k] = field[k]; continue; }
        var gx = i - s * u[k], gy = j - s * v[k];
        dst[k] = sampleField(field, gx, gy);
      }
    }

    function advectVelocity(dt) {
      advect(u, u0, dt); advect(v, v0, dt);
      u.set(u0); v.set(v0);
    }

    function advectScalars(dt) {
      advect(T, tmp, dt); T.set(tmp);
      advect(qv, tmp, dt); qv.set(tmp);
      advect(qc, tmp, dt); qc.set(tmp);
      advect(qr, tmp, dt); qr.set(tmp);
    }

    // Surface energy budget: solar (0..1) heats land strongly, sea weakly; a gentle
    // radiative relaxation pulls T back to 0. This temperature contrast is what the
    // thermal-divergence term turns into a sea breeze.
    function surfaceHeat(dt, solar) {
      for (var k = 0; k < n2; k++) {
        if (solid[k]) continue;
        var heat = (sea[k] ? C.SEA_HEAT : C.LAND_HEAT) * solar;
        T[k] += (heat - C.RADCOOL * T[k]) * dt;
      }
    }

    // Warm-rain column microphysics: condensation (qv->qc) with latent heating,
    // evaporation of cloud when subsaturated, autoconversion (qc->qr), and rain
    // fallout to the surface with partial re-evaporation. Conserves total water
    // (qv+qc+qr+rainAccum) up to the heating bookkeeping.
    function moisture(dt) {
      var cr = Math.min(1, C.COND_RATE * dt);
      for (var k = 0; k < n2; k++) {
        if (solid[k]) continue;
        var qvs = C.QVS0 * Math.exp(0.07 * T[k]);
        if (qv[k] > qvs) {
          var dq = (qv[k] - qvs) * cr;
          qv[k] -= dq; qc[k] += dq; T[k] += C.Lv_over_cp * dq;
        } else if (qc[k] > 0) {
          var ev = Math.min(qc[k], (qvs - qv[k]) * cr);
          if (ev > 0) { qc[k] -= ev; qv[k] += ev; T[k] -= C.Lv_over_cp * ev; }
        }
        if (qc[k] > C.QC_AUTOCONV) {
          var ac = (qc[k] - C.QC_AUTOCONV) * Math.min(1, C.AUTOCONV_RATE * dt);
          qc[k] -= ac; qr[k] += ac;
        }
        if (qr[k] > 0) {
          var fall = qr[k] * Math.min(1, C.RAIN_FALL * dt);
          qr[k] -= fall; rainAccum[k] += fall;
          var qvs2 = C.QVS0 * Math.exp(0.07 * T[k]);
          if (qv[k] < qvs2) {
            var re = Math.min(qr[k], (qvs2 - qv[k]) * Math.min(1, C.RAIN_EVAP * dt));
            qr[k] -= re; qv[k] += re; T[k] -= C.Lv_over_cp * re;
          }
        }
      }
    }

    // Thermally-forced target divergence: warm/cloudy (buoyant) columns draw in
    // surface air (convergence, div<0). Returns into `div`.
    function thermalDivergence() {
      for (var k = 0; k < n2; k++) {
        if (solid[k]) { div[k] = 0; continue; }
        var buoy = T[k] + C.BUOY_FROM_CLOUD * qc[k];
        div[k] = -C.THERMAL_DIV * buoy;     // convergence under buoyancy
      }
    }

    // Project velocity so that ∇·V == div (target). Solid = no-penetration.
    function project(dt) {
      var inv2dx = 1 / (2 * dx);
      // current divergence minus target, scaled, as Poisson RHS
      for (var j = 1; j < N - 1; j++) for (var i = 1; i < N - 1; i++) {
        var k = idx(i, j);
        if (solid[k]) { tmp[k] = 0; p[k] = 0; continue; }
        var ue = solid[idx(i + 1, j)] ? u[k] : u[idx(i + 1, j)];
        var uw = solid[idx(i - 1, j)] ? u[k] : u[idx(i - 1, j)];
        var vn = solid[idx(i, j + 1)] ? v[k] : v[idx(i, j + 1)];
        var vs = solid[idx(i, j - 1)] ? v[k] : v[idx(i, j - 1)];
        var d = (ue - uw + vn - vs) * inv2dx;
        tmp[k] = d - div[k];               // want this driven to zero
        p[k] = 0;
      }
      // red-black Gauss-Seidel for ∇²p = tmp/dt  (p absorbs the dt)
      var rhsScale = dx * dx;
      for (var it = 0; it < C.PROJ_ITERS; it++) {
        for (var color = 0; color < 2; color++) {
          for (var jj = 1; jj < N - 1; jj++) for (var ii = 1; ii < N - 1; ii++) {
            if (((ii + jj) & 1) !== color) continue;
            var kk = idx(ii, jj);
            if (solid[kk]) continue;
            // Neumann (∂p/∂n=0) at solids: reuse centre value
            var pe = solid[idx(ii + 1, jj)] ? p[kk] : p[idx(ii + 1, jj)];
            var pw = solid[idx(ii - 1, jj)] ? p[kk] : p[idx(ii - 1, jj)];
            var pn = solid[idx(ii, jj + 1)] ? p[kk] : p[idx(ii, jj + 1)];
            var ps = solid[idx(ii, jj - 1)] ? p[kk] : p[idx(ii, jj - 1)];
            p[kk] = (pe + pw + pn + ps - rhsScale * tmp[kk]) * 0.25;
          }
        }
      }
      // subtract pressure gradient
      for (var j2 = 1; j2 < N - 1; j2++) for (var i2 = 1; i2 < N - 1; i2++) {
        var k2 = idx(i2, j2);
        if (solid[k2]) { u[k2] = 0; v[k2] = 0; continue; }
        var pe2 = solid[idx(i2 + 1, j2)] ? p[k2] : p[idx(i2 + 1, j2)];
        var pw2 = solid[idx(i2 - 1, j2)] ? p[k2] : p[idx(i2 - 1, j2)];
        var pn2 = solid[idx(i2, j2 + 1)] ? p[k2] : p[idx(i2, j2 + 1)];
        var ps2 = solid[idx(i2, j2 - 1)] ? p[k2] : p[idx(i2, j2 - 1)];
        u[k2] -= (pe2 - pw2) * inv2dx;
        v[k2] -= (pn2 - ps2) * inv2dx;
      }
      applyBoundary();
    }

    function applyBoundary() {
      for (var i = 0; i < N; i++) {
        // south (j=0) inflow if wind from south, else copy; keep it simple: clamp edges to global wind
        set(i, 0, Ux, Uy); set(i, N - 1, Ux, Uy);
      }
      for (var j = 0; j < N; j++) { set(0, j, Ux, Uy); set(N - 1, j, Ux, Uy); }
      function set(i, j, uu, vv) { var k = idx(i, j); if (!solid[k]) { u[k] = uu; v[k] = vv; } }
    }

    function step(dt, solar) {
      surfaceHeat(dt, solar == null ? 0 : solar);
      addForces(dt);
      advectVelocity(dt);
      advectScalars(dt);
      moisture(dt);
      thermalDivergence();
      project(dt);
    }

    // max |∇·V - div| over open cells (test hook: ~0 after project)
    function residualDivergence() {
      var inv2dx = 1 / (2 * dx), m = 0;
      for (var j = 1; j < N - 1; j++) for (var i = 1; i < N - 1; i++) {
        var k = idx(i, j); if (solid[k]) continue;
        var d = (u[idx(i + 1, j)] - u[idx(i - 1, j)] + v[idx(i, j + 1)] - v[idx(i, j - 1)]) * inv2dx;
        var r = Math.abs(d - div[k]); if (r > m) m = r;
      }
      return m;
    }

    return {
      N: N, L: L, dx: dx,
      u: u, v: v, T: T, qv: qv, qc: qc, qr: qr,
      hgt: hgt, solid: solid, sea: sea, rainAccum: rainAccum, div: div,
      setTerrain: setTerrain, setWind: setWind, step: step,
      surfaceHeat: surfaceHeat, moisture: moisture, advectScalars: advectScalars,
      sampleVel: sampleVel, sampleField: sampleField,
      residualDivergence: residualDivergence,
      idx: idx, _C: C,
    };
  }

  return { createSim: createSim };
}));
