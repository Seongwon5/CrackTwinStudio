// viewer.js — 3D 모델을 돌려보고, 표면 한 점을 클릭하면 그 지점을 찍은 사진을 보여준다.
import * as THREE from 'three';
import { PLYLoader } from 'three/addons/loaders/PLYLoader.js';
import { buildBVH } from '/bvh.js';
import { buildLOD } from '/lod.js';
import { shiftToLocalOrigin, shiftMetaOrigin } from '/origin.js';
import { createTileSet, parseTile, legacySource } from '/tiles.js';

const REPORT = window.__VIEWER_REPORT__;
const _q = encodeURIComponent(REPORT);
const api = (kind, extra = '') => `/api/report/${kind}?p=${_q}${extra}`;
const _boot = document.getElementById('boot');
const _say = m => { if (_boot) _boot.textContent = m; };

// ---------------------------------------------------------------- 3D 무대

const view = document.getElementById('view3d');
const W = () => view.clientWidth, H = () => view.clientHeight;

const renderer = new THREE.WebGLRenderer({ antialias: true });
const MAX_RATIO = Math.min(devicePixelRatio, 1.5);   // 2배는 픽셀 부하 4배, 체감 차이는 작다
renderer.setPixelRatio(MAX_RATIO);
view.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x14181d);
const camera = new THREE.PerspectiveCamera(60, 1, 0.01, 1000);

// ---------------------------------------------------------------- 보고서 로드
// 메시는 계층 LOD 타일로 받는다(tiles.js 머리말 참조) — 처음엔 루트 타일 하나로 그리고,
// 카메라가 다가가는 영역만 세밀한 타일로 바꿔 간다. 6GB 메시도 화면엔 수백만 면만 올라간다.
// 타일이 없는 옛 보고서(mesh.ply)는 통째로 받아 간략 메시 + 원본의 두 단계 타일셋으로 감싼다.

async function _fetch(url, as) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url.split('?')[0]} → ${r.status} ${await r.text()}`);
  return as === 'json' ? r.json() : r.arrayBuffer();
}

let DATA, tiles;
try {
  _say('메타 읽는 중…');
  DATA = await _fetch(api('meta'), 'json');
  let src;
  if (DATA.tiles) {
    // 타일 좌표는 이미 국소좌표(tiler.py 가 원점을 뺐다) — 카메라만 같은 원점으로 맞춘다
    shiftMetaOrigin(DATA, DATA.tiles.origin);
    const color = !!DATA.tiles.color;
    src = { nodes: DATA.tiles.nodes, root: DATA.tiles.root, box: DATA.tiles.box, color,
            load: async id => parseTile(THREE, await _fetch(api('tile', `&id=${id}`), 'buf'), !color) };
    _say(`3D 타일 ${DATA.tiles.nodes.length.toLocaleString()}개 · 사진 ${DATA.counts.cameras}장 — 첫 화면 준비 중…`);
  } else {
    _say(`3D 메시 받는 중… (사진 ${DATA.counts.cameras}장)`);
    const buf = await _fetch(api('mesh'), 'buf');   // ArrayBuffer — 문자열 한계와 무관
    // 큰 절대좌표(UTM)를 국소좌표로 — float32 로 내려가기 전에 옮겨야 한다(origin.js 머리말 참조).
    shiftToLocalOrigin(buf, DATA);
    const geo = new PLYLoader().parse(buf);
    if (geo.hasAttribute('color') && geo.hasAttribute('normal')) geo.deleteAttribute('normal');
    else if (!geo.hasAttribute('color')) geo.computeVertexNormals();
    src = legacySource(THREE, buildLOD, geo, 700000);
  }
  const mat = src.color
    ? new THREE.MeshBasicMaterial({ vertexColors: true })   // 정점색 — 법선 불필요
    : new THREE.MeshStandardMaterial({ color: 0x9aa4ad });
  // LOD 타일은 요약 단계에서 감기 방향이 뒤집힐 수 있어 양면으로 그린다(tiler.py simplify 참조).
  if (!src.legacy) mat.side = THREE.DoubleSide;
  if (!src.color) {
    scene.add(new THREE.AmbientLight(0xffffff, 0.8));
    const dl = new THREE.DirectionalLight(0xffffff, 0.6);
    dl.position.set(1, 1, 2);
    scene.add(dl);
  }
  // 예산: 화면 500만 면 · 타일 1.5GB — RTX 5050(8GB)·32GB PC 기준. 넘치면 문턱이 스스로 올라간다.
  tiles = createTileSet({ THREE, buildBVH }, src,
                        { material: mat, triBudget: 5e6, byteBudget: 1.5e9, ssePx: 2, movingScale: 2.5 });
  await tiles.ready;
  window.__tiles = tiles; window.__camera = camera;   // 개발자 도구 디버그용(__tiles.stats)
} catch (e) {
  _say('보고서를 열 수 없습니다: ' + e.message);
  throw e;
}

document.getElementById('wrap').style.display = 'flex';
renderer.setSize(W(), H());
camera.aspect = W() / H();
scene.add(tiles.group);

// ---------------------------------------------------------------- FPS 카메라
// 좌드래그=시점 회전 · WASD/Space/Ctrl=이동 · 우클릭 홀드=확대(FOV) · 휠=전진/후진.
// 궤도(orbit)·패닝은 없다 — 회전 중심이 따로 없으니 벽면에 바짝 붙어 훑는 조작이 자연스럽다.
// 시선은 yaw(월드 Z축 회전) + pitch(수평선 기준 올려본 각)로만 표현한다. 따라서:
//  · camera.up 은 항상 (0,0,1) — 롤이 생길 수 없다
//  · pitch 는 언제나 atan2(dz, 수평거리) 로 계산 → 부호가 뒤집힐 경로가 없다(상하 반전 금지)
let camYaw = 0, camPitch = 0;
const PITCH_LIMIT = Math.PI / 2 - 0.02;          // 수직(±90°)에서 lookAt 특이점 회피
const BASE_FOV = 60, SCOPE_FOV = 22;             // 우클릭 스코프 확대 FOV
let scoping = false;                             // 우클릭 홀드 중?
let _idle = 0;                                   // 마지막 조작 이후 흐른 시간(초) — 해상도 적응이 읽는다
const _ld = new THREE.Vector3();
const clampPitch = p => Math.max(-PITCH_LIMIT, Math.min(PITCH_LIMIT, p));

function lookDir(out) {                          // yaw/pitch → 단위 시선 벡터(Z-up)
  const cp = Math.cos(camPitch);
  return out.set(cp * Math.cos(camYaw), cp * Math.sin(camYaw), Math.sin(camPitch));
}
function applyLook() {                           // 현재 위치에서 yaw/pitch 방향을 바라보게
  lookDir(_ld);
  camera.up.set(0, 0, 1);                        // 매 프레임 강제 → up 절대 불변
  camera.lookAt(camera.position.x + _ld.x, camera.position.y + _ld.y, camera.position.z + _ld.z);
}
function lookToward(fromV, toV) {                // 한 점을 바라보도록 yaw/pitch 설정(일관 부호)
  const dx = toV.x - fromV.x, dy = toV.y - fromV.y, dz = toV.z - fromV.z;
  camYaw = Math.atan2(dy, dx);
  camPitch = clampPitch(Math.atan2(dz, Math.hypot(dx, dy)));
}

const bs = tiles.sphere, bb = tiles.box;         // 전체 모델의 경계(타일 메타에서)

camera.up.set(0, 0, 1);                          // 사진측량 모델은 Z-up — 롤 없이 유지
camera.position.copy(bs.center).add(new THREE.Vector3(0, -bs.radius * 2.2, bs.radius * 0.7));
camera.near = bs.radius / 100;
camera.far = bs.radius * 100;
camera.updateProjectionMatrix();

const modelDiag = bb.getSize(new THREE.Vector3()).length();   // 이동 속도·휠 보폭의 기준 크기
lookToward(camera.position, bs.center);          // 모델 중심을 바라보게 yaw/pitch 초기화
applyLook();

// 바닥 그리드 — 모델 최저면에 깔아 크기·자세의 기준을 준다
const grid = new THREE.GridHelper(bb.getSize(new THREE.Vector3()).length() * 1.5, 50, 0x5a6672, 0x333c45);
grid.rotation.x = Math.PI / 2;                   // GridHelper는 XZ 평면 기준
grid.position.set(bs.center.x, bs.center.y, bb.min.z);
scene.add(grid);

// 클릭 지점 표식 — 확대·축소와 무관하게 화면상 크기를 일정하게 유지한다.
// 단위 구(반지름 1)를 매 프레임 거리·화각에 맞춰 스케일해 항상 MARKER_PX 픽셀로 보이게 한다.
const MARKER_PX = 3;                              // 화면상 반지름(px) 목표
const marker = new THREE.Mesh(
  new THREE.SphereGeometry(1, 16, 16),
  new THREE.MeshBasicMaterial({ color: 0xff33cc, depthTest: false }));
marker.renderOrder = 999;
marker.visible = false;
scene.add(marker);

function sizeMarker() {
  if (!marker.visible) return;
  const dist = camera.position.distanceTo(marker.position);
  // 화면 세로 1px 이 marker 거리에서 차지하는 월드 길이 × 목표 픽셀 = 월드 반지름
  const worldPerPx = 2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * dist / H();
  marker.scale.setScalar(Math.max(worldPerPx * MARKER_PX, bs.radius * 1e-5));
}

// 클릭 가속 구조(BVH)는 tiles.js 가 광선이 실제로 지나는 타일에만 그때 만든다 — 미리 만들 게 없다.
_boot.style.display = 'none';

// ---------------------------------------------------------------- 사진 선택

// 3D 점을 사진 평면에 투영해 정규화 좌표(u,v)를 반환한다. 화면 밖·카메라 뒤면 null.
// 3D 좌표는 왜곡 보정된 광선 기준이므로, 원본 사진에 얹으려면 왜곡을 다시 적용해야 한다.
function projectToCam(cam, X) {
  const rx = X.x - cam.pos[0], ry = X.y - cam.pos[1], rz = X.z - cam.pos[2];
  const Zc = rx * cam.fwd[0] + ry * cam.fwd[1] + rz * cam.fwd[2];
  if (Zc <= 1e-6) return null;
  const Xc = rx * cam.right[0] + ry * cam.right[1] + rz * cam.right[2];
  const Yc = -(rx * cam.up[0] + ry * cam.up[1] + rz * cam.up[2]);
  const x = Xc / Zc, y = Yc / Zc;
  const r2 = x * x + y * y;
  // FOV 가드 — 화각(진짜 프레임) 밖 점은 버린다. 브라운-콘래디 왜곡식은 화각 밖에서
  // 단조성이 깨져 '접히므로', 이 검사가 없으면 프레임 밖 점이 (u,v)∈[0,1] 안으로 접혀 들어와
  // 유효한 척한다. 그런 허상은 점수(근접·정면)가 높아 대표 사진으로 뽑히기 쉽고 — 시야는 트여
  // 가림 검사도 통과 — 붉은 점이 엉뚱한 곳에 찍힌다(대표만 틀리고 아래 후보는 맞던 원인).
  const rcx = Math.max(cam.cx, cam.w - cam.cx) / cam.fx;   // 화면 모서리의 이상반경(왜곡 전)
  const rcy = Math.max(cam.cy, cam.h - cam.cy) / cam.fy;
  if (r2 > (rcx * rcx + rcy * rcy) * 1.10) return null;    // 여유 (반경 ×1.05)²
  const d = (cam.dist || []).concat([0, 0, 0, 0, 0]).slice(0, 5);
  const rad = 1 + d[0] * r2 + d[1] * r2 * r2 + d[4] * r2 * r2 * r2;
  const xd = x * rad + 2 * d[2] * x * y + d[3] * (r2 + 2 * x * x);
  const yd = y * rad + d[2] * (r2 + 2 * y * y) + 2 * d[3] * x * y;
  const u = (cam.fx * xd + cam.cx) / cam.w, v = (cam.fy * yd + cam.cy) / cam.h;
  return (u < 0 || u > 1 || v < 0 || v > 1) ? null : { u, v };
}

// 점수 내림차순 후보 목록. 점수 = cosθ / 거리 — 스치듯 본 사진보다 정면으로 마주 본 사진이
// 그 지점을 크고 또렷하게 담는다. 조건은 ① 화면 안 ② 표면 앞쪽이며, 없으면 단계적으로 완화한다.
function cameraCandidates(p, n) {
  const strict = [], loose = [];
  for (const c of (DATA.cameras || [])) {
    const dx = c.pos[0] - p.x, dy = c.pos[1] - p.y, dz = c.pos[2] - p.z;
    const dist = Math.hypot(dx, dy, dz) || 1e-6;
    if (!projectToCam(c, p)) continue;
    const cos = n ? (n.x * dx + n.y * dy + n.z * dz) / dist : 1;
    const item = { cam: c, cos, dist, score: Math.max(cos, 0) / dist };
    loose.push(item);
    if (!(n && cos <= 0)) strict.push(item);
  }
  let list = strict.length ? strict : loose;
  if (!list.length) {
    list = (DATA.cameras || []).map(c => {
      const dist = Math.hypot(c.pos[0] - p.x, c.pos[1] - p.y, c.pos[2] - p.z) || 1e-6;
      return { cam: c, cos: 0, dist, score: 1 / dist };
    });
  }
  return list.sort((a, b) => b.score - a.score);
}

// ---------------------------------------------------------------- 사진 패널

let lastPick = null;      // {cam, uv} — 붉은 원과 확대 창의 기준

function showPhoto(cam, p) {
  const wrap = document.getElementById('photoWrap');
  const img = document.getElementById('photo');
  const mark = document.getElementById('photoMark');
  document.getElementById('hint').style.display = 'none';
  wrap.style.display = 'inline-block';
  lastPick = { cam, uv: p ? projectToCam(cam, p) : null };   // 완화 선택 시 화면 밖일 수 있다
  mark.style.display = 'none';
  img.onload = () => {                                       // 표시 크기 확정 후 배치
    if (!lastPick.uv) return;
    mark.style.left = (lastPick.uv.u * 100) + '%';
    mark.style.top = (lastPick.uv.v * 100) + '%';
    const d = Math.max(18, img.clientWidth * 0.09);
    mark.style.width = mark.style.height = d + 'px';
    mark.style.display = 'block';
  };
  img.src = api('img', `&id=${cam.id}&kind=full`);
  document.getElementById('pname').textContent = cam.source;
  syncAltSelection(cam);
}

// 후보 썸네일 줄. 각 썸네일에 클릭 지점을 점으로 찍어 고르기 전에 어디가 보이는지 알 수 있게 한다.
function renderAlts(cands, p) {
  const box = document.getElementById('alts');
  box.innerHTML = '';
  const top = cands.slice(0, 12);
  if (top.length < 2) { box.style.display = 'none'; return; }
  for (const it of top) {
    const uv = p ? projectToCam(it.cam, p) : null;
    const el = document.createElement('div');
    el.className = 'alt';
    el.dataset.id = it.cam.id;
    el.title = it.cam.source;
    el.innerHTML = `<img loading="lazy" src="${api('img', `&id=${it.cam.id}&kind=thumb`)}">`
      + (uv ? `<div class="dot" style="left:${uv.u * 100}%;top:${uv.v * 100}%"></div>` : '');
    el.onclick = () => showPhoto(it.cam, p);
    box.appendChild(el);
  }
  box.style.display = 'flex';
}

function syncAltSelection(cam) {
  for (const el of document.querySelectorAll('#alts .alt'))
    el.classList.toggle('sel', +el.dataset.id === cam.id);
}

// ---------------------------------------------------------------- 가림 검사

// 카메라에서 클릭 점까지의 시야가 트였는지 — 사이를 다른 면이 막고 있으면 그 사진엔 이 점이 안 보인다.
// 사진측량 모델은 자기 가림이 많아, 기하학적으로 프레임 안이어도 실제로는 가려진 카메라가 흔하다(댐 실측 ~24%).
// 이 검사가 없으면 점수 1등 사진이 엉뚱한 표면을 가리켜 붉은 점이 어긋난다. 작은 볼록 모델은 가림이 없어 티가 안 났다.
const _camPos = new THREE.Vector3(), _occDir = new THREE.Vector3();
function cameraSeesPoint(cam, p) {
  _camPos.set(cam.pos[0], cam.pos[1], cam.pos[2]);
  _occDir.copy(p).sub(_camPos);
  const dist = _occDir.length();
  const tol = Math.max(dist * 0.01, bs.radius * 1.5e-3);   // 점 자신·메시 노이즈를 가림으로 오인하지 않게
  if (dist <= tol) return true;
  _occDir.divideScalar(dist);
  // 점보다 '앞'을 막는 면이 하나라도 있으면 가려진 것 — 가장 가까운 것을 찾을 필요는 없다.
  // 지금 그려지는 타일(멀리는 요약 메시)로 검사한다 — 허용오차가 요약 오차보다 크다.
  return tiles.raycast(_camPos, _occDir, dist - tol, true) === null;
}

// 점수 순으로 내려가며 실제로 보이는 첫 카메라를 고른다. 전부 가려지면 최고점으로 되돌린다(표시 자체는 유지).
function pickVisibleCam(cands, p) {
  const CAP = 16;                                          // 클릭당 레이캐스트 상한 — 지연 방지
  for (let i = 0; i < cands.length && i < CAP; i++)
    if (cameraSeesPoint(cands[i].cam, p)) return cands[i].cam;
  return cands.length ? cands[0].cam : null;
}

// ---------------------------------------------------------------- 클릭 처리

const ray = new THREE.Raycaster();

function pickAt(clientX, clientY) {
  const r = renderer.domElement.getBoundingClientRect();
  if (!r.width || !r.height) return false;
  const mx = ((clientX - r.left) / r.width) * 2 - 1;
  const my = -((clientY - r.top) / r.height) * 2 + 1;
  ray.setFromCamera(new THREE.Vector2(mx, my), camera);
  // 지금 그려지는 타일에 대해 교차 — 타일에 변환을 걸지 않으므로 정점 좌표가 곧 월드 좌표다.
  const hit = tiles.raycast(ray.ray.origin, ray.ray.direction, Infinity, false);
  if (!hit) return false;

  const p = ray.ray.at(hit.distance, new THREE.Vector3());
  // 맞은 면의 법선을 세 정점으로 직접 구한다(BVH 는 면 번호만 돌려준다).
  const n = tiles.faceNormal(hit);
  // 클릭했다는 건 이 면이 지금 보인다는 뜻 — 법선을 시점 쪽으로 맞춰 바깥을 확정한다.
  // PLY의 면 winding이 일관되지 않아도 앞/뒤 판정이 뒤집히지 않는다.
  if (n && n.dot(camera.position.clone().sub(p)) < 0) n.negate();
  marker.position.copy(p);
  marker.visible = true;

  const cands = cameraCandidates(p, n);
  renderAlts(cands, p);
  const best = pickVisibleCam(cands, p);           // 가림 없이 실제로 보이는 사진 우선
  if (best) showPhoto(best, p);
  return true;
}

// ---- FPS 입력: 좌드래그=시점 회전 · 짧은 탭=사진 선택 · 우클릭 홀드=스코프 확대 ----
// 좌버튼을 누른 뒤 4px 넘게 움직이거나 180ms 넘게 유지하면 '시점 모드'로 확정한다.
// → 짧게 탭하면 그 지점의 사진, 살짝만 끌거나 조금 오래 눌러도 곧바로 시점 회전으로 넘어간다.
const LOOK_SENS = 0.0025, HOLD_MS = 180, MOVE_PX = 4;
const _drag = { down: false, sx: 0, sy: 0, lx: 0, ly: 0, t: 0, look: false };
renderer.domElement.addEventListener('mousedown', e => {
  if (e.button === 2) { scoping = true; e.preventDefault(); return; }   // 우클릭 = 스코프
  if (e.button !== 0) return;
  Object.assign(_drag, { down: true, look: false, sx: e.clientX, sy: e.clientY,
                         lx: e.clientX, ly: e.clientY, t: performance.now() });
});
renderer.domElement.addEventListener('contextmenu', e => e.preventDefault());   // 우클릭 메뉴 차단
addEventListener('mousemove', e => {
  if (!_drag.down) return;
  if (!_drag.look && (Math.abs(e.clientX - _drag.sx) + Math.abs(e.clientY - _drag.sy) > MOVE_PX
                      || performance.now() - _drag.t > HOLD_MS)) {
    _drag.look = true;
  }
  if (_drag.look) {
    camYaw -= (e.clientX - _drag.lx) * LOOK_SENS;                        // 좌우: yaw
    camPitch = clampPitch(camPitch - (e.clientY - _drag.ly) * LOOK_SENS);// 상하: pitch(반전 없음)
    _idle = 0;
  }
  _drag.lx = e.clientX; _drag.ly = e.clientY;
});
addEventListener('mouseup', e => {
  if (e.button === 2) { scoping = false; return; }
  if (e.button !== 0 || !_drag.down) return;
  _drag.down = false;
  if (!_drag.look && performance.now() - _drag.t <= HOLD_MS) pickAt(e.clientX, e.clientY);
});
addEventListener('blur', () => { _drag.down = false; _drag.look = false; scoping = false; });

// 휠 = 시선 방향 전진/후진(줌). 위로 굴리면 전진. Shift 로 4배 가속. 방향은 그대로 유지된다.
renderer.domElement.addEventListener('wheel', e => {
  e.preventDefault();
  const step = modelDiag * 0.004 * (e.shiftKey ? 4 : 1) * (e.deltaY < 0 ? 1 : -1);
  camera.position.addScaledVector(lookDir(_ld), step);
  _idle = 0;
}, { passive: false });

// ---- WASD 자유 이동(플라이) ----
// 시선(yaw/pitch)은 그대로 두고 카메라 위치만 옮긴다 — 방향은 루프의 applyLook() 이 매 프레임 유지.
// 속도는 모델 크기에 비례(모델이 570m든 50m든 체감 일정). Space/Ctrl 은 시선과 무관하게 월드 상하.
const _keys = new Set();
const _MOVE = ['KeyW', 'KeyA', 'KeyS', 'KeyD', 'Space', 'ControlLeft', 'ControlRight'];
// 지금 입력 칸에 타이핑 중인가 — 맞으면 WASD 를 이동으로 가로채지 않는다.
function _typingTarget() {
  const t = document.activeElement, tag = t && t.tagName;
  return tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA';
}
addEventListener('keydown', e => {
  if (_typingTarget()) return;
  if (_MOVE.includes(e.code)) { _keys.add(e.code); e.preventDefault(); }        // Space 스크롤 방지
  else if (e.code === 'ShiftLeft' || e.code === 'ShiftRight') _keys.add(e.code);// 가속(단축키 방해 X)
});
addEventListener('keyup', e => { _keys.delete(e.code); });
addEventListener('blur', () => _keys.clear());   // 포커스 이탈 시 키 잔류로 계속 움직이는 것 방지

const _fwd = new THREE.Vector3(), _right = new THREE.Vector3(), _move = new THREE.Vector3();
let _flyRamp = 0;                                // 0→1 가속 램프
const FLY_ACCEL = 1.2;                           // 최대 속도까지 걸리는 시간(초)
const FLY_MIN = 0.05;                            // 시작 속도 = 최대의 5% (천천히 출발)
// 멈췄다 다시 누르면 램프가 0부터라 항상 천천히 출발한다. Shift 는 4배 가속.
function updateFly(dt) {
  const f = (_keys.has('KeyW') ? 1 : 0) - (_keys.has('KeyS') ? 1 : 0);
  const r = (_keys.has('KeyD') ? 1 : 0) - (_keys.has('KeyA') ? 1 : 0);
  const u = (_keys.has('Space') ? 1 : 0)
          - ((_keys.has('ControlLeft') || _keys.has('ControlRight')) ? 1 : 0);
  if (!f && !r && !u) { _flyRamp = 0; return; }   // 멈추면 램프 리셋 → 다음엔 다시 천천히 출발
  _flyRamp = Math.min(1, _flyRamp + dt / FLY_ACCEL);
  lookDir(_fwd);
  _right.crossVectors(_fwd, camera.up).normalize();
  _move.set(0, 0, 0).addScaledVector(_fwd, f).addScaledVector(_right, r);
  _move.z += u;                                  // 상하는 월드 Z(카메라가 어딜 보든 위/아래)
  if (_move.lengthSq() === 0) return;
  _move.normalize();                             // 대각 이동이 빨라지지 않게
  const sprint = (_keys.has('ShiftLeft') || _keys.has('ShiftRight')) ? 4 : 1;
  const speed = FLY_MIN + (1 - FLY_MIN) * _flyRamp;             // 처음 느리게 → 가속 → 최대
  _move.multiplyScalar(modelDiag * 0.15 * dt * sprint * speed); // 모델 크기 비례 속도
  camera.position.add(_move);                    // 위치만 이동(방향은 applyLook 이 유지)
  _idle = 0;
}

// ---------------------------------------------------------------- 확대 창

const ctx = document.getElementById('ctx');
document.getElementById('photo').addEventListener('contextmenu', e => {
  e.preventDefault();
  if (!lastPick) return;
  ctx.style.display = 'block';
  ctx.style.left = Math.min(e.clientX, innerWidth - ctx.offsetWidth - 8) + 'px';
  ctx.style.top = Math.min(e.clientY, innerHeight - ctx.offsetHeight - 8) + 'px';
});
addEventListener('pointerdown', e => { if (!ctx.contains(e.target)) ctx.style.display = 'none'; });
document.getElementById('ctxDetail').addEventListener('click', () => {
  ctx.style.display = 'none';
  openImageModal();
});

const modal = document.getElementById('imgmodal');
const imView = document.getElementById('imView');
const imStage = document.getElementById('imStage');
const imImg = document.getElementById('imImg');
const imMark = document.getElementById('imMark');
let mv = { scale: 1, tx: 0, ty: 0 }, fitScale = 1;

function applyStage() {
  imStage.style.transform = `translate(${mv.tx}px,${mv.ty}px) scale(${mv.scale})`;
  // 확대할수록 붉은 원을 옅게 — 위치 안내용이라 표면을 볼 때는 방해하지 않아야 한다
  const z = mv.scale / fitScale;
  imMark.style.opacity = Math.max(0, Math.min(1, 1 - (z - 1) / 2));
}

function openImageModal() {
  const { cam, uv } = lastPick;
  document.getElementById('imTitle').textContent = cam.source;
  imImg.onload = () => {
    const vw = imView.clientWidth, vh = imView.clientHeight;
    fitScale = Math.min(vw / imImg.naturalWidth, vh / imImg.naturalHeight) * 0.98;
    mv = { scale: fitScale,
           tx: (vw - imImg.naturalWidth * fitScale) / 2,
           ty: (vh - imImg.naturalHeight * fitScale) / 2 };
    if (uv) {
      const d = Math.max(36, imImg.naturalWidth * 0.05);
      imMark.style.width = imMark.style.height = d + 'px';
      imMark.style.left = (uv.u * imImg.naturalWidth) + 'px';
      imMark.style.top = (uv.v * imImg.naturalHeight) + 'px';
      imMark.style.display = 'block';
    } else {
      imMark.style.display = 'none';
    }
    applyStage();
  };
  imImg.src = api('img', `&id=${cam.id}&kind=full`);
  modal.style.display = 'flex';
}

function closeModal() { modal.style.display = 'none'; imImg.removeAttribute('src'); }
document.getElementById('imClose').addEventListener('click', closeModal);
addEventListener('keydown', e => { if (e.key === 'Escape' && modal.style.display === 'flex') closeModal(); });

// 휠 — 커서 아래 픽셀이 제자리에 남도록 확대/축소
imView.addEventListener('wheel', e => {
  e.preventDefault();
  const r = imView.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  const ns = Math.max(fitScale * 0.5, Math.min(fitScale * 40, mv.scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
  mv.tx = mx - (mx - mv.tx) * (ns / mv.scale);
  mv.ty = my - (my - mv.ty) * (ns / mv.scale);
  mv.scale = ns;
  applyStage();
}, { passive: false });

let _mdrag = null;
imView.addEventListener('pointerdown', e => {
  _mdrag = { x: e.clientX, y: e.clientY, tx: mv.tx, ty: mv.ty };
  imView.classList.add('drag');
  imView.setPointerCapture(e.pointerId);
});
imView.addEventListener('pointermove', e => {
  if (!_mdrag) return;
  mv.tx = _mdrag.tx + e.clientX - _mdrag.x;
  mv.ty = _mdrag.ty + e.clientY - _mdrag.y;
  applyStage();
});
imView.addEventListener('pointerup', () => { _mdrag = null; imView.classList.remove('drag'); });

// ---------------------------------------------------------------- XYZ 기즈모

const GZ = (function () {
  const host = document.getElementById('gizmo'), C = 44, R = 30;
  const AX = [{ v: new THREE.Vector3(1, 0, 0), col: '#ff5a72', lab: 'X' },
              { v: new THREE.Vector3(0, 1, 0), col: '#5ad46a', lab: 'Y' },
              { v: new THREE.Vector3(0, 0, 1), col: '#4aa8ff', lab: 'Z' }];
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 88 88');
  host.appendChild(svg);
  const mk = (t, a) => {
    const e = document.createElementNS(NS, t);
    for (const k in a) e.setAttribute(k, a[k]);
    return e;
  };

  // 그 축 방향에서 모델 중심을 본다(현재 거리 유지). ±Z는 시선이 up과 평행해지는 특이점이라
  // 살짝 비껴 세운다 — clampPitch 가 잘라 주지만 yaw 가 불안정해지는 것을 막는다.
  function snapView(dir) {
    const dist = camera.position.distanceTo(bs.center);
    const p = bs.center.clone().addScaledVector(dir, dist);
    if (Math.abs(dir.z) > 0.99) p.y -= dist * 0.02;
    camera.position.copy(p);
    lookToward(camera.position, bs.center);      // 방향은 yaw/pitch 로 — 루프의 applyLook 이 반영
    _idle = 0;
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
    g.appendChild(circ);
    g.appendChild(txt);
    svg.appendChild(g);
    g.addEventListener('click', e => { e.stopPropagation(); snapView(ax.v.clone().multiplyScalar(s)); });
    nodes.push({ ax, s, line, g });
  }

  // 월드축을 뷰 좌표로 투영해 배치. z 순으로 다시 붙여 앞쪽 축이 위에 오게 한다.
  function update() {
    const q = camera.quaternion.clone().invert();
    const proj = nodes.map(n => {
      const p = n.ax.v.clone().multiplyScalar(n.s).applyQuaternion(q);
      return { n, x: C + p.x * R, y: C - p.y * R, z: p.z };
    });
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

// ---------------------------------------------------------------- 루프

addEventListener('resize', () => {
  camera.aspect = W() / H();
  camera.updateProjectionMatrix();
  renderer.setSize(W(), H());
});

// 조작 중 프레임이 떨어지면 내부 해상도를 낮추고, 여유가 생기거나 멈추면 되돌린다.
// 큰 메시에서 병목은 대개 픽셀이라 해상도를 줄이면 바로 회복된다.
let _ratio = MAX_RATIO, _acc = 0, _n = 0;
const MIN_RATIO = 0.6;

function adapt(dt) {
  _acc += dt; _n++;
  if (_n < 20) return;                           // 20프레임 평균 — 스파이크에 흔들리지 않게
  const fps = _n / _acc;
  _acc = 0; _n = 0;
  const moving = _idle < 0.4;
  let want = _ratio;
  if (moving && fps < 40) want = Math.max(MIN_RATIO, _ratio - 0.2);
  else if (!moving || fps > 55) want = Math.min(MAX_RATIO, _ratio + 0.2);
  if (Math.abs(want - _ratio) > 0.01) {
    _ratio = want;
    renderer.setPixelRatio(_ratio);
    renderer.setSize(W(), H());
  }
}

const _clock = new THREE.Clock();
(function loop() {
  requestAnimationFrame(loop);
  const dt = Math.min(_clock.getDelta(), 0.25);
  _idle += dt;
  updateFly(dt);                                 // WASD 이동(위치만) — _idle 도 여기서 리셋된다
  // 우클릭 스코프: fov 를 목표값으로 부드럽게 좁혔다 되돌린다
  const fovT = scoping ? SCOPE_FOV : BASE_FOV;
  if (Math.abs(camera.fov - fovT) > 0.05) {
    camera.fov += (fovT - camera.fov) * Math.min(1, dt * 12);
    camera.updateProjectionMatrix();
    _idle = 0;
  }
  applyLook();                                   // yaw/pitch(+이동된 위치)를 반영 · up 항상 Z
  camera.updateMatrixWorld();                    // 타일 선택이 이번 프레임의 카메라 행렬을 읽는다
  tiles.update(camera, H(), _idle < 0.4);        // 보이는 타일 선택·요청·해제 (조작 중엔 거칠게)
  GZ.update();
  sizeMarker();
  renderer.render(scene, camera);
  adapt(dt);
})();
