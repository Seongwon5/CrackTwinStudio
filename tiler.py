# -*- coding: utf-8 -*-
"""
tiler.py — PLY 메시를 계층 LOD 타일(옥트리)로 쪼개 .twin(zip)에 넣는다.

왜 — 6GB(정점 9,600만·면 1억 9천만) PLY 는 브라우저 ArrayBuffer 하나로 받을 수도,
GPU 4~8GB 에 통째로 올릴 수도 없다. 대신 메시를 공간 옥트리로 나누고 노드마다
"그 영역 전체를 그 노드 크기에 맞는 해상도로 요약한 메시"를 미리 만들어 두면, 뷰어는
카메라에 보이는 노드만 · 거리에 맞는 해상도로 골라 받는다(3D Tiles 의 REPLACE 정제와
같은 구조). 화면에는 언제나 수백만 면 안팎만 올라가고, 가까이 가면 잎 노드의 원본
삼각형이 그대로 보인다.

구조
  · 잎(leaf)   : 원본 삼각형 그대로 (≤ LEAF_MAX_TRIS). 기하 오차 0.
  · 내부(node) : 자식 8개의 메시를 이 노드 정육면체를 GRID³ 격자로 나눠 칸마다 정점을
                 평균 하나로 합친 것(격자 병합). 기하 오차 = 칸 한 변(= 정육면체/GRID).
  · 뷰어는 오차 × (화면높이/거리) 가 문턱(px)을 넘으면 자식으로 내려간다.

처리 순서 (build_tiles)
  1. 헤더 해석 → 정점/면 블록을 memmap 으로 잡는다(6GB 를 한 번에 올리지 않는다)
  2. 정점: double 좌표에서 원점(경계상자 중심, 정수 m)을 빼 float32 로 — origin.js 와 같은
     이유(float32 정밀도). 색은 uint8. 면: 전부 삼각형인지 확인하며 int32 로.
  3. 면마다 첫 정점의 모튼 코드(12비트×3축) → 정렬. 정렬 뒤엔 어떤 옥트리 노드의 면도
     배열의 연속 구간이라, 노드 = (시작, 끝) 두 정수면 된다(별도 리스트 없음).
  4. 재귀(후위): 잎이면 그 구간의 삼각형을 그대로 타일로, 내부면 자식 메시를 합쳐
     격자 병합 → 타일로. 자식부터 처리하므로 부모는 자식 결과(≤ 8×수십만 면)만 보면
     되고, 원본 1억 9천만 면을 단계마다 다시 읽지 않는다.
  5. 타일 하나가 끝날 때마다 zip 에 바로 쓴다(tiles/<id>.bin, 무압축). 노드 목록은
     meta.json 의 tiles 항목으로 돌려준다.

메모리(96M 정점·192M 면): 정점 1.2GB + 색 0.3GB + 면 2.3GB + 정렬 키·순서 3GB(일시)
→ 최대 약 9GB, 정렬 뒤 5.3GB. 32GB PC 기준.

타일 바이너리(tiles/<id>.bin, little-endian)
  'TWT1' | uint32 nV | uint32 nF | uint32 flags(1=색 있음, 2=16비트 인덱스)
  float32 pos[nV*3] | uint8 rgb[nV*3] + 4바이트 패딩 | uint16|uint32 idx[nF*3]
  좌표는 원점을 뺀 국소 좌표. 읽는 쪽: ui/tiles.js parseTile().
"""
import os
import re
import struct
import time
import zipfile

import numpy as np

LEAF_MAX_TRIS = 100_000     # 잎 노드 최대 삼각형 수 — 타일 하나 ≈ 1.5~2MB
GRID = 128                  # 내부 노드 격자 분할 수(축당) — 표면 노드 ≈ 3~6만 면
MAX_DEPTH = 12              # 모튼 코드 비트 수(축당) = 옥트리 최대 깊이
MAGIC = b"TWT1"
MEASURE_CELL_FACTOR = 1.8   # 측정용 컷 칸 크기 보정(실측 면 수/목표 = 3.5 → sqrt) — build_tiles 참조

_PLY_NP = {"char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1",
           "short": "i2", "int16": "i2", "ushort": "u2", "uint16": "u2",
           "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
           "float": "f4", "float32": "f4", "double": "f8", "float64": "f8"}
_COLOR_NAMES = (("red", "green", "blue"), ("r", "g", "b"), ("diffuse_red", "diffuse_green", "diffuse_blue"))


class PlyError(ValueError):
    """타일로 만들 수 없는 PLY — 호출측은 원본 mesh.ply 그대로 저장하는 경로로 물러난다."""


class _Report:
    """한 단계의 진행을 progress(0~1) 로 넘기고, 로그에는 절제해서(10%마다 또는 5초마다) 한 줄 찍는다.

    긴 단계(6GB 읽기 1분 · 정렬 2분 · 타일 12분)에서 아무 말 없이 멈춘 것처럼 보이지 않게 한다.
    w0~w1: 이 단계가 전체 타일 작업에서 차지하는 구간(가중치). label: 로그에 붙는 단계 이름.
    """
    def __init__(self, log, progress, w0, w1, label, prefix="메시 타일"):
        self.log, self.progress, self.w0, self.w1, self.label, self.prefix = log, progress, w0, w1, label, prefix
        self.last_f, self.last_t, self.t0 = -1.0, 0.0, time.time()

    def __call__(self, frac, detail=""):
        frac = max(0.0, min(1.0, frac))
        overall = self.w0 + (self.w1 - self.w0) * frac
        if self.progress:
            self.progress(overall)
        now = time.time()
        if frac >= 1.0 or frac - self.last_f >= 0.1 or now - self.last_t >= 5.0:
            self.last_f, self.last_t = frac, now
            self.log(f"  [{self.prefix} {overall * 100:3.0f}%] {self.label} {frac * 100:.0f}%"
                     + (f" · {detail}" if detail else "") + f" · {now - self.t0:.0f}초")


# ---------------------------------------------------------------- PLY 읽기

def read_header(path):
    """헤더를 해석해 (elements, 데이터 시작 오프셋)을 돌려준다. binary_little_endian 만."""
    with open(path, "rb") as f:
        head = f.read(1 << 16)
    m = re.search(rb"end_header[ \t]*\r?\n", head)
    if not m:
        raise PlyError("PLY 헤더를 찾을 수 없음")
    lines = head[:m.start()].decode("latin1").splitlines()
    if not lines or lines[0].strip() != "ply":
        raise PlyError("PLY 파일이 아님")
    fmt = next((l for l in lines if l.startswith("format ")), "")
    if "binary_little_endian" not in fmt:
        raise PlyError("binary_little_endian 형식만 지원: " + fmt.strip())
    elements = []
    for l in lines:
        p = l.split()
        if not p:
            continue
        if p[0] == "element":
            elements.append({"name": p[1], "count": int(p[2]), "props": []})
        elif p[0] == "property" and elements:
            elements[-1]["props"].append(tuple(p[1:]))
    return elements, m.end()


def _vertex_dtype(props):
    fields = []
    for p in props:
        if p[0] == "list":
            raise PlyError("vertex 에 list 속성 — 고정 크기가 아님")
        if p[0] not in _PLY_NP:
            raise PlyError("모르는 속성 타입: " + p[0])
        name = p[1]
        while name in [f[0] for f in fields]:
            name += "_"
        fields.append((name, "<" + _PLY_NP[p[0]]))
    return np.dtype(fields)


def load_mesh(path, log, progress=None, local=True):
    """PLY → (P (nV,3), C uint8 (nV,3)|None, F int32 (nF,3), origin[3]).

    local=True : P 는 원점(origin)을 뺀 float32 국소좌표 (타일용)
    local=False: P 는 원점을 빼지 않은 float64 절대좌표, origin 은 [0,0,0] (파이프라인 측정용 —
                 mm 정밀도를 float64 그대로 유지한다)
    progress(0~1) 로 읽기 진행을 알린다(정점 0~0.6 · 면 0.6~1). 삼각형 메시가 아니거나
    (면 정점 수 ≠ 3) 형식이 맞지 않으면 PlyError.
    """
    rep_ = progress or (lambda f, d="": None)
    elements, off = read_header(path)
    names = [e["name"] for e in elements]
    if names[:2] != ["vertex", "face"]:
        raise PlyError("요소 순서가 vertex, face 가 아님: " + ", ".join(names))
    ev, ef = elements[0], elements[1]
    nV, nF = ev["count"], ef["count"]
    if nV <= 0 or nF <= 0:
        raise PlyError("정점 또는 면이 없음(점군?)")
    vdt = _vertex_dtype(ev["props"])
    for k in ("x", "y", "z"):
        if k not in vdt.names:
            raise PlyError("vertex 에 %s 없음" % k)
    if len(ef["props"]) != 1 or ef["props"][0][0] != "list":
        raise PlyError("face 는 list 속성 하나여야 함")
    _, cnt_t, item_t, _ = ef["props"][0]
    if cnt_t not in _PLY_NP or item_t not in _PLY_NP:
        raise PlyError("face list 타입 미지원")
    fdt = np.dtype([("n", "<" + _PLY_NP[cnt_t]), ("v", "<" + _PLY_NP[item_t], (3,))])

    fsize = os.path.getsize(path)
    face_off = off + nV * vdt.itemsize
    if face_off + nF * fdt.itemsize > fsize:
        raise PlyError("면 블록이 삼각형 고정 크기보다 작음 — 삼각형 메시가 아님")

    mmV = np.memmap(path, dtype=vdt, mode="r", offset=off, shape=(nV,))
    mmF = np.memmap(path, dtype=fdt, mode="r", offset=face_off, shape=(nF,))

    # 원점: 표본으로 잡은 경계상자 중심(정수 m). 정확할 필요는 없고 좌표가 작아지기만 하면 된다.
    # (표본 20만 점이 파일 전체에 흩어져 있어 6GB 에서 수십 초 — 절대좌표가 필요한 측정용 로드는 건너뛴다)
    origin = [0.0, 0.0, 0.0]
    if local:
        step = max(1, nV // 200_000)
        origin = []
        for k in ("x", "y", "z"):
            s = np.asarray(mmV[k][::step], dtype=np.float64)
            origin.append(float(round((s.min() + s.max()) / 2)))
        log(f"  원점 이동: {origin[0]:.0f}, {origin[1]:.0f}, {origin[2]:.0f}")

    color = next((c for c in _COLOR_NAMES if all(n in vdt.names for n in c)), None)
    if color and any(vdt[n] != np.dtype("u1") for n in color):
        log("  ⚠ 색이 uchar 가 아니라 무시합니다")
        color = None

    P = np.empty((nV, 3), np.float32 if local else np.float64)
    C = np.empty((nV, 3), np.uint8) if color else None
    CH = 2_000_000
    for s in range(0, nV, CH):
        e = min(nV, s + CH)
        blk = np.asarray(mmV[s:e])
        for k, name in enumerate(("x", "y", "z")):
            P[s:e, k] = blk[name].astype(np.float64) - origin[k]
        if color:
            for k, name in enumerate(color):
                C[s:e, k] = blk[name]
        rep_(0.6 * e / nV, f"정점 {e:,}/{nV:,}")
    del mmV

    F = np.empty((nF, 3), np.int32)
    CH = 4_000_000
    for s in range(0, nF, CH):
        e = min(nF, s + CH)
        blk = np.asarray(mmF[s:e])
        if not np.all(blk["n"] == 3):
            raise PlyError("삼각형이 아닌 면이 있음(사각형 등) — 삼각형으로 내보내 주세요")
        F[s:e] = blk["v"]
        rep_(0.6 + 0.4 * e / nF, f"면 {e:,}/{nF:,}")
    del mmF
    if int(F.max()) >= nV or int(F.min()) < 0:
        raise PlyError("면 인덱스가 정점 범위를 벗어남")
    return P, C, F, origin


# ---------------------------------------------------------------- 모튼 코드

def _part1by2(x):
    """21비트 정수의 비트를 3칸 간격으로 벌린다(uint64)."""
    x = x.astype(np.uint64) & np.uint64(0x1FFFFF)
    x = (x | (x << np.uint64(32))) & np.uint64(0x1F00000000FFFF)
    x = (x | (x << np.uint64(16))) & np.uint64(0x1F0000FF0000FF)
    x = (x | (x << np.uint64(8))) & np.uint64(0x100F00F00F00F00F)
    x = (x | (x << np.uint64(4))) & np.uint64(0x10C30C30C30C30C3)
    x = (x | (x << np.uint64(2))) & np.uint64(0x1249249249249249)
    return x


def morton(q):
    """(n,3) 정수 격자 좌표 → 모튼 키. 비트 순서 x=0, y=1, z=2 (자식 번호 o = x | y<<1 | z<<2)."""
    return _part1by2(q[:, 0]) | (_part1by2(q[:, 1]) << np.uint64(1)) | (_part1by2(q[:, 2]) << np.uint64(2))


# ---------------------------------------------------------------- 타일 생성

def pack_tile(P, C, I):
    """타일 바이너리(머리말 참조)."""
    nV, nF = len(P), len(I)
    idx16 = nV <= 65535
    flags = (1 if C is not None else 0) | (2 if idx16 else 0)
    parts = [MAGIC, struct.pack("<III", nV, nF, flags), np.ascontiguousarray(P, dtype="<f4").tobytes()]
    if C is not None:
        cb = np.ascontiguousarray(C, dtype="u1").tobytes()
        parts += [cb, b"\0" * ((-len(cb)) % 4)]
    parts.append(np.ascontiguousarray(I, dtype="<u2" if idx16 else "<u4").tobytes())
    return b"".join(parts)


def simplify(parts, cmin, csize, grid=GRID):
    """자식 메시들을 합쳐 정육면체(cmin, csize)의 grid³ 격자로 병합한다.

    칸마다 정점 하나(위치·색 평균). 삼각형은 원본 삼각형을 옮기는 대신 **칸 인접 그래프의
    3-클리크**로 만든다 — 원본 삼각형이 칸보다 훨씬 작으면(1cm 삼각형 vs 4cm~1m 칸)
    네 칸이 만나는 지점마다 원본 삼각형 하나가 대각선 한쪽만 덮고 반대쪽은 구멍이 남는다
    (격자 병합의 알려진 약점 — 실측: 원전 돔 루트가 격자 구멍투성이로 보였다).
    네 칸 A·B·C·D 가 만나는 지점을 품는 원본 삼각형은 반드시 세 칸 이상 또는 마주보는
    두 칸에 걸치므로 대각선 인접(A–C)이 항상 생기고, 그러면 ABC·ACD 두 클리크가 사각형을
    빈틈없이 덮는다. 클리크가 두 대각선을 다 만들면 겹치지만 거의 같은 평면이라 티가 안 난다.
    감기(winding)는 자식 면 법선의 칸별 합으로 맞춘다(얇은 벽은 양면이 상쇄돼 임의 —
    뷰어가 양면 렌더·양면 교차라 보이는 데는 문제없다).
    """
    Pc = np.concatenate([p[0] for p in parts])
    Cc = np.concatenate([p[1] for p in parts]) if parts[0][1] is not None else None
    offs = np.cumsum([0] + [len(p[0]) for p in parts[:-1]])
    Ic = np.concatenate([p[2].astype(np.int64) + o for p, o in zip(parts, offs)])

    cell = csize / grid
    q = np.floor((Pc - cmin) / cell).astype(np.int64)
    np.clip(q, 0, grid - 1, out=q)
    cid = (q[:, 0] * grid + q[:, 1]) * grid + q[:, 2]
    uniq, inv, cnt = np.unique(cid, return_inverse=True, return_counts=True)
    inv = inv.ravel()
    m = len(uniq)
    Pn = np.empty((m, 3), np.float32)
    for k in range(3):
        Pn[:, k] = np.bincount(inv, weights=Pc[:, k], minlength=m) / cnt
    Cn = None
    if Cc is not None:
        Cn = np.empty((m, 3), np.uint8)
        for k in range(3):
            Cn[:, k] = np.clip(np.rint(np.bincount(inv, weights=Cc[:, k], minlength=m) / cnt), 0, 255)

    # 칸별 법선 = 그 칸에 정점을 둔 자식 면들의 법선 합(감기 방향 결정용)
    A, B, Cv = Pc[Ic[:, 0]].astype(np.float64), Pc[Ic[:, 1]].astype(np.float64), Pc[Ic[:, 2]].astype(np.float64)
    fn = np.cross(B - A, Cv - A)
    t = inv[Ic]
    Nn = np.zeros((m, 3), np.float64)
    for corner in range(3):
        for k in range(3):
            Nn[:, k] += np.bincount(t[:, corner], weights=fn[:, k], minlength=m)

    # 칸 인접 그래프(무향, u<v) — 자식 삼각형의 세 변이 서로 다른 칸을 잇는 곳
    e = np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]])
    e = e[e[:, 0] != e[:, 1]]
    e.sort(axis=1)
    keys = np.unique(e[:, 0] * m + e[:, 1])              # m ≤ 2^21 → 42비트
    u, v = keys // m, keys % m                             # u 순 정렬 → 같은 u 가 연속

    # 3-클리크 (u<v<w): 같은 u 의 이웃 쌍 (v,w) 중 (v,w) 도 인접한 것
    n_e = len(u)
    grp = np.searchsorted(u, u)                            # 각 원소가 속한 u 그룹의 시작
    deg = np.bincount(u, minlength=m)[u]
    pos = np.arange(n_e) - grp
    count = deg - pos - 1                                  # 이 원소 뒤에 남은 같은 그룹 원소 수
    total = int(count.sum())
    ii = np.repeat(np.arange(n_e), count)
    jj = ii + 1 + (np.arange(total) - np.repeat(np.cumsum(count) - count, count))
    v1, v2 = v[ii], v[jj]
    look = np.searchsorted(keys, v1 * m + v2)
    ok = (look < n_e) & (keys[np.minimum(look, n_e - 1)] == v1 * m + v2)
    tri = np.stack([u[ii][ok], v1[ok], v2[ok]], axis=1)

    # 감기: 삼각형 법선이 세 칸 법선 합과 같은 쪽을 보게
    P0, P1, P2 = Pn[tri[:, 0]].astype(np.float64), Pn[tri[:, 1]].astype(np.float64), Pn[tri[:, 2]].astype(np.float64)
    tn = np.cross(P1 - P0, P2 - P0)
    ref = Nn[tri[:, 0]] + Nn[tri[:, 1]] + Nn[tri[:, 2]]
    flip = np.einsum("ij,ij->i", tn, ref) < 0
    tri[flip, 1], tri[flip, 2] = tri[flip, 2], tri[flip, 1]
    return Pn, Cn, tri.astype(np.uint32)


def build_tiles(ply_path, zf, log, progress, leaf_max=LEAF_MAX_TRIS, grid=GRID, measure_max_faces=None):
    """PLY 를 타일로 쪼개 zf(zipfile, 쓰기 모드)에 tiles/<id>.bin 으로 넣고 meta 의 tiles 항목을 돌려준다.

    progress(0~1) 은 타일 작업 전체 진행률(읽기 8% · 공간 정렬 12% · 타일 생성 80% — 6GB 실측 비율).
    로그에는 단계마다 10% 또는 5초 간격으로 진행 줄을 찍는다.

    measure_max_faces 를 주면 (meta, measure) 를 돌려준다. measure 는 측정(역투영)용 메시 —
    {"P": float64 절대좌표 (n,3), "F": int32 (m,3), "cell": 셀 크기 m, "faces_src": 원본 면 수}.
    원본 면 수가 그 값 이하면 원본 그대로, 넘으면 옥트리에서 "칸 크기 ≤ 목표 < 부모 칸 크기" 인
    노드들(루트→잎 경로마다 정확히 하나)을 모아 만든다 = 원본을 목표 칸 크기로 요약한 메시.
    목표 칸 = 평균 변 길이 × sqrt(원본 면 수 / 상한) → 면 수가 상한 근처로 떨어진다.
    왜 — trimesh 는 면마다 float64 삼각형·법선·외적을 캐시해 1억 9천만 면이면 20GB 를 넘는다(실측
    MemoryError). 폭 측정은 인접한 두 광선의 착지점 차이라 표면이 몇 cm 격자로 요약돼도 국소 평면
    위에서는 거의 그대로다(돔 반경 20m·3cm 격자의 현 오차 6µm).
    """
    t0 = time.time()
    P, C, F, origin = load_mesh(ply_path, log, progress=_Report(log, progress, 0.0, 0.08, "PLY 읽기"))
    nV, nF = len(P), len(F)
    log(f"  정점 {nV:,} · 면 {nF:,} · 색 {'있음' if C is not None else '없음'} ({time.time() - t0:.0f}초)")

    # 측정용 컷의 목표 칸 크기
    m_target, m_parts = None, []
    if measure_max_faces and nF > measure_max_faces:
        smp = F[np.random.default_rng(0).choice(nF, min(nF, 100_000), replace=False)]
        e_mean = float(np.linalg.norm(P[smp[:, 0]].astype(np.float64) - P[smp[:, 1]].astype(np.float64), axis=1).mean())
        # 격자 병합 결과는 칸당 삼각형 2개가 아니라 약 3.5개(클리크·노드 경계 중복) — 6GB 실측:
        # 보정 없이 2.7cm 로 잡았더니 6,900만 면(목표 2천만의 3.5배). sqrt(3.5)≈1.8 배 큰 칸(4.8cm)에서 3,180만 면.
        m_target = e_mean * float(np.sqrt(nF / measure_max_faces)) * MEASURE_CELL_FACTOR
        log(f"  측정용 메시: 원본 면 {nF:,} > 상한 {measure_max_faces:,} → 칸 {m_target * 100:.1f}cm 로 요약"
            f"(평균 변 {e_mean * 100:.2f}cm)")

    lo, hi = P.min(0).astype(np.float64), P.max(0).astype(np.float64)
    size = float((hi - lo).max()) * 1.0001 + 1e-6
    cube_min = (lo + hi) / 2 - size / 2

    # 면 → 첫 정점의 모튼 키 → 정렬. 정렬 뒤 노드 = 연속 구간.
    rep_sort = _Report(log, progress, 0.08, 0.20, "공간 정렬")
    keys = np.empty(nF, np.uint64)
    scale = (1 << MAX_DEPTH) / size
    CH = 8_000_000
    for s in range(0, nF, CH):
        e = min(nF, s + CH)
        q = ((P[F[s:e, 0]].astype(np.float64) - cube_min) * scale).astype(np.int64)
        np.clip(q, 0, (1 << MAX_DEPTH) - 1, out=q)
        keys[s:e] = morton(q)
        rep_sort(0.3 * e / nF, f"모튼 키 {e:,}/{nF:,}")
    rep_sort(0.3, "정렬 시작(면 %s개 — 수 분)" % f"{nF:,}")
    order = np.argsort(keys)
    rep_sort(0.8, "정렬 완료 · 면 재배열")
    keys = keys[order]
    F = F[order]
    del order
    rep_sort(1.0)
    log(f"  공간 정렬 완료 ({time.time() - t0:.0f}초)")

    nodes = []
    state = {"done": 0, "tris": 0, "last": 0.0}
    rep_tile = _Report(log, progress, 0.20, 1.0, "타일 생성")

    def emit(Pt, Ct, It, err, children, child_boxes):
        nid = len(nodes)
        bmin, bmax = Pt.min(0).astype(np.float64), Pt.max(0).astype(np.float64)
        for b in child_boxes:                        # 자식 영역까지 감싸야 컬링이 자식을 놓치지 않는다
            bmin = np.minimum(bmin, b[:3]); bmax = np.maximum(bmax, b[3:])
        box = [round(float(v), 4) for v in np.concatenate([bmin, bmax])]
        nodes.append({"b": box, "e": round(float(err), 6), "t": int(len(It)), "v": int(len(Pt)), "c": children})
        zf.writestr(f"tiles/{nid}.bin", pack_tile(Pt, Ct, It), zipfile.ZIP_STORED)
        state["tris"] += len(It)
        return nid, box

    def build(k0, depth, cmin, csize, s, e):
        n = e - s
        if n <= leaf_max or depth >= MAX_DEPTH:
            f = F[s:e]
            u, inv = np.unique(f.ravel(), return_inverse=True)
            Pt, Ct = P[u], (C[u] if C is not None else None)
            It = inv.ravel().reshape(-1, 3).astype(np.uint32)
            nid, box = emit(Pt, Ct, It, 0.0, [], [])
            if m_target is not None and (depth == 0 or 2 * csize / grid > m_target):   # 잎: 부모 칸이 목표보다 크면 채택
                m_parts.append((Pt, It))
            state["done"] += n
            if time.time() - state["last"] > 0.5:
                state["last"] = time.time()
                rep_tile(state["done"] / nF, f"타일 {len(nodes):,}개 · 면 {state['done']:,}/{nF:,}")
            return nid, box, (Pt, Ct, It)
        shift = 3 * (MAX_DEPTH - depth - 1)
        half = csize / 2
        kv = keys[s:e]
        ids, boxes, parts = [], [], []
        for o in range(8):
            lo_k = k0 + (o << shift)
            cs = s + int(np.searchsorted(kv, np.uint64(lo_k), "left"))
            ce = s + int(np.searchsorted(kv, np.uint64(lo_k + (1 << shift)), "left"))
            if ce <= cs:
                continue
            off = cmin + half * np.array([o & 1, (o >> 1) & 1, (o >> 2) & 1], np.float64)
            cid, cbox, mesh = build(lo_k, depth + 1, off, half, cs, ce)
            ids.append(cid); boxes.append(cbox); parts.append(mesh)
        Pm, Cm, Im = simplify(parts, cmin, csize, grid)
        nid, box = emit(Pm, Cm, Im, csize / grid, ids, boxes)
        e_self = csize / grid
        if m_target is not None and e_self <= m_target and (depth == 0 or 2 * e_self > m_target):
            m_parts.append((Pm, Im))                 # 내부: 내 칸 ≤ 목표 < 부모 칸
        return nid, box, (Pm, Cm, Im)

    root, root_box, _ = build(0, 0, cube_min, size, 0, nF)
    rep_tile(1.0, f"타일 {len(nodes):,}개")
    leaves = sum(1 for n in nodes if not n["c"])
    log(f"  타일 {len(nodes):,}개(잎 {leaves:,}) · 타일 삼각형 합계 {state['tris']:,} · "
        f"루트 {nodes[root]['t']:,}면 ({time.time() - t0:.0f}초)")
    meta = {"version": 1, "origin": origin, "box": root_box, "root": root, "color": C is not None,
            "grid": grid, "leaf_max": leaf_max, "max_depth": MAX_DEPTH,
            "source": {"vertices": nV, "faces": nF}, "nodes": nodes}
    if measure_max_faces is None:
        return meta
    # 측정용 메시 — 원본(상한 이하) 또는 옥트리 컷. 큰 배열(P·C·F·keys)은 먼저 놓아준다.
    if m_target is None:
        Pm, Fm = P.astype(np.float64) + np.asarray(origin), F.astype(np.int32)
        cell = 0.0
    else:
        offs = np.cumsum([0] + [len(p_) for p_, _ in m_parts[:-1]])
        Pm = np.concatenate([p_ for p_, _ in m_parts]).astype(np.float64) + np.asarray(origin)
        Fm = np.concatenate([i_.astype(np.int64) + o for (_, i_), o in zip(m_parts, offs)]).astype(np.int32)
        cell = m_target
    del P, C, F, keys, m_parts
    log(f"  측정용 메시 준비: 정점 {len(Pm):,} · 면 {len(Fm):,} · 칸 {cell * 100:.1f}cm")
    return meta, {"P": Pm, "F": Fm, "cell": cell, "faces_src": nF}


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser(description="PLY → 타일 zip (테스트용)")
    ap.add_argument("ply"); ap.add_argument("out")
    ap.add_argument("--leaf", type=int, default=LEAF_MAX_TRIS)
    ap.add_argument("--grid", type=int, default=GRID)
    a = ap.parse_args()
    with zipfile.ZipFile(a.out, "w", allowZip64=True) as z:
        meta = build_tiles(a.ply, z, print, lambda f: None, a.leaf, a.grid)
        z.writestr("meta.json", json.dumps({"version": 2, "counts": {"cameras": 0}, "cameras": [], "tiles": meta}),
                   zipfile.ZIP_STORED)
    print("완료:", a.out, f"{os.path.getsize(a.out) / 1e6:.1f} MB")
