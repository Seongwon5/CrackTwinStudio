import * as THREE from 'three';
import { PLYLoader } from 'three/addons/loaders/PLYLoader.js';
import { buildBVH } from '/bvh.js';
import { buildLOD } from '/lod.js';
import { shiftToLocalOrigin, shiftMetaOrigin } from '/origin.js';
import { createTileSet, parseTile, legacySource } from '/tiles.js';

const scene = new THREE.Scene(); scene.background = new THREE.Color(0x0e1319);
// Z축을 위(UP)로 설정 (일반적인 드론/사진 측량 좌표계)
THREE.Object3D.DEFAULT_UP = new THREE.Vector3(0,0,1);
const camera = new THREE.PerspectiveCamera(60, innerWidth/innerHeight, 0.01, 1000000);
camera.up.set(0,0,1);

const renderer = new THREE.WebGLRenderer({antialias:true});
renderer.setSize(innerWidth, innerHeight); renderer.setPixelRatio(devicePixelRatio);
document.body.appendChild(renderer.domElement);

scene.add(new THREE.AmbientLight(0xffffff,0.85));
const dl=new THREE.DirectionalLight(0xffffff,0.5); dl.position.set(1,1,2); scene.add(dl);
// ================= FPS 자유 이동 카메라 (OrbitControls 대체) =================
// 좌드래그=시점 회전 · WASD/Space/Ctrl=이동 · 우클릭 홀드=확대(FOV). 휠 줌·패닝·orbit 없음.
// 시선은 yaw(월드 Z축 회전)+pitch(수평선 기준 올려본 각)로만 표현한다. 따라서:
//  · camera.up 은 항상 (0,0,1) — 롤이 생길 수 없다(요구사항: 카메라 선택 후에도 up 불변).
//  · pitch 는 언제나 atan2(dz, 수평거리) 로 계산 → 부호가 뒤집힐 경로가 없다(상하 반전 금지).
let camYaw = 0, camPitch = 0;
const PITCH_LIMIT = Math.PI/2 - 0.02;           // 수직(±90°)에서 lookAt 특이점 회피
const BASE_FOV = 60, SCOPE_FOV = 22;            // 우클릭 스코프 확대 FOV
let scoping = false;                            // 우클릭 홀드 중?
const _ld = new THREE.Vector3();
function clampPitch(p){ return Math.max(-PITCH_LIMIT, Math.min(PITCH_LIMIT, p)); }  // 수직 특이점 직전에서 자름
function lookDir(out){                            // yaw/pitch → 단위 시선 벡터(Z-up)
  const cp = Math.cos(camPitch);
  return out.set(cp*Math.cos(camYaw), cp*Math.sin(camYaw), Math.sin(camPitch));
}
function applyLook(){                             // 현재 위치에서 yaw/pitch 방향을 바라보게
  lookDir(_ld);
  camera.up.set(0,0,1);                          // 매 프레임 강제 → up 절대 불변
  camera.lookAt(camera.position.x+_ld.x, camera.position.y+_ld.y, camera.position.z+_ld.z);
}
function lookToward(fromV, toV){                  // 한 점을 바라보도록 yaw/pitch 설정(일관 부호)
  const dx=toV.x-fromV.x, dy=toV.y-fromV.y, dz=toV.z-fromV.z;
  camYaw = Math.atan2(dy, dx);
  camPitch = clampPitch(Math.atan2(dz, Math.hypot(dx,dy)));
}

// ================= 보고서 로딩 =================
// 예전에는 백엔드가 .twin 전체를 여기에 인라인했다(`const DATA = {...2GB...}`). 그 방식은
// V8 최대 문자열(536,870,888자 = 536.9MB)을 넘는 순간 "Invalid string length" 로 죽는다 — RAM 과
// 무관한 하드 한계다. 그래서 지금은:
//   · 메타(기하만, 수십MB)  → JSON 으로 fetch
//   · 메시(GB 급)          → ArrayBuffer 로 fetch (문자열 한계와 무관. 1.6GB 도 실측 확인)
//   · 이미지               → <img src=API> 로 지연 로드 (브라우저가 캐시/해제를 관리)
const REPORT = window.__VIEWER_REPORT__;
const ASSET = window.__VIEWER_ASSET__;          // 자산(검사 간 비교) 모드: compare_twins 의 asset.json. null=일반 뷰
const _q = encodeURIComponent(REPORT);
const api = (kind, extra = '') => `/api/report/${kind}?p=${_q}${extra}`;
const imgUrl = (cam, kind) => api('img', `&id=${cam.id}&kind=${kind}`);

// 보고서 API 호출 공통 래퍼. as='json' 이면 JSON, 아니면 ArrayBuffer(메시용).
// 404/500 을 조용히 넘기지 않고 본문까지 붙여 던진다 — 부팅 실패 원인이 화면에 보이게.
async function _fetch(url, as) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url.split('?')[0]} → ${r.status} ${await r.text()}`);
  return as === 'json' ? r.json() : r.arrayBuffer();
}

const _boot = document.getElementById('bootMsg');
const _say = m => { if (_boot) _boot.textContent = m; };

let DATA, tileSrc;
let WORLD_ORIGIN = [0, 0, 0];        // 국소좌표에 더하면 보고서 원본(절대) 좌표가 된다(아래 원점 이동 참조)
try {
  _say('보고서 메타 읽는 중...');
  DATA = await _fetch(api('meta'), 'json');
  if (DATA.tiles) {
    // 계층 LOD 타일 보고서(tiler.py) — 타일 좌표는 이미 국소좌표라 meta 쪽만 같은 원점으로 맞춘다.
    WORLD_ORIGIN = DATA.tiles.origin.slice();
    shiftMetaOrigin(DATA, WORLD_ORIGIN);
    const color = !!DATA.tiles.color;
    tileSrc = { nodes: DATA.tiles.nodes, root: DATA.tiles.root, box: DATA.tiles.box, color,
                load: async id => parseTile(THREE, await _fetch(api('tile', `&id=${id}`), 'buf'), !color) };
    _say(`3D 타일 ${DATA.tiles.nodes.length.toLocaleString()}개 (카메라 ${DATA.counts.cameras}대 · 크랙 ${DATA.counts.hotspots}개) — 첫 화면 준비 중...`);
  } else {
    // 옛 보고서(mesh.ply) — 통째로 받아 간략 메시(lod.js) + 원본의 두 단계 타일셋으로 감싼다.
    _say(`3D 메시 받는 중... (카메라 ${DATA.counts.cameras}대 · 크랙 ${DATA.counts.hotspots}개)`);
    const buf = await _fetch(api('mesh'), 'buf');
    _say('좌표계 정리 중...');
    WORLD_ORIGIN = shiftToLocalOrigin(buf, DATA);     // origin.js — 메시 바이너리와 meta 를 같은 원점으로
    tileSrc = legacySource(THREE, buildLOD, new PLYLoader().parse(buf), 700000);
  }
} catch (e) {
  _say('보고서를 열 수 없습니다: ' + e.message);
  throw e;
}
document.getElementById('hdrCounts').textContent =
  `분석 이미지: ${DATA.counts.cameras}장 · 검출 포인트: ${DATA.counts.hotspots}개`;

// ================= 좌표 원점 이동(float32 정밀도 확보) =================
// PLY 와 meta 는 UTM 절대좌표(예: Y≈4,113,800)를 double 로 담는데, three.js 는 위치를
// Float32BufferAttribute 로만 GPU 에 올린다. 값이 크면 눈금이 0.25 m 까지 벌어져 표면이 계단처럼
// 층지고 크랙 튜브 중심선도 밀린다(실측·근거는 ui/origin.js 머리말). 그래서 그리기 전에 같은 원점을 뺀다.
//   · 타일 보고서: tiler.py 가 생성 시 원점을 빼 두었고 meta.tiles.origin 에 적어 둔다 → meta 만 옮긴다
//   · 옛 보고서  : origin.js 의 shiftToLocalOrigin 이 메시 바이너리와 meta 를 함께 옮긴다
// 화면·CSV 에 절대좌표를 내보내는 자리에서는 wpt() 로 원점을 다시 더한다.
// 길이·거리·각도는 차분량이라 이동과 무관하다(캘리브레이션·측정 결과 불변).
// 국소좌표 → 표시용 절대좌표
const wpt = p => [p[0] + WORLD_ORIGIN[0], p[1] + WORLD_ORIGIN[1], p[2] + WORLD_ORIGIN[2]];
// 좌표 한 줄 표기(회색). 화면에는 국소좌표가 아니라 보고서 원본 좌표를 보여준다.
function fmtXYZ(p, d = 2){
  const w = wpt(p);
  return '<span style="color:#888">X ' + w[0].toFixed(d) +
         '  Y ' + w[1].toFixed(d) + '  Z ' + w[2].toFixed(d) + '</span>';
}

// ===== 검사원 검토(확인/유형수정/오탐) — 사이드카(<twin>.review.json) 영속 =====
// (유형 분류는 폐지됨 — 검토는 확인/오탐/메모만. 오탐 라벨은 검출 모델 재학습 데이터가 된다.)
let REVIEW = {version:1, reviews:{}};
try{ REVIEW = await _fetch('/api/review?p='+_q, 'json'); }catch(e){}
// 균열 i 의 검토 기록(없으면 null).
function revOf(i){ return (REVIEW.reviews||{})[String(i)] || null; }
// 검토를 서버 사이드카에 저장하고 로컬 REVIEW 도 함께 갱신한다(state:'clear' 면 삭제).
// 저장 성공분만 반영하므로 화면과 파일이 어긋나지 않는다.
async function saveReview(i, patch){
  const r = await fetch('/api/review', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(Object.assign({p: REPORT, idx: i}, patch))});
  const d = await r.json();
  if(d.error){ alert('검토 저장 실패: '+d.error); return null; }
  if(patch.state==='clear') delete REVIEW.reviews[String(i)];
  else REVIEW.reviews[String(i)] = d.review;
  return d.review;
}
const REV_LABEL={confirmed:'확인됨', rejected:'오탐', edited:'수정됨'};
const REV_COLOR={confirmed:'#3fbf7f', rejected:'#e0574e', edited:'#ffb454'};

// ===== 스케일 캘리브레이션: 기준 거리(실측)로 모든 mm 수치를 보정하는 전역 계수 =====
// 파이프라인의 mm 는 전부 '메시 1단위=1m' 전제에서 나온 선형량 → 한 계수로 일괄 보정 가능.
const CALIB_KEY = 'ctw_calib::' + (new URLSearchParams(location.search).get('report') || location.pathname);
let calibScale = 1.0, calibInfo = null;             // calibInfo={scale, measured_m, real_mm}
// 1차 저장소는 사이드카(REVIEW.calib) — PC/브라우저가 바뀌어도 유지되고 PDF 도 같은 값을 쓴다.
// 사이드카에 없고 localStorage(구버전 저장소)에만 있으면 1회 마이그레이션해 올린다.
try{
  const sc = REVIEW.calib;
  if(sc && sc.scale > 0){ calibScale = sc.scale; calibInfo = sc; }
  else {
    const s = JSON.parse(localStorage.getItem(CALIB_KEY) || 'null');
    if(s && s.scale > 0){
      calibScale = s.scale; calibInfo = s;
      fetch('/api/calib', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({p: REPORT, scale: s.scale, measured_m: s.measured_m, real_mm: s.real_mm})})
        .catch(()=>{});                             // 실패해도 이번 세션 표시는 정상(다음 부팅에 재시도)
    }
  }
}catch(e){}
function cmm(x){ return x==null ? null : x * calibScale; }   // 데이터 mm → 보정 mm
// 총연장(가지 포함) — 구 .twin 은 length_total_mm 이 없어 length_mm(최장경로)로 폴백
function ltot(h){ return cmm(h.length_total_mm!=null ? h.length_total_mm : h.length_mm); }
// 길이 표기: "길이 183mm" + 가지가 있어 총연장이 더 길면 "(총연장 201)" 덧붙임
function fmtLen(h){
  const L=cmm(h.length_mm), T=ltot(h);
  const base='길이 '+(L!=null?Math.round(L).toLocaleString():'?')+'mm';
  return (T!=null && L!=null && T>L+0.5) ? base+' (총연장 '+Math.round(T).toLocaleString()+')' : base;
}
// ===== 측정 바닥(분해능 한계) =====
// 폭 < 2×GSD 면 그 폭은 분해능 안에서 잰 값이 아니라 '상한값'이다 — "≤"로 검열 표기해
// 가짜 정밀도를 없앤다. gsd_mm(대표 뷰의 픽셀당 mm)은 파이프라인이 기록하며,
// 캘리브레이션은 선형 보정이라 GSD 에도 같은 계수가 곱해진다. 구 .twin(gsd_mm 없음)은 그대로 표기.
function wFloor(h){ return (h && h.gsd_mm != null) ? cmm(h.gsd_mm) * 2 : null; }
// '보정된' 폭 w 하나를 표기 — 바닥 미만이면 "≤바닥".
function fmtW(h, w, d=1){
  if(w == null) return '?';
  const f = wFloor(h);
  return (f != null && w < f) ? '≤' + f.toFixed(d) : w.toFixed(d);
}
// 뷰 간 산포 불확실도(±IQR/2, 뷰 3장 이상일 때만 파이프라인이 기록).
function fmtUnc(h){ return (h && h.w_unc_mm != null) ? ' ±' + cmm(h.w_unc_mm).toFixed(2) : ''; }
// ===== 심각도(절대 폭 기준, 단일 체계) =====
// 예전엔 '심각도(상대 순위)'와 '판정 등급 A~D'가 따로 있어 기준이 둘이었다 → 하나로 통일.
// 보정(cmm)된 폭으로 매번 재계산하므로 캘리브레이션 즉시 반영된다.
const SEV_B = (DATA.report_meta && DATA.report_meta.severity_bounds_mm) || [0.5, 1.0, 3.0, 5.0, 10.0];
const SEV_KEYS = ['S1','S2','S3','S4','S5','S6'];
const SEV_NAME = {S1:'경미', S2:'관찰', S3:'보통', S4:'심각', S5:'매우 심각', S6:'특이값'};
const SEV_HEX  = {S1:'#9ccb5b', S2:'#2e9e5b', S3:'#f0c419', S4:'#ee8a3c', S5:'#d9433c', S6:'#8a6bc4'};
function sevWidth(h){                                // 심각도 판정에 쓰는 대표 폭(보정 반영)
  return cmm(h.width_mm_p95 != null ? h.width_mm_p95
             : (h.width_mm_median != null ? h.width_mm_median : h.width_mm_max));
}
function sevGrade(w){                                // 폭(mm) → 'S1'~'S6'
  if(w == null) return null;
  let k = 0; for(const b of SEV_B) if(w >= b) k++;
  return SEV_KEYS[k];
}
function hotSev(i){ return sevGrade(sevWidth(DATA.hotspots[i])); }   // 균열 i 의 심각도 등급
function hotSevColor(i){                             // 균열 i 의 심각도 색(등급 없으면 회색)
  const s = hotSev(i);
  return new THREE.Color(s ? SEV_HEX[s] : '#8899aa');
}
// ===== 균열 신뢰도(conf) — 화면의 유일한 '신뢰' 지표 =====
// conf = AI 가 그 지점을 균열이라고 본 확률(0~1). 예전의 '측정 신뢰 A/B/C'는 화면에 '신뢰'가
// 둘이라 혼동이 커서 폐지했다(실척 의심 경고는 좌측 상단 scale_warning 박스가 담당).
const CONF_B = (DATA.report_meta && DATA.report_meta.conf_bounds) || [0.7, 0.9];
const CONF_NAME = ['낮음', '보통', '높음'];
const CONF_HEX  = ['#e0574e', '#ffb454', '#12b5cc'];
function confBand(h){                              // 0=낮음 · 1=보통 · 2=높음
  const c = (h && h.conf != null) ? h.conf : null;
  if(c == null) return null;
  return c >= CONF_B[1] ? 2 : (c >= CONF_B[0] ? 1 : 0);
}
function confBadge(h){                             // 신뢰도 배지 한 조각(html) — 색 + 이름 + 실제 확률
  const b = confBand(h); if(b == null) return '';
  return '<span style="color:'+CONF_HEX[b]+'">균열 신뢰도 '+CONF_NAME[b]
       + ' ' + Number(h.conf).toFixed(2) + '</span>';
}
function gradeBadge(h){                            // 말풍선/상세용 한 줄(html)
  const sv=sevGrade(sevWidth(h)), cb=confBadge(h);
  if(!sv) return cb;
  return '<span style="color:'+SEV_HEX[sv]+';font-weight:600">'+SEV_NAME[sv]+'</span>'
       + (cb ? ' · ' + cb : '');
}
// 심각도 범례 — 경계는 meta 에서 읽으므로 기준을 바꾸면 자동 반영된다.
function sevRange(k){
  return k === 0 ? ('< ' + SEV_B[0])
       : k === SEV_KEYS.length - 1 ? ('≥ ' + SEV_B[SEV_B.length - 1])
       : (SEV_B[k - 1] + '–' + SEV_B[k]);
}
(function fillSevLegend(){
  const el=document.getElementById('sevRows'); if(!el) return;
  el.innerHTML=SEV_KEYS.map((g,i)=>
    '<span style="display:inline-flex;align-items:center;gap:5px;margin-right:10px;white-space:nowrap;">'
    +'<span style="width:9px;height:9px;border-radius:3px;background:'+SEV_HEX[g]+'"></span>'
    +SEV_NAME[g]+' <span style="color:var(--tx-3)">'+sevRange(i)+'mm</span></span>').join('');
})();
// 균열 신뢰도 범례 — 구간은 meta 에서 읽으므로 기준을 바꾸면 자동 반영된다.
(function fillConfLegend(){
  const el=document.getElementById('confRows'); if(!el) return;
  const rng=['< '+CONF_B[0], CONF_B[0]+'–'+CONF_B[1], '≥ '+CONF_B[1]];
  el.innerHTML=CONF_NAME.map((nm,i)=>
    '<span style="display:inline-flex;align-items:center;gap:5px;margin-right:11px;white-space:nowrap;">'
    +'<span style="width:9px;height:9px;border-radius:3px;background:'+CONF_HEX[i]+'"></span>'
    +nm+' <span style="color:var(--tx-3)">'+rng[i]+'</span></span>').join('');
})();
// 핫스팟 요약문(파이프라인이 구워둔 h.text 대신 — 보정 계수를 반영해 매번 생성)
function fmtHotText(h){
  if(h.width_mm_median==null) return h.text || '크랙';
  const f=(x,d=1)=>fmtW(h, x!=null?Number(cmm(x)):null, d);
  return `크랙 폭 중앙 ${f(h.width_mm_median)}mm${fmtUnc(h)} (p95 ${f(h.width_mm_p95)} · 최대 ${f(h.width_mm_max)})`
    + (h.width_mm_3d!=null && h.width_src!=='3d' ? ` · 3D폭 ${f(h.width_mm_3d)}` : '')   // 1차가 3D 면 중복 생략
    + ` · ${fmtLen(h)}`;
}

// 보고서 메타데이터 표시 (report_meta 가 없는 구형 보고서는 건너뜀)
function renderMeta(){
  const m = DATA.report_meta;
  if(!m || !Object.keys(m).length) return;
  const esc = s => String(s).replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
  const rows = [];
  if(m.created) rows.push('생성: ' + esc(m.created.replace('T',' ')));
  if(m.mesh_file) rows.push('메시: ' + esc(m.mesh_file) + (m.mesh_faces ? ` (${m.mesh_faces.toLocaleString()}면, ${m.mesh_diag_m}m)` : ''));
  if(m.images_found != null){
    let s = `이미지: ${m.images_analyzed}/${m.images_found}장 분석`;
    if(m.images_skipped) s += ` · <span style="color:#ffb84d">${m.images_skipped}장 제외</span>`;
    rows.push(s);
  }
  if(m.width_mm_range) rows.push(`크랙 폭: ${cmm(m.width_mm_range[0]).toFixed(2)}~${cmm(m.width_mm_range[1]).toFixed(2)}mm`
    + (calibScale!==1 ? ' <span style="color:#7fffd4">(보정됨)</span>' : ''));
  // 뷰 간 정합 오차(실측) — 병합 eps 대비 3/4 를 넘으면 병합·위치 신뢰가 흔들리는 신호
  if(m.view_reg_mm_median != null){
    const bad = m.merge_eps_mm && m.view_reg_mm_median > m.merge_eps_mm * 0.75;
    rows.push(`뷰 간 정합: 중앙 ${m.view_reg_mm_median}mm · p90 ${m.view_reg_mm_p90}mm`
      + (bad ? ` <span style="color:#ffb84d" title="정합 오차가 병합 기준(eps ${m.merge_eps_mm}mm)에 근접 — 같은 균열이 갈라지거나 위치가 흔들릴 수 있습니다">⚠</span>` : '')
      + (m.split_suspect_pairs ? ` · <span style="color:#ffb84d" title="서로 3×eps 안까지 접근한 크랙 쌍 — 정합 오차로 갈라진 같은 균열일 수 있습니다">조각남 의심 ${m.split_suspect_pairs}쌍</span>` : ''));
  }
  document.getElementById('reportMeta').innerHTML = rows.join('<br>');
  const warn = document.getElementById('scaleWarn');
  if(calibScale !== 1){                              // 보정 적용됨 → 경고 대신 보정 상태 표시
    warn.style.display='block';
    warn.style.color='#7fffd4'; warn.style.background='rgba(10,60,45,.35)'; warn.style.borderColor='#1a5a4a';
    warn.innerHTML = '✔ 스케일 보정 적용: ×' + calibScale.toFixed(4)
      + (calibInfo ? `<br><span style="color:#9fd8c8">기준 ${(calibInfo.measured_m).toFixed(3)}m → 실측 ${calibInfo.real_mm.toLocaleString()}mm</span>` : '');
  } else if(m.scale_warning){
    warn.style.display='block';
    warn.style.color='#ffb84d'; warn.style.background='rgba(80,50,10,.35)'; warn.style.borderColor='#6a4a1a';
    warn.innerHTML =
      '⚠ 측정 스케일 확인 필요<br><span style="color:#c9a86a">크랙 폭이 비정상적으로 큽니다'
      + (m.width_mm_range ? ` (최대 ${m.width_mm_range[1]}mm)` : '')
      + '. 메시가 실척(1단위=1m)이 아닐 수 있어 mm 값을 신뢰하기 어렵습니다.'
      + ' 아래 <b>스케일 캘리브레이션</b>으로 보정할 수 있습니다.</span>';
  }
}
renderMeta();

// 그리드 및 좌표축 그룹
const gridGroup = new THREE.Group();
scene.add(gridGroup);

// ================= 3D 메시: 계층 LOD 타일 (ui/tiles.js 머리말 참조) =================
// 처음엔 루트 타일 하나로 그리고, 카메라가 다가가는 영역만 세밀한 타일로 바꿔 간다.
// 6GB(면 1억 9천만) 메시도 화면엔 수백만 면만 올라간다. 모든 타일이 meshMat 하나를 공유한다.
let globalMesh = null;                   // 타일 그룹(THREE.Group) — 예전 단일 Mesh 자리
let meshMat = null;                      // 타일 공용 재질(어두운 메시 토글이 만진다)
let tiles = null;                        // 타일셋 — update/raycast/faceNormal/box/sphere
let originalMeshColor = 0xcccccc;
let hasVertexColors = false;
let sceneCenter = new THREE.Vector3();   // 물체 중심(회전 피벗 기준)

try {
  hasVertexColors = !!tileSrc.color;
  originalMeshColor = hasVertexColors ? 0xffffff : 0xcccccc;
  // DoubleSide — LOD 타일은 요약 과정에서 감기 방향이 뒤집힐 수 있다(tiler.py simplify 참조)
  meshMat = new THREE.MeshBasicMaterial({ vertexColors: hasVertexColors, color: originalMeshColor,
                                         side: THREE.DoubleSide });
  // 예산: 화면 500만 면 · 타일 1.5GB · 정제 문턱 2px(조작 중 5px). 넘치면 문턱이 스스로 올라간다.
  tiles = createTileSet({ THREE, buildBVH }, tileSrc,
                        { material: meshMat, triBudget: 5e6, byteBudget: 1.5e9, ssePx: 2, movingScale: 2.5 });
  await tiles.ready;                     // 루트 타일 도착 → 경계·중심을 안다
  globalMesh = tiles.group;
  scene.add(globalMesh);
  window.__tiles = tiles;                // 개발자 도구 디버그용(__tiles.stats)
  window.__viewer = { renderer, scene, camera, THREE };   // 진단용(이음새 검사 등) — 동작에는 관여하지 않는다

  const s = tiles.sphere;
  sceneCenter.copy(s.center);
  camera.position.copy(s.center).add(new THREE.Vector3(0,-s.radius*2.5,s.radius*0.6));
  lookToward(camera.position, s.center);         // 중심을 바라보게 yaw/pitch 초기화
  camera.near=s.radius/100; camera.far=s.radius*100; camera.updateProjectionMatrix(); applyLook();
} catch(e) {
  alert('내장 3D 모델 로드 실패: ' + e);
}

// 둥근 점 텍스처(가운데 꽉 찬 원 + 부드러운 가장자리). PointsMaterial 기본은 각진 사각형이라
// 점이 뭉치면 격자처럼 보인다 → 원형으로 바꿔 크랙 형상이 읽히게 한다.
// (검출 픽셀 점군 표시 기능은 제거 — 크랙 형상은 리본이 담당)

// ================= 크랙 리본 (표면 위, 실폭 반영 가변-반경 튜브) =================
// poly(중심선 3D)를 poly_n(바깥 법선)으로 살짝 띄우고, 각 정점 반경을 실제 폭(poly_w)에 비례시켜
// 그린다 → 튜브가 크랙 모양(넓/좁)을 그대로 따라간다. 굵기는 과장(EXAG), 색은 노랑 고정.
const RIBBON_COLOR = 0xffff00;
const RIBBON_MAXGAP_M = 0.3;                      // 이보다 긴 구간은 유령 점프로 보고 끊는다
// ---- 튜브 굵기 두 모드 ----
// 실폭(×1, 기본): 튜브 지름 = 측정 폭 그대로(공칭 mm → 메시 단위). 0.3mm 크랙은 0.3mm 튜브 —
//   가까이 가야 보이는 게 실척의 의미다. 멀리서는 히트박스(화면 14px)로 여전히 호버/클릭된다.
// 강조: 예전 동작(최소 반경 2cm + 2배 과장) — 멀리서 크랙 '분포'를 훑을 때 쓴다.
let ribbonTrueScale = true;
try{ ribbonTrueScale = (localStorage.getItem('ctw_ribbon_true') ?? '1') === '1'; }catch(e){}
const RIBBON_OFFSET_M = 0.06;                     // [강조] 표면 위로 확실히 띄워 메시에 안 가리게
const RIBBON_WIDTH_EXAG = 2.0;                    // [강조] 실폭 대비 과장 배율
const RIBBON_MIN_R = 0.02, RIBBON_MAX_R = 0.5;    // [강조] 반경 하한/상한(m)
const RIBBON_TRUE_MIN_R = 5e-5;                   // [실폭] 반경 하한 0.05mm — 퇴화(반경 0) 방지용
const RIBBON_RADIAL = 6;
const ribbonGroup = new THREE.Group(); scene.add(ribbonGroup);
const ribbonHitLines = [];            // 크랙 히트박스(투명 라인) — 보이는 튜브보다 크게 호버/클릭 피킹
const HIT_MAT = new THREE.LineBasicMaterial({visible:false});   // material.visible=false → 렌더 X, 레이캐스트는 O (객체 visible=false 로 두면 이 three 버전은 피킹에서 제외됨)
const HITBOX_PX = 14;                 // 크랙 중심선 주변 화면상 여유 반경(px) — 이만큼 덜 갖다대도 잡힘
const ribbonMats = [];                // 리본별 재질 — 인덱스=리본번호
const ribbonBase = [];                // 리본 기본색(심각도 hex) — 호버/선택 해제 시 되돌릴 색
const ribsByHot = {};                 // 핫스팟(hot) → 그에 속한 리본 인덱스들(크랙 하이라이트용)
const RIBBON_HL = 0xffffff;           // 호버/선택 하이라이트 = 흰색
let hoverHot = -1;                     // 현재 호버 중인 크랙(핫스팟)

// 정점 좌표 + 정점별 반경 → 가변-반경 튜브 지오메트리(평행이송 프레임으로 링을 이어 붙임)
function variableTube(pts, radii, radialSeg){
  const N=pts.length, pos=[], idx=[];
  const tan=pts.map((p,i)=>new THREE.Vector3().subVectors(pts[Math.min(N-1,i+1)], pts[Math.max(0,i-1)]).normalize());
  let nrm=new THREE.Vector3(0,0,1);
  if(Math.abs(tan[0].dot(nrm))>0.9) nrm.set(0,1,0);
  nrm.crossVectors(tan[0], nrm).normalize();
  const bin=new THREE.Vector3(), v=new THREE.Vector3();
  for(let i=0;i<N;i++){
    nrm.addScaledVector(tan[i], -nrm.dot(tan[i])).normalize();   // T에 직교로 재정렬(평행이송)
    bin.crossVectors(tan[i], nrm).normalize();
    const r=radii[i];
    for(let j=0;j<radialSeg;j++){
      const a=j/radialSeg*Math.PI*2, cs=Math.cos(a)*r, sn=Math.sin(a)*r;
      v.copy(pts[i]).addScaledVector(nrm, cs).addScaledVector(bin, sn);
      pos.push(v.x, v.y, v.z);
    }
  }
  for(let i=0;i<N-1;i++) for(let j=0;j<radialSeg;j++){
    const a=i*radialSeg+j, b=i*radialSeg+(j+1)%radialSeg, c=(i+1)*radialSeg+j, d=(i+1)*radialSeg+(j+1)%radialSeg;
    idx.push(a,c,b, b,c,d);
  }
  const g=new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos,3));
  g.setIndex(idx); g.computeVertexNormals();
  return g;
}
function radiusOf(wmm){                            // 폭(mm) → 튜브 반경(m)
  if(ribbonTrueScale)                              // 실폭: 지름 = 측정 폭. 과장·최소굵기 없음
    return Math.min(RIBBON_MAX_R, Math.max(RIBBON_TRUE_MIN_R, (wmm==null?0:wmm)/1000/2));
  const r=(wmm==null?0:wmm)/1000/2*RIBBON_WIDTH_EXAG;
  return Math.min(RIBBON_MAX_R, Math.max(RIBBON_MIN_R, r));
}
// ---- 병합 렌더: 수만 개 리본을 메시 1개(드로우콜 1회)로 — 대규모 데이터 FPS 의 핵심 ----
// 리본마다 Mesh 를 만들면 1.7만 드로우콜 + 씬그래프 순회로 6fps 가 된다. 정점색으로 심각도를
// 굽고 전부 한 버퍼에 병합한다. 호버/선택 하이라이트는 그 크랙만 흰 오버레이 튜브로 따로 그린다.
// (유형 분류 폐지 — 유형별 버퍼 분리도 제거하고 병합 버퍼 하나만 쓴다.
//  구 .twin 의 type 필드는 무시된다. 오탐 억제는 검토 + 검출 모델 재학습이 담당.)
let ribGeo = {pos:[],col:[],idx:[],vbase:0};         // 전체 리본을 굽는 단일 병합 버퍼
const ribHot=[];                                     // ri → 핫스팟 인덱스(비교 모드 상태색용)
const ribVRange=[];                                  // ri → [{v0,v1}] 병합 버퍼 정점 범위(재도색용)
const ribRuns = [];                                  // ri → [{pts,rr}] 하이라이트 오버레이 재생성용
function tubeInto(G, pts, radii, radialSeg, color){  // variableTube 와 동일 기하를 병합 버퍼 G 에 축적
  const N=pts.length;
  const tan=pts.map((p,i)=>new THREE.Vector3().subVectors(pts[Math.min(N-1,i+1)], pts[Math.max(0,i-1)]).normalize());
  let nrm=new THREE.Vector3(0,0,1);
  if(Math.abs(tan[0].dot(nrm))>0.9) nrm.set(0,1,0);
  nrm.crossVectors(tan[0], nrm).normalize();
  const bin=new THREE.Vector3(), v=new THREE.Vector3();
  const b0=G.vbase;
  for(let i=0;i<N;i++){
    nrm.addScaledVector(tan[i], -nrm.dot(tan[i])).normalize();
    bin.crossVectors(tan[i], nrm).normalize();
    const r=radii[i];
    for(let j=0;j<radialSeg;j++){
      const a=j/radialSeg*Math.PI*2, cs=Math.cos(a)*r, sn=Math.sin(a)*r;
      v.copy(pts[i]).addScaledVector(nrm, cs).addScaledVector(bin, sn);
      G.pos.push(v.x, v.y, v.z); G.col.push(color.r, color.g, color.b, 1);   // RGBA — 알파로 필터
    }
  }
  for(let i=0;i<N-1;i++) for(let j=0;j<radialSeg;j++){
    const a=b0+i*radialSeg+j, b=b0+i*radialSeg+(j+1)%radialSeg,
          c=b0+(i+1)*radialSeg+j, d=b0+(i+1)*radialSeg+(j+1)%radialSeg;
    G.idx.push(a,c,b, b,c,d);
  }
  G.vbase += N*radialSeg;
}
// 리본 하나를 유형별 병합 버퍼에 굽는다(자체 메시를 만들지 않는다 — 드로우콜 절약).
// 중심선을 면 법선 방향으로 RIBBON_OFFSET_M 만큼 띄워 메시에 파묻히지 않게 하고,
// 폭 프로파일을 반경으로 바꿔 가변-반경 튜브로 만든다.
function buildRibbon(h, i){
  const poly=h.poly, pn=h.poly_n, pw=h.poly_w||[];
  if(!poly || poly.length<2) return;
  const rad=poly.map((p,k)=>radiusOf(pw[k]));
  const pts=poly.map((p,k)=>{
    const n=(pn&&pn[k])?pn[k]:[0,0,1];
    // 실폭 모드: 반경의 60%만 띄워 표면에 '얹힌' 듯 보이게 — 6cm 를 띄우면 mm 튜브가 공중에 뜬다.
    const off = ribbonTrueScale ? (rad[k]*0.6 + 0.0005) : RIBBON_OFFSET_M;
    return new THREE.Vector3(p[0]+n[0]*off, p[1]+n[1]*off, p[2]+n[2]*off);
  });
  const hot=(h.hot!=null?h.hot:-1);
  const G=ribGeo;                                    // 단일 병합 버퍼
  // 크랙 형상 색 = 심각도 — 정점색으로 병합 버퍼에 굽는다.
  const base=(hot>=0 ? hotSevColor(hot) : new THREE.Color(RIBBON_COLOR));
  ribbonBase[i]=base.getHex(); ribHot[i]=hot;
  if(hot>=0){ (ribsByHot[hot]=ribsByHot[hot]||[]).push(i); }        // 핫스팟→리본 역매핑
  let run=[], rr=[]; const hitPts=[]; const runs=[];
  const flush=()=>{
    if(run.length>=2){
      const _v0=G.vbase;
      tubeInto(G, run, rr, RIBBON_RADIAL, base);
      (ribVRange[i]=ribVRange[i]||[]).push({v0:_v0, v1:G.vbase});
      runs.push({pts:run, rr});                                      // 오버레이용 원본 경로 보존
      for(let j=1;j<run.length;j++){ const a=run[j-1],b=run[j]; hitPts.push(a.x,a.y,a.z,b.x,b.y,b.z); }  // 히트박스용 중심선 세그먼트
    }
    run=[]; rr=[];
  };
  for(let k=0;k<pts.length;k++){
    if(k>0 && pts[k].distanceTo(pts[k-1])>RIBBON_MAXGAP_M) flush();   // 유령 점프에서 끊기
    run.push(pts[k]); rr.push(rad[k]);
  }
  flush();
  ribRuns[i]=runs;
  if(hitPts.length){                                   // 보이는 튜브보다 큰 '히트박스'(투명 라인, 임계값으로 굵게 피킹)
    const hg=new THREE.BufferGeometry();
    hg.setAttribute('position', new THREE.Float32BufferAttribute(hitPts,3));
    const hl=new THREE.LineSegments(hg, HIT_MAT); hl.userData={ri:i, hot};  // 객체는 visible(피킹 가능), 재질만 안 그려짐
    hl.matrixAutoUpdate=false;                          // 정적 — 매 프레임 행렬 갱신 생략
    ribbonGroup.add(hl); ribbonHitLines.push(hl);
  }
}

const hud=document.getElementById('hud');
const hudMarkers=[];            // {el, h, i, sprite}
let selIdx=-1;                  // 선택된 크랙 인덱스
let hoverIdx=-1, hoverTag=-1;   // 레이캐스트로 짚은 레티클 / 포인터가 얹힌 태그
const hotGroup=new THREE.Group(); scene.add(hotGroup);   // 3D 레티클(스프라이트) 묶음

// 레티클 텍스처: 흰색으로 그려두고 스프라이트별 material.color 로 심각도 색을 입힌다.
// (CSS .reticle 의 회전 사각형 + 발광 테두리 + 중심 앵커점을 캔버스로 옮긴 것)
function makeReticleTex(){
  const S=256, c=document.createElement('canvas'); c.width=c.height=S;
  const x=c.getContext('2d');
  // 1) 바깥 발광 헤일로 — 어두운 구조물 위에서 '빛나는' 느낌을 만든다
  const g=x.createRadialGradient(S/2,S/2,0, S/2,S/2,S/2);
  g.addColorStop(0,'rgba(255,255,255,0.50)'); g.addColorStop(0.30,'rgba(255,255,255,0.20)');
  g.addColorStop(0.62,'rgba(255,255,255,0.05)'); g.addColorStop(1,'rgba(255,255,255,0)');
  x.fillStyle=g; x.fillRect(0,0,S,S);
  x.translate(S/2,S/2); x.rotate(Math.PI/4);
  const d=S*0.40;
  // 2) 다이아몬드 테두리 + 3) 중심 앵커점 — 각각 두 번 겹쳐 그려 진하게(가산합성 없이 채도 유지)
  x.shadowColor='#fff'; x.shadowBlur=S*0.14; x.strokeStyle='#fff'; x.lineWidth=S*0.055;
  x.strokeRect(-d/2,-d/2,d,d); x.strokeRect(-d/2,-d/2,d,d);
  x.shadowBlur=S*0.10; x.fillStyle='#fff';
  const i=d*0.44; x.fillRect(-i/2,-i/2,i,i); x.fillRect(-i/2,-i/2,i,i);
  const t=new THREE.CanvasTexture(c); t.needsUpdate=true; return t;
}
const reticleTex=makeReticleTex();
// 태그 내용: 폭·길이 (두 줄)
function cmpLine(i){                                // 비교 모드: 상태 + Δ폭 한 줄(html)
  if(!CMP || i==null || i<0) return '';
  const st=CMP.stByHot[i]; if(!st) return '';
  const d=CMP.dlByHot[i];
  let s='<span style="color:'+CMP_COLOR[st]+';font-weight:600">'+CMP_LABEL[st]+'</span>';
  if(d && d.dw!=null) s+=' Δ폭 '+(d.dw>0?'+':'')+cmm(d.dw).toFixed(2)+'mm';
  return '<br>'+s;
}
function fmtWL(h, i){                               // 말풍선(호버): 평균 폭 + 길이 + 등급·신뢰(최대·p95 는 클릭 시)
  const w=cmm(h.width_mm_median!=null?h.width_mm_median:(h.width_mm_max!=null?h.width_mm_max:h.width_mm));
  const gb=gradeBadge(h);
  return '평균 폭 '+fmtW(h, w!=null?Number(w):null, 2)+'mm'+fmtUnc(h)+'<br>'+fmtLen(h)
       + (gb?'<br>'+gb:'') + cmpLine(i);
}
// 크랙 정보는 '말풍선(mm 태그)'로만 — 다이아몬드 레티클은 제거(겹치고 클릭 혼란). 리본이 전체
// 지도를 담당하고, 크랙을 호버/선택하면 그 크랙의 말풍선(폭·길이)이 뜬다.
// 색 = 절대 심각도(폭 로그 등급). 예전의 상대 순위(rankFrac)는 보고서마다 색이 달라져 폐지.

// 리더선+말풍선 = DOM. 어차피 호버/선택된 1~2개만 보이므로 DOM 은 그때 처음 만든다(지연 생성).
// 수만 개 크랙에서 미리 다 만들면 DOM 노드 수만 개 생성·순회로 로드·프레임이 모두 무너진다.
DATA.hotspots.forEach((h,i)=>{ hudMarkers.push({el:null,h,i,sprite:null}); });
// 균열 i 의 말풍선 DOM 을 필요할 때 처음 만들어 캐시한다(호버/선택된 것만 생긴다).
// 만들면서 호버·선택 리스너와, 말풍선 위에서 시작한 드래그도 시점 회전이 되게 하는 처리를 함께 건다.
function ensureMarkerEl(i){
  const m=hudMarkers[i]; if(m.el) return m.el;
  const col=hotSevColor(i);                        // 절대 심각도 색(폭 로그 등급)
  const rgb='rgb('+Math.round(col.r*255)+','+Math.round(col.g*255)+','+Math.round(col.b*255)+')';
  const el=document.createElement('div'); el.className='hud-marker'; el.style.setProperty('--sev',rgb);
  el.innerHTML='<div class="leader"></div><div class="tag">'+fmtWL(m.h, m.i)+'</div>';
  el.addEventListener('mouseenter',()=>{ hoverTag=i; });
  el.addEventListener('mouseleave',()=>{ if(hoverTag===i) hoverTag=-1; });
  // 말풍선 위에서 시작하는 좌드래그도 시점 회전이 되도록 캔버스 드래그를 시작시킨다.
  // (mousemove/up 은 window 라 시작만 하면 태그 위에서도 계속된다. 짧은 탭이면 아래 click 이 선택)
  el.addEventListener('mousedown',ev=>{
    if(ev.button!==0) return;
    Object.assign(_drag, {down:true, look:false, sx:ev.clientX, sy:ev.clientY, lx:ev.clientX, ly:ev.clientY, t:performance.now()});
  });
  el.addEventListener('click',ev=>{ ev.stopPropagation(); selectHotspot(i); showHotspotRep(m.h); });
  hud.appendChild(el); m.el=el; return el;
}
// 리본: 병합과 분리해 '모든 2D 크랙 인스턴스'를 그대로 렌더 → 병합에서 형상이 사라지지 않는다.
// (구버전 .twin 은 ribbons 가 없으니 hotspots 의 poly 로 폴백)
const _ribs = DATA.ribbons || DATA.hotspots.filter(h=>h.poly);
// 단일 병합 메시(드로우콜 1). 필터는 정점 알파로 즉시 반영.
let ribMesh = null;
// 리본 전체를 (다시) 굽는다 — 부팅 1회 + 실폭/강조 토글 시.
// 반경·오프셋이 병합 버퍼에 구워져 있어 토글은 재생성이 불가피하다(초기 로드와 같은 비용).
function rebuildRibbons(reapply){
  if(ribMesh){ ribbonGroup.remove(ribMesh); ribMesh.geometry.dispose(); ribMesh=null; }   // 이전 세대 해제
  ribGeo={pos:[],col:[],idx:[],vbase:0};
  for(const hl of ribbonHitLines){ ribbonGroup.remove(hl); hl.geometry.dispose(); }
  ribbonHitLines.length=0;
  ribHot.length=0; ribVRange.length=0; ribRuns.length=0; ribbonBase.length=0;
  for(const k in ribsByHot) delete ribsByHot[k];
  _ribs.forEach((rb,ri)=>buildRibbon(rb, ri));
  if(ribGeo.pos.length){
    const g=new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(ribGeo.pos,3));
    g.setAttribute('color', new THREE.Float32BufferAttribute(ribGeo.col,4));   // itemSize 4 = 알파 필터용
    g.setIndex(ribGeo.idx);
    // alphaTest: 알파 0 인 정점(필터로 숨긴 크랙)은 프래그먼트를 버린다 — 반투명 정렬 문제 없이 on/off
    ribMesh=new THREE.Mesh(g, new THREE.MeshBasicMaterial({vertexColors:true, transparent:true, alphaTest:0.5,
      polygonOffset:true, polygonOffsetFactor:-4, polygonOffsetUnits:-4}));
    ribMesh.matrixAutoUpdate=false; ribbonGroup.add(ribMesh);
    ribGeo.pos=ribGeo.col=ribGeo.idx=null;            // 병합 후 원본 배열 해제(메모리)
  }
  if(reapply){                                        // 토글 경로: 필터·비교색·하이라이트 재적용
    while(hlGroup.children.length){ const c=hlGroup.children.pop(); if(c.geometry) c.geometry.dispose(); }
    _hlHots=new Set();
    applyFilters();
    if(cmpColorOn && CMP) repaintRibbons();
    refreshRibbonColor(-1);                           // 선택·호버 강조를 새 기하로 재생성
  }
}
rebuildRibbons(false);                                // 부팅: 필터 상태가 아직 없으므로 굽기만
// ===== 필터: 심각도 · 균열 신뢰도 (AND 로 적용) =====
// 병합 버퍼 하나라 메시 visible 로는 개별 크랙을 끌 수 없다 →
// 정점 알파(RGBA)를 0/1 로 바꿔 alphaTest 로 걸러낸다(지오메트리 재생성 없음, 드로우콜 불변).
const sevVisible={S1:true,S2:true,S3:true,S4:true,S5:true,S6:true};
const confVisible={0:true,1:true,2:true};
const _sevCount={S1:0,S2:0,S3:0,S4:0,S5:0,S6:0};
const _confCount={0:0,1:0,2:0};
// 심각도·신뢰도 구간별 개수를 다시 센다(칩에 표시되는 숫자).
// 캘리브레이션이 바뀌면 폭이 달라져 등급도 달라지므로 그때마다 다시 부른다.
function recountBands(){
  for(const k in _sevCount) _sevCount[k]=0;
  for(const k in _confCount) _confCount[k]=0;
  DATA.hotspots.forEach((h,i)=>{
    const s=hotSev(i); if(s) _sevCount[s]++;
    const c=confBand(h); if(c!=null) _confCount[c]++;
  });
}
recountBands();
function hotPasses(i){                               // 필터(심각도·신뢰도)를 모두 통과하는가
  const h=DATA.hotspots[i]; if(!h) return true;
  const s=hotSev(i); if(s && sevVisible[s]===false) return false;
  const c=confBand(h); if(c!=null && confVisible[c]===false) return false;
  return true;
}
// 세 필터를 화면에 반영한다. 지오메트리를 다시 만들지 않고 정점 알파만 0/1 로 바꾸므로
// 수만 개 크랙에서도 즉시 적용된다(드로우콜 불변).
function applyFilters(){
  // ① 리본: 정점 알파 갱신(리본별 정점 범위는 ribVRange 에 기록돼 있다)
  const at = ribMesh && ribMesh.geometry.getAttribute('color');
  if(at){
    for(let ri=0; ri<ribVRange.length; ri++){
      const ranges=ribVRange[ri]; if(!ranges) continue;
      const a=hotPasses(ribHot[ri]) ? 1 : 0;
      for(const rg of ranges) for(let v=rg.v0; v<rg.v1; v++) at.setW(v, a);
    }
    at.needsUpdate=true;
  }
  // ② 히트박스(피킹)도 같은 조건으로
  for(const hl of ribbonHitLines) hl.visible = hotPasses(hl.userData.hot);
  // ③ 걸러진 크랙의 호버/선택 해제
  if(selIdx>=0 && !hotPasses(selIdx)) deselectHotspot();
  if(hoverHot>=0 && !hotPasses(hoverHot)) setHoverHot(-1);
  updateHUD();
  if(typeof clRefresh==='function' && document.getElementById('cracklist').style.display==='flex') clRefresh();
}
// 상단 필터 바: 심각도 · 신뢰도 칩
const _sevChipEl={}, _confChipEl={};
// 칩들의 on/off 표시를 현재 필터 상태와 맞춘다('전체' 칩은 모두 켜졌을 때만 on).
function _syncChips(){
  for(const g of SEV_KEYS){ if(_sevChipEl[g]){ _sevChipEl[g].classList.toggle('on', sevVisible[g]); _sevChipEl[g].classList.toggle('off', !sevVisible[g]); } }
  for(let b=0;b<3;b++){ if(_confChipEl[b]){ _confChipEl[b].classList.toggle('on', confVisible[b]); _confChipEl[b].classList.toggle('off', !confVisible[b]); } }
  document.getElementById('tbAll').classList.toggle('on',
    SEV_KEYS.every(g=>sevVisible[g]) && [0,1,2].every(b=>confVisible[b]));
}
function _sep(box){                                  // 필터 바에 세로 구분선 하나 추가
  const d=document.createElement('span');
  d.style.cssText='border-left:1px solid var(--line-2);align-self:stretch;margin:7px 3px;';
  box.appendChild(d);
}
function _label(box, txt){                           // 필터 바에 그룹 이름표 하나 추가
  const l=document.createElement('span'); l.className='lb'; l.textContent=txt; box.appendChild(l);
}
(function buildFilterUI(){
  const box=document.getElementById('typeChips');
  _label(box, '심각도');
  for(const g of SEV_KEYS){
    const el=document.createElement('span'); el.className='tychip2 on';
    el.innerHTML='<span class="dot" style="background:'+SEV_HEX[g]+'"></span>'+SEV_NAME[g]
      +' <span class="cnt">'+_sevCount[g].toLocaleString()+'</span>';
    el.style.color=SEV_HEX[g]; el.title=SEV_NAME[g]+' — 폭 '+sevRange(SEV_KEYS.indexOf(g))+'mm';
    box.appendChild(el); _sevChipEl[g]=el;
    el.onclick=()=>{ sevVisible[g]=!sevVisible[g]; _syncChips(); applyFilters(); };
  }
  _sep(box); _label(box, '신뢰도');
  for(let b=2;b>=0;b--){
    const el=document.createElement('span'); el.className='tychip2 on';
    el.innerHTML='<span class="dot" style="background:'+CONF_HEX[b]+'"></span>'+CONF_NAME[b]
      +' <span class="cnt">'+_confCount[b].toLocaleString()+'</span>';
    el.style.color=CONF_HEX[b];
    box.appendChild(el); _confChipEl[b]=el;
    el.onclick=()=>{ confVisible[b]=!confVisible[b]; _syncChips(); applyFilters(); };
  }
  document.getElementById('tbAll').onclick=()=>{
    const anyOff=SEV_KEYS.some(g=>!sevVisible[g]) || [0,1,2].some(b=>!confVisible[b]);
    for(const g of SEV_KEYS) sevVisible[g]=anyOff;
    for(let b=0;b<3;b++) confVisible[b]=anyOff;
    _syncChips(); applyFilters();
  };
  _syncChips();
})();

// ===== 비교(자산) 모드: 검사 간 상태색 · 이전 검사 고스트 · Δ표시 =====
// 원칙(도구와 동일): 위치는 신원, 측정은 상태. 상태색은 compare_twins 의 판정을 그대로 그린다.
const CMP = (function(){
  if(!ASSET || !ASSET.cracks) return null;
  const insp = ASSET.inspections;
  const curId = insp[insp.length-1].id, prevId = insp[insp.length-2].id;
  const stByHot={}, dlByHot={}, histByHot={};        // 현재(.twin=DATA) 핫스팟 기준
  const missingPrev=[];                              // 이전 검사에만 있는(미검출) 이전핫 목록
  for(const c of ASSET.cracks){
    for(const bi of (c.matches[curId]||[])){
      stByHot[bi]=c.status; dlByHot[bi]=c.delta||null; histByHot[bi]=c.history||null;
    }
    if(c.status==='missing') missingPrev.push(...(c.matches[prevId]||[]));
  }
  return {curId, prevId, insp, stByHot, dlByHot, histByHot, missingPrev};
})();
const CMP_COLOR={new:'#e0574e', grown:'#ffb454', stable:'#64788a', shrunk:'#3fbf7f', missing:'#4a9fe0'};
const CMP_LABEL={new:'신규', grown:'성장', stable:'불변', shrunk:'감소?', missing:'미검출'};
const CMP_DIM=new THREE.Color('#232d36');
let cmpColorOn = !!CMP;                              // 자산 모드면 기본 상태색
const cmpEmph={new:true, grown:true, stable:true, shrunk:true};
function cmpStatus(hot){ return (CMP && CMP.stByHot[hot]) || 'stable'; }   // 비교 상태(기본 '불변')
// 리본 색을 통째로 다시 굽는다. 색 기준이 바뀔 때마다 부른다
// (비교 모드 on/off · 강조 토글 · 캘리브레이션으로 심각도가 달라졌을 때).
function repaintRibbons(){
  // 병합 버퍼의 정점색을 리본 단위로 다시 굽는다: 비교 모드=상태색(강조 해제 시 어둡게), 아니면 심각도색.
  const at = ribMesh && ribMesh.geometry.getAttribute('color');
  if(!at) return;
  const c=new THREE.Color();
  for(let ri=0; ri<ribVRange.length; ri++){
    const ranges=ribVRange[ri]; if(!ranges) continue;
    if(cmpColorOn && CMP){
      const st=cmpStatus(ribHot[ri]);
      c.copy(cmpEmph[st]===false ? CMP_DIM : new THREE.Color(CMP_COLOR[st]||CMP_COLOR.stable));
    } else c.setHex(ribbonBase[ri]);
    for(const rg of ranges)
      for(let v=rg.v0; v<rg.v1; v++){ at.setXYZW(v, c.r, c.g, c.b, at.getW(v)); }
  }
  at.needsUpdate=true;
}
// 이전 검사 고스트: 이전 meta 의 리본 중심선을 현재 좌표계로 변환해 라인으로 겹쳐 그린다.
// 매칭된 것=은은한 파랑(맥락) · 미검출=밝은 파랑(주목: '지난번엔 여기 있었다').
const ghostGroup=new THREE.Group(); scene.add(ghostGroup); ghostGroup.visible=false;
let ghostMissing=null, ghostMatched=null, _ghostLoaded=false;
// 이전 검사의 리본을 받아 현재 좌표계로 변환해 고스트 라인으로 얹는다(최초 1회만).
// 두 검사는 좌표계가 다르므로 자산에 기록된 정합 행렬로 이전→현재 변환 M 을 만들어 적용한다.
async function loadGhost(){
  if(_ghostLoaded || !CMP) return; _ghostLoaded=true;
  try{
    const prev = CMP.insp[CMP.insp.length-2];
    const cur  = CMP.insp[CMP.insp.length-1];
    const meta = await _fetch('/api/report/meta?p='+encodeURIComponent(prev.twin), 'json');
    const Tp=new THREE.Matrix4().fromArray([].concat(...prev.T_to_ref)).transpose();  // 행우선→Matrix4
    const Tc=new THREE.Matrix4().fromArray([].concat(...cur.T_to_ref)).transpose();
    const M=new THREE.Matrix4().copy(Tc).invert().multiply(Tp);   // 이전좌표 → 현재좌표
    // 이전 meta 는 절대좌표 그대로다(원점 이동은 현재 DATA 에만 적용됨) → 변환 뒤 원점을 빼
    // 현재 국소좌표계에 맞춘다. 변환에 이어붙이면 점마다 따로 뺄 필요가 없다.
    M.premultiply(new THREE.Matrix4().makeTranslation(
      -WORLD_ORIGIN[0], -WORLD_ORIGIN[1], -WORLD_ORIGIN[2]));
    const missSet=new Set(CMP.missingPrev);
    const segMiss=[], segMatch=[]; const v=new THREE.Vector3();
    const rbs = meta.ribbons || (meta.hotspots||[]).filter(h=>h.poly);
    rbs.forEach(rb=>{
      const arr=(missSet.has(rb.hot) ? segMiss : segMatch);
      const poly=rb.poly||[];
      let prevP=null;
      for(const p of poly){
        v.set(p[0],p[1],p[2]).applyMatrix4(M);
        if(prevP) arr.push(prevP.x,prevP.y,prevP.z, v.x,v.y,v.z);
        prevP=v.clone();
      }
    });
    const mk=(seg,color,op)=>{ if(!seg.length) return null;
      const g=new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.Float32BufferAttribute(seg,3));
      const l=new THREE.LineSegments(g, new THREE.LineBasicMaterial({color, transparent:true, opacity:op, depthWrite:false}));
      l.matrixAutoUpdate=false; ghostGroup.add(l); return l; };
    ghostMatched=mk(segMatch, 0x3a6a9a, 0.35);
    ghostMissing=mk(segMiss, 0x4a9fe0, 0.9);
  }catch(e){ console.warn('고스트 로드 실패:', e); }
}
// 상단 바 UI: 상태 칩(강조 토글) + 미검출(고스트) + 이전 검사 고스트 체크
(function buildCompareUI(){
  if(!CMP) return;
  const box=document.getElementById('typeChips');
  const sep=document.createElement('span');
  sep.style.cssText='border-left:1px solid var(--line-2);align-self:stretch;margin:6px 4px;';
  box.appendChild(sep);
  const lb=document.createElement('span'); lb.className='lb';
  lb.textContent=`비교 ${CMP.prevId}→${CMP.curId}`; box.appendChild(lb);
  const cnt={new:0,grown:0,stable:0,shrunk:0};
  DATA.hotspots.forEach((h,i)=>{ const s=cmpStatus(i); if(cnt[s]!=null) cnt[s]++; });
  for(const st of ['new','grown','stable','shrunk']){
    const el=document.createElement('span'); el.className='tychip2 on'; el.style.color=CMP_COLOR[st];
    el.innerHTML='<span class="dot" style="background:'+CMP_COLOR[st]+'"></span>'+CMP_LABEL[st]
      +' <span class="cnt">'+cnt[st].toLocaleString()+'</span>';
    el.onclick=()=>{ cmpEmph[st]=!cmpEmph[st]; el.classList.toggle('on',cmpEmph[st]); el.classList.toggle('off',!cmpEmph[st]); repaintRibbons(); };
    box.appendChild(el);
  }
  const miss=document.createElement('span'); miss.className='tychip2 off'; miss.style.color=CMP_COLOR.missing;
  miss.innerHTML='<span class="dot" style="background:'+CMP_COLOR.missing+'"></span>미검출 <span class="cnt">'
    +CMP.missingPrev.length.toLocaleString()+'</span>';
  miss.title='이전 검사에만 있던 균열(고스트로 표시) — 보수/미관측 여부는 검사원 판단';
  miss.onclick=async ()=>{ await loadGhost();
    const on=!ghostGroup.visible; ghostGroup.visible=on;
    if(ghostMatched) ghostMatched.visible=false;          // 칩 단독은 '미검출만'
    miss.classList.toggle('on',on); miss.classList.toggle('off',!on); };
  box.appendChild(miss);
  const gl=document.createElement('label'); gl.className='lb'; gl.style.cursor='pointer';
  gl.innerHTML='<input type="checkbox" id="ckGhost" style="accent-color:var(--accent);margin:0 4px 0 6px"> 이전 전체 고스트';
  box.appendChild(gl);
  gl.querySelector('input').onchange=async e=>{ await loadGhost();
    ghostGroup.visible=e.target.checked || ghostGroup.visible;
    if(ghostMatched) ghostMatched.visible=e.target.checked;
    if(!e.target.checked && !miss.classList.contains('on')) ghostGroup.visible=false; };
  repaintRibbons();                                       // 초기: 상태색
})();
// 호버/선택 하이라이트: 그 크랙의 리본만 흰 튜브로 위에 겹쳐 그린다(병합 버퍼는 불변).
const hlGroup=new THREE.Group(); ribbonGroup.add(hlGroup);
const HL_MAT=new THREE.MeshBasicMaterial({color:RIBBON_HL,
  polygonOffset:true, polygonOffsetFactor:-6, polygonOffsetUnits:-6});
// 균열 i 를 선택 상태로(이전 선택은 해제). 말풍선을 띄우고 리본을 흰색으로 강조한다.
function selectHotspot(i){
  if(selIdx>=0 && selIdx!==i) deselectHotspot();
  selIdx=i;
  if(i>=0){ ensureMarkerEl(i).classList.add('active','show'); refreshRibbonColor(i); }   // 선택 크랙 흰색
}
// 선택을 해제하고 색을 원래대로 되돌린다(호버 중이면 호버 강조는 남는다).
function deselectHotspot(){
  if(selIdx<0) return;
  const m=hudMarkers[selIdx];
  if(m.el){
    m.el.classList.remove('active');
    if(!m.el.classList.contains('high')) m.el.classList.remove('show');
  }
  const prev=selIdx; selIdx=-1; refreshRibbonColor(prev);   // 심각도색으로 복원(호버 중이면 유지)
}
// 호버/선택된 1~2개의 리더선+말풍선(DOM)만 크랙 위치(h.xyz)를 따라다닌다 — 활성분만 순회.
let _hudShown=new Set();
// 활성 말풍선(호버·선택·태그호버)만 3D 좌표를 화면 좌표로 투영해 위치를 갱신한다.
// 화면 밖으로 나간 것은 숨기고, 활성에서 빠진 것도 숨긴다 — 매 프레임 전체를 순회하지 않는다.
function updateHUD(){
  if(hud.style.display==='none') return;
  const W=innerWidth,H=innerHeight, v=new THREE.Vector3();
  const act=new Set();
  for(const i of [hoverIdx,selIdx,hoverTag]) if(i!=null && i>=0 && hotPasses(i)) act.add(i);
  for(const i of _hudShown) if(!act.has(i)){ const el=hudMarkers[i] && hudMarkers[i].el; if(el) el.style.display='none'; }
  for(const i of act){
    const m=hudMarkers[i]; if(!m) continue;
    const el=ensureMarkerEl(i);
    const x=m.h.xyz; v.set(x[0],x[1],x[2]).project(camera);
    if(v.z>1||v.x<-1.2||v.x>1.2||v.y<-1.2||v.y>1.2){ el.style.display='none'; continue; }
    el.style.display='block'; el.classList.add('show');
    el.style.left=((v.x*0.5+0.5)*W)+'px'; el.style.top=((-v.y*0.5+0.5)*H)+'px';
  }
  _hudShown=act;
}

// ================= 카메라 3D 객체 (밝은 파란색 프러스텀) =================
let modelDiag = 1;
const camGroup = new THREE.Group(); scene.add(camGroup);
const camMeshes = [];              // 클릭용 콘 메시
const camBySource = {};
let selectedCam = null;            // 선택된 카메라 그룹
const CAM_BLUE = 0x30c0ff, CAM_YELLOW = 0xffee00;
const allCams = DATA.cameras || [];
const camDataBySource = {}; allCams.forEach(c => camDataBySource[c.source] = c);

// 3D 점을 카메라 이미지 평면에 투영 → 정규화 픽셀(0~1). 화면 밖/뒤면 null.
// 렌즈 왜곡(Brown–Conrady) 순방향 적용: 정규화 (x,y) → 왜곡 (xd,yd).
// 3D 중심선은 왜곡 '보정된' 광선으로 만들어졌으므로, 원본(왜곡된) 사진에 되투영할 땐
// 왜곡을 다시 적용해야 정확히 얹힌다(실측: 핀홀만 쓰면 중앙값 33px, 적용 시 0.2px).
function distortXY(cam, x, y){
  const d=cam.dist; if(!d || !d.length) return [x, y];      // 구버전 .twin: 왜곡계수 없음 → 핀홀
  const k1=d[0]||0, k2=d[1]||0, p1=d[2]||0, p2=d[3]||0, k3=d[4]||0;
  const r2=x*x+y*y, rad=1 + k1*r2 + k2*r2*r2 + k3*r2*r2*r2;
  return [x*rad + 2*p1*x*y + p2*(r2+2*x*x),
          y*rad + p1*(r2+2*y*y) + 2*p2*x*y];
}
// 3D 점 → 그 카메라 사진의 정규화 좌표(u,v ∈ 0~1). 카메라 뒤이거나 화면 밖이면 null.
// 파이썬 쪽 pipeline.project_to_cam 과 같은 규약이어야 한다(왜곡 재적용 포함).
function projectToCam(cam, X){
  if(cam.fx === undefined) return null;
  const rx=X[0]-cam.pos[0], ry=X[1]-cam.pos[1], rz=X[2]-cam.pos[2];
  const Zc = rx*cam.fwd[0]+ry*cam.fwd[1]+rz*cam.fwd[2];
  if(Zc <= 1e-6) return null;
  const Xc = rx*cam.right[0]+ry*cam.right[1]+rz*cam.right[2];
  const Yc = -(rx*cam.up[0]+ry*cam.up[1]+rz*cam.up[2]);
  const [xd, yd] = distortXY(cam, Xc/Zc, Yc/Zc);
  const u = (cam.fx*xd + cam.cx)/cam.w;
  const v = (cam.fy*yd + cam.cy)/cam.h;
  if(u<0||u>1||v<0||v>1) return null;
  return {u, v};
}
// ---- 선택 크랙 하이라이트(2D): 크랙의 3D 중심선을 사진에 투영해 노란 발광 폴리라인으로 ----
// 마스크 PNG 는 모든 크랙이 한 장에 구워져 있어 크랙별 분리가 불가 → 기하(poly)로 그린다.
function projCamRaw(cam, X){                        // projectToCam 과 동일하되 화면 밖도 반환(선이 잘리지 않게)
  if(cam.fx === undefined) return null;
  const rx=X[0]-cam.pos[0], ry=X[1]-cam.pos[1], rz=X[2]-cam.pos[2];
  const Zc = rx*cam.fwd[0]+ry*cam.fwd[1]+rz*cam.fwd[2];
  if(Zc <= 1e-6) return null;
  const Xc = rx*cam.right[0]+ry*cam.right[1]+rz*cam.right[2];
  const Yc = -(rx*cam.up[0]+ry*cam.up[1]+rz*cam.up[2]);
  const [xd, yd] = distortXY(cam, Xc/Zc, Yc/Zc);    // 왜곡 적용(원본 사진 좌표계)
  return {u:(cam.fx*xd + cam.cx)/cam.w, v:(cam.fy*yd + cam.cy)/cam.h};
}
function crackPathsInCam(hot, cam){                 // hot 크랙의 중심선 → 이 카메라의 uv 폴리라인들
  const paths=[];
  for(const ri of (ribsByHot[hot]||[])){
    const poly=(_ribs[ri] && _ribs[ri].poly) || [];
    let cur=[], prev=null;
    for(const p of poly){
      const pr=projCamRaw(cam, p);
      const jump = (pr && prev) ? (Math.abs(pr.u-prev.u)+Math.abs(pr.v-prev.v)) > 0.2 : false;
      if(!pr || jump){ if(cur.length>1) paths.push(cur); cur=[]; }
      if(pr) cur.push(pr);
      prev=pr;
    }
    if(cur.length>1) paths.push(cur);
  }
  return paths;
}
function drawCrackPaths(ctx, paths, mapUV, lw){     // 노란 발광(가는 선 + 은은한 글로우)
  if(!paths.length) return;
  ctx.strokeStyle='#ffee00'; ctx.lineWidth=lw; ctx.lineCap='round'; ctx.lineJoin='round';
  ctx.shadowColor='#ffee00'; ctx.shadowBlur=lw*1.5;
  ctx.beginPath();
  for(const path of paths){
    let first=true;
    for(const q of path){ const xy=mapUV(q); if(!xy) continue;
      if(first){ ctx.moveTo(xy[0],xy[1]); first=false; } else ctx.lineTo(xy[0],xy[1]); }
  }
  ctx.stroke();
}
// ---- 투영만 믿지 않는다: 멤버 뷰 확인 + 검출 마스크 스냅 ----
// 문제(실측): ① 크랙이 안 보이는(가려진·반대편) 사진에도 투영선이 그려짐
// ② 다른 뷰에서 만든 융합 기하는 뷰 간 정합 오차로 50~500px 어긋나는 쌍이 존재(전수 감사).
// 해법: 그 크랙이 '검출된' 뷰에서만 그리고, 투영점을 그 사진의 마스크 최근접 크랙 픽셀로
// 스냅(반경 제한). 스냅 성공률이 낮으면 그 뷰에선 그리지 않는다(오도 방지).
const _maskData={};                                  // cam.id → {on,W,H} | null(없음/실패)
// 그 카메라의 검출 마스크를 비트맵({on,W,H})으로 만들어 cb 에 넘긴다(캐시·비동기).
// 마스크가 없거나 로드에 실패하면 null 을 넘겨 호출부가 스냅 없이 진행하게 한다.
function withMaskData(cam, cb){
  const key=cam.id;
  if(key in _maskData){ cb(_maskData[key]); return; }
  if(!cam.has_mask){ _maskData[key]=null; cb(null); return; }
  const im=new Image();
  im.onload=()=>{
    try{
      const c=document.createElement('canvas'); c.width=im.naturalWidth; c.height=im.naturalHeight;
      const x=c.getContext('2d'); x.drawImage(im,0,0);
      const a=x.getImageData(0,0,c.width,c.height).data;
      const W=c.width,H=c.height, on=new Uint8Array(W*H);
      for(let i=3,p=0;i<a.length;i+=4,p++) if(a[i]>0) on[p]=1;
      _maskData[key]={on,W,H};
    }catch(e){ _maskData[key]=null; }
    cb(_maskData[key]);
  };
  im.onerror=()=>{ _maskData[key]=null; cb(null); };
  im.src=imgUrl(cam,'mask');
}
// (x,y) 에서 가장 가까운 크랙 픽셀을 반경 R 안에서 정사각 링을 넓혀가며 찾는다(없으면 null).
function _nearestOn(md, x, y, R){
  const {on,W,H}=md;
  if(x>=0&&y>=0&&x<W&&y<H&&on[y*W+x]) return [x,y];
  for(let r=1;r<=R;r++){
    for(let dy=-r;dy<=r;dy++) for(let dx=-r;dx<=r;dx++){
      if(Math.max(Math.abs(dx),Math.abs(dy))!==r) continue;
      const nx=x+dx, ny=y+dy;
      if(nx>=0&&ny>=0&&nx<W&&ny<H&&on[ny*W+nx]) return [nx,ny];
    }
  }
  return null;
}
// 투영 폴리라인을 그 사진의 검출 마스크에 맞춰 정렬한다.
// 반환: 원본(이미 맞음) · 평행이동한 사본 · null(이 뷰에선 그리지 말 것).
function snapPaths(paths, md){
  // 정점별 독립 스냅은 근처의 다른 검출 덩어리로 흩어져 사각형·지그재그로 모양을 망가뜨린다.
  // → '강체 이동 정렬': 전체 정점의 최근접 오프셋 중앙값(dx,dy) 하나로만 평행이동해
  //   모양은 100% 보존하고 위치만 보정한다. 이미 정렬돼 있으면 원본 그대로 반환.
  const {W,H}=md, R=Math.max(10, Math.round(W*0.02));   // 탐색 반경 ≈ 폭의 2%
  const offs=[], dists=[]; let tot=0;
  for(const path of paths) for(const q of path){
    tot++;
    const x=Math.round(q.u*W), y=Math.round(q.v*H);
    const s=_nearestOn(md, x, y, R);
    if(s){ offs.push([s[0]-x, s[1]-y]); dists.push(Math.hypot(s[0]-x, s[1]-y)); }
  }
  if(offs.length<4 || offs.length/Math.max(tot,1)<0.35) return null;   // 이 뷰와 정합 불가
  dists.sort((a,b)=>a-b);
  if(dists[dists.length>>1] <= 4) return paths;         // 이미 크랙 위 → 모양 그대로
  const med=a=>{ const s=[...a].sort((x,y)=>x-y); return s[s.length>>1]; };
  const dx=med(offs.map(o=>o[0]))/W, dy=med(offs.map(o=>o[1]))/H;
  const shifted=paths.map(p=>p.map(q=>({u:q.u+dx, v:q.v+dy})));
  // 이동 후 검증: 절반 이상이 6px 내로 붙어야 표시(아니면 이 뷰에선 숨김)
  let ok=0, n=0;
  for(const path of shifted) for(const q of path){
    n++;
    if(_nearestOn(md, Math.round(q.u*W), Math.round(q.v*H), 6)) ok++;
  }
  return (n>0 && ok/n>=0.5) ? shifted : null;
}
function crackSeenIn(cam){                           // 이 카메라가 선택 크랙을 실제로 검출했는가
  if(selIdx<0) return false;
  const h=DATA.hotspots[selIdx];
  return (h.views||[]).some(v=>v.source===cam.source);
}
function drawCrackHL(cam, ctx, mapUV, lw){           // 하이라이트 공통 진입점(비동기: 마스크 로드 후)
  if(selIdx<0 || !crackSeenIn(cam)) return;          // 멤버 뷰가 아니면 그리지 않음(가림·오도 방지)
  const hot=selIdx, paths=crackPathsInCam(hot, cam);
  if(!paths.length) return;
  withMaskData(cam, md=>{
    if(selIdx!==hot) return;                         // 그 사이 선택이 바뀌면 무시
    if(md){ const sp=snapPaths(paths, md); if(sp===null) return; drawCrackPaths(ctx, sp, mapUV, lw); }
    else drawCrackPaths(ctx, paths, mapUV, lw);      // 마스크 로드 실패 시 원시 투영 폴백
  });
}
// 핫스팟의 대표 뷰: 크랙 '중심선을 가장 많이 보는' 멤버 뷰(커버리지 최대).
// 예전엔 중심점(h.xyz) 하나가 보이는 첫 뷰를 골랐는데, 긴 크랙은 각 멤버 뷰가 끝부분만
// 보므로 중심점이 어느 화면에도 안 들어가 폴백(화면 정중앙 크롭)으로 떨어졌다 —
// "대표 뷰를 못 찾는" 증상. 커버리지 방식은 부분만 보여도 가장 잘 보는 뷰를 항상 찾는다.
function pickRepView(h){
  const hot = DATA.hotspots.indexOf(h);
  let P = [];                                        // 크랙 전체 기하(융합 리본) — 없으면 중심점 하나
  for(const ri of (ribsByHot[hot]||[])) for(const p of (_ribs[ri].poly||[])) P.push(p);
  if(!P.length) P = [h.xyz];
  const step = Math.max(1, Math.floor(P.length/64)); // ≤64점 서브샘플이면 순위 판단에 충분
  const Ps = []; for(let i=0;i<P.length;i+=step) Ps.push(P[i]);
  let best = null;
  for(const vw of (h.views||[])){
    const cam = camDataBySource[vw.source]; if(!cam) continue;
    if(!best) best = {cam, u:0.5, v:0.5, frac:0};    // 최후 폴백: 아무 데도 안 보여도 뷰는 보여준다
    const uvs = [];
    for(const p of Ps){ const pr = projectToCam(cam, p); if(pr) uvs.push(pr); }
    if(!uvs.length) continue;
    const frac = uvs.length/Ps.length;
    if(frac <= best.frac) continue;                  // 동률이면 앞선 뷰(신뢰도순) 유지
    // 크롭 중심 = '이 뷰에 보이는 구간'의 중앙값 — 전체 중심이 화면 밖이어도 크롭이 크랙 위에 앉는다.
    const us = uvs.map(q=>q.u).sort((a,b)=>a-b), vs = uvs.map(q=>q.v).sort((a,b)=>a-b);
    const span = Math.max(us[us.length-1]-us[0], vs[vs.length-1]-vs[0]);
    best = {cam, u:us[us.length>>1], v:vs[vs.length>>1], frac,
            crop: Math.min(0.9, Math.max(0.32, span*1.3))};   // 보이는 구간이 크면 크롭도 넓게
  }
  return best;
}

const CAM_SIZE_M = 0.30;    // 카메라 글리프 실측 크기(프러스텀 깊이 ≈ 30cm). 옛 방식(대각선 비율)은
                            // 댐 622m 에서 콘 높이 13.7m 로 거대해졌다. 절대 크기라 모델과 무관.
function buildCameras(diag){
  camGroup.clear(); camMeshes.length = 0;
  const L = CAM_SIZE_M, coneR = CAM_SIZE_M*0.20, coneH = CAM_SIZE_M*0.44;   // 옛 비율(5:1, 5:2.2) 유지
  allCams.forEach(c=>{
    const P=new THREE.Vector3(c.pos[0],c.pos[1],c.pos[2]);
    const f=new THREE.Vector3(c.fwd[0],c.fwd[1],c.fwd[2]).normalize();
    const up=new THREE.Vector3(c.up[0],c.up[1],c.up[2]).normalize();
    const rt=new THREE.Vector3(c.right[0],c.right[1],c.right[2]).normalize();
    const g=new THREE.Group(); g.userData={cam:c};
    // 카메라 몸체 콘(클릭 대상) — f 방향을 가리킴
    const coneMat=new THREE.MeshBasicMaterial({color:CAM_BLUE});
    const cone=new THREE.Mesh(new THREE.ConeGeometry(coneR,coneH,14),coneMat);
    cone.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0), f);
    cone.position.copy(P).addScaledVector(f, coneH*0.5);
    cone.userData={cam:c}; g.add(cone); camMeshes.push(cone);
    // 시야 프러스텀 라인
    const hh=L*Math.tan(c.fov*Math.PI/180/2), hw=hh*c.aspect;
    const fc=P.clone().addScaledVector(f,L);
    const c1=fc.clone().addScaledVector(rt, hw).addScaledVector(up, hh);
    const c2=fc.clone().addScaledVector(rt,-hw).addScaledVector(up, hh);
    const c3=fc.clone().addScaledVector(rt,-hw).addScaledVector(up,-hh);
    const c4=fc.clone().addScaledVector(rt, hw).addScaledVector(up,-hh);
    const pts=[P,c1, P,c2, P,c3, P,c4, c1,c2, c2,c3, c3,c4, c4,c1];
    const lmat=new THREE.LineBasicMaterial({color:CAM_BLUE});
    g.add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(pts), lmat));
    g.userData.materials=[coneMat,lmat];
    camGroup.add(g); camBySource[c.source]=g; c._group=g;
  });
}
function setCamColor(g,color){ if(g&&g.userData.materials) g.userData.materials.forEach(m=>m.color.setHex(color)); }  // 콘+프러스텀 색 동시 변경

// ================= 카메라 선택/강조/정보 =================
function selectCamera(c, fly){
  const g = c._group || camBySource[c.source];
  if(selectedCam && selectedCam!==g) setCamColor(selectedCam, CAM_BLUE);
  selectedCam = g; setCamColor(g, CAM_YELLOW);
  document.getElementById('caminfo').style.display='block';
  document.getElementById('camName').textContent = c.source;
  const _cw = wpt(c.pos);                          // 국소좌표 → 보고서 원본 좌표로 되돌려 표시
  document.getElementById('camCoord').innerHTML =
    'X : '+_cw[0].toFixed(3)+'<br>Y : '+_cw[1].toFixed(3)+'<br>Z : '+_cw[2].toFixed(3);
  document.querySelectorAll('.imgItem').forEach(el=>el.classList.toggle('active', el.dataset.src===c.source));
  if(fly) flyToCamera(c);
}

// ================= 카메라 시점으로 빠르게 이동 =================
// 위치 + 시선(yaw/pitch) + 화각(fov)만 부드럽게 맞춘다. 롤(up)·레터박스는 쓰지 않는다 →
// 카메라를 선택해도 up 은 계속 월드 Z 이고, pitch 는 광축 f 에서 일관 부호로 계산해 상하 반전이 없다.
let anim=null;
function lerpAngle(a, b, t){                        // yaw 최단경로 보간(±π 감싸기)
  let d = (b - a) % (Math.PI*2);
  if(d >  Math.PI) d -= Math.PI*2;
  if(d < -Math.PI) d += Math.PI*2;
  return a + d*t;
}
// 그 사진을 찍은 위치·시선으로 0.7초에 걸쳐 이동하는 애니메이션을 예약한다(실제 보간은 tickAnim).
function flyToCamera(c){
  const P=new THREE.Vector3(c.pos[0],c.pos[1],c.pos[2]);
  const f=new THREE.Vector3(c.fwd[0],c.fwd[1],c.fwd[2]).normalize();
  const yaw1   = Math.atan2(f.y, f.x);
  const pitch1 = clampPitch(Math.atan2(f.z, Math.hypot(f.x, f.y)));   // lookToward 와 동일 공식
  anim={t:0, dur:0.7, p0:camera.position.clone(), p1:P.clone(),
        yaw0:camYaw, yaw1, pitch0:camPitch, pitch1};                  // fov 는 스코프가 따로 관리
}
// 매 프레임 비행 애니메이션을 한 스텝 진행한다(easeInOutQuad, 끝나면 anim 해제).
function tickAnim(dt){
  if(!anim) return;
  anim.t=Math.min(1, anim.t + dt/anim.dur);
  const e = anim.t<0.5 ? 2*anim.t*anim.t : 1-Math.pow(-2*anim.t+2,2)/2;  // easeInOutQuad
  camera.position.lerpVectors(anim.p0,anim.p1,e);
  camYaw   = lerpAngle(anim.yaw0, anim.yaw1, e);
  camPitch = anim.pitch0 + (anim.pitch1-anim.pitch0)*e;
  if(anim.t>=1) anim=null;
}
function cancelAnim(){ anim=null; }               // 사용자가 조작을 시작하면 진행 중 비행 취소


// ================= 우측 전체 이미지 리스트 =================
// 썸네일은 70×50 에 object-fit:cover 라 원본이 잘려 들어간다. 정규화 좌표(u,v)를 그대로
// 70×50 에 곱하면 어긋나므로 cover 의 스케일·크롭 오프셋을 그대로 재현해 얹는다.
const TW = 70, TH = 50;
// 정규화 좌표(u,v) → 썸네일 안의 픽셀 위치. cover 크롭으로 잘려나간 영역이면 null(점을 찍지 않는다).
function uvToThumbPx(cam, uv){
  const a = cam.w / cam.h, b = TW / TH;
  const dw = (a > b) ? TH * a : TW;          // cover: 더 큰 쪽에 맞춰 채우고 나머지를 자른다
  const dh = (a > b) ? TH : TW / a;
  const x = (TW - dw) / 2 + uv.u * dw;
  const y = (TH - dh) / 2 + uv.v * dh;
  return (x < 0 || y < 0 || x > TW || y > TH) ? null : {x, y};   // 잘려나간 영역이면 표시 안 함
}
// 카메라 콘 클릭 → 우측 패널에 그 카메라의 이미지 한 장을 크게 표시(크랙 하이라이트 없음)
function showCameraImage(c){
  document.getElementById('imgTitle').textContent='카메라 이미지';
  document.getElementById('imgReset').style.display='inline-block';
  document.getElementById('imgHot').innerHTML='📷 '+c.source+
    '<br><span style="color:#888">클릭: 이 카메라 시점으로 이동 · 우클릭: 원본 크게 보기</span>';
  const items=document.getElementById('imgItems'); items.innerHTML='';
  const box=document.createElement('div');
  box.style.cssText='position:relative;width:100%;cursor:pointer;';
  const im=new Image(); im.style.cssText='width:100%;border-radius:8px;display:block;';
  im.src=imgUrl(c,'thumb');
  box.appendChild(im);
  box.title='클릭: 이 카메라 시점으로 이동 · 우클릭: 원본 크게 보기';
  box.onclick=()=>selectCamera(c, true);              // 시점 이동 + 카메라 정보 갱신
  box.oncontextmenu=e=>{ e.preventDefault(); openImageModal(c, null, null, true); };   // noHL
  items.appendChild(box);
}
// 우측 이미지 패널을 다시 그린다. 셋 중 한 모드로 쓰인다:
//   전체(인자 없음) · 핫스팟(그 균열을 검출한 뷰들) · 지점 검사(info + uvmap).
// uvmap 이 있으면 썸네일 위에 해당 지점을 노란 점으로 찍는다.
function renderImageList(cams, hotspot, uvmap, info){
  const items=document.getElementById('imgItems'); items.innerHTML='';
  document.getElementById('imgTitle').textContent = info ? '지점 연결 이미지' : (hotspot ? '핫스팟 이미지' : '전체 이미지');
  document.getElementById('imgReset').style.display = (hotspot || info) ? 'inline-block' : 'none';
  document.getElementById('imgHot').innerHTML = info ? info : (hotspot
    ? ('▶ "'+fmtHotText(hotspot)+'" · 신뢰도 '+(hotspot.conf*100).toFixed(0)+'% · '+cams.length+'장<br>'+
       fmtXYZ(hotspot.xyz))
    : (allCams.length+'장 · 이미지를 클릭하면 촬영 카메라 시점으로 이동합니다.'));
  cams.forEach(c=>{
    const el=document.createElement('div'); el.className='imgItem'; el.dataset.src=c.source;
    if(selectedCam && camBySource[c.source]===selectedCam) el.classList.add('active');
    const uv = uvmap && uvmap[c.source];
    const px = uv ? uvToThumbPx(c, uv) : null;
    el.innerHTML='<div class="tw"><img loading="lazy" src="'+imgUrl(c,'thumb')+'">'
      + (c.has_mask ? '<img class="mk" loading="lazy" src="'+imgUrl(c,'mask')+'">' : '')
      + (px ? '<div class="uvdot" style="left:'+px.x.toFixed(1)+'px;top:'+px.y.toFixed(1)+'px"></div>' : '')
      + '</div><span class="nm">'+c.source+'</span>';
    el.title = uv ? '클릭: 카메라 시점으로 이동 · 우클릭: 원본에서 이 지점(노란 원) 크게 보기'
                  : '클릭: 카메라 시점으로 이동 · 우클릭: 원본 크게 보기';
    el.onclick=()=>selectCamera(c, true);
    el.oncontextmenu=(ev)=>{ ev.preventDefault(); openImageModal(c, uv || null, uv ? 'inspect' : null); };
    items.appendChild(el);
  });
}

// ================= 지점 검사 모드 =================
// 임의의 표면 지점을 클릭 → 그 점이 찍힌 사진들을 찾아 목록에 띄우고, 각 썸네일에 점 위치를 표시.
// 핫스팟과 달리 '실제로 검출된 뷰' 목록이 없으므로 직접 골라내야 한다:
//   ① projectToCam 으로 화면 안에 들어오는지(프러스텀)
//   ② 표면 법선으로 카메라가 앞쪽에 있는지 — 이게 없으면 벽 반대편에서 찍은 사진까지 딸려온다
// ②는 '자기 가림'(앞을 막는 구조물)까지는 못 거른다. 정확히 하려면 카메라마다 메시 레이캐스트가
// 필요한데 브라우저에선 1회 196ms(실측)라 594대면 116초 → 불가능. 필요해지면 서버(embreex)로.
let inspectMode=false;
// 지점 검사 모드를 켜고 끈다(측정 모드와 배타 — 클릭 하나에 두 동작이 걸리면 안 된다).
function setInspect(on){
  inspectMode = !!on;
  if(inspectMode && measureMode) setMode(measureMode);        // 측정 모드와 배타
  document.getElementById('btnInspect').classList.toggle('active', inspectMode);
  renderer.domElement.style.cursor = inspectMode ? 'crosshair' : 'default';
  document.getElementById('inspectMsg').textContent = inspectMode
    ? '구조물의 한 점을 클릭하세요 → 그 점이 찍힌 사진들이 뜹니다.'
    : '끄면 일반 조작으로 돌아갑니다.';
  if(!inspectMode){
    if(worldMarker) worldMarker.visible=false;
    renderImageList(allCams, null);
  }
}
// 클릭한 표면 점을 찍은 사진들을 골라 우측에 띄운다(가까운 순).
// 프러스텀 안 + 표면 법선 앞쪽인 카메라만 남긴다 — 법선 검사가 없으면 벽 반대편 사진까지 딸려온다.
function inspectAt(e){
  const hit = pickMesh(e);
  if(!hit){ document.getElementById('inspectMsg').textContent='구조물 표면을 클릭하세요.'; return; }
  const p = hit.point, n = hit.normal;
  placeWorldMarker(p);
  const P=[p.x,p.y,p.z], uvmap={}, seen=[];
  let nFrustum=0;
  for(const c of allCams){
    const uv = projectToCam(c, P);
    if(!uv) continue;
    nFrustum++;
    if(n){                                                   // 카메라가 표면 뒤쪽이면 제외
      const dx=c.pos[0]-p.x, dy=c.pos[1]-p.y, dz=c.pos[2]-p.z;
      if(n.x*dx + n.y*dy + n.z*dz <= 0) continue;
    }
    uvmap[c.source]=uv;
    seen.push({c, d: Math.hypot(c.pos[0]-p.x, c.pos[1]-p.y, c.pos[2]-p.z)});
  }
  seen.sort((a,b)=>a.d-b.d);                                 // 가까운 사진부터
  const info = '▶ 이 지점이 찍힌 사진 '+seen.length+'장 (전체 '+allCams.length+'대 중)<br>'
    + '<span style="color:#888">X '+p.x.toFixed(2)+'  Y '+p.y.toFixed(2)+'  Z '+p.z.toFixed(2)
    + ' · 노란 원 = 이 지점 · 법선 뒤 '+(nFrustum-seen.length)+'대 제외</span>';
  renderImageList(seen.map(s=>s.c), null, uvmap, info);
  document.getElementById('inspectMsg').textContent =
    seen.length ? '다른 지점을 클릭하면 갱신됩니다.' : '이 지점을 찍은 사진이 없습니다.';
}

// 크랙 위치(u,v 정규화)를 중심으로 확대한 크롭 썸네일. 클릭 → 원본 모달.
function makeCrop(container, cam, u, v, cropFrac){
  container.innerHTML='';
  const box=document.createElement('div');
  box.style.cssText='position:relative;width:100%;height:210px;overflow:hidden;border-radius:8px;background:#000;cursor:pointer;';
  const im=new Image(); im.style.cssText='position:absolute;left:0;top:0;max-width:none;';
  // 검출 마스크를 원본과 똑같이 정렬해 겹쳐 → 대표 뷰에서 크랙이 보이게(마스크는 원본과 동일 해상도)
  const mask = cam.has_mask ? new Image() : null;
  if(mask) mask.style.cssText='position:absolute;left:0;top:0;max-width:none;pointer-events:none;';
  const hlv=document.createElement('canvas');       // 선택 크랙 노란 하이라이트 오버레이
  hlv.style.cssText='position:absolute;left:0;top:0;pointer-events:none;';
  // 크롭 배율·위치를 계산해 원본·마스크·하이라이트를 겹쳐 배치한다(이미지 로드/리사이즈 때마다).
  function layout(){
    const CW=box.clientWidth||280, CH=box.clientHeight||210;
    const s=CW/(im.naturalWidth*cropFrac);
    const w=im.naturalWidth*s, hh=im.naturalHeight*s;
    // 크랙을 중앙에 두되, 가장자리 크랙에서 이미지가 박스 밖으로 밀려 검은 배경이 보이지 않게 클램프
    let L=(CW/2 - u*w), T=(CH/2 - v*hh);
    L = (w<=CW) ? (CW-w)/2 : Math.min(0, Math.max(CW-w, L));
    T = (hh<=CH) ? (CH-hh)/2 : Math.min(0, Math.max(CH-hh, T));
    im.style.width=w+'px'; im.style.height=hh+'px'; im.style.left=L+'px'; im.style.top=T+'px';
    if(mask){ mask.style.width=w+'px'; mask.style.height=hh+'px'; mask.style.left=L+'px'; mask.style.top=T+'px'; }
    hlv.width=CW; hlv.height=CH;                    // 선택된 크랙만 노란 발광(멤버 뷰 한정 + 마스크 스냅)
    drawCrackHL(cam, hlv.getContext('2d'), q=>[L+q.u*w, T+q.v*hh], 1.6);
  }
  im.onload=layout;
  im.src=imgUrl(cam,'full');
  box.appendChild(im);
  if(mask){ mask.onload=layout; mask.src=imgUrl(cam,'mask'); box.appendChild(mask); }
  box.appendChild(hlv);
  box.title='좌클릭: 카메라 시점으로 이동 · 우클릭: 자세히 보기(원본)';
  box.onclick=()=>selectCamera(cam, true);                                   // 좌클릭 → 시점 이동
  box.oncontextmenu=(e)=>{ e.preventDefault(); openImageModal(cam, {u:u, v:v}); };  // 우클릭 → 자세히
  container.appendChild(box);
}

// 핫스팟 클릭 → 대표 크랙 이미지를 크게(확대) 보여준다.
// ---- 검토 UI: 상세 패널의 확인/오탐/메모 행 ----
// 오탐 라벨은 검출(세그멘테이션) 모델 재학습의 하드 네거티브가 된다 — 검토가 곧 학습 데이터.
function buildReviewRow(i){
  if(i==null || i<0) return '';
  const rv=revOf(i); const st=rv&&rv.state;
  const badge = st
    ? '<span style="color:'+REV_COLOR[st]+';font-weight:600">'+REV_LABEL[st]+'</span>'
      +(rv.at?' <span style="color:var(--tx-3)">'+rv.at.replace('T',' ')+'</span>':'')
    : '<span style="color:var(--tx-3)">미검토</span>';
  return '<div id="revRow" style="margin-top:9px;padding:8px 10px;background:var(--surface-2);'
    +'border:1px solid var(--line);border-radius:var(--r-sm);">'
    +'<span style="color:var(--tx-3)">검사원 검토</span> · '+badge
    +'<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:7px;">'
    +'<button id="revOk" class="revbtn" style="color:#3fbf7f;border-color:#3fbf7f">✓ 확인</button>'
    +'<button id="revNo" class="revbtn" style="color:#e0574e;border-color:#e0574e">✗ 오탐</button>'
    +(st?'<button id="revClear" class="revbtn">해제</button>':'')
    +'</div>'
    +'<input id="revNote" placeholder="메모 (Enter 로 저장)" value="'+String((rv&&rv.note)||'').replace(/"/g,'&quot;')+'"'
    +' style="margin-top:7px;width:100%;background:var(--surface-3);color:var(--tx);border:1px solid var(--line-2);'
    +'border-radius:4px;padding:5px 8px;font-size:var(--fs-cap);font-family:inherit;box-sizing:border-box;">'
    +'</div>';
}
// 상단 '검토 n/전체' 진행 표시를 갱신한다.
function updateRevProg(){
  const el=document.getElementById('revProg'); if(!el) return;
  const n=Object.keys(REVIEW.reviews||{}).length;
  el.textContent='검토 '+n.toLocaleString()+'/'+DATA.hotspots.length.toLocaleString();
}
// 검토를 저장한 뒤 화면 세 곳(상세 패널·진행 표시·크랙 목록)을 한꺼번에 되그린다.
function _revRefresh(i, h){
  showHotspotRep(h); updateRevProg();
  if(typeof clRefresh==='function' && document.getElementById('cracklist').style.display==='flex') clRefresh();
}
// 방금 그린 검토 행(확인·오탐·해제·메모)에 핸들러를 건다.
function wireReviewRow(i, h){
  if(i==null || i<0) return;
  const g=id=>document.getElementById(id);
  if(g('revOk'))    g('revOk').onclick    = async()=>{ await saveReview(i,{state:'confirmed'}); _revRefresh(i,h); };
  if(g('revNo'))    g('revNo').onclick    = async()=>{ await saveReview(i,{state:'rejected'}); _revRefresh(i,h); };
  if(g('revClear')) g('revClear').onclick = async()=>{ await saveReview(i,{state:'clear'}); _revRefresh(i,h); };
  if(g('revNote'))  g('revNote').onkeydown = async e=>{
    if(e.key!=='Enter') return;
    const keep=(revOf(i)&&revOf(i).state)||'edited';
    await saveReview(i,{note:e.target.value, state:keep}); _revRefresh(i,h);
  };
}
// 균열 상세를 우측 패널에 띄운다 — 폭(중앙/p95/최대/3D)·길이·심각도·신뢰도·비교 이력,
// 검토 행, 그리고 가장 잘 보이는 뷰의 확대 크롭.
function showHotspotRep(h){
  document.getElementById('imgTitle').textContent='대표 크랙 이미지';
  document.getElementById('imgReset').style.display='inline-block';
  // 클릭 시엔 평균 폭 + p95·최대(상세) + 크랙 총 길이 모두 표시 (캘리브레이션·측정바닥 반영)
  const f=(x,d=1)=>fmtW(h, x!=null?Number(cmm(x)):null, d);
  const _hi = DATA.hotspots.indexOf(h);
  let _cmp='';
  if(CMP && _hi>=0 && CMP.histByHot[_hi]){            // 검사 이력: 전체 시점 체인(T1 → … → TN)
    const H=CMP.histByHot[_hi];
    const parts=CMP.insp.map(x=>x.id).filter(id=>H[id])
      .map(id=>`${id} 폭 ${cmm(H[id].w).toFixed(2)}·길이 ${Math.round(cmm(H[id].L))}`);
    if(parts.length) _cmp = ' · 이력 ' + parts.join(' → ');
  }
  let _gr='';
  { const sv=sevGrade(sevWidth(h)), cb=confBadge(h);
    if(sv) _gr=' · <span style="color:'+SEV_HEX[sv]+';font-weight:600">'+SEV_NAME[sv]+'</span>'+(cb?' · '+cb:'');
    else if(cb) _gr=' · '+cb; }
  // 측정 바닥 안내: 표기가 "≤"(상한값)인 이유를 상세 패널에서 한 줄로 설명
  let _fl='';
  { const fl=wFloor(h), wm=cmm(h.width_mm_median!=null?h.width_mm_median:h.width_mm_max);
    if(fl!=null && wm!=null && wm<fl)
      _fl=' · <span style="color:#ffb84d" title="폭이 이 뷰의 픽셀 분해능(2×GSD) 미만이라 실폭을 분해하지 못했습니다 — 표기값은 상한">분해능 한계(GSD '+cmm(h.gsd_mm).toFixed(2)+'mm/px)</span>'; }
  // 폭 방식 불일치: 3D 실측과 스케일맵이 30% 이상 다르면 착지·스케일 문제 신호
  if(h.width_flag && h.width_mm_median_alt!=null)
    _fl+=' · <span style="color:#ffb84d" title="'+(h.width_src==='3d'?'1차: 3D 엣지 실측 · 대조: 스케일맵':'1차: 스케일맵(3D 표본 부족) · 대조: 3D 실측')
      +' — 두 방식이 30% 이상 다릅니다. 광선 착지나 국소 스케일 문제일 수 있으니 참고값과 함께 판단하세요">폭 방식 불일치(대조 '
      +cmm(h.width_mm_median_alt).toFixed(2)+'mm)</span>';
  const wtxt = ((h.width_mm_median!=null)
    ? `크랙 평균 폭 ${f(h.width_mm_median)}mm${fmtUnc(h)} (p95 ${f(h.width_mm_p95)} · 최대 ${f(h.width_mm_max)})`
      + (h.width_mm_3d!=null && h.width_src!=='3d' ? ` · 3D폭 ${f(h.width_mm_3d)}` : '')   // 1차가 3D 면 중복 생략
      + ` · ${fmtLen(h)}`
    : (h.text||'크랙')) + _gr + _fl + cmpLine(_hi) + _cmp;
  document.getElementById('imgHot').innerHTML =
    '▶ '+wtxt+'<br>'+fmtXYZ(h.xyz)
    + buildReviewRow(_hi);
  wireReviewRow(_hi, h);
  const items=document.getElementById('imgItems'); items.innerHTML='';
  const rep=pickRepView(h);
  if(!rep){ items.innerHTML='<div style="color:#888;padding:8px">대표 이미지를 찾지 못했습니다.</div>'; return; }
  const crop=document.createElement('div'); items.appendChild(crop);
  makeCrop(crop, rep.cam, rep.u, rep.v, rep.crop || 0.32);
  const lbl=document.createElement('div');
  lbl.style.cssText='font-size:11px;color:#9cf;margin:7px 0;';
  // 부분만 보이는 긴 크랙이면 몇 % 구간인지 밝힌다(다른 뷰에서 나머지를 볼 수 있다는 힌트)
  const covTxt = (rep.frac!=null && rep.frac>0 && rep.frac<0.995) ? ' · 크랙의 '+Math.round(rep.frac*100)+'% 구간' : '';
  lbl.textContent='대표 뷰: '+rep.cam.source+covTxt+'  (좌클릭: 시점 이동 · 우클릭: 자세히)';
  items.appendChild(lbl);
  const others=(h.views||[]).filter(v=>!rep.cam || v.source!==rep.cam.source);
  if(others.length){
    const t=document.createElement('div');
    t.style.cssText='font-size:11px;color:#888;margin-top:8px;';
    t.textContent='다른 뷰 '+others.length+'장 (좌클릭: 시점 이동 · 우클릭: 자세히)';
    items.appendChild(t);
    const row=document.createElement('div');
    row.style.cssText='display:flex;gap:6px;flex-wrap:wrap;margin-top:5px;';
    others.forEach(vw=>{
      const cam=camDataBySource[vw.source]; if(!cam) return;
      const im=document.createElement('div'); im.className='twmini'; im.style.cursor='pointer';
      im.innerHTML='<img src="'+imgUrl(cam,'thumb')+'">'
        + (cam.has_mask ? '<img class="mk" src="'+imgUrl(cam,'mask')+'">' : '')
        + '<canvas class="mk" width="58" height="44"></canvas>';
      {                                              // 선택 크랙만 노란 발광(멤버 뷰 한정 + 마스크 스냅)
        const a=cam.w/cam.h, b=58/44, dw=(a>b)?44*a:58, dh=(a>b)?44:58/a;
        drawCrackHL(cam, im.querySelector('canvas').getContext('2d'),
          q=>[(58-dw)/2+q.u*dw, (44-dh)/2+q.v*dh], 1.2);
      }
      im.title=cam.source+' — 좌클릭: 시점 이동+대표교체 · 우클릭: 자세히';
      im.onclick=()=>{ selectCamera(cam, true);                                   // 좌클릭 → 시점 이동
        const pr=projectToCam(cam,h.xyz)||{u:0.5,v:0.5};
        makeCrop(crop, cam, pr.u, pr.v, 0.32);                                    //  + 대표 이미지 자리 교체
        lbl.textContent='대표 뷰: '+cam.source+'  (좌클릭: 시점 이동 · 우클릭: 자세히)'; };
      im.oncontextmenu=(e)=>{ e.preventDefault();
        const pr=projectToCam(cam,h.xyz)||{u:0.5,v:0.5}; openImageModal(cam, pr); };  // 우클릭 → 자세히
      row.appendChild(im);
    });
    items.appendChild(row);
  }
}
document.getElementById('imgReset').onclick=()=>{
  deselectHotspot();
  if(worldMarker) worldMarker.visible=false;      // 지점 검사 표식도 함께 정리
  renderImageList(allCams, null);
};
document.getElementById('camClear').onclick=()=>{
  if(selectedCam){ setCamColor(selectedCam,CAM_BLUE); selectedCam=null; }
  document.getElementById('caminfo').style.display='none';
  document.querySelectorAll('.imgItem').forEach(el=>el.classList.remove('active'));
  if(worldMarker) worldMarker.visible=false;
};
document.getElementById('ckCam').onchange=e=>{ camGroup.visible=e.target.checked; };
renderImageList(allCams, null);

// ================= 원본 크게 보기 모달 (앱 내부 GUI + 이미지 점→3D 좌표) =================
let modalCam=null, modalMark=null, mv={scale:1,tx:0,ty:0}, mvFitScale=1, mDrag=null, mMoved=false;
let worldMarker=null;

// 확대할수록 크랙 마커(빨간 원)를 점점 흐리게 → 픽셀 정밀검사 방해 최소화
function crackMarkerOpacity(){
  const r = mv.scale / (mvFitScale || mv.scale);   // 맞춤=1, 확대할수록 커짐
  if(r <= 2) return 1;
  if(r >= 8) return 0.05;
  return 1 - (r - 2) / 6 * 0.95;
}

// kind='inspect' 면 지점 검사에서 온 표식(노랑). noHL=true 면 크랙 하이라이트 억제(카메라 이미지 보기).
let modalNoHL=false;
// 원본 사진 확대 모달을 연다(마스크 오버레이·선택 크랙 하이라이트·지점 표식을 상황에 맞게 켠다).
function openImageModal(c, markUV, kind, noHL){
  modalCam=c; modalMark = markUV || null; modalNoHL = !!noHL;
  document.getElementById('imTitle').textContent=c.source;
  document.getElementById('imMarker').style.display='none';
  const cm=document.getElementById('imCrackMarker');
  cm.style.display='none';
  cm.classList.toggle('inspect', kind==='inspect');
  document.getElementById('imCoord').textContent = (modalMark && kind==='inspect')
    ? '노란 원 = 클릭하신 지점 · 드래그: 이동 · 휠: 확대/축소'
    : (modalNoHL ? '드래그: 이동 · 휠: 확대/축소'
       : (crackSeenIn(c) ? '노란 발광 = 선택한 크랙 · 드래그: 이동 · 휠: 확대/축소'
          : (selIdx>=0 ? '선택한 크랙은 이 사진에서 검출되지 않음 · 드래그: 이동 · 휠: 확대/축소'
                       : '드래그: 이동 · 휠: 확대/축소')));
  const img=document.getElementById('imImg');
  img.onload=()=>{ fitModal(); placeCrackMarker(); drawModalHL(); };
  img.src=imgUrl(c,'full');
  // 검출 마스크 오버레이: 해당 이미지에 마스크가 있을 때만 토글 노출·로드
  const maskImg=document.getElementById('imMask'), tog=document.getElementById('imMaskToggle');
  if(c.has_mask){
    document.getElementById('imMaskLabel').style.display='flex';
    tog.checked = true;                             // 이미지 클릭 시 기본으로 마스크 씌워 보여줌
    maskImg.src=imgUrl(c,'mask');
    maskImg.style.display = 'block';
    document.getElementById('imHL').style.display='block';   // 하이라이트도 토글 상태와 동기화
  } else {
    document.getElementById('imMaskLabel').style.display='none';
    maskImg.removeAttribute('src'); maskImg.style.display='none';
    document.getElementById('imHL').style.display='none';
  }
  document.getElementById('imgmodal').style.display='flex';
}
// 모달 하이라이트: 선택 크랙의 중심선을 원본 해상도 캔버스에 노란 발광으로(줌·팬은 imStage transform 이 처리)
function drawModalHL(){
  const cvs=document.getElementById('imHL'), img=document.getElementById('imImg');
  if(!img.naturalWidth || selIdx<0 || !modalCam || modalNoHL){ cvs.width=1; cvs.height=1; return; }
  cvs.width=img.naturalWidth; cvs.height=img.naturalHeight;
  drawCrackHL(modalCam, cvs.getContext('2d'),
    q=>[q.u*cvs.width, q.v*cvs.height], Math.max(1.5, cvs.width*0.0011));
}
// 모달 위 지점 표식(노란 원)을 제자리에 놓는다. 균열은 마스크로 보여주므로 지점 검사일 때만 표시.
function placeCrackMarker(){
  const mk=document.getElementById('imCrackMarker'), img=document.getElementById('imImg');
  // 크랙(빨간 원)은 검출 마스크로 대체 → 지점검사(노란 원, inspect)일 때만 표시
  if(modalMark && img.naturalWidth && mk.classList.contains('inspect')){
    mk.style.left=(modalMark.u*img.naturalWidth)+'px';
    mk.style.top =(modalMark.v*img.naturalHeight)+'px';
    mk.style.display='block';
    mk.style.opacity=crackMarkerOpacity();
  } else { mk.style.display='none'; }
}
function closeModal(){ document.getElementById('imgmodal').style.display='none'; modalCam=null; }   // 모달 닫기
// 현재 줌·팬(mv)을 스테이지 transform 에 반영한다 — 이미지·마스크·하이라이트가 함께 움직인다.
function applyModal(){
  document.getElementById('imStage').style.transform='translate('+mv.tx+'px,'+mv.ty+'px) scale('+mv.scale+')';
  const mk=document.getElementById('imCrackMarker');
  if(mk && mk.style.display!=='none') mk.style.opacity=crackMarkerOpacity();
}
// 이미지 전체가 보이도록 배율·위치를 초기화한다(로드 직후·리사이즈 시).
function fitModal(){
  const img=document.getElementById('imImg'), view=document.getElementById('imView');
  if(!img.naturalWidth) return;
  const s=Math.min(view.clientWidth/img.naturalWidth, view.clientHeight/img.naturalHeight)*0.98;
  mvFitScale = s;
  mv={scale:s, tx:(view.clientWidth-img.naturalWidth*s)/2, ty:(view.clientHeight-img.naturalHeight*s)/2};
  applyModal();
}
// 마우스 위치(mx,my)를 고정점으로 배율을 fac 배 한다(그 점 아래 픽셀이 제자리에 남는다).
function zoomModal(mx,my,fac){
  const ns=Math.max(0.05, Math.min(30, mv.scale*fac));
  mv.tx=mx-(mx-mv.tx)*(ns/mv.scale); mv.ty=my-(my-mv.ty)*(ns/mv.scale); mv.scale=ns; applyModal();
}

// 지점 검사에서 클릭한 3D 지점에 분홍 구를 놓는다(지연 생성, 화면상 크기 고정).
function placeWorldMarker(p){
  if(!worldMarker){
    worldMarker=new THREE.Mesh(new THREE.SphereGeometry(1,16,16),
        new THREE.MeshBasicMaterial({color:0xff33cc, depthTest:false}));
    worldMarker.renderOrder=1001; scene.add(worldMarker);
  }
  worldMarker.visible=true; worldMarker.position.copy(p);
  worldMarker.scale.setScalar(MARK_PX*worldPerPixel(camera.position.distanceTo(p)));
}
// (제거됨) 모달에서 점 클릭 → 3D 좌표 계산 기능 — 지점 검사는 3D 화면의 '지점 검사 모드'로 수행.
// placeWorldMarker 는 지점 검사 모드에서 계속 사용한다.
(function(){
  const view=document.getElementById('imView');
  view.addEventListener('pointerdown', e=>{ mDrag={x:e.clientX,y:e.clientY,tx:mv.tx,ty:mv.ty}; mMoved=false; view.setPointerCapture(e.pointerId); });
  view.addEventListener('pointermove', e=>{ if(!mDrag)return; const dx=e.clientX-mDrag.x, dy=e.clientY-mDrag.y;
    if(Math.abs(dx)+Math.abs(dy)>3) mMoved=true; mv.tx=mDrag.tx+dx; mv.ty=mDrag.ty+dy; applyModal(); });
  view.addEventListener('pointerup', ()=>{ mDrag=null; });
  view.addEventListener('wheel', e=>{ e.preventDefault(); const r=view.getBoundingClientRect();
    zoomModal(e.clientX-r.left, e.clientY-r.top, e.deltaY<0?1.15:1/1.15); }, {passive:false});
  document.getElementById('imClose').onclick=closeModal;
  document.getElementById('imZoomIn').onclick=()=>{ const r=view.getBoundingClientRect(); zoomModal(r.width/2,r.height/2,1.25); };
  document.getElementById('imZoomOut').onclick=()=>{ const r=view.getBoundingClientRect(); zoomModal(r.width/2,r.height/2,1/1.25); };
  document.getElementById('imZoomReset').onclick=fitModal;
  document.getElementById('imMaskToggle').onchange=e=>{               // 검출 마스크 on/off
    const mk=document.getElementById('imMask');
    mk.style.display=(e.target.checked && mk.getAttribute('src'))?'block':'none';
    // 선택 크랙 노란 하이라이트도 마스크와 함께 켜고 끈다(마스크 없이 선만 뜨면 혼란)
    document.getElementById('imHL').style.display=e.target.checked?'block':'none';
  };
  window.addEventListener('keydown', e=>{ if(e.key==='Escape') closeModal(); });
})();

let baseScale=1;
// 메시 크기를 재서 모델 의존 값들을 한 번에 정한다 — 화면상 크기 기준(baseScale),
// 카메라 글리프, 바닥 그리드와 좌표축. 메시 로드가 끝난 뒤 한 번 부른다.
function fitSizes(){
  // 타일 그룹은 타일이 흘러들어오는 중이라 setFromObject 로는 경계를 못 잰다 — 타일 메타의 전체 경계를 쓴다
  const box = tiles ? tiles.box.clone() : new THREE.Box3().setFromObject(scene);
  const d = box.getSize(new THREE.Vector3()).length();
  baseScale = d * 0.005;
  modelDiag = d; buildCameras(d);

  // 좌표계 및 그리드 생성 (바운딩 박스 기준)
  gridGroup.clear();
  const center = box.getCenter(new THREE.Vector3());
  sceneCenter.copy(center);
  const minZ = box.min.z; // 모델의 가장 바닥면
  
  // GridHelper (Three.js 기본은 XZ 평면이므로, Z-UP 환경에 맞게 X축 회전)
  const gridHelper = new THREE.GridHelper(d * 1.5, 50, 0x888888, 0x444444);
  gridHelper.rotation.x = Math.PI / 2;
  gridHelper.position.set(center.x, center.y, minZ);
  gridGroup.add(gridHelper);
  
  // AxesHelper (빨강=X, 초록=Y, 파랑=Z)
  const axesHelper = new THREE.AxesHelper(d * 0.3);
  axesHelper.position.set(center.x, center.y, minZ);
  // 축이 잘 보이도록 굵기 조정 (선택적)
  axesHelper.material.linewidth = 3;
  gridGroup.add(axesHelper);
}
fitSizes();                              // 경계는 tiles.ready 시점에 확정돼 있다(지연 불필요)

// 이벤트 리스너: UI 기능 매핑
document.getElementById('ckRibbon').onchange = e => { ribbonGroup.visible = e.target.checked; };
// 실폭(×1) ↔ 강조(과장) 굵기 토글 — 반경이 버퍼에 구워져 있어 리본 전체를 다시 굽는다
(function(){
  const ck = document.getElementById('ckTrueW'); if(!ck) return;
  ck.checked = ribbonTrueScale;
  ck.onchange = e => {
    ribbonTrueScale = e.target.checked;
    try{ localStorage.setItem('ctw_ribbon_true', ribbonTrueScale ? '1' : '0'); }catch(err){}
    rebuildRibbons(true);
    toast(ribbonTrueScale ? '실폭(×1) 굵기 — 얇은 크랙은 가까이에서 보입니다'
                          : '강조 굵기(최소 4cm·2배 과장)');
  };
})();
document.getElementById('ckHot').onchange = e => { hud.style.display = e.target.checked ? '' : 'none'; };   // 말풍선 표시
document.getElementById('ckGrid').onchange = e => { gridGroup.visible = e.target.checked; };

// 메쉬 다크 모드 스위치
document.getElementById('ckDarkMesh').onchange = e => {
  if(!meshMat) return;                   // 모든 타일이 meshMat 하나를 공유하므로 한 번만 바꾸면 된다
  if(e.target.checked) {
      // 메시를 매우 어두운 색상으로 변경하고 고유 색상(VertexColor) 비활성화
      meshMat.color.setHex(0x111111);
      if(meshMat.vertexColors) meshMat.vertexColors = false;
  } else {
      // 원래 상태로 복구
      meshMat.color.setHex(originalMeshColor);
      if(hasVertexColors) meshMat.vertexColors = true;
  }
  meshMat.needsUpdate = true;
};

// 상호작용 (Raycaster)
const ray=new THREE.Raycaster(), mouse=new THREE.Vector2(); let mIn=false;
renderer.domElement.addEventListener('mousemove',e=>{mouse.x=(e.clientX/innerWidth)*2-1;mouse.y=-(e.clientY/innerHeight)*2+1;mIn=true;});
renderer.domElement.addEventListener('mouseleave',()=>mIn=false);
// 카메라 콘 / 핫스팟 레티클 위에 커서 포인터. 스프라이트 레이캐스트는 쿼드 몇 개라 매 프레임도 싸다.
// 크랙(hot) 하이라이트 갱신: 호버·선택된 크랙의 리본만 흰 오버레이 튜브로 재생성.
// (병합 버퍼는 건드리지 않는다 — 인자는 호환용이고 현재 hoverHot/selIdx 상태로 전체 동기화)
let _hlHots=new Set();
// 흰색 강조 오버레이를 현재 호버·선택 상태와 동기화한다(바뀐 게 없으면 즉시 반환).
// 인자는 호환용으로만 남아 있고, 실제로는 hoverHot/selIdx 를 보고 전체를 다시 맞춘다.
function refreshRibbonColor(_hot){
  const want=new Set();
  if(hoverHot>=0) want.add(hoverHot);
  if(selIdx>=0) want.add(selIdx);
  if(want.size===_hlHots.size && [..._hlHots].every(h=>want.has(h))) return;
  _hlHots=want;
  while(hlGroup.children.length){ const c=hlGroup.children.pop(); if(c.geometry) c.geometry.dispose(); }
  for(const hot of want){
    for(const ri of (ribsByHot[hot]||[]))
      for(const run of (ribRuns[ri]||[]))
        hlGroup.add(new THREE.Mesh(variableTube(run.pts, run.rr, RIBBON_RADIAL), HL_MAT));
  }
}
// 호버 중인 균열을 바꾼다(-1 = 없음). 이전 것은 원래 색으로, 새 것은 흰색 강조로.
function setHoverHot(hot){
  if(hot===hoverHot) return;
  const prev=hoverHot; hoverHot=hot;
  refreshRibbonColor(prev); refreshRibbonColor(hot);   // 이전 것 복원, 새 것 흰색
}
// 크랙 히트박스 피킹: 보이는 튜브보다 넉넉히 잡되 화면상 HITBOX_PX 안쪽만 유효.
// 여러 크랙이 겹치면 커서에서 화면상 가장 가까운 크랙을 고른다. (ray 는 호출 전 setFromCamera 되어 있어야 함)
function pickRibbonHot(){
  if(!ribbonGroup.visible || !ribbonHitLines.length) return -1;
  const saved = ray.params.Line.threshold;
  // 후보 임계값 = 화면 14px 의 월드 길이(관측 거리 근사). 예전 modelDiag 비례(622m 모델 → 19m!)는
  // 수만 히트라인 전부가 후보로 잡혀 프레임을 잡아먹는다. 아래 화면거리 필터가 정밀 판정을 맡는다.
  ray.params.Line.threshold = Math.max(RIBBON_MAX_R,
    HITBOX_PX * worldPerPixel(camera.position.distanceTo(sceneCenter)));
  const hits = ray.intersectObjects(ribbonHitLines,false);
  ray.params.Line.threshold = saved;
  if(!hits.length) return -1;
  // Line 교차엔 distanceToRay 가 없다 → 교차점을 화면에 투영해 커서와의 픽셀 거리로 판정
  const cx=(mouse.x*0.5+0.5)*innerWidth, cy=(-mouse.y*0.5+0.5)*innerHeight;
  const v=new THREE.Vector3();
  let bestHot=-1, bestPx=Infinity;
  for(const h of hits){
    v.copy(h.point).project(camera);
    const sx=(v.x*0.5+0.5)*innerWidth, sy=(-v.y*0.5+0.5)*innerHeight;
    const px=Math.hypot(sx-cx, sy-cy);                             // 커서~크랙(교차점) 화면상 거리(px)
    // 필터로 숨긴 크랙은 히트라인 visible=false 라 레이캐스트에서 이미 제외된다
    if(px<=HITBOX_PX && px<bestPx){ bestPx=px; bestHot=h.object.userData.hot; }   // 14px 안쪽 · 화면상 가장 가까운 크랙
  }
  return bestHot;
}
let _lastPickT=0, _lastPickHot=-1;                    // 호버 피킹 스로틀 상태
// 매 프레임 커서 아래를 보고 호버 상태·커서 모양을 갱신한다.
// 리본 피킹은 비싸서 60ms 로 스로틀한다(수만 크랙에서 매 프레임은 낭비).
function updateHover(){
  if(measureMode||!mIn){ renderer.domElement.style.cursor='default'; hoverIdx=-1; setHoverHot(-1); return; }
  ray.setFromCamera(mouse,camera);
  const c = camGroup.visible ? ray.intersectObjects(camMeshes,false) : [];
  let hot = -1;
  if(ribbonGroup.visible){                            // 리본 호버 → 리본의 '자기' 크랙(핫스팟)
    const now=performance.now();                      // 피킹은 60ms 마다(수만 크랙에서 매 프레임은 낭비)
    if(now-_lastPickT>60){ _lastPickHot=pickRibbonHot(); _lastPickT=now; }
    hot=_lastPickHot;
  }
  hoverIdx = hot;                                    // 말풍선(폭·길이) 표시
  setHoverHot(hot);                                  // 그 크랙의 리본 빨강
  renderer.domElement.style.cursor = (c.length||hot>=0) ? 'pointer' : 'default';
}

// 짧은 탭(클릭)일 때만 선택: 측정/검사 모드 우선, 아니면 핫스팟 → 카메라 순으로 레이캐스트
function handleClick(e){
  if(measureMode){ onMeasureClick(e); return; }
  if(inspectMode){ inspectAt(e); return; }
  mouse.x=(e.clientX/innerWidth)*2-1; mouse.y=-(e.clientY/innerHeight)*2+1;
  ray.setFromCamera(mouse,camera);
  // 클릭 대상은 '크랙(리본)'만. 핫스팟 레티클은 시각 마커·호버 하이라이트 전용(클릭 가로채기 제거).
  if(ribbonGroup.visible){
    const hi = pickRibbonHot();                       // 호버와 동일한 히트박스 피킹
    if(hi>=0){ selectHotspot(hi); showHotspotRep(DATA.hotspots[hi]); return; }
  }
  const camHit = camGroup.visible ? ray.intersectObjects(camMeshes,false) : [];
  if(camHit.length){                                  // 카메라 콘 클릭 → 우측에 그 이미지 표시
    const c=camHit[0].object.userData.cam;
    selectCamera(c, false); showCameraImage(c);
  }
}

// ---- FPS 입력: 좌드래그=시점 회전 · 짧은 탭=선택 · 우클릭 홀드=스코프 확대 ----
// 좌버튼을 누른 뒤 4px 넘게 움직이거나 180ms 넘게 유지하면 '시점 모드'로 확정한다.
// → 짧게 탭하면 핫스팟 선택, 살짝만 끌거나 조금 오래 눌러도 곧바로 시점 회전으로 넘어간다.
const LOOK_SENS = 0.0025, HOLD_MS = 180, MOVE_PX = 4;
const _drag = {down:false, sx:0, sy:0, lx:0, ly:0, t:0, look:false};
renderer.domElement.addEventListener('mousedown', e=>{
  if(e.button===2){ scoping=true; e.preventDefault(); return; }          // 우클릭 = 스코프
  if(e.button!==0) return;
  Object.assign(_drag, {down:true, look:false, sx:e.clientX, sy:e.clientY, lx:e.clientX, ly:e.clientY, t:performance.now()});
});
renderer.domElement.addEventListener('contextmenu', e=>e.preventDefault());   // 우클릭 메뉴 차단
addEventListener('mousemove', e=>{
  if(!_drag.down) return;
  if(!_drag.look && (Math.abs(e.clientX-_drag.sx)+Math.abs(e.clientY-_drag.sy) > MOVE_PX
                     || performance.now()-_drag.t > HOLD_MS)){
    _drag.look = true; cancelAnim();                 // 시점 모드 진입 → 진행 중 비행 취소
  }
  if(_drag.look){
    camYaw   -= (e.clientX-_drag.lx)*LOOK_SENS;       // 좌우: yaw
    camPitch  = clampPitch(camPitch - (e.clientY-_drag.ly)*LOOK_SENS);   // 상하: pitch(반전 없음)
  }
  _drag.lx=e.clientX; _drag.ly=e.clientY;
});
addEventListener('mouseup', e=>{
  if(e.button===2){ scoping=false; return; }
  if(e.button!==0 || !_drag.down) return;
  _drag.down=false;
  if(!_drag.look && performance.now()-_drag.t <= HOLD_MS) handleClick(e);   // 짧은 탭만 선택
});
addEventListener('blur', ()=>{ _drag.down=false; _drag.look=false; scoping=false; });

// ================= 측정 도구 (거리 / 반경) =================
// 좌표 규약: PLY 1 단위 = 현실 1 m. 표면 클릭은 레이캐스팅으로 스냅.
let measureMode=null, measPts=[], measNormals=[], lastMeasure=null;
const measureGroup=new THREE.Group(); scene.add(measureGroup);
const DIST_COLOR=0x00e5ff, RAD_COLOR=0xff9800, CALIB_COLOR=0xb388ff;
let calibPending=null;                              // 캘리브레이션 대기 중인 측정 거리(메시 단위)

// ---- 화면상 크기 고정(줌 무관): 정밀 검사를 위해 마커/라벨/핫스팟은 항상 같은 픽셀 크기 ----
const HOT_PX = 7;      // 핫스팟 화면 반경(px)
const MARK_PX = 5;     // 측정 마커 화면 반경(px)
const LABEL_PX = 26;   // 측정 라벨 화면 높이(px)
// 카메라에서 dist 만큼 떨어진 지점에서 화면 1px 에 해당하는 월드 길이
function worldPerPixel(dist){
  const vFOV = camera.fov * Math.PI/180;
  return (2*dist*Math.tan(vFOV/2)) / (renderer.domElement.clientHeight || innerHeight);
}
// 매 프레임 호출: 각 오브젝트를 카메라 거리에 비례해 재스케일 → 화면 픽셀 크기 일정
function keepConstantSize(){
  const cp = camera.position;
  if(worldMarker && worldMarker.visible){
    worldMarker.scale.setScalar(MARK_PX * worldPerPixel(cp.distanceTo(worldMarker.position)));
  }
  for(const o of measureGroup.children){
    if(o.isSprite){                                   // 숫자 라벨
      const h = LABEL_PX * worldPerPixel(cp.distanceTo(o.position));
      o.scale.set(h*(o.userData.aspect||1), h, 1);
    } else if(o.userData && o.userData.mark){         // 측정 끝점 마커
      o.scale.setScalar(MARK_PX * worldPerPixel(cp.distanceTo(o.position)));
    }
    // 선/원(Line)은 실제 위치·크기 유지 (스케일 고정하지 않음)
  }
}

// ---- 핫스팟 레티클(스프라이트): 화면 크기 고정 + 자기 표면과의 z-fighting 회피 + 맥동 ----
const HOT_QUAD = HOT_PX*3.7;   // 글로우 여백 포함 쿼드 한 변 ≈26px → 다이아몬드 ≈15px (CSS와 동일)
const HOT_FAR_M = 0.20;        // 멀리서 볼 때 핫스팟 월드 고정 크기(20cm). 화면상한과 min 으로 결합
const HOT_LIFT = 0.004;        // 카메라 쪽으로 거리의 0.4% 당김. 핫스팟은 표면 '위'의 점이라
                               // 그대로 두면 자기 표면에 반쯤 묻힌다.
const _hp = new THREE.Vector3();
// 다이아몬드 레티클 제거 — 3D 스프라이트 갱신은 더 이상 없다(정보는 말풍선 DOM 이 담당).

function setMeasResult(html){ document.getElementById('measResult').innerHTML=html; }   // 측정 결과/안내 문구 표시
function modePrompt(){                               // 현재 측정 모드의 다음 단계 안내 문구
  if(measureMode==='distance') return '① 첫 번째 점을 클릭하세요.';
  if(measureMode==='radius')   return '① 원의 중심을 클릭하세요.';
  if(measureMode==='calib')    return '① 기준 거리의 첫 번째 점을 클릭하세요.';
  return '측정 모드를 선택하세요.';
}
function updateModeButtons(){                        // 세 측정 버튼의 active 표시를 현재 모드와 맞춤
  document.getElementById('btnDist').classList.toggle('active', measureMode==='distance');
  document.getElementById('btnRad').classList.toggle('active', measureMode==='radius');
  document.getElementById('btnCalib').classList.toggle('active', measureMode==='calib');
}
// three 객체의 GPU 자원(지오메트리·머티리얼·텍스처)을 해제한다 — 측정 객체는 자주 만들고 지운다.
function disposeObj(o){
  if(o.geometry) o.geometry.dispose();
  if(o.material){ if(o.material.map) o.material.map.dispose(); o.material.dispose(); }
}
// 찍은 점과 그려진 측정 객체를 모두 지운다(clearText:false 면 안내 문구는 그대로 둔다).
function resetMeasure(clearText){
  measPts=[]; measNormals=[]; lastMeasure=null;
  while(measureGroup.children.length){ disposeObj(measureGroup.children.pop()); }
  if(clearText!==false) setMeasResult(modePrompt());
}
// 측정 모드를 켜고 끈다(같은 버튼을 다시 누르면 해제). 지점 검사 모드와는 배타.
function setMode(m){
  measureMode = (measureMode===m) ? null : m;
  if(measureMode && inspectMode) setInspect(false);          // 지점 검사와 배타
  resetMeasure(false); updateModeButtons();
  renderer.domElement.style.cursor = measureMode ? 'crosshair' : 'default';
  setMeasResult(measureMode ? modePrompt() : '측정 모드를 선택하세요.');
}

// 표면 한 점 픽킹(월드 좌표 + 표면 법선)
function pickMesh(e){
  if(!tiles) return null;
  const mx=(e.clientX/innerWidth)*2-1, my=-(e.clientY/innerHeight)*2+1;
  ray.setFromCamera(new THREE.Vector2(mx,my), camera);
  // 지금 그려지는 타일에 대해 교차(타일별 BVH 는 처음 광선이 지날 때 만들어 붙여 둔다).
  // 타일에 변환을 걸지 않으므로 정점 좌표가 곧 월드 좌표다.
  const h = tiles.raycast(ray.ray.origin, ray.ray.direction, Infinity, false);
  if(!h) return null;
  const point = ray.ray.at(h.distance, new THREE.Vector3());
  const normal = tiles.faceNormal(h);
  // 클릭했다는 건 이 면이 보인다는 뜻 — 법선을 시점 쪽으로 맞춘다(타일은 감기 방향을 보장하지 않는다)
  if(normal && normal.dot(camera.position.clone().sub(point)) < 0) normal.negate();
  return {point, normal};
}

// 측정 끝점 구슬 하나(깊이 테스트 없음 → 메시에 가려지지 않는다, 화면상 크기 고정).
function addMarker(pos,color){
  const m=new THREE.Mesh(new THREE.SphereGeometry(1,16,16),
      new THREE.MeshBasicMaterial({color, depthTest:false}));
  m.renderOrder=999; m.position.copy(pos); m.userData.mark=true;
  m.scale.setScalar(MARK_PX * worldPerPixel(camera.position.distanceTo(pos)));
  measureGroup.add(m); return m;
}
// 두 점을 잇는 측정선(역시 항상 앞에 보이게 그린다).
function addLine(a,b,color){
  const l=new THREE.Line(new THREE.BufferGeometry().setFromPoints([a,b]),
      new THREE.LineBasicMaterial({color, depthTest:false}));
  l.renderOrder=998; measureGroup.add(l); return l;
}
// 표면 법선을 평면 법선으로 하는 원(가장자리 시각화)
function addCircle(center,normal,radius,color){
  let n = normal ? normal.clone().normalize()
                 : camera.getWorldDirection(new THREE.Vector3()).clone().negate();
  let tmp=new THREE.Vector3(1,0,0); if(Math.abs(n.dot(tmp))>0.9) tmp.set(0,1,0);
  const u=new THREE.Vector3().crossVectors(n,tmp).normalize();
  const v=new THREE.Vector3().crossVectors(n,u).normalize();
  const pts=[], seg=100;
  for(let i=0;i<=seg;i++){ const t=i/seg*Math.PI*2;
    pts.push(center.clone().addScaledVector(u,Math.cos(t)*radius).addScaledVector(v,Math.sin(t)*radius)); }
  const l=new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({color, depthTest:false}));
  l.renderOrder=998; measureGroup.add(l); return l;
}
// 3D 공간에 떠 있는 수치 라벨(캔버스 스프라이트)
function makeLabel(text,pos,cssColor){
  const pad=22, fs=46; const cv=document.createElement('canvas'); let ctx=cv.getContext('2d');
  ctx.font='bold '+fs+'px Segoe UI, sans-serif'; const tw=Math.ceil(ctx.measureText(text).width);
  cv.width=tw+pad*2; cv.height=fs+pad; ctx=cv.getContext('2d');
  ctx.font='bold '+fs+'px Segoe UI, sans-serif';
  ctx.fillStyle='rgba(0,0,0,0.72)'; ctx.fillRect(0,0,cv.width,cv.height);
  ctx.fillStyle=cssColor; ctx.textAlign='center'; ctx.textBaseline='middle';
  ctx.fillText(text, cv.width/2, cv.height/2);
  const tex=new THREE.CanvasTexture(cv); tex.minFilter=THREE.LinearFilter;
  const sp=new THREE.Sprite(new THREE.SpriteMaterial({map:tex, depthTest:false, transparent:true}));
  sp.renderOrder=1000; sp.position.copy(pos);
  sp.userData.aspect = cv.width/cv.height;
  const h = LABEL_PX * worldPerPixel(camera.position.distanceTo(pos));
  sp.scale.set(h*sp.userData.aspect, h, 1);
  measureGroup.add(sp); return sp;
}

// 단위 변환/표시 (기본 mm, 자동 선택 시 크기에 따라 mm/cm/m) — 캘리브레이션 계수 반영
function fmtUnit(meters){
  meters = meters * calibScale;                     // 기하 측정도 같은 선형 계수로 보정
  const u=document.getElementById('unitSelect').value;
  const mm=meters*1000, cm=meters*100;
  const f=(v,d)=>v.toLocaleString('en-US',{maximumFractionDigits:d, minimumFractionDigits:2});
  if(u==='mm') return f(mm,2)+' mm';
  if(u==='cm') return f(cm,2)+' cm';
  if(u==='m')  return f(meters,4)+' m';
  if(mm<10)    return f(mm,2)+' mm';
  if(mm<1000)  return f(cm,2)+' cm';
  return f(meters,4)+' m';
}
// 같은 값을 mm·cm·m 세 단위로 한 줄에(보조 표시용).
function allUnits(meters){
  meters = meters * calibScale;
  return (meters*1000).toFixed(2)+' mm · '+(meters*100).toFixed(2)+' cm · '+meters.toFixed(4)+' m';
}
// 마지막 측정값을 좌측 패널에 표시한다(반경은 지름도 함께).
function showResult(){
  if(!lastMeasure) return;
  if(lastMeasure.type==='distance'){
    setMeasResult('<b style="color:#00e5ff;">거리</b><br>'+
      '<span style="font-size:16px;font-weight:bold;">'+fmtUnit(lastMeasure.value)+'</span><br>'+
      '<span style="color:#aaa;">'+allUnits(lastMeasure.value)+'</span>');
  } else {
    const r=lastMeasure.value;
    setMeasResult('<b style="color:#ff9800;">반경</b><br>'+
      '<span style="font-size:16px;font-weight:bold;">R = '+fmtUnit(r)+'</span><br>'+
      '<span style="color:#ccc;">지름 D = '+fmtUnit(r*2)+'</span><br>'+
      '<span style="color:#888;">'+allUnits(r)+'</span>');
  }
}
// 3D 숫자 라벨을 다시 만든다(단위 변경·캘리브레이션으로 표기가 달라졌을 때).
function relabel(){
  if(!lastMeasure) return;
  if(lastMeasure.label){ measureGroup.remove(lastMeasure.label); disposeObj(lastMeasure.label); }
  const txt=(lastMeasure.type==='radius'?'R ':'')+fmtUnit(lastMeasure.value);
  lastMeasure.label=makeLabel(txt, lastMeasure.mid,
      lastMeasure.type==='radius' ? '#ff9800' : '#00e5ff');
}

// 측정 모드에서의 표면 클릭 처리. 세 모드 모두 '점 2개'로 완성되며, 세 번째 클릭은 새 측정을 시작한다.
//   distance 두 점 사이 거리 · radius 중심+가장자리 → 반경 · calib 두 점을 실측값과 대응시켜 배율 산출
function onMeasureClick(e){
  const hit=pickMesh(e);
  if(!hit){ setMeasResult('⚠ 모델 표면을 클릭하세요 (빈 공간이 선택됨).'); return; }
  if(measureMode==='distance'){
    if(measPts.length>=2) resetMeasure(false);
    measPts.push(hit.point); addMarker(hit.point, DIST_COLOR);
    if(measPts.length<2){ setMeasResult('② 두 번째 점을 클릭하세요.'); return; }
    const d=measPts[0].distanceTo(measPts[1]);
    addLine(measPts[0],measPts[1],DIST_COLOR);
    const mid=measPts[0].clone().add(measPts[1]).multiplyScalar(0.5);
    lastMeasure={type:'distance', value:d, mid:mid, label:null};
    relabel(); showResult();
  } else if(measureMode==='radius'){
    if(measPts.length>=2) resetMeasure(false);
    measPts.push(hit.point); measNormals.push(hit.normal);
    addMarker(hit.point, measPts.length===1 ? 0xffc107 : RAD_COLOR);
    if(measPts.length<2){ setMeasResult('② 원 위(가장자리)의 한 점을 클릭하세요.'); return; }
    const c=measPts[0], p=measPts[1], r=c.distanceTo(p);
    addLine(c,p,RAD_COLOR); addCircle(c, measNormals[0], r, RAD_COLOR);
    const mid=c.clone().add(p).multiplyScalar(0.5);
    lastMeasure={type:'radius', value:r, mid:mid, label:null};
    relabel(); showResult();
  } else if(measureMode==='calib'){
    if(measPts.length>=2) resetMeasure(false);
    measPts.push(hit.point); addMarker(hit.point, CALIB_COLOR);
    if(measPts.length<2){ setMeasResult('② 기준 거리의 두 번째 점을 클릭하세요.'); return; }
    const d=measPts[0].distanceTo(measPts[1]);      // 메시 단위(공칭 m) — 보정 전 원시값
    addLine(measPts[0],measPts[1],CALIB_COLOR);
    calibPending = d;
    document.getElementById('calibMeasured').textContent = d.toFixed(4)+' (메시 단위)';
    document.getElementById('calibForm').style.display='block';
    setMeasResult('<b style="color:#b388ff;">기준 거리</b> '+d.toFixed(4)+' 단위<br>'+
      '<span style="color:#aaa;">아래에 실제 거리를 입력하고 [적용]을 누르세요.</span>');
  }
}

document.getElementById('btnInspect').onclick=()=>setInspect(!inspectMode);
document.getElementById('btnDist').onclick=()=>setMode('distance');
document.getElementById('btnRad').onclick =()=>setMode('radius');
document.getElementById('btnReset').onclick=()=>resetMeasure(true);
document.getElementById('unitSelect').onchange=()=>{ showResult(); relabel(); };

// ===== 스케일 캘리브레이션: 적용/해제 =====
function refreshCalibUI(){
  const st=document.getElementById('calibStatus');
  document.getElementById('btnCalibClear').style.display = calibScale!==1 ? 'inline-block' : 'none';
  st.innerHTML = calibScale!==1
    ? '✔ 보정 계수 <b style="color:#7fffd4">×'+calibScale.toFixed(4)+'</b> 적용 중 — 모든 mm 수치에 반영됩니다.'
    : '실측 길이를 아는 두 점(표척·조인트 등)을 클릭해 모든 mm 수치를 보정합니다.';
  // 열려 있는 표시들 일괄 갱신: 말풍선 태그 · 메타/경고 · 크랙 목록 · 측정 결과
  hudMarkers.forEach(m=>{ if(!m.el) return; const t=m.el.querySelector('.tag'); if(t) t.innerHTML=fmtWL(m.h); });
  renderMeta(); showResult(); relabel();
  if(typeof clRefresh==='function' && document.getElementById('cracklist').style.display==='flex') clRefresh();
  if(selIdx>=0) showHotspotRep(DATA.hotspots[selIdx]);
}
document.getElementById('btnCalib').onclick=()=>{
  document.getElementById('calibForm').style.display='none'; calibPending=null;
  setMode('calib');
};
document.getElementById('btnCalibApply').onclick=()=>{
  const v=parseFloat(document.getElementById('calibReal').value);
  const mul=parseFloat(document.getElementById('calibUnit').value);   // mm 환산 배수
  if(!(v>0) || !(calibPending>0)){ setMeasResult('⚠ 실제 거리를 올바르게 입력하세요.'); return; }
  const real_mm = v*mul;
  calibScale = real_mm / (calibPending*1000);       // (실제 mm) / (공칭 mm)
  calibInfo = {scale:calibScale, measured_m:calibPending, real_mm};
  try{ localStorage.setItem(CALIB_KEY, JSON.stringify(calibInfo)); }catch(e){}   // 오프라인 폴백 캐시
  // 사이드카에 영속 — PDF·다른 PC 도 같은 보정을 쓴다
  fetch('/api/calib', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({p: REPORT, scale: calibScale, measured_m: calibPending, real_mm})})
    .then(r=>r.json()).then(d=>{ if(d.error) toast('보정 저장 실패: '+d.error); })
    .catch(e=>toast('보정 저장 실패: '+e));
  document.getElementById('calibForm').style.display='none';
  setMode(null); resetMeasure(true); refreshCalibUI();
  setMeasResult('스케일 보정 적용: <b style="color:var(--accent)">×'+calibScale.toFixed(4)+'</b>');
  toast('스케일 보정 적용됨 · ×'+calibScale.toFixed(4));
};
document.getElementById('btnCalibClear').onclick=()=>{
  calibScale=1.0; calibInfo=null; calibPending=null;
  try{ localStorage.removeItem(CALIB_KEY); }catch(e){}
  fetch('/api/calib', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({p: REPORT, scale: 0})}).catch(()=>{});   // 사이드카에서도 해제
  document.getElementById('calibForm').style.display='none';
  refreshCalibUI(); setMeasResult('스케일 보정을 해제했습니다.');
};
refreshCalibUI();

// ================= 크랙 목록 패널 (정렬 · 필터 · CSV 내보내기) =================
// 수만 개 크랙을 표로 탐색: 300행씩 청크 렌더(스크롤 시 추가), 행 클릭 → 해당 크랙으로 비행.
const clPanel=document.getElementById('cracklist'), clRowsEl=document.getElementById('clRows');
let clData=[], clShown=0; const CL_CHUNK=300;
// 표시할 균열을 골라 정렬해 clData 에 담는다(최소 폭·길이 필터는 보정된 mm 기준).
function clFilterSort(){
  const minW=parseFloat(document.getElementById('clMinW').value)||0;
  const minL=parseFloat(document.getElementById('clMinL').value)||0;
  const get={wmax:h=>h.width_mm_max??h.width_mm??0, wmed:h=>h.width_mm_median??h.width_mm_max??0,
             len:h=>h.length_mm??0, views:h=>(h.views||[]).length, conf:h=>h.conf??0
            }[document.getElementById('clSort').value];
  clData=[];
  DATA.hotspots.forEach((h,i)=>{
    if(cmm(h.width_mm_max??h.width_mm??0) < minW) return;      // 필터는 보정된 mm 기준
    if(cmm(h.length_mm??0) < minL) return;
    clData.push([i,h]);
  });
  clData.sort((a,b)=>get(b[1])-get(a[1]));
}
// 목록 행 하나(번호·검토표시·심각도·폭·길이·뷰 수). 클릭하면 그 균열로 날아간다.
function clRow(i,h){
  const el=document.createElement('div'); el.className='clRow'; el.dataset.hi=i;
  const c=hotSevColor(i);
  const rgb='rgb('+Math.round(c.r*255)+','+Math.round(c.g*255)+','+Math.round(c.b*255)+')';
  const wmed=cmm(h.width_mm_median??h.width_mm_max), wmax=cmm(h.width_mm_max??h.width_mm), L=cmm(h.length_mm), T=ltot(h);
  const lenTitle=(T!=null&&L!=null&&T>L+0.5)?' title="최장경로 '+Math.round(L).toLocaleString()+'mm · 총연장 '+Math.round(T).toLocaleString()+'mm"':'';
  const sv=sevGrade(sevWidth(h)), cb_=confBand(h);
  const gTitle=sv?(' title="심각도 '+SEV_NAME[sv]+' (폭 '+sevRange(SEV_KEYS.indexOf(sv))+'mm)'
    +(cb_!=null?' · 균열 신뢰도 '+CONF_NAME[cb_]+' '+Number(h.conf).toFixed(2):'')+'"'):'';
  const rv=revOf(i);
  const rvMark=rv&&rv.state?'<span style="color:'+REV_COLOR[rv.state]+'" title="'+REV_LABEL[rv.state]+'"> '
    +({confirmed:'✓',rejected:'✗',edited:'△'})[rv.state]+'</span>':'';
  el.innerHTML='<span><span class="sevchip" style="background:'+rgb+'"></span>C-'+String(i+1).padStart(4,'0')+rvMark+'</span>'
    +'<span'+gTitle+' style="color:'+(sv?SEV_HEX[sv]:'inherit')+';font-weight:600">'+(sv?SEV_NAME[sv]:'?')
      +(cb_===0?'<span style="color:'+CONF_HEX[0]+'" title="균열 신뢰도 낮음">?</span>':'')+'</span>'
    +'<span>'+fmtW(h, wmed, 2)+'</span>'
    +'<span>'+fmtW(h, wmax, 2)+'</span>'
    +'<span'+lenTitle+'>'+(L!=null?Math.round(L).toLocaleString():'?')+(lenTitle?' *':'')+'</span>'
    +'<span>'+(h.views||[]).length+'</span>';
  el.onclick=()=>{ clRowsEl.querySelectorAll('.clRow.sel').forEach(x=>x.classList.remove('sel'));
    el.classList.add('sel'); flyToCrack(i); };
  return el;
}
// 다음 300개를 이어 그린다(무한 스크롤). 수만 개를 한 번에 만들면 DOM 이 감당하지 못한다.
function clRenderMore(){
  const end=Math.min(clData.length, clShown+CL_CHUNK);
  const frag=document.createDocumentFragment();
  for(let k=clShown;k<end;k++) frag.appendChild(clRow(clData[k][0], clData[k][1]));
  clRowsEl.appendChild(frag); clShown=end;
  document.getElementById('clFoot').textContent = clShown<clData.length
    ? clShown.toLocaleString()+' / '+clData.length.toLocaleString()+'개 표시 — 스크롤하면 더 불러옵니다'
    : clData.length.toLocaleString()+'개 표시'+(calibScale!==1?' · 보정 ×'+calibScale.toFixed(4):'');
}
// 목록을 처음부터 다시 만든다(정렬·필터 변경, 검토 저장, 캘리브레이션 변경 시).
function clRefresh(){
  clFilterSort(); clRowsEl.innerHTML=''; clShown=0; clRenderMore();
  document.getElementById('clCount').textContent =
    '('+clData.length.toLocaleString()+' / '+DATA.hotspots.length.toLocaleString()+')';
  updateRevProg();
}
clRowsEl.addEventListener('scroll',()=>{
  if(clRowsEl.scrollTop+clRowsEl.clientHeight > clRowsEl.scrollHeight-200 && clShown<clData.length) clRenderMore();
});
['clSort','clMinW','clMinL'].forEach(id=>document.getElementById(id).addEventListener('input',clRefresh));
document.getElementById('clClose').onclick=()=>showPane(null);

// ================= 상단 메뉴 ↔ 좌측 도크 (같은 메뉴 재클릭 = 닫기) =================
const PANES=['pane-info','pane-view','cracklist','pane-inspect','pane-measure','pane-calib'];
let activePane='pane-info';
// 좌측 도크에 패널 하나만 띄운다. 같은 메뉴를 다시 누르거나 null 이면 도크를 닫는다.
function showPane(id){
  activePane = (id!=null && activePane!==id) ? id : null;
  for(const p of PANES){ const el=document.getElementById(p);
    if(el) el.style.display = (p===activePane) ? (p==='cracklist'?'flex':'block') : 'none'; }
  document.getElementById('leftdock').style.display = activePane ? 'flex' : 'none';
  document.getElementById('leftgrip').style.display = activePane ? 'block' : 'none';
  document.querySelectorAll('#topbar .tmenu').forEach(b=>b.classList.toggle('active', b.dataset.pane===activePane));
  if(activePane==='cracklist') clRefresh();
}
document.querySelectorAll('#topbar .tmenu').forEach(b=>b.onclick=()=>showPane(b.dataset.pane));

// ================= 도크 폭 조절(그립 드래그 · localStorage 기억) =================
(function(){
  const root=document.documentElement;
  try{ const s=JSON.parse(localStorage.getItem('ctw_dockw')||'{}');
    if(s.l) root.style.setProperty('--leftw', s.l+'px');
    if(s.r) root.style.setProperty('--rightw', s.r+'px'); }catch(e){}
  const curL=()=>parseInt(getComputedStyle(root).getPropertyValue('--leftw'))||340;
  const curR=()=>parseInt(getComputedStyle(root).getPropertyValue('--rightw'))||300;
  // 그립 하나에 드래그 리사이즈를 건다. apply(clientX) 가 실제 폭 계산을 맡고,
  // 드래그가 끝나면 좌우 폭을 localStorage 에 기억한다.
  function grip(el, apply){
    el.addEventListener('pointerdown', e=>{
      e.preventDefault(); el.classList.add('drag');
      const move=ev=>apply(ev.clientX);
      const up=()=>{ el.classList.remove('drag');
        removeEventListener('pointermove',move); removeEventListener('pointerup',up);
        try{ localStorage.setItem('ctw_dockw', JSON.stringify({l:curL(), r:curR()})); }catch(err){} };
      addEventListener('pointermove',move); addEventListener('pointerup',up);
    });
  }
  grip(document.getElementById('leftgrip'),
       x=>root.style.setProperty('--leftw', Math.max(240, Math.min(innerWidth*0.6, x))+'px'));
  grip(document.getElementById('rightgrip'),
       x=>root.style.setProperty('--rightw', Math.max(200, Math.min(innerWidth*0.6, innerWidth-x))+'px'));
})();

// 행 클릭 → 크랙 앞으로 비행(현 시선 유지, 크랙 크기에 비례한 거리) + 선택·상세 표시
// 크랙을 검출한 카메라(멤버 뷰) 중 그 크랙에 가장 가까운 카메라를 고른다.
// 크랙이 실제로 화면 안에 들어오는 뷰를 우선하고, 그중 최단 거리를 택한다.
function nearestMemberCam(h){
  const t=new THREE.Vector3(h.xyz[0],h.xyz[1],h.xyz[2]);
  let best=null, bestD=Infinity, bestVis=false;
  for(const vw of (h.views||[])){
    const cam=camDataBySource[vw.source]; if(!cam) continue;
    const d=t.distanceTo(new THREE.Vector3(cam.pos[0],cam.pos[1],cam.pos[2]));
    const vis=!!projectToCam(cam, h.xyz);            // 크랙이 이 카메라 화면 안에 보이는가
    // 화면에 보이는 뷰를 항상 우선, 그다음 거리로 비교
    if((vis && !bestVis) || (vis===bestVis && d<bestD)){ best=cam; bestD=d; bestVis=vis; }
  }
  return best;
}
// 균열 i 를 선택하고 상세를 띄운 뒤, 그 균열을 검출한 가장 가까운 카메라 시점으로 날아간다.
// 멤버 카메라가 없으면(예외) 현 시선을 유지한 채 균열 크기에 비례한 거리에서 바라본다.
function flyToCrack(i){
  const h=DATA.hotspots[i]; if(!h) return;
  selectHotspot(i); showHotspotRep(h);
  const cam=nearestMemberCam(h);
  if(cam){ flyToCamera(cam); }                        // 그 크랙을 찍은 가장 가까운 카메라 시점으로(우측은 대표 크롭 유지)
  else {                                              // 멤버 카메라가 없으면(예외) 기존 방식 폴백
    const t=new THREE.Vector3(h.xyz[0],h.xyz[1],h.xyz[2]);
    const dist=Math.max(1.2, Math.min(10, ((h.length_mm||500)/1000)*0.9));
    const dir=camera.getWorldDirection(new THREE.Vector3());
    const p1=t.clone().addScaledVector(dir,-dist);
    const f=t.clone().sub(p1).normalize();
    anim={t:0, dur:0.7, p0:camera.position.clone(), p1,
          yaw0:camYaw, yaw1:Math.atan2(f.y,f.x),
          pitch0:camPitch, pitch1:clampPitch(Math.atan2(f.z,Math.hypot(f.x,f.y)))};
  }
}

// CSV: 현재 필터·정렬 결과를 그대로(보정된 mm) — Excel 한글 호환 BOM 포함
function clCsvText(){
  const fx=v=>v==null?'':(Math.round(v*1000)/1000);
  const head=['id','severity','severity_name','confidence','conf_band','review','review_note',
              'width_mm_median','width_mm_max','width_mm_p95','width_mm_3d','length_mm','length_total_mm',
              'views','conf','x','y','z','calib_scale','sources'];
  const lines=[head.join(',')];
  for(const [i,h] of clData){
    const _rv=revOf(i)||{};
    lines.push(['C-'+String(i+1).padStart(4,'0'),
      sevGrade(sevWidth(h))||'', (sevGrade(sevWidth(h))?SEV_NAME[sevGrade(sevWidth(h))]:''), (h.conf??''), (confBand(h)!=null?CONF_NAME[confBand(h)]:''),
      _rv.state||'', '"'+String(_rv.note||'').replace(/"/g,'""')+'"',
      fx(cmm(h.width_mm_median)), fx(cmm(h.width_mm_max)), fx(cmm(h.width_mm_p95)), fx(cmm(h.width_mm_3d)),
      fx(cmm(h.length_mm)), fx(ltot(h)), (h.views||[]).length, h.conf??'',
      ...wpt(h.xyz), calibScale,                       // CSV 는 보고서 원본(절대) 좌표로 내보낸다
      '"'+(h.views||[]).map(v=>v.source).join('|')+'"'].join(','));
  }
  return lines.join('\r\n');
}
document.getElementById('clCsv').onclick=()=>{
  const blob=new Blob(['﻿'+clCsvText()],{type:'text/csv;charset=utf-8'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
  a.download='crack_list'+(calibScale!==1?'_calibrated':'')+'.csv';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href), 3000);
  document.getElementById('clFoot').textContent='CSV 다운로드: '+clData.length.toLocaleString()+'행';
  toast('CSV 내보내기 · '+clData.length.toLocaleString()+'행');
};
document.getElementById('clCopy').onclick=async()=>{
  try{ await navigator.clipboard.writeText(clCsvText());
       document.getElementById('clFoot').textContent='클립보드에 복사됨 ('+clData.length.toLocaleString()+'행) — Excel 에 붙여넣기 가능';
       toast('클립보드에 복사됨 · '+clData.length.toLocaleString()+'행'); }
  catch(e){ document.getElementById('clFoot').textContent='복사 실패: '+e; }
};

addEventListener('resize',()=>{renderer.setSize(innerWidth,innerHeight);});
// 휠 줌: 현 이동 방식(WASD·드래그·스코프)은 그대로 두고, 휠은 '시선 방향 전진/후진'으로 줌.
// 위로 굴리면 전진(줌인), 아래로 후진. Shift 로 가속. 방향은 그대로(applyLook 이 유지).
renderer.domElement.addEventListener('wheel', e=>{
  e.preventDefault();
  cancelAnim();                                   // 진행 중 비행 취소
  const dir=camera.getWorldDirection(new THREE.Vector3());
  const sprint=(e.shiftKey?4:1);
  const step=(modelDiag||1)*0.004*sprint*(e.deltaY<0?1:-1);   // 모델 크기 비례 · 노치당 일정
  camera.position.addScaledVector(dir, step);
}, {passive:false});
document.getElementById('boot').style.display='none';   // 씬 구성 완료 → 로딩 표시 제거

// ================= 성능 계측 (F2) =================
// 프레임이 안 나오는 원인을 추측하지 않기 위해 단계별로 시간을 따로 잰다.
// draw call / 삼각형 수만 보면 원인을 잘못 짚기 쉽다(hammer 는 310 call · 2M 삼각형인데도 느리다).
const _st = {on:false, el:document.getElementById('stats'), n:0, t0:performance.now(),
             fps:0, acc:{hover:0, retic:0, hud:0, render:0, total:0}};
addEventListener('keydown', e => {
  if(e.key === 'F2'){ _st.on = !_st.on; _st.el.style.display = _st.on ? 'block' : 'none'; }
});
const _now = () => performance.now();
// 성능 오버레이를 0.5초마다 갱신한다(FPS·구간별 프레임 시간·드로우콜·삼각형 수).
// 최적화 작업의 근거를 눈으로 보기 위한 것 — 평상시엔 꺼 둔다.
function _statsTick(){
  _st.n++;
  const now = _now(), dt = now - _st.t0;
  if(dt >= 500){                                  // 0.5초마다 갱신
    const f = _st.n, a = _st.acc;
    _st.fps = f / (dt/1000);
    const r = renderer.info.render, mem = renderer.info.memory;
    _st.el.textContent =
      `FPS ${_st.fps.toFixed(1)}   frame ${(dt/f).toFixed(2)}ms\n` +
      `── 프레임당 ────────────\n` +
      `hover      ${(a.hover/f).toFixed(2)}ms\n` +
      `reticles   ${(a.retic/f).toFixed(2)}ms\n` +
      `hud(DOM)   ${(a.hud/f).toFixed(2)}ms\n` +
      `render     ${(a.render/f).toFixed(2)}ms\n` +
      `기타       ${((dt - a.total)/f).toFixed(2)}ms\n` +
      `── 씬 ──────────────────\n` +
      `draw calls ${r.calls}\n` +
      `triangles  ${r.triangles.toLocaleString()}\n` +
      `points     ${r.points.toLocaleString()}\n` +
      `geometries ${mem.geometries}   textures ${mem.textures}\n` +
      `pixelRatio ${renderer.getPixelRatio()}  ${renderer.domElement.width}×${renderer.domElement.height}`;
    _st.n = 0; _st.t0 = now;
    for(const k in a) a[k] = 0;
  }
}

// ================= WASD 자유 이동(플라이) 카메라 =================
// 시선(yaw/pitch)은 그대로 두고 카메라 위치만 옮긴다. 방향은 loop 의 applyLook() 이 매 프레임 다시 맞춘다.
// 속도는 모델 크기에 비례(모델이 622m든 50m든 체감 일정). Space/Ctrl 은 화면 시선과 무관하게 월드 상하.
const _keys = new Set();
const _MOVE = ['KeyW','KeyA','KeyS','KeyD','Space','ControlLeft','ControlRight'];
// 지금 입력 칸에 타이핑 중인가 — 맞으면 WASD 를 이동으로 가로채지 않는다.
function _typingTarget(){
  const t = document.activeElement, tag = t && t.tagName;
  return tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA';
}
addEventListener('keydown', e=>{
  if(_typingTarget()) return;
  if(_MOVE.includes(e.code)){ _keys.add(e.code); e.preventDefault(); }         // Space 페이지 스크롤 방지
  else if(e.code==='ShiftLeft'||e.code==='ShiftRight') _keys.add(e.code);      // 가속(다른 단축키 방해 X)
});
addEventListener('keyup', e=>{ _keys.delete(e.code); });
addEventListener('blur', ()=> _keys.clear());   // 포커스 이탈 시 눌린 키 잔류로 계속 움직이는 것 방지

const _fwd=new THREE.Vector3(), _right=new THREE.Vector3(), _move=new THREE.Vector3();
let _flyRamp = 0;                                 // 0→1 가속 램프
const FLY_ACCEL = 1.2;                            // 최대 속도까지 걸리는 시간(초)
const FLY_MIN = 0.05;                             // 시작 속도 = 최대의 5% (천천히 출발)
// 눌린 키에 따라 카메라 위치를 옮긴다(방향은 건드리지 않는다 — applyLook 이 매 프레임 유지).
// 멈췄다 다시 누르면 램프가 0부터라 항상 천천히 출발한다. Shift 는 4배 가속.
function updateFly(dt){
  const f = (_keys.has('KeyW')?1:0) - (_keys.has('KeyS')?1:0);
  const r = (_keys.has('KeyD')?1:0) - (_keys.has('KeyA')?1:0);
  const u = (_keys.has('Space')?1:0) - ((_keys.has('ControlLeft')||_keys.has('ControlRight'))?1:0);
  if(!f && !r && !u){ _flyRamp = 0; return; }     // 멈추면 램프 리셋 → 다음엔 다시 천천히 출발
  _flyRamp = Math.min(1, _flyRamp + dt/FLY_ACCEL);
  cancelAnim();                                   // 이동 시작 → 진행 중 비행 취소
  camera.getWorldDirection(_fwd);                 // 시선 방향
  _right.crossVectors(_fwd, camera.up).normalize();
  _move.set(0,0,0).addScaledVector(_fwd, f).addScaledVector(_right, r);
  _move.z += u;                                   // 상하는 월드 Z(카메라가 어딜 보든 위/아래)
  if(_move.lengthSq() === 0) return;
  _move.normalize();                              // 대각 이동이 빨라지지 않게
  const sprint = (_keys.has('ShiftLeft')||_keys.has('ShiftRight')) ? 4 : 1;
  const speed = FLY_MIN + (1 - FLY_MIN) * _flyRamp;            // 처음 느리게 → 가속 → 최대
  _move.multiplyScalar((modelDiag||1) * 0.15 * dt * sprint * speed);   // 모델 크기 비례 속도
  camera.position.add(_move);                     // 위치만 이동(방향은 applyLook 이 유지)
}

// ================= 토스트 알림 =================
let _toastT=null;
// 화면 하단에 잠깐 뜨는 알림(같은 요소를 재사용하므로 연달아 부르면 마지막 것만 남는다).
function toast(msg, ms=2200){
  const el=document.getElementById('toast');
  el.innerHTML='<svg class="ic"><use href="#i-check"/></svg><span></span>';
  el.querySelector('span').textContent=msg;
  el.classList.add('show');
  clearTimeout(_toastT); _toastT=setTimeout(()=>el.classList.remove('show'), ms);
}

// ================= 방향 기즈모(우하단) =================
// 카메라 회전에 따라 XYZ 축이 도는 미니 위젯. 각 축 라벨 클릭 → 그 방향에서 모델 중심을 보는 뷰로.
const GZ = (function(){
  const host=document.getElementById('gizmo'), C=44, R=30;
  const AX=[ {k:'x',v:new THREE.Vector3(1,0,0),col:'#ff5a72',lab:'X'},
             {k:'y',v:new THREE.Vector3(0,1,0),col:'#5ad46a',lab:'Y'},
             {k:'z',v:new THREE.Vector3(0,0,1),col:'#4aa8ff',lab:'Z'} ];
  const NS='http://www.w3.org/2000/svg';
  const svg=document.createElementNS(NS,'svg'); svg.setAttribute('viewBox','0 0 88 88'); host.appendChild(svg);
  const els=[];
  // SVG 요소 하나를 속성과 함께 만든다(기즈모 전용 축약 헬퍼).
  function mk(t,a){ const e=document.createElementNS(NS,t); for(const k in a) e.setAttribute(k,a[k]); return e; }
  // 각 축마다 +/- 두 방향 노드(양=채움 라벨, 음=빈 원)
  const nodes=[];
  for(const ax of AX){
    for(const s of [1,-1]){
      const line = s>0 ? mk('line',{stroke:ax.col,'stroke-width':2,'stroke-linecap':'round'}) : null;
      if(line) svg.appendChild(line);
      const g=mk('g',{class:'gzl',cursor:'pointer'});
      const circ=mk('circle',{r:9, fill:s>0?ax.col:'#0e1319', stroke:ax.col, 'stroke-width':1.5});
      const txt=mk('text',{'text-anchor':'middle','dominant-baseline':'central',
        'font-size':11,'font-weight':600, fill:s>0?'#0e1319':ax.col});
      txt.textContent = (s<0?'-':'')+ax.lab;
      g.appendChild(circ); g.appendChild(txt); svg.appendChild(g);
      g.addEventListener('click', e=>{ e.stopPropagation(); snapView(ax.v.clone().multiplyScalar(s)); });
      nodes.push({ax,s,line,g,circ,txt});
    }
  }
  // 현재 카메라 자세로 축 노드 위치를 다시 계산해 그린다(매 프레임).
  // z 순으로 재정렬해 앞쪽 축이 위에 오게 하고, 뒤쪽은 흐리게 표시한다.
  function update(){
    const q=camera.quaternion.clone().invert();      // 월드축 → 카메라(뷰) 좌표
    const proj=nodes.map(n=>{ const p=n.ax.v.clone().multiplyScalar(n.s).applyQuaternion(q);
      return {n, x:C+p.x*R, y:C-p.y*R, z:p.z}; });
    proj.sort((a,b)=>a.z-b.z);                        // 뒤(z작음) 먼저 그려 앞이 위로
    for(const p of proj){
      const n=p.n;
      if(n.line){
        svg.appendChild(n.line);                     // z순으로 재정렬(appendChild=이동)
        n.line.setAttribute('x1',C); n.line.setAttribute('y1',C);
        n.line.setAttribute('x2',p.x); n.line.setAttribute('y2',p.y);
        n.line.setAttribute('opacity', p.z>0?1:0.4);
      }
      svg.appendChild(n.g);
      n.g.setAttribute('transform',`translate(${p.x},${p.y})`);
      n.g.setAttribute('opacity', p.z>0?1:0.55);
      n.circ.setAttribute('r', n.s>0?9:7);
    }
  }
  return {update};
})();
function snapView(dirWorld){                          // 모델 중심을 dirWorld 쪽에서 바라보는 뷰로 비행
  const d=(modelDiag||10)*0.6;
  const from=sceneCenter.clone().addScaledVector(dirWorld.normalize(), d);
  const f=sceneCenter.clone().sub(from).normalize();
  anim={t:0, dur:0.6, p0:camera.position.clone(), p1:from,
        yaw0:camYaw, yaw1:Math.atan2(f.y,f.x),
        pitch0:camPitch, pitch1:clampPitch(Math.atan2(f.z,Math.hypot(f.x,f.y)))};
}

const _clock=new THREE.Clock();
const _camPrev = new THREE.Matrix4(); let _camIdle = 9;   // 카메라가 멈춘 뒤 흐른 시간(초) — 타일 문턱 완화용
(function loop(){
  requestAnimationFrame(loop);
  const _f0 = _st.on ? _now() : 0;
  const _dt = _clock.getDelta();
  tickAnim(_dt); updateFly(_dt);
  // 우클릭 스코프: fov 를 목표값으로 부드럽게(비행 중이 아닐 때만 스코프가 fov 관리)
  if(!anim){
    const fovT = scoping ? SCOPE_FOV : BASE_FOV;
    if(Math.abs(camera.fov-fovT) > 0.05){ camera.fov += (fovT-camera.fov)*Math.min(1,_dt*12); camera.updateProjectionMatrix(); }
  }
  applyLook();                          // yaw/pitch(+이동된 위치)를 카메라에 반영 · up 항상 Z
  if(tiles){
    // 보이는 타일 선택·요청·해제. 카메라가 움직이는 동안(멈춘 뒤 0.4초까지)은 거칠게 그린다.
    camera.updateMatrixWorld();
    const moved = !camera.matrixWorld.equals(_camPrev);
    if(moved) _camPrev.copy(camera.matrixWorld);
    _camIdle = moved ? 0 : _camIdle + _dt;
    tiles.update(camera, renderer.domElement.clientHeight || innerHeight, _camIdle < 0.4);
  }
  GZ.update();                          // 방향 기즈모 회전 동기화
  let t = _st.on ? _now() : 0;
  updateHover();                       if(_st.on){ _st.acc.hover += _now()-t; t = _now(); }
  keepConstantSize(); if(_st.on){ _st.acc.retic += _now()-t; t = _now(); }
  updateHUD();                         if(_st.on){ _st.acc.hud   += _now()-t; }
  const cw=renderer.domElement.clientWidth||innerWidth, ch=renderer.domElement.clientHeight||innerHeight;
  const _r0 = _st.on ? _now() : 0;
  const a=cw/ch;
  if(Math.abs(camera.aspect-a)>1e-4){ camera.aspect=a; camera.updateProjectionMatrix(); }
  renderer.setViewport(0,0,cw,ch);
  renderer.render(scene,camera);
  if(_st.on){
    // 주의: render() 는 GPU 작업을 '제출'만 하므로 이 값은 CPU 측 제출 시간이다.
    // GPU 가 병목이면 여기가 아니라 '기타'(rAF 대기)로 잡힌다.
    _st.acc.render += _now()-_r0;
    _st.acc.total  += _now()-_f0;
    _statsTick();
  }
})();
