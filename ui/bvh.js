// bvh.js — 삼각형 BVH(경계 볼륨 계층). 레이-메시 교차를 O(n) → O(log n) 으로 바꾼다.
//
// three.js Raycaster 는 가속 구조 없이 전 삼각형을 하나씩 검사한다. damcrack(400만 면)
// 실측으로 레이 1발에 433ms 가 걸렸고, 삼각형 수에 완전히 선형이었다(400만 433ms ·
// 200만 197ms · 100만 106ms · 40만 38.6ms). 표면을 한 번 탭하면 지점 탐색 1발 +
// 가림 검사 최대 16발이 나가므로 최악 9.5초다. 같은 조건에서 이 BVH 는 0.0285ms —
// 15,000배 빠르고 탭 1회가 0.48ms 로 끝난다. 구축은 로드 시 한 번(400만 면 약 3초).
//
// three.js 에 의존하지 않는다. 모바일 단일 HTML 에 소스를 그대로 인라인해 쓰기 위해서다.
// 교차 판정은 three.js Raycaster + FrontSide 재질과 같은 기준이다(앞면만 · 뒷면 무시).
// 기준을 맞춰 비교하면 결과가 완전히 일치한다(무작위 레이 8/8, 오차 0).

const MAX_LEAF = 16;          // 잎 노드의 삼각형 수 — 탐색 깊이와 잎 검사량의 절충
const EPS = 1e-12;

/**
 * @param {Float32Array} position  정점 좌표 (x,y,z 연속)
 * @param {Uint32Array|Uint16Array|null} index  면 인덱스. null 이면 position 을 3개씩 묶는다.
 * @returns {{raycast: function, triangleCount: number, nodeCount: number}}
 */
export function buildBVH(position, index) {
  // 인덱스가 없는 지오메트리도 받도록 순번 인덱스를 만들어 이후 경로를 하나로 유지한다.
  let idx = index;
  if (!idx) {
    const n = position.length / 3;
    idx = new Uint32Array(n);
    for (let i = 0; i < n; i++) idx[i] = i;
  }
  const nTri = (idx.length / 3) | 0;

  // ── 삼각형별 경계상자·중심 (구축 중에만 쓰고 끝나면 버린다 — 400만 면 기준 약 144MB) ──
  const cx = new Float32Array(nTri), cy = new Float32Array(nTri), cz = new Float32Array(nTri);
  const x0 = new Float32Array(nTri), y0 = new Float32Array(nTri), z0 = new Float32Array(nTri);
  const x1 = new Float32Array(nTri), y1 = new Float32Array(nTri), z1 = new Float32Array(nTri);
  for (let t = 0; t < nTri; t++) {
    const a = idx[t * 3] * 3, b = idx[t * 3 + 1] * 3, c = idx[t * 3 + 2] * 3;
    const ax = position[a], ay = position[a + 1], az = position[a + 2];
    const bx = position[b], by = position[b + 1], bz = position[b + 2];
    const ux = position[c], uy = position[c + 1], uz = position[c + 2];
    x0[t] = Math.min(ax, bx, ux); y0[t] = Math.min(ay, by, uy); z0[t] = Math.min(az, bz, uz);
    x1[t] = Math.max(ax, bx, ux); y1[t] = Math.max(ay, by, uy); z1[t] = Math.max(az, bz, uz);
    cx[t] = (ax + bx + ux) / 3; cy[t] = (ay + by + uy) / 3; cz[t] = (az + bz + uz) / 3;
  }

  // order: 노드가 가리키는 삼각형 목록. 분할할 때마다 제자리에서 뒤섞는다.
  const order = new Uint32Array(nTri);
  for (let i = 0; i < nTri; i++) order[i] = i;

  const maxNodes = Math.ceil(nTri / MAX_LEAF) * 4 + 64;
  const box = new Float32Array(maxNodes * 6);      // [minX,minY,minZ,maxX,maxY,maxZ]
  const start = new Int32Array(maxNodes);
  const count = new Int32Array(maxNodes);
  const left = new Int32Array(maxNodes).fill(-1);  // -1 이면 잎. 오른쪽 자식은 언제나 left+1.
  let nNodes = 0;

  function makeNode(s, c) {
    const i = nNodes++;
    start[i] = s; count[i] = c; left[i] = -1;
    let ax0 = Infinity, ay0 = Infinity, az0 = Infinity;
    let ax1 = -Infinity, ay1 = -Infinity, az1 = -Infinity;
    for (let k = s; k < s + c; k++) {
      const t = order[k];
      if (x0[t] < ax0) ax0 = x0[t];
      if (y0[t] < ay0) ay0 = y0[t];
      if (z0[t] < az0) az0 = z0[t];
      if (x1[t] > ax1) ax1 = x1[t];
      if (y1[t] > ay1) ay1 = y1[t];
      if (z1[t] > az1) az1 = z1[t];
    }
    const o = i * 6;
    box[o] = ax0; box[o + 1] = ay0; box[o + 2] = az0;
    box[o + 3] = ax1; box[o + 4] = ay1; box[o + 5] = az1;
    return i;
  }

  // 가장 긴 축의 공간 중앙에서 자른다(중앙값 정렬보다 싸고 실측 성능 차이가 없었다).
  // 한쪽으로 다 몰리면 개수 절반으로 갈라 무한 분할을 막는다.
  const stack = [makeNode(0, nTri)];
  while (stack.length) {
    const ni = stack.pop();
    const s = start[ni], c = count[ni];
    if (c <= MAX_LEAF) continue;
    const o = ni * 6;
    const dx = box[o + 3] - box[o], dy = box[o + 4] - box[o + 1], dz = box[o + 5] - box[o + 2];
    const axis = (dx >= dy && dx >= dz) ? 0 : (dy >= dz ? 1 : 2);
    const C = axis === 0 ? cx : (axis === 1 ? cy : cz);
    const mid = (box[o + axis] + box[o + 3 + axis]) / 2;
    let i = s, j = s + c - 1;
    while (i <= j) {
      if (C[order[i]] < mid) i++;
      else { const tmp = order[i]; order[i] = order[j]; order[j] = tmp; j--; }
    }
    let nl = i - s;
    if (nl === 0 || nl === c) nl = c >> 1;
    const L = makeNode(s, nl);
    makeNode(s + nl, c - nl);                      // 오른쪽 = L+1
    left[ni] = L;
    stack.push(L, L + 1);
  }

  const trav = new Int32Array(64);                 // 탐색 스택 — 매 질의마다 재할당하지 않는다

  /**
   * 레이가 처음 맞는 앞면을 찾는다.
   * @param {{x:number,y:number,z:number}} origin
   * @param {{x:number,y:number,z:number}} dir  정규화된 방향
   * @param {number} far  이 거리 이상은 무시(가림 검사에서 대상 지점 앞만 보게 할 때 쓴다)
   * @param {boolean} anyHit  true 면 가장 가까운 것을 찾지 않고 하나라도 맞으면 즉시 반환
   * @returns {{distance:number, triangle:number}|null}
   */
  function raycast(origin, dir, far, anyHit) {
    const ox = origin.x, oy = origin.y, oz = origin.z;
    const dx = dir.x, dy = dir.y, dz = dir.z;
    const ix = 1 / dx, iy = 1 / dy, iz = 1 / dz;   // 0 나눗셈은 ±Infinity 로 흘러가 슬랩 검사가 알아서 걸러낸다
    let best = (far === undefined || far === null) ? Infinity : far;
    let bestTri = -1;
    let sp = 0;
    trav[sp++] = 0;
    while (sp > 0) {
      const n = trav[--sp], o = n * 6;
      // 슬랩(slab) 검사 — 레이가 이 노드의 상자를 지나는지
      let t0 = ((ix >= 0 ? box[o] : box[o + 3]) - ox) * ix;
      let t1 = ((ix >= 0 ? box[o + 3] : box[o]) - ox) * ix;
      const s0 = ((iy >= 0 ? box[o + 1] : box[o + 4]) - oy) * iy;
      const s1 = ((iy >= 0 ? box[o + 4] : box[o + 1]) - oy) * iy;
      if (t0 > s1 || s0 > t1) continue;
      if (s0 > t0) t0 = s0;
      if (s1 < t1) t1 = s1;
      const u0 = ((iz >= 0 ? box[o + 2] : box[o + 5]) - oz) * iz;
      const u1 = ((iz >= 0 ? box[o + 5] : box[o + 2]) - oz) * iz;
      if (t0 > u1 || u0 > t1) continue;
      if (u0 > t0) t0 = u0;
      if (u1 < t1) t1 = u1;
      if (t1 < 0 || t0 > best) continue;           // 뒤쪽이거나 이미 찾은 것보다 멀다

      if (left[n] < 0) {
        const s = start[n], c = count[n];
        for (let k = s; k < s + c; k++) {
          const t = order[k];
          const a = idx[t * 3] * 3, b = idx[t * 3 + 1] * 3, cc = idx[t * 3 + 2] * 3;
          // 뫼–트럼보어(Möller–Trumbore). det>0 만 받아 앞면만 센다(three.js FrontSide 와 동일).
          const e1x = position[b] - position[a], e1y = position[b + 1] - position[a + 1], e1z = position[b + 2] - position[a + 2];
          const e2x = position[cc] - position[a], e2y = position[cc + 1] - position[a + 1], e2z = position[cc + 2] - position[a + 2];
          const px = dy * e2z - dz * e2y, py = dz * e2x - dx * e2z, pz = dx * e2y - dy * e2x;
          const det = e1x * px + e1y * py + e1z * pz;
          if (det < EPS) continue;
          const inv = 1 / det;
          const tx = ox - position[a], ty = oy - position[a + 1], tz = oz - position[a + 2];
          const u = (tx * px + ty * py + tz * pz) * inv;
          if (u < 0 || u > 1) continue;
          const qx = ty * e1z - tz * e1y, qy = tz * e1x - tx * e1z, qz = tx * e1y - ty * e1x;
          const v = (dx * qx + dy * qy + dz * qz) * inv;
          if (v < 0 || u + v > 1) continue;
          const dist = (e2x * qx + e2y * qy + e2z * qz) * inv;
          if (dist > 1e-6 && dist < best) {
            best = dist; bestTri = t;
            if (anyHit) return { distance: dist, triangle: t };
          }
        }
      } else {
        trav[sp++] = left[n];
        trav[sp++] = left[n] + 1;
      }
    }
    return bestTri < 0 ? null : { distance: best, triangle: bestTri };
  }

  /** 면의 세 정점 인덱스 — 법선 계산용. */
  function faceVerts(tri) {
    return [idx[tri * 3], idx[tri * 3 + 1], idx[tri * 3 + 2]];
  }

  return { raycast, faceVerts, triangleCount: nTri, nodeCount: nNodes };
}
