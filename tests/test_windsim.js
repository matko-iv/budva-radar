// Stage-A physics core tests for docs/windsim.js (wind + terrain + projection +
// thermal convergence). Run: node tests/test_windsim.js
const { createSim } = require(require('path').join(__dirname, '..', 'docs', 'windsim.js'));

let fails = 0;
function ck(d, cond, extra) {
  if (!cond) { fails++; console.log('FAIL', d, extra || ''); } else console.log('PASS', d, extra || '');
}
function flat(N) { return { height: new Float32Array(N * N), sea: new Uint8Array(N * N).fill(1) }; }
function sum(a) { let s = 0; for (let k = 0; k < a.length; k++) s += a[k]; return s; }

// 1) uniform wind over flat terrain stays uniform and divergence-free
(function () {
  const N = 48, sim = createSim({ N, L: 10000, terrain: flat(N) });
  sim.setWind(270, 10);                       // from west -> blows east, Ux=+10
  for (let k = 0; k < N * N; k++) { sim.u[k] = 10; sim.v[k] = 0; }
  for (let s = 0; s < 30; s++) sim.step(0.5);
  let maxdev = 0, maxv = 0;
  for (let j = 4; j < N - 4; j++) for (let i = 4; i < N - 4; i++) {
    const k = sim.idx(i, j); maxdev = Math.max(maxdev, Math.abs(sim.u[k] - 10)); maxv = Math.max(maxv, Math.abs(sim.v[k]));
  }
  ck('uniform: u stays ~10', maxdev < 0.5, 'maxdev=' + maxdev.toFixed(3));
  ck('uniform: v stays ~0', maxv < 0.5, 'maxv=' + maxv.toFixed(3));
  ck('uniform: residual div ~0', sim.residualDivergence() < 1e-2, 'res=' + sim.residualDivergence().toExponential(2));
})();

// 2) obstacle -> no-penetration + lateral deflection + stable + div-consistent
(function () {
  const N = 64, h = new Float32Array(N * N), sea = new Uint8Array(N * N).fill(1);
  for (let j = 0; j < N; j++) for (let i = 0; i < N; i++) {
    if (Math.abs(i - N / 2) < 5 && Math.abs(j - N / 2) < 5) { h[j * N + i] = 1000; sea[j * N + i] = 0; }
  }
  const sim = createSim({ N, L: 10000, terrain: { height: h, sea } });
  sim.setWind(270, 10);
  for (let k = 0; k < N * N; k++) { sim.u[k] = 10; sim.v[k] = 0; }
  for (let s = 0; s < 60; s++) sim.step(0.5);
  let maxSolid = 0; for (let k = 0; k < N * N; k++) if (sim.solid[k]) maxSolid = Math.max(maxSolid, Math.abs(sim.u[k]) + Math.abs(sim.v[k]));
  ck('obstacle: solid cells ~0 velocity', maxSolid < 1e-6, 'maxSolid=' + maxSolid);
  let maxLat = 0; for (let j = 2; j < N - 2; j++) for (let i = 2; i < N - 2; i++) if (!sim.solid[j * N + i]) maxLat = Math.max(maxLat, Math.abs(sim.v[j * N + i]));
  ck('obstacle: lateral deflection present', maxLat > 0.5, 'maxLat=' + maxLat.toFixed(3));
  let ok = true, maxmag = 0; for (let k = 0; k < N * N; k++) { if (!isFinite(sim.u[k]) || !isFinite(sim.v[k])) ok = false; maxmag = Math.max(maxmag, Math.abs(sim.u[k]), Math.abs(sim.v[k])); }
  ck('obstacle: finite & bounded', ok && maxmag < 50, 'maxmag=' + maxmag.toFixed(2));
  ck('obstacle: residual div ~0', sim.residualDivergence() < 5e-2, 'res=' + sim.residualDivergence().toExponential(2));
})();

// 3) bilinear sample correctness on a linear field
(function () {
  const N = 16, sim = createSim({ N, L: 1600, terrain: flat(N) });
  for (let j = 0; j < N; j++) for (let i = 0; i < N; i++) sim.T[sim.idx(i, j)] = 2 * i + 3 * j;
  const got = sim.sampleField(sim.T, 4.25, 5.5), want = 2 * 4.25 + 3 * 5.5;
  ck('bilinear sample', Math.abs(got - want) < 1e-4, 'got=' + got + ' want=' + want);
})();

// 4) thermal convergence: a warm column draws surface air inward
(function () {
  const N = 48, sim = createSim({ N, L: 10000, terrain: flat(N) });
  sim.setWind(0, 0);                          // calm
  for (let j = 0; j < N; j++) for (let i = 0; i < N; i++) {
    const dx = i - N / 2, dy = j - N / 2; if (dx * dx + dy * dy < 25) sim.T[sim.idx(i, j)] = 3;
  }
  for (let s = 0; s < 40; s++) sim.step(0.5);
  const ie = Math.floor(N / 2) + 6, jc = Math.floor(N / 2);
  const uEast = sim.u[sim.idx(ie, jc)];       // east of warm blob should flow west (toward it)
  ck('thermal: convergence toward warm column', uEast < -0.01, 'uEast=' + uEast.toFixed(3));
})();

// 5) moisture: water conserved + condensation + latent heating (isolated, no advection)
(function () {
  const N = 8, sim = createSim({ N, L: 800, terrain: flat(N) });
  for (let k = 0; k < N * N; k++) { sim.qv[k] = 18; sim.T[k] = 0; }   // qvs0=12 -> supersaturated
  const total0 = sum(sim.qv) + sum(sim.qc) + sum(sim.qr) + sum(sim.rainAccum), T0 = sim.T[0];
  for (let s = 0; s < 50; s++) sim.moisture(0.5);
  const total1 = sum(sim.qv) + sum(sim.qc) + sum(sim.qr) + sum(sim.rainAccum);
  ck('moisture: total water conserved', Math.abs(total1 - total0) < 1e-2, 'd=' + (total1 - total0).toExponential(2));
  ck('moisture: condensation produced cloud/rain', (sum(sim.qc) + sum(sim.qr) + sum(sim.rainAccum)) > 0.1);
  ck('moisture: latent heat raised T', sim.T[0] > T0 + 0.1, 'T=' + sim.T[0].toFixed(3));
})();

// 6) no spurious condensation when subsaturated
(function () {
  const N = 8, sim = createSim({ N, L: 800, terrain: flat(N) });
  for (let k = 0; k < N * N; k++) { sim.qv[k] = 5; sim.T[k] = 0; }    // 5 < qvs0=12
  for (let s = 0; s < 20; s++) sim.moisture(0.5);
  ck('moisture: no cloud when subsaturated', sum(sim.qc) === 0 && sum(sim.qr) === 0);
})();

// 7) surface heating: land warms more than sea (the sea-breeze driver)
(function () {
  const N = 8, h = new Float32Array(N * N), sea = new Uint8Array(N * N);
  for (let j = 0; j < N; j++) for (let i = 0; i < N; i++) sea[j * N + i] = i < N / 2 ? 1 : 0;
  const sim = createSim({ N, L: 800, terrain: { height: h, sea } });
  for (let s = 0; s < 10; s++) sim.surfaceHeat(0.5, 1.0);
  const seaT = sim.T[sim.idx(1, 4)], landT = sim.T[sim.idx(N - 2, 4)];
  ck('heating: land warmer than sea', landT > seaT + 0.5, 'land=' + landT.toFixed(2) + ' sea=' + seaT.toFixed(2));
})();

console.log(fails ? ('\n' + fails + ' FAIL') : '\nALL PASS');
process.exit(fails ? 1 : 0);
