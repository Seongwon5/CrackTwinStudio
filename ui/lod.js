// lod.js — 조작 중에 쓸 간략 메시(LOD)를 만든다. 정점 버퍼는 그대로 두고 인덱스만 새로 만든다.
//
// 렌더 비용을 재 보니 삼각형 수에 완전히 선형이고 화면 해상도와는 무관했다
// (damcrack 400만 면 · 해상도 고정: 400만 5.69ms · 200만 3.61 · 100만 2.26 · 40만 0.63ms /
//  삼각형 고정 · 픽셀 9배: 7.11 → 7.17 → 4.63ms 로 변화 없음).
// 즉 폰에서 버벅이는 원인은 픽셀이 아니라 정점이고, 해상도를 낮춰 봐야 소용이 없다.
// 줄일 수 있는 건 삼각형 수뿐이다.
//
// 격자 병합 방식이되, 대표 정점을 새로 만들지 않고 **그 칸에 처음 들어온 실제 정점**으로
// 삼는다. 그래서 position·color 버퍼를 원본과 공유하고 인덱스만 따로 가지면 된다
// (메모리 추가 없음, 구축 0.2초 이내).
//
// 실측(damcrack 400만 면): 격자 512 → 67만 면(17%) · 구축 166ms · 렌더 9.58ms → 1.49ms.
// 화질 손해는 조작 중에만 생기고, 손을 떼면 원본으로 되돌린다.

const MAX_GRID = 1023;        // 칸 좌표를 30비트(10+10+10)에 담기 위한 상한

/**
 * @param {Float32Array} position  정점 좌표
 * @param {Uint32Array|Uint16Array} index  원본 면 인덱스
 * @param {{min:{x,y,z}, max:{x,y,z}}} box  경계 상자
 * @param {number} targetTris  목표 삼각형 수
 * @returns {{index: Uint32Array, triangles: number, grid: number}|null}  줄일 필요가 없으면 null
 */
export function buildLOD(position, index, box, targetTris) {
  const nV = position.length / 3, nF = index.length / 3;
  if (nF <= targetTris * 1.2) return null;          // 이미 충분히 가볍다

  // 해시 테이블 — 표면 메시라 점유 칸은 정점 수를 넘지 않는다.
  let cap = 1;
  while (cap < nV * 2) cap <<= 1;
  const mask = cap - 1;
  let keys = new Int32Array(cap);
  let vals = new Uint32Array(cap);
  const rep = new Uint32Array(nV);

  const ox = box.min.x, oy = box.min.y, oz = box.min.z;
  const ex = Math.max(box.max.x - ox, 1e-9), ey = Math.max(box.max.y - oy, 1e-9), ez = Math.max(box.max.z - oz, 1e-9);

  function pass(N) {
    keys.fill(-1);
    const sx = (N - 1) / ex, sy = (N - 1) / ey, sz = (N - 1) / ez;
    for (let v = 0; v < nV; v++) {
      const i = v * 3;
      const cx = (position[i] - ox) * sx | 0;
      const cy = (position[i + 1] - oy) * sy | 0;
      const cz = (position[i + 2] - oz) * sz | 0;
      const key = (cx << 20) | (cy << 10) | cz;     // 빈 칸 표시(-1)와 겹치지 않는다(항상 ≥ 0)
      let h = (Math.imul(cx, 73856093) ^ Math.imul(cy, 19349663) ^ Math.imul(cz, 83492791)) & mask;
      for (;;) {
        const k = keys[h];
        if (k === -1) { keys[h] = key; vals[h] = v; rep[v] = v; break; }
        if (k === key) { rep[v] = vals[h]; break; }
        h = (h + 1) & mask;
      }
    }
    // 대표 정점이 겹쳐 납작해진 삼각형은 버린다
    const out = new Uint32Array(index.length);
    let n = 0;
    for (let f = 0; f < nF; f++) {
      const a = rep[index[f * 3]], b = rep[index[f * 3 + 1]], c = rep[index[f * 3 + 2]];
      if (a === b || b === c || a === c) continue;
      out[n++] = a; out[n++] = b; out[n++] = c;
    }
    return { index: out.slice(0, n), triangles: n / 3, grid: N };
  }

  // 표면이라 삼각형 수는 대략 격자의 제곱에 비례한다 → 한 번 재 보고 한 번만 보정한다.
  let best = pass(512);
  if (best.triangles < targetTris * 0.6 || best.triangles > targetTris * 1.7) {
    const scale = Math.sqrt(targetTris / Math.max(best.triangles, 1));
    const N = Math.max(32, Math.min(MAX_GRID, Math.round(512 * scale)));
    if (N !== 512) {
      const alt = pass(N);
      // 목표에 더 가까운 쪽을 쓴다
      if (Math.abs(alt.triangles - targetTris) < Math.abs(best.triangles - targetTris)) best = alt;
    }
  }
  keys = null; vals = null;                          // 큰 임시 버퍼는 바로 놓아준다
  return best;
}
