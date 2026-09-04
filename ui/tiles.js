// tiles.js — 계층 LOD 타일 메시 런타임. 보이는 타일만 · 거리에 맞는 해상도로 받아 그린다.
//
// 왜 — 6GB(1억 9천만 면) 메시는 브라우저 메모리에도 GPU 에도 통째로 들어가지 않는다.
// tiler.py 가 메시를 옥트리 타일로 쪼개 두면(노드마다 그 영역을 요약한 메시), 뷰어는
//   · 카메라 절두체 밖 노드는 건드리지 않고
//   · 노드의 기하 오차가 화면에서 문턱(px)보다 크게 보이면 자식 8개로 바꿔 그리고(정제)
//   · 자식이 아직 안 왔으면 부모를 그대로 두며(구멍 없음)
//   · 오래 안 쓴 타일은 메모리 예산에 맞춰 버린다.
// 화면 삼각형 합계가 예산을 넘으면 문턱을 조금 올리고, 여유가 생기면 내린다 — 어떤 PC 든
// 프레임이 무너지지 않는다. (3D Tiles 의 REPLACE 정제와 같은 구조.)
//
// three.js 를 import 하지 않고 인자로 받는다 — 모바일 단일 HTML 에 소스를 그대로 인라인해
// 쓰기 위해서다(bvh.js 와 같은 이유). 클릭용 BVH 는 실제로 광선이 지나는 타일에만 그때
// 만들고 붙여 둔다(타일당 10만 면 ≈ 0.1초, 이후 재사용).
//
// 기존 .twin(mesh.ply 하나)도 같은 경로로 연다 — legacySource() 가 간략 메시(루트) + 원본
// (자식) 두 단계 타일셋으로 감싼다. 조작 중엔 문턱을 올려 간략 메시가 그려지고, 멈추면
// 원본으로 돌아온다(종전 모바일 뷰어의 동작과 같다).

const MAGIC = 0x31545754;   // 'TWT1' little-endian

/**
 * tiles/<id>.bin → BufferGeometry (형식은 tiler.py 머리말 참조).
 * @param {object} THREE
 * @param {ArrayBuffer} buf
 * @param {boolean} wantNormals  색이 없어 조명을 쓸 때 법선을 계산한다
 */
export function parseTile(THREE, buf, wantNormals) {
  const dv = new DataView(buf);
  if (buf.byteLength < 16 || dv.getUint32(0, true) !== MAGIC) throw new Error('타일 형식 오류');
  const nV = dv.getUint32(4, true), nF = dv.getUint32(8, true), flags = dv.getUint32(12, true);
  const hasColor = !!(flags & 1), idx16 = !!(flags & 2);
  let off = 16;
  const pos = new Float32Array(buf, off, nV * 3); off += nV * 12;
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  if (hasColor) {
    const col = new Uint8Array(buf, off, nV * 3); off += nV * 3; off += (4 - (off & 3)) & 3;
    geo.setAttribute('color', new THREE.BufferAttribute(col, 3, true));
  }
  const idx = idx16 ? new Uint16Array(buf, off, nF * 3) : new Uint32Array(buf, off, nF * 3);
  geo.setIndex(new THREE.BufferAttribute(idx, 1));
  if (wantNormals) geo.computeVertexNormals();
  return geo;
}

/**
 * 기존 보고서(mesh.ply 하나) → 두 단계 타일셋 소스. 루트 = 격자 병합 간략 메시, 자식 = 원본.
 * @param {object} THREE
 * @param {function} buildLOD  lod.js
 * @param {BufferGeometry} geo  PLYLoader 결과(원점 이동 완료)
 * @param {number} coarseTris  간략 메시 목표 삼각형 수
 */
export function legacySource(THREE, buildLOD, geo, coarseTris) {
  geo.computeBoundingBox();
  const bb = geo.boundingBox;
  const b = [bb.min.x, bb.min.y, bb.min.z, bb.max.x, bb.max.y, bb.max.z];
  const tris = geo.index ? geo.index.count / 3 : geo.attributes.position.count / 3;
  const full = { b, e: 0, t: tris, v: geo.attributes.position.count, c: [] };
  const geos = [geo];
  const nodes = [full];
  let root = 0;
  const lod = geo.index ? buildLOD(geo.attributes.position.array, geo.index.array, bb, coarseTris) : null;
  if (lod) {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', geo.attributes.position);        // 버퍼 공유 — 메모리 추가 없음
    if (geo.hasAttribute('color')) g.setAttribute('color', geo.attributes.color);
    if (geo.hasAttribute('normal')) g.setAttribute('normal', geo.attributes.normal);
    g.setIndex(new THREE.BufferAttribute(lod.index, 1));
    const ext = Math.max(bb.max.x - bb.min.x, bb.max.y - bb.min.y, bb.max.z - bb.min.z);
    nodes.push({ b, e: ext / lod.grid, t: lod.triangles, v: full.v, c: [0] });
    geos.push(g);
    root = 1;
  }
  return { nodes, root, box: b, color: geo.hasAttribute('color'),
           load: id => Promise.resolve(geos[id]), legacy: true };
}

/**
 * @param {{THREE:object, buildBVH:function}} deps
 * @param {{nodes:Array, root:number, box:number[], color:boolean, load:function}} src
 *        nodes[i] = {b:[minx,miny,minz,maxx,maxy,maxz], e:기하오차, t:삼각형수, v:정점수, c:[자식 id]}
 *        load(id) → Promise<BufferGeometry>
 * @param {object} opts
 *   material     타일 재질
 *   triBudget    화면 삼각형 예산(초과하면 문턱을 올린다)
 *   byteBudget   로드해 둘 타일 바이트 예산(초과하면 오래된 것부터 버린다)
 *   ssePx        정제 문턱(px) — 노드 오차가 화면에서 이보다 크면 자식으로
 *   movingScale  조작 중 문턱 배율(간략하게 그려 프레임 확보)
 *   concurrency  동시 요청 수
 *   onChange     타일이 도착·해제돼 다시 그려야 할 때 부르는 콜백
 */
export function createTileSet(deps, src, opts) {
  const { THREE, buildBVH } = deps;
  const o = Object.assign({ triBudget: 5e6, byteBudget: 1.2e9, ssePx: 2, movingScale: 2.5,
                            concurrency: 6, onChange: null }, opts || {});
  const material = o.material;
  const group = new THREE.Group();
  group.matrixAutoUpdate = false;

  // 노드 표 — 메타의 압축 표기를 풀어 객체로
  const nodes = src.nodes.map((n, id) => ({
    id, box: new THREE.Box3(new THREE.Vector3(n.b[0], n.b[1], n.b[2]), new THREE.Vector3(n.b[3], n.b[4], n.b[5])),
    err: n.e, tris: n.t, verts: n.v, children: n.c, parent: -1,
    geo: null, mesh: null, bvh: null, bytes: 0, loading: false, failedAt: 0, lastUsed: 0, drawn: 0,
  }));
  for (const n of nodes) for (const c of n.children) nodes[c].parent = n.id;
  const root = nodes[src.root];
  const box = new THREE.Box3(new THREE.Vector3(src.box[0], src.box[1], src.box[2]),
                             new THREE.Vector3(src.box[3], src.box[4], src.box[5]));
  const sphere = box.getBoundingSphere(new THREE.Sphere());

  const loaded = new Set();
  let loadedBytes = 0, active = 0, frame = 0, sseBase = o.ssePx;
  let lastDrawn = [];
  const stats = { tris: 0, drawn: 0, loaded: 0, pending: 0, bytes: 0, sse: sseBase };

  // ── 로드/해제 ──
  function notify() { if (o.onChange) o.onChange(); }

  function startLoad(n) {
    n.loading = true; active++;
    src.load(n.id).then(geo => {
      n.geo = geo;
      const pa = geo.attributes.position, ca = geo.attributes.color, na = geo.attributes.normal;
      n.bytes = pa.array.byteLength + (ca ? ca.array.byteLength : 0) + (na ? na.array.byteLength : 0)
              + (geo.index ? geo.index.array.byteLength : 0);
      const m = new THREE.Mesh(geo, material);
      m.matrixAutoUpdate = false; m.frustumCulled = false;   // 컬링은 여기서 노드 상자로 한다
      m.visible = false;
      group.add(m);
      n.mesh = m;
      loaded.add(n); loadedBytes += n.bytes;
    }).catch(err => {
      n.failedAt = performance.now();
      console.warn('[타일] 로드 실패', n.id, err);
    }).finally(() => { n.loading = false; active--; notify(); });
  }

  function unload(n) {
    if (!n.geo) return;
    group.remove(n.mesh);
    if (!src.legacy) n.geo.dispose();          // 레거시는 버퍼를 공유하므로 GPU 자원을 남긴다
    loaded.delete(n); loadedBytes -= n.bytes;
    n.geo = null; n.mesh = null; n.bvh = null; n.bytes = 0;
  }

  // ── 매 프레임 선택 ──
  const frustum = new THREE.Frustum(), _pv = new THREE.Matrix4();
  const wanted = [];
  let drawnNow = [];

  function inView(n) { return frustum.intersectsBox(n.box); }

  function want(n, prio) {
    if (n.geo || n.loading) return;
    if (n.failedAt && performance.now() - n.failedAt < 5000) return;
    wanted.push({ n, prio });
  }

  // 노드 오차가 화면에서 몇 px 로 보이는지. 상자 안이면 무한대(반드시 정제).
  function sse(n, camPos, k) {
    if (n.err <= 0) return 0;
    const d = n.box.distanceToPoint(camPos);
    return d <= 0 ? Infinity : n.err * k / d;
  }

  function traverse(n, camPos, k, threshold) {
    if (!inView(n)) return;
    const s = sse(n, camPos, k);
    if (s > threshold && n.children.length) {
      let ready = true;
      for (const c of n.children) {
        const cn = nodes[c];
        if (!inView(cn)) continue;
        if (!cn.geo) { ready = false; want(cn, s); }
      }
      if (ready) {
        for (const c of n.children) traverse(nodes[c], camPos, k, threshold);
        return;
      }
    }
    if (n.geo) {
      drawnNow.push(n);
      for (let a = n; a && a.lastUsed !== frame; a = a.parent >= 0 ? nodes[a.parent] : null) a.lastUsed = frame;
    }
  }

  /**
   * @param {PerspectiveCamera} camera
   * @param {number} viewportH  화면 높이(px)
   * @param {boolean} moving    조작 중이면 문턱을 올려 가볍게 그린다
   * @returns {{changed:boolean}}  그려지는 타일 집합이 바뀌었는지(변화 없으면 다시 그릴 필요 없음)
   */
  function update(camera, viewportH, moving) {
    frame++;
    wanted.length = 0;
    drawnNow = [];
    _pv.multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse);
    frustum.setFromProjectionMatrix(_pv);
    const k = viewportH / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)));
    const threshold = sseBase * (moving ? o.movingScale : 1);

    if (!root.geo) want(root, Infinity);
    else traverse(root, camera.position, k, threshold);

    // 예산 적응 — 넘치면 문턱을 올리고(거칠게), 여유가 크면 내린다(정밀하게)
    let tris = 0;
    for (const n of drawnNow) tris += n.tris;
    if (tris > o.triBudget) sseBase = Math.min(sseBase * 1.1, o.ssePx * 16);
    else if (tris < o.triBudget * 0.5 && sseBase > o.ssePx) sseBase = Math.max(o.ssePx, sseBase / 1.05);

    // 가시성 반영 + 변화 감지
    for (const n of loaded) n.mesh.visible = false;
    for (const n of drawnNow) n.mesh.visible = true;
    let changed = drawnNow.length !== lastDrawn.length;
    for (let i = 0; !changed && i < drawnNow.length; i++) changed = drawnNow[i] !== lastDrawn[i];
    lastDrawn = drawnNow;

    // 요청 — 화면에서 큰 것(오차가 크게 보이는 것)부터
    if (wanted.length && active < o.concurrency) {
      wanted.sort((a, b) => b.prio - a.prio);
      for (let i = 0; i < wanted.length && active < o.concurrency; i++) startLoad(wanted[i].n);
    }

    // 해제 — 이번 프레임에 안 쓴 것 중 오래된 것부터, 예산 안으로 들어올 때까지
    if (loadedBytes > o.byteBudget) {
      const cands = [];
      for (const n of loaded) if (n.lastUsed !== frame && n !== root) cands.push(n);
      cands.sort((a, b) => a.lastUsed - b.lastUsed);
      for (const n of cands) { if (loadedBytes <= o.byteBudget * 0.85) break; unload(n); changed = true; }
    }

    stats.tris = tris; stats.drawn = drawnNow.length; stats.loaded = loaded.size;
    stats.pending = wanted.length + active; stats.bytes = loadedBytes; stats.sse = sseBase;
    return { changed };
  }

  // ── 클릭: 그려지는 타일에 대해 광선 교차 ──
  const _ray = new THREE.Ray(), _pt = new THREE.Vector3();

  function bvhOf(n) {
    if (!n.bvh) {
      // 타일은 감기 방향이 일정하지 않을 수 있어(tiler.py simplify 참조) 양면을 센다 — 재질도 DoubleSide 다.
      n.bvh = buildBVH(n.geo.attributes.position.array, n.geo.index ? n.geo.index.array : null, !src.legacy);
    }
    return n.bvh;
  }

  /**
   * bvh.js 의 raycast 와 같은 규약(앞면만). 지금 화면에 그려지는 타일만 본다.
   * @returns {{distance:number, triangle:number, node:object}|null}
   */
  function raycast(origin, dir, far, anyHit) {
    _ray.origin.copy(origin); _ray.direction.copy(dir);
    const lim = (far === undefined || far === null) ? Infinity : far;
    const cands = [];
    for (const n of lastDrawn) {
      if (!_ray.intersectBox(n.box, _pt)) continue;
      const t = _pt.distanceTo(origin);
      if (t <= lim) cands.push({ n, t });
    }
    cands.sort((a, b) => a.t - b.t);              // 가까운 상자부터 — 더 먼 상자는 일찍 끊는다
    let best = null;
    for (const { n, t } of cands) {
      if (best && t > best.distance) break;
      const h = bvhOf(n).raycast(origin, dir, best ? best.distance : lim, anyHit);
      if (h && (!best || h.distance < best.distance)) {
        best = { distance: h.distance, triangle: h.triangle, node: n };
        if (anyHit) return best;
      }
    }
    return best;
  }

  /** 맞은 면의 법선(정규화). 세 정점이 겹치면 null. */
  const _fa = new THREE.Vector3(), _fb = new THREE.Vector3(), _fc = new THREE.Vector3();
  function faceNormal(hit) {
    const P = hit.node.geo.attributes.position.array;
    const [ia, ib, ic] = hit.node.bvh.faceVerts(hit.triangle);
    _fa.fromArray(P, ia * 3); _fb.fromArray(P, ib * 3); _fc.fromArray(P, ic * 3);
    const n = _fb.sub(_fa).cross(_fc.sub(_fa));
    return n.lengthSq() > 0 ? n.normalize().clone() : null;
  }

  // 루트를 바로 요청한다 — 첫 화면은 루트 하나로 그려진다.
  const ready = new Promise((res, rej) => {
    startLoad(root);
    const tick = () => {
      if (root.geo) res();
      else if (!root.loading) rej(new Error('루트 타일을 받지 못했습니다'));
      else setTimeout(tick, 30);
    };
    tick();
  });

  function dispose() { for (const n of [...loaded]) unload(n); }

  return { group, box, sphere, nodes, update, raycast, faceNormal, ready, stats, dispose };
}
