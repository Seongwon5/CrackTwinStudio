// viewer.js — viewer-simple: 3D 모델을 돌려보고, 표면 한 점을 클릭하면 그 점을 찍은
// 가장 가까운 사진을 우측에 보여준다. 측정·검토·필터·비교 — 아무것도 없다.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { PLYLoader } from 'three/addons/loaders/PLYLoader.js';

const REPORT = window.__VIEWER_REPORT__;
const _q = encodeURIComponent(REPORT);
const api = (kind, extra = '') => `/api/report/${kind}?p=${_q}${extra}`;
const _boot = document.getElementById('boot');
const _say = m => { if (_boot) _boot.textContent = m; };

// ---- 보고서 로드: 메타(JSON) + 메시(ArrayBuffer — 문자열 한계와 무관, main 브랜치 실측) ----
async function _fetch(url, as) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url.split('?')[0]} → ${r.status} ${await r.text()}`);
  return as === 'json' ? r.json() : r.arrayBuffer();
}
let DATA, _meshBuf;
try {
  _say('보고서 메타 읽는 중…');
  DATA = await _fetch(api('meta'), 'json');
  _say(`3D 메시 받는 중… (사진 ${DATA.counts.cameras}장)`);
  _meshBuf = await _fetch(api('mesh'), 'buf');
} catch (e) { _say('보고서를 열 수 없습니다: ' + e.message); throw e; }

// ---- three.js 기본 무대 ----
const view = document.getElementById('view3d');
document.getElementById('wrap').style.display = 'flex';
const W = () => view.clientWidth, H = () => view.clientHeight;
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(W(), H());
view.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x14181d);
const camera = new THREE.PerspectiveCamera(60, W() / H(), 0.01, 1000);

const geo = new PLYLoader().parse(_meshBuf); _meshBuf = null;
geo.computeVertexNormals();
// 포토그래메트리 메시는 정점색을 가진다 — 있으면 그대로, 없으면 무광 회색
const mat = geo.hasAttribute('color')
  ? new THREE.MeshBasicMaterial({ vertexColors: true })
  : new THREE.MeshStandardMaterial({ color: 0x9aa4ad });
if (!geo.hasAttribute('color')) {                 // 무색 메시일 때만 조명 필요
  scene.add(new THREE.AmbientLight(0xffffff, .8));
  const dl = new THREE.DirectionalLight(0xffffff, .6); dl.position.set(1, 1, 2); scene.add(dl);
}
const mesh = new THREE.Mesh(geo, mat);
scene.add(mesh);

// 시야·컨트롤을 메시 크기에 맞춘다
geo.computeBoundingSphere(); geo.computeBoundingBox();
const bs = geo.boundingSphere, bb = geo.boundingBox;
camera.up.set(0, 0, 1);                           // 포토그래메트리는 Z-up — up 을 +Z 로 고정(롤 없음)
camera.position.copy(bs.center).add(new THREE.Vector3(0, -bs.radius * 2.2, bs.radius * 0.7));
camera.near = bs.radius / 100; camera.far = bs.radius * 100; camera.updateProjectionMatrix();
// OrbitControls: 좌드래그 = 중심(target) 기준 회전 · 휠 = 줌 · 우드래그 = 이동.
// up 을 컨트롤 생성 '전에' +Z 로 두면 회전축이 월드 Z 로 유지된다(수평선이 기울지 않음).
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.copy(bs.center);
controls.enableDamping = true; controls.dampingFactor = 0.08;
controls.update();                                // 부트 즉시 시선 확정 — rAF 첫 프레임 전에도 레이캐스트가 옳게 동작

// 바닥 그리드 — 모델 최저면(z=min)에 깔아 크기·자세의 기준을 준다
const d = bb.getSize(new THREE.Vector3()).length();
const grid = new THREE.GridHelper(d * 1.5, 50, 0x5a6672, 0x333c45);
grid.rotation.x = Math.PI / 2;                    // GridHelper 는 XZ 평면(Y-up 기준) → Z-up 으로 눕힘
grid.position.set(bs.center.x, bs.center.y, bb.min.z);
scene.add(grid);

// 클릭 지점 표식(작은 구) — 어디를 골랐는지만 보여준다
const marker = new THREE.Mesh(
  new THREE.SphereGeometry(bs.radius * 0.006, 16, 16),
  new THREE.MeshBasicMaterial({ color: 0xff33cc, depthTest: false }));
marker.renderOrder = 999; marker.visible = false; scene.add(marker);

_boot.style.display = 'none';

// ---- 클릭 → 그 지점을 찍은 가장 가까운 사진 ----
// 카메라 포즈(XMP)로 판정한다: ① 점이 그 사진 화면 안에 들어오고(왜곡 포함 투영)
// ② 카메라가 표면 앞쪽에 있으며(법선 검사 — 없으면 벽 반대편 사진이 뽑힌다)
// ③ 그중 카메라와 점 사이 거리가 최소인 사진. 셋 다 실패하면 단계적으로 완화한다.
function projectToCam(cam, X) {
  const rx = X.x - cam.pos[0], ry = X.y - cam.pos[1], rz = X.z - cam.pos[2];
  const Zc = rx * cam.fwd[0] + ry * cam.fwd[1] + rz * cam.fwd[2];
  if (Zc <= 1e-6) return null;                     // 카메라 뒤
  const Xc = rx * cam.right[0] + ry * cam.right[1] + rz * cam.right[2];
  const Yc = -(rx * cam.up[0] + ry * cam.up[1] + rz * cam.up[2]);
  let x = Xc / Zc, y = Yc / Zc;
  const d = (cam.dist || []).concat([0, 0, 0, 0, 0]).slice(0, 5);
  const r2 = x * x + y * y, rad = 1 + d[0] * r2 + d[1] * r2 * r2 + d[4] * r2 * r2 * r2;
  const xd = x * rad + 2 * d[2] * x * y + d[3] * (r2 + 2 * x * x);
  const yd = y * rad + d[2] * (r2 + 2 * y * y) + 2 * d[3] * x * y;
  const u = (cam.fx * xd + cam.cx) / cam.w, v = (cam.fy * yd + cam.cy) / cam.h;
  return (u < 0 || u > 1 || v < 0 || v > 1) ? null : { u, v };
}
function nearestCamera(p, n) {
  let best = null, bestD = Infinity, relax = null, relaxD = Infinity;
  for (const c of (DATA.cameras || [])) {
    const dx = c.pos[0] - p.x, dy = c.pos[1] - p.y, dz = c.pos[2] - p.z;
    const dist = Math.hypot(dx, dy, dz);
    if (!projectToCam(c, p)) continue;             // ① 화면 밖 → 제외
    if (dist < relaxD) { relax = c; relaxD = dist; }          // 법선 무시 후보(완화용)
    if (n && (n.x * dx + n.y * dy + n.z * dz) <= 0) continue; // ② 표면 뒤쪽 → 제외
    if (dist < bestD) { best = c; bestD = dist; }
  }
  if (best) return best;
  if (relax) return relax;                         // 법선 조건 완화
  let any = null, anyD = Infinity;                 // 최후: 그냥 제일 가까운 카메라
  for (const c of (DATA.cameras || [])) {
    const dist = Math.hypot(c.pos[0] - p.x, c.pos[1] - p.y, c.pos[2] - p.z);
    if (dist < anyD) { any = c; anyD = dist; }
  }
  return any;
}
function showPhoto(cam) {
  const img = document.getElementById('photo');
  document.getElementById('hint').style.display = 'none';
  img.style.display = 'block';
  img.src = api('img', `&id=${cam.id}&kind=full`);
  document.getElementById('pname').textContent = cam.source;
}

// 화면 좌표(clientX/Y) 하나를 받아: 레이캐스트 → 표식 → 가장 가까운 사진 표시.
const ray = new THREE.Raycaster();
function pickAt(clientX, clientY) {
  const r = renderer.domElement.getBoundingClientRect();
  if (!r.width || !r.height) return false;
  const mx = ((clientX - r.left) / r.width) * 2 - 1;
  const my = -((clientY - r.top) / r.height) * 2 + 1;
  ray.setFromCamera(new THREE.Vector2(mx, my), camera);
  const hit = ray.intersectObject(mesh, false);
  if (!hit.length) return false;
  const p = hit[0].point;
  let n = null;
  if (hit[0].face) n = hit[0].face.normal.clone()
    .applyMatrix3(new THREE.Matrix3().getNormalMatrix(mesh.matrixWorld)).normalize();
  marker.position.copy(p); marker.visible = true;
  const cam2 = nearestCamera(p, n);
  if (cam2) showPhoto(cam2);
  return true;
}
// 짧은 탭만 클릭으로 인정 — 드래그(회전)와 구분한다
let _dn = null;
renderer.domElement.addEventListener('pointerdown', e => { _dn = { x: e.clientX, y: e.clientY, t: performance.now() }; });
renderer.domElement.addEventListener('pointerup', e => {
  if (!_dn) return;
  const moved = Math.hypot(e.clientX - _dn.x, e.clientY - _dn.y);
  const held = performance.now() - _dn.t; _dn = null;
  if (moved > 5 || held > 400 || e.button !== 0) return;
  pickAt(e.clientX, e.clientY);
});

// ---- 우하단 XYZ 기즈모: 카메라 자세를 따라 도는 축 표시 + 축 클릭 = 그 방향에서 보기 ----
const GZ = (function () {
  const host = document.getElementById('gizmo'), C = 44, R = 30;
  const AX = [{ v: new THREE.Vector3(1, 0, 0), col: '#ff5a72', lab: 'X' },
              { v: new THREE.Vector3(0, 1, 0), col: '#5ad46a', lab: 'Y' },
              { v: new THREE.Vector3(0, 0, 1), col: '#4aa8ff', lab: 'Z' }];
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg'); svg.setAttribute('viewBox', '0 0 88 88'); host.appendChild(svg);
  const mk = (t, a) => { const e = document.createElementNS(NS, t); for (const k in a) e.setAttribute(k, a[k]); return e; };
  // 축 방향에서 모델 중심을 보도록 카메라를 옮긴다(거리는 현재 거리 유지).
  // ±Z 정면은 up(+Z)과 시선이 평행해 특이점 — 살짝 비껴 세운다.
  function snapView(dir) {
    const dist = camera.position.distanceTo(controls.target);
    const p = controls.target.clone().addScaledVector(dir, dist);
    if (Math.abs(dir.z) > 0.99) p.y -= dist * 0.02;
    camera.position.copy(p); controls.update();
  }
  const nodes = [];
  for (const ax of AX) for (const s of [1, -1]) {
    const line = s > 0 ? mk('line', { stroke: ax.col, 'stroke-width': 2, 'stroke-linecap': 'round' }) : null;
    if (line) svg.appendChild(line);
    const g = mk('g', { class: 'gzl', cursor: 'pointer' });
    const circ = mk('circle', { r: 9, fill: s > 0 ? ax.col : '#0e1319', stroke: ax.col, 'stroke-width': 1.5 });
    const txt = mk('text', { 'text-anchor': 'middle', 'dominant-baseline': 'central',
      'font-size': 11, 'font-weight': 600, fill: s > 0 ? '#0e1319' : ax.col });
    txt.textContent = (s < 0 ? '-' : '') + ax.lab;
    g.appendChild(circ); g.appendChild(txt); svg.appendChild(g);
    g.addEventListener('click', e => { e.stopPropagation(); snapView(ax.v.clone().multiplyScalar(s)); });
    nodes.push({ ax, s, line, g });
  }
  // 매 프레임: 월드축을 카메라(뷰) 좌표로 투영해 노드 배치. z 순 재정렬로 앞 축이 위에 오게.
  function update() {
    const q = camera.quaternion.clone().invert();
    const proj = nodes.map(n => { const p = n.ax.v.clone().multiplyScalar(n.s).applyQuaternion(q);
      return { n, x: C + p.x * R, y: C - p.y * R, z: p.z }; });
    proj.sort((a, b) => a.z - b.z);
    for (const p of proj) {
      const n = p.n;
      if (n.line) {
        svg.appendChild(n.line);
        n.line.setAttribute('x1', C); n.line.setAttribute('y1', C);
        n.line.setAttribute('x2', p.x); n.line.setAttribute('y2', p.y);
        n.line.setAttribute('opacity', p.z > 0 ? 1 : 0.4);
      }
      svg.appendChild(n.g);
      n.g.setAttribute('transform', `translate(${p.x},${p.y})`);
      n.g.setAttribute('opacity', p.z > 0 ? 1 : 0.55);
    }
  }
  return { update };
})();

addEventListener('resize', () => {
  camera.aspect = W() / H(); camera.updateProjectionMatrix();
  renderer.setSize(W(), H());
});
(function loop() { requestAnimationFrame(loop); controls.update(); GZ.update(); renderer.render(scene, camera); })();
window.__dbg = { scene, camera, mesh, controls, nearestCamera, pickAt, DATA };   // 콘솔 진단용(기능 아님)
