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

// 고해상도 디스플레이에서 2× 픽셀비는 4배 픽셀 부하 — 1.5 로 상한(체감 차이 적고 큰 메시에서 FPS↑)

const MAX_RATIO = Math.min(devicePixelRatio, 1.5);

renderer.setPixelRatio(MAX_RATIO);

renderer.setSize(W(), H());

view.appendChild(renderer.domElement);

const scene = new THREE.Scene();

scene.background = new THREE.Color(0x14181d);

const camera = new THREE.PerspectiveCamera(60, W() / H(), 0.01, 1000);



const geo = new PLYLoader().parse(_meshBuf); _meshBuf = null;

// 포토그래메트리 메시는 정점색을 가진다 — 있으면 그대로(MeshBasic 은 법선을 안 쓰므로

// computeVertexNormals 를 생략: 수백만 면에서 로드 수 초 + 정점당 12바이트 절약), 없으면 무광 회색.

const mat = geo.hasAttribute('color')

  ? new THREE.MeshBasicMaterial({ vertexColors: true })

  : new THREE.MeshStandardMaterial({ color: 0x9aa4ad });

if (!geo.hasAttribute('color')) {                 // 무색 메시일 때만 법선·조명 필요

  geo.computeVertexNormals();

  scene.add(new THREE.AmbientLight(0xffffff, .8));

  const dl = new THREE.DirectionalLight(0xffffff, .6); dl.position.set(1, 1, 2); scene.add(dl);

} else if (geo.hasAttribute('normal')) {

  // PLY 에 법선이 들어 있어도 MeshBasic 은 쓰지 않는다 → 버린다(정점당 12바이트 GPU 절약).

  // 대형 메시에서 업로드 시간·VRAM 이 눈에 띄게 줄어든다.

  geo.deleteAttribute('normal');

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

let lastPick = null;                              // {cam, uv} — 썸네일·모달 붉은 원의 근거

function showPhoto(cam, p) {

  const wrap = document.getElementById('photoWrap');

  const img = document.getElementById('photo');

  const mark = document.getElementById('photoMark');

  document.getElementById('hint').style.display = 'none';

  wrap.style.display = 'inline-block';

  // 클릭한 3D 점이 이 사진의 어디인지(정규화 uv). 완화 폴백으로 화면 밖일 수 있다 → null 허용

  lastPick = { cam, uv: p ? projectToCam(cam, p) : null };

  mark.style.display = 'none';

  img.onload = () => {                            // 표시 크기가 확정된 뒤 %좌표·크기를 놓는다

    if (!lastPick.uv) return;

    mark.style.left = (lastPick.uv.u * 100) + '%';

    mark.style.top = (lastPick.uv.v * 100) + '%';

    const d = Math.max(18, img.clientWidth * 0.09);

    mark.style.width = mark.style.height = d + 'px';

    mark.style.display = 'block';

  };

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

  if (cam2) showPhoto(cam2, p);

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



// ---- 우클릭 컨텍스트 메뉴 → 이미지 자세히 보기 모달 ----

const ctx = document.getElementById('ctx');

document.getElementById('photo').addEventListener('contextmenu', e => {

  e.preventDefault();

  if (!lastPick) return;

  ctx.style.display = 'block';

  // 화면 밖으로 밀리지 않게 클램프

  ctx.style.left = Math.min(e.clientX, innerWidth - ctx.offsetWidth - 8) + 'px';

  ctx.style.top = Math.min(e.clientY, innerHeight - ctx.offsetHeight - 8) + 'px';

});

addEventListener('pointerdown', e => { if (!ctx.contains(e.target)) ctx.style.display = 'none'; });

document.getElementById('ctxDetail').addEventListener('click', () => {

  ctx.style.display = 'none';

  openImageModal();

});



// 모달 상태: mv = 스테이지 변환(배율·평행이동). fitScale = 전체가 보이는 배율(페이드 기준).

const modal = document.getElementById('imgmodal');

const imView = document.getElementById('imView');

const imStage = document.getElementById('imStage');

const imImg = document.getElementById('imImg');

const imMark = document.getElementById('imMark');

let mv = { scale: 1, tx: 0, ty: 0 }, fitScale = 1;



function applyStage() {

  imStage.style.transform = `translate(${mv.tx}px,${mv.ty}px) scale(${mv.scale})`;

  // 확대할수록 붉은 원을 투명하게 — fit 의 3배 배율에서 완전히 사라진다.

  // (원은 위치 안내용이라, 확대해서 실제 표면을 볼 때는 방해가 되지 않아야 한다)

  const z = mv.scale / fitScale;

  imMark.style.opacity = Math.max(0, Math.min(1, 1 - (z - 1) / 2));

}

function openImageModal() {

  const { cam, uv } = lastPick;

  document.getElementById('imTitle').textContent = cam.source;

  imImg.onload = () => {

    const W2 = imView.clientWidth, H2 = imView.clientHeight;

    fitScale = Math.min(W2 / imImg.naturalWidth, H2 / imImg.naturalHeight) * 0.98;

    mv = { scale: fitScale,

           tx: (W2 - imImg.naturalWidth * fitScale) / 2,

           ty: (H2 - imImg.naturalHeight * fitScale) / 2 };

    if (uv) {                                     // 클릭 지점 = 반투명 붉은 원(이미지 픽셀 공간)

      const d = Math.max(36, imImg.naturalWidth * 0.05);

      imMark.style.width = imMark.style.height = d + 'px';

      imMark.style.left = (uv.u * imImg.naturalWidth) + 'px';

      imMark.style.top = (uv.v * imImg.naturalHeight) + 'px';

      imMark.style.display = 'block';

    } else imMark.style.display = 'none';

    applyStage();

  };

  imImg.src = api('img', `&id=${cam.id}&kind=full`);

  modal.style.display = 'flex';

}

function closeModal() { modal.style.display = 'none'; imImg.removeAttribute('src'); }

document.getElementById('imClose').addEventListener('click', closeModal);

addEventListener('keydown', e => { if (e.key === 'Escape' && modal.style.display === 'flex') closeModal(); });

// 휠: 커서 위치를 고정점으로 확대/축소 — 그 점 아래 픽셀이 제자리에 남는다

imView.addEventListener('wheel', e => {

  e.preventDefault();

  const r = imView.getBoundingClientRect();

  const mx = e.clientX - r.left, my = e.clientY - r.top;

  const ns = Math.max(fitScale * 0.5, Math.min(fitScale * 40, mv.scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));

  mv.tx = mx - (mx - mv.tx) * (ns / mv.scale);

  mv.ty = my - (my - mv.ty) * (ns / mv.scale);

  mv.scale = ns; applyStage();

}, { passive: false });

// 드래그: 이동

let _mdrag = null;

imView.addEventListener('pointerdown', e => {

  _mdrag = { x: e.clientX, y: e.clientY, tx: mv.tx, ty: mv.ty };

  imView.classList.add('drag'); imView.setPointerCapture(e.pointerId);

});

imView.addEventListener('pointermove', e => {

  if (!_mdrag) return;

  mv.tx = _mdrag.tx + e.clientX - _mdrag.x;

  mv.ty = _mdrag.ty + e.clientY - _mdrag.y;

  applyStage();

});

imView.addEventListener('pointerup', () => { _mdrag = null; imView.classList.remove('drag'); });



addEventListener('resize', () => {

  camera.aspect = W() / H(); camera.updateProjectionMatrix();

  renderer.setSize(W(), H());

});



// ---- 적응형 해상도: 무거우면 내부 해상도를 낮추고, 여유가 생기면 되돌린다 ----

// 큰 메시에서 병목은 대개 픽셀(래스터라이즈)이라 픽셀비를 줄이면 프레임이 바로 올라간다.

// 정지 화면에서는 항상 최대 품질로 복귀시켜, 움직일 때만 흐려지고 멈추면 선명해진다.

let _ratio = MAX_RATIO, _acc = 0, _n = 0, _idle = 0;

const MIN_RATIO = 0.6;

function adapt(dt) {

  _acc += dt; _n++;

  if (_n < 20) return;                            // 20프레임 평균으로 판단(스파이크에 흔들리지 않게)

  const fps = _n / _acc; _acc = 0; _n = 0;

  const moving = controls.enableDamping ? _idle < 0.4 : false;

  let want = _ratio;

  if (moving && fps < 40) want = Math.max(MIN_RATIO, _ratio - 0.2);       // 조작 중 버벅 → 낮춘다

  else if (!moving || fps > 55) want = Math.min(MAX_RATIO, _ratio + 0.2); // 여유/정지 → 되돌린다

  if (Math.abs(want - _ratio) > 0.01) {

    _ratio = want; renderer.setPixelRatio(_ratio); renderer.setSize(W(), H());

  }

}

controls.addEventListener('change', () => { _idle = 0; });                 // 조작 중임을 표시



const _clock = new THREE.Clock();

(function loop() {

  requestAnimationFrame(loop);

  const dt = Math.min(_clock.getDelta(), 0.25);

  _idle += dt;

  controls.update(); GZ.update();

  renderer.render(scene, camera);

  adapt(dt);

})();

window.__dbg = { scene, camera, mesh, controls, nearestCamera, pickAt, DATA };   // 콘솔 진단용(기능 아님)