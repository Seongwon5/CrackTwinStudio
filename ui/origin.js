// origin.js — 큰 절대좌표(UTM)를 국소좌표로 옮겨 float32 정밀도를 확보한다.
//
// PLY 와 meta 는 UTM 절대좌표(예: Y≈4,113,800)를 double 로 담는데, three.js 는 위치를
// Float32BufferAttribute 로만 GPU 에 올린다. float32 의 눈금 간격은 값 크기에 비례하므로
//   X≈3.0e5 → 0.031 m      Y≈4.11e6 → 0.25 m      Z≈10¹ → 0.00002 m
// 축마다 다르게 뭉개진다. 삼각형 변(≈57 mm)보다 큰 단차라 균일한 잡음이 아니라 한 방향으로
// 계단이 지고, 그 위에서 돌아가는 것들이 함께 어긋난다.
//
//   실측(100MEDIA, 정점 200만) — float32 Y 고유값 1,607개뿐(= 계단 1,607층),
//   벽면 평면 잔차 14.8 → 46.9 mm, 카메라 위치 변위 중앙 64 mm.
//
// 이 브랜치에서는 보이는 것만의 문제가 아니다. BVH 도 faceNormal 도 float32 정점 배열을
// 그대로 읽으므로 클릭 지점·면 법선이 최대 0.125 m 흔들리고, 가림 검사 허용오차(근거리에서
// 0.136 m)와 같은 자릿수라 멀쩡한 카메라가 가려진 것으로 판정될 수 있다. 게다가 메시는
// float32 인데 cameras[].pos 는 meta 의 double 이라 둘이 어긋난 채로 투영된다 —
// 0.125 m 는 촬영거리 13.6 m·GSD 4.55 mm/px 사진에서 27 px 이다(붉은 점이 그만큼 빗나간다).
//
// 그래서 PLYLoader 에 넘기기 **전에** 메시 바이너리와 meta 좌표에서 같은 원점을 뺀다.
// PLY 는 고정 스트라이드 바이너리라 헤더만 읽으면 제자리 수정이 가능해 커스텀 파서가 필요 없다.
// 원점 근처(±300 m)면 눈금이 0.003 mm 로 떨어져 사실상 무손실이다.
//
// .twin 파일은 건드리지 않는다 — 메모리 안에서만 옮기므로 기존 보고서가 그대로 열린다.
// 길이·거리·각도는 차분량이라 이동과 무관하다. 절대좌표를 화면에 내보낼 일이 생기면
// 돌려받은 origin 을 다시 더하면 된다(현재 이 브랜치는 좌표를 표시하지 않는다).

const PLY_SZ = {char:1, uchar:1, int8:1, uint8:1, short:2, ushort:2, int16:2, uint16:2,
                int:4, uint:4, int32:4, uint32:4, float:4, float32:4, double:8, float64:8};

// PLY 헤더를 읽어 vertex 블록의 x/y/z 바이트 위치를 찾는다.
// 이동을 안전하게 적용할 수 없는 형식이면 null (호출측은 원본 그대로 진행).
function plyVertexLayout(buf) {
  const head = new TextDecoder('latin1').decode(
    new Uint8Array(buf, 0, Math.min(buf.byteLength, 1 << 16)));
  const m = /end_header[ \t]*\r?\n/.exec(head);
  if (!m) return null;
  const lines = head.slice(0, m.index).split(/\r?\n/).map(s => s.trim());
  const fmt = lines.find(l => l.startsWith('format '));
  if (!fmt || !/^format\s+binary_little_endian/.test(fmt)) return null;   // ascii·big-endian 미지원
  // 첫 element 가 vertex 여야 한다 — 앞에 다른 요소가 있으면 시작 오프셋을 확정할 수 없다.
  const ei = lines.findIndex(l => l.startsWith('element '));
  if (ei < 0) return null;
  const ep = lines[ei].split(/\s+/);
  if (ep[1] !== 'vertex') return null;
  const count = parseInt(ep[2], 10);
  if (!(count > 0)) return null;
  let off = 0; const at = {}, ty = {};
  for (let i = ei + 1; i < lines.length; i++) {
    const l = lines[i];
    if (l.startsWith('element ')) break;                 // vertex 요소 끝
    if (!l.startsWith('property ')) continue;
    const p = l.split(/\s+/);
    if (p[1] === 'list') return null;                    // 가변 길이 → 고정 스트라이드 불가
    const sz = PLY_SZ[p[1]];
    if (!sz) return null;                                // 모르는 타입 → 안전하게 포기
    if (p[2] === 'x' || p[2] === 'y' || p[2] === 'z') { at[p[2]] = off; ty[p[2]] = p[1]; }
    off += sz;
  }
  if (at.x == null || at.y == null || at.z == null) return null;
  if (ty.x !== ty.y || ty.y !== ty.z) return null;       // 축마다 타입이 다르면 미지원
  const dbl = (ty.x === 'double' || ty.x === 'float64');
  if (!dbl && !(ty.x === 'float' || ty.x === 'float32')) return null;
  const dataStart = m.index + m[0].length;
  if (dataStart + count * off > buf.byteLength) return null;   // 헤더와 실제 크기가 어긋남
  return { dataStart, count, stride: off, off: [at.x, at.y, at.z], dbl };
}

// vertex 를 성글게 훑어 경계상자 중심(정수 m)을 구한다. 원점은 대충 가운데면 충분하므로
// 최대 20만 점만 표본으로 본다(GB 급 메시에서 전수 조회를 피한다).
function plyOrigin(buf, L) {
  const dv = new DataView(buf), get = L.dbl ? 'getFloat64' : 'getFloat32';
  const step = Math.max(1, Math.floor(L.count / 200000));
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < L.count; i += step) {
    const b = L.dataStart + i * L.stride;
    for (let k = 0; k < 3; k++) {
      const v = dv[get](b + L.off[k], true);
      if (v < lo[k]) lo[k] = v;
      if (v > hi[k]) hi[k] = v;
    }
  }
  return lo.map((v, k) => Math.round((v + hi[k]) / 2));
}

// vertex 블록의 x/y/z 에서 원점을 뺀다(버퍼 제자리 수정).
function plyShift(buf, L, O) {
  const dv = new DataView(buf);
  const get = L.dbl ? 'getFloat64' : 'getFloat32', set = L.dbl ? 'setFloat64' : 'setFloat32';
  for (let i = 0; i < L.count; i++) {
    const b = L.dataStart + i * L.stride;
    for (let k = 0; k < 3; k++) {
      const a = b + L.off[k];
      dv[set](a, dv[get](a, true) - O[k], true);
    }
  }
}

/**
 * 메시 바이너리와 meta 좌표를 같은 원점만큼 옮긴다. **PLYLoader 로 파싱하기 전에** 부른다.
 * @param {ArrayBuffer} meshBuf  .twin 의 mesh.ply — 제자리에서 수정된다
 * @param {object} DATA          meta.json — cameras/ribbons/hotspots/text_points 가 제자리 수정된다
 * @returns {number[]}           뺀 원점 [x,y,z]. 더하면 원래(절대) 좌표로 돌아간다. 이동 없으면 [0,0,0]
 */
export function shiftToLocalOrigin(meshBuf, DATA) {
  const L = plyVertexLayout(meshBuf);
  if (!L) {
    console.warn('[좌표] PLY 형식을 해석할 수 없어 원점 이동을 건너뜁니다. ' +
                 '절대좌표가 크면 표면이 계단처럼 보이고 클릭 지점이 흔들릴 수 있습니다.');
    return [0, 0, 0];
  }
  const O = plyOrigin(meshBuf, L);
  if (!O.every(Number.isFinite)) return [0, 0, 0];
  if (Math.max(...O.map(Math.abs)) < 1024) return [0, 0, 0];   // 이미 국소좌표 — 옮길 이유가 없다
  plyShift(meshBuf, L, O);
  const sub = p => { p[0] -= O[0]; p[1] -= O[1]; p[2] -= O[2]; };
  // 방향 벡터(cameras[].fwd/up/right, ribbons[].poly_n)는 이동 대상이 아니라 건드리지 않는다.
  // ribbons/hotspots/text_points 는 이 브랜치가 쓰지 않지만, main 에서 만든 .twin 도 열리므로
  // 좌표계를 하나로 유지해 둔다.
  const D = DATA || {};
  (D.cameras || []).forEach(c => sub(c.pos));
  (D.ribbons || []).forEach(r => (r.poly || []).forEach(sub));
  (D.hotspots || []).forEach(h => { if (h.xyz) sub(h.xyz); if (h.poly) h.poly.forEach(sub); });
  (D.text_points || []).forEach(sub);
  console.info('[좌표] 원점 이동 적용:', O.join(', '));
  return O;
}
