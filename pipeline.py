# -*- coding: utf-8 -*-
"""
pipeline.py  ―  크랙 디지털 트윈 분석 파이프라인 (통합 모듈)
================================================================
이미지+XMP 폴더와 3D 메시(PLY)를 받아 best.pt(DeepLabV3)로 크랙을 검출하고,
크랙을 3D로 역투영해 실척(mm) 폭·길이를 산출한 자체완결형 보고서(.twin)를 만든다.
필요한 함수만 한 파일에 모았다: 기하(XMP/카메라/역투영) · 크랙추론 · 정량화 · 보고서.
"""
import os
import sys
import json
import zipfile
import numpy as np
import cv2
from scipy import ndimage as ndi
from scipy.spatial import cKDTree
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra, minimum_spanning_tree
from skimage.morphology import skeletonize
import trimesh

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)                       # best.pt 언피클용 network 패키지 경로

# ---- 설정값 ----
CRACK_WEIGHTS = os.path.join(HERE, "model", "best.pt")
IMG_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")
# 병합 반경은 '절대값(m)'이다. 모델 대각선 비율로 잡으면 안 된다 — 크랙은 mm 단위의 절대 크기
# 특징이라 모델이 커진다고 같이 커지지 않는다. (비율(0.01/0.002) 방식일 때 실측:
#  망치 메시 50m → 병합 503mm / 복셀 101mm  ·  댐 메시 622m → 병합 6,225mm / 복셀 1,245mm
#  → 댐에서 6.2m 안의 크랙이 전부 1개로 뭉쳐 594장에서 핫스팟이 51개(장당 0.09)밖에 안 나왔고,
#    1.2m 격자당 점 1개만 남아 점군이 듬성듬성해졌다.)
# 전제: PLY 1 단위 = 1 m (README 규약). 메시가 실척으로 정규화돼 있지 않으면 이 값도 어긋난다.
HOTSPOT_MERGE_M = 0.30            # (구) 단일점 반경 병합 — DBSCAN 도입으로 미사용
TEXTPOINT_MERGE_M = 0.02          # 크랙 점군 복셀 다운샘플 격자
# DBSCAN 기반 크랙 병합: 핫스팟의 중심선(poly) 기하를 3D에서 밀도 연결로 묶는다.
# eps 안으로 인접한 중심선은 '연속된 한 크랙'으로 병합 → 조각난/살짝 끊긴 크랙이 이어진다.
DBSCAN_EPS_M = 0.02              # 병합·갭브리징 거리(m). "몇 mm 이내면 연결"의 그 거리(튜닝 노브)
DBSCAN_MIN_SAMPLES = 2          # 코어 최소 이웃(고립된 오검출 점 제거)
DBSCAN_MAX_PTS = 80000          # 병합용 조밀화 점 상한(성능 보호)
FUSE_VOXEL_M = 0.02             # 다시점 융합 복셀(m). 이 안의 여러 뷰 중심선점을 한 점으로 합침
FULL_MAX_PX, FULL_JPEG_Q = 1920, 82   # 내장 원본 이미지 최대변/품질(용량 제어)
THUMB_MAX_PX = 900                # 썸네일 최대변
CRACK_THR = 0.4                   # 크랙 이진화 임계값
USE_FP16 = True                   # GPU에서 FP16 추론(속도↑·VRAM↓). CPU는 자동 무시
MIN_THIN_PX = 6                   # 얇은 크랙 최소 픽셀: min_object_px 미만이어도 길쭉하면 살린다
THIN_ELONG = 3.0                  # 이 이상 길쭉(주축/부축 표준편차비)하면 얇은 크랙, 미만이면 노이즈 blob
CRACK_BRIDGE_PX = 5               # 이 반경 안에서 끊긴 크랙을 하나의 인스턴스로 잇는다(2D 클로징)
CRACK_BATCH = 4                   # 타일 배치 크기. 처리량은 배치에 거의 무관(GPU가 타일 하나로 이미
                                  # 포화)하므로, 타일 1024 에서 VRAM 여유를 남기는 값으로 잡는다.
MAX_CRACK_POINTS_PER_IMG = 30000  # 이미지당 크랙 점군 상한
SCALE_MAP_CAP = 60000             # 스케일맵/점군용 크랙 픽셀 역투영 상한
COS_CLAMP = 0.2                   # 경사(grazing)에서 스케일 폭주 방지 cosθ 하한


# ==================================================================
# 1. 이미지 / 카메라 기하
# ==================================================================
def find_pairs(folder):
    files = os.listdir(folder)
    
    # XMP 파일 목록 확보
    xmps = {f.lower(): f for f in files if f.lower().endswith(".xmp")}
    pairs = []
    
    for f in files:
        if f.lower().endswith(IMG_EXTS):
            # 후보 1: photo.jpg.xmp 형태
            cand1 = (f + ".xmp").lower()
            
            # 후보 2: photo.xmp 형태 (기존 확장자를 제거하고 .xmp 부착)
            base_name, _ = os.path.splitext(f)
            cand2 = (base_name + ".xmp").lower()
            
            # 둘 중 하나라도 딕셔너리에 있는지 확인
            matched_xmp = None
            if cand1 in xmps:
                matched_xmp = xmps[cand1]
            elif cand2 in xmps:
                matched_xmp = xmps[cand2]
            
            # 경로 병합 후 추가
            img_path = os.path.join(folder, f)
            xmp_path = os.path.join(folder, matched_xmp) if matched_xmp else None
            
            pairs.append((img_path, xmp_path))
            
    return sorted(pairs, key=lambda p: p[0])

def parse_xmp(path):
    """XMP에서 내부파라미터 K·왜곡 dist·회전 R·평행이동 T·보정해상도(w,h)를 읽는다."""
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    cal, ext = root.find(".//calibration"), root.find(".//extrinsics")
    if cal is None or ext is None:
        raise RuntimeError("calibration/extrinsics 없음")
    K = np.array([[float(cal.get("fx")), 0, float(cal.get("cx"))],
                  [0, float(cal.get("fy")), float(cal.get("cy"))], [0, 0, 1]], float)
    dist = np.array([float(cal.get("k1", 0)), float(cal.get("k2", 0)),
                     float(cal.get("p1", 0)), float(cal.get("p2", 0)), float(cal.get("k3", 0))], float)
    w, h = cal.get("w"), cal.get("h")
    calib_wh = (int(w), int(h)) if (w and h) else None
    R = np.array([float(x) for x in ext.find("rotation").text.split()], float).reshape(3, 3)
    T = np.array([float(x) for x in ext.find("translation").text.split()], float)
    return K, dist, R, T, calib_wh


def orient_to_xmp(file_img, calib_wh):
    """파일 이미지를 XMP 보정 방향(가로/세로)에 맞도록 90° 단위로 회전해 반환."""
    base_k = 0
    if calib_wh:
        target_hw = (calib_wh[1], calib_wh[0])
        for k in (0, 1, 2, 3):
            if np.rot90(file_img, k).shape[:2] == target_hw:
                base_k = k
                break
    return (np.rot90(file_img, base_k).copy() if base_k else file_img.copy())


def encode_jpeg_bytes(img_bgr, max_px, quality):
    """BGR 이미지를 최대변 max_px 로 축소한 뒤 JPEG '바이트'로 인코딩한다.
    (예전의 data-URI/base64 는 용량을 4/3 로 부풀리고 JS 문자열 한계에 걸리게 만들었다)"""
    h, w = img_bgr.shape[:2]
    s = min(1.0, float(max_px) / max(h, w))
    im = cv2.resize(img_bgr, (0, 0), fx=s, fy=s) if s < 1.0 else img_bgr
    ok, buf = cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    return buf.tobytes()


def encode_mask_png(mask, max_px, color_bgr=(40, 40, 255)):
    """이진 크랙 마스크 → 반투명 오버레이용 RGBA PNG 바이트(크랙=색, 배경=투명).
    full 이미지와 같은 스케일(max_px)로 최근접 축소해 픽셀 좌표가 정확히 겹치게 한다."""
    h, w = mask.shape[:2]
    s = min(1.0, float(max_px) / max(h, w))
    # full JPEG 과 동일한 fx/fy 축소(같은 반올림) → 픽셀 크기가 정확히 일치해 오버레이가 어긋나지 않음
    m = (cv2.resize(mask.astype(np.uint8), (0, 0), fx=s, fy=s, interpolation=cv2.INTER_NEAREST)
         if s < 1.0 else mask.astype(np.uint8))
    on = m > 0
    bgra = np.zeros((m.shape[0], m.shape[1], 4), np.uint8)
    bgra[on, 0], bgra[on, 1], bgra[on, 2], bgra[on, 3] = color_bgr[0], color_bgr[1], color_bgr[2], 255
    ok, buf = cv2.imencode(".png", bgra)
    return buf.tobytes()


def camera_pose(K, R, T, dw, dh):
    """월드 좌표계 카메라 pose: 중심·전방(f)·상(up)·우(right)·화각·종횡비·내부파라미터.
    X_cam = R(X-C) 규약 → C=-R^T T, f=R^T e_z, up=-R^T e_y, right=R^T e_x."""
    C = (-R.T @ T)
    f = R.T @ np.array([0.0, 0.0, 1.0]); up = -(R.T @ np.array([0.0, 1.0, 0.0]))
    right = R.T @ np.array([1.0, 0.0, 0.0])
    fx, fy = float(K[0, 0]), float(K[1, 1]); cx, cy = float(K[0, 2]), float(K[1, 2])
    fov_v = float(np.degrees(2.0 * np.arctan((dh / 2.0) / fy)))
    u = lambda v: (v / np.linalg.norm(v)) if np.linalg.norm(v) > 1e-12 else v
    return {"pos": [float(C[0]), float(C[1]), float(C[2])],
            "fwd": [float(x) for x in u(f)], "up": [float(x) for x in u(up)],
            "right": [float(x) for x in u(right)], "fov": fov_v,
            "aspect": float(dw) / float(dh), "w": int(dw), "h": int(dh),
            "fx": fx, "fy": fy, "cx": cx, "cy": cy}


def pick_center(K, R, T):
    """카메라 중심 C = -R^T T (역투영 광선의 출발점)."""
    return -R.T @ T


def backproject(uv, K, dist, R, C, mesh):
    """이미지 픽셀(uv)에서 광선을 쏴 메시 표면과의 교점을 구한다(왜곡 보정 포함).
    반환: (3D위치, ray인덱스, 삼각형인덱스, 월드광선방향)."""
    if len(uv) == 0:
        return np.zeros((0, 3)), np.zeros(0, int), np.zeros(0, int), np.zeros((0, 3))
    und = cv2.undistortPoints(uv.reshape(-1, 1, 2).astype(np.float64), K, dist).reshape(-1, 2)
    d_cam = np.column_stack([und, np.ones(len(und))]); d_cam /= np.linalg.norm(d_cam, axis=1, keepdims=True)
    d_world = (R.T @ d_cam.T).T
    origins = np.tile(C, (len(uv), 1))
    locs, ray_idx, tri_idx = mesh.ray.intersects_location(
        ray_origins=origins, ray_directions=d_world, multiple_hits=False)
    return locs, ray_idx, tri_idx, d_world


def cluster_voxel(pts, radius):
    """점군을 radius 격자로 복셀화해 각 셀의 평균점으로 다운샘플한다."""
    pts = np.asarray(pts, float)
    if len(pts) == 0:
        return np.zeros((0, 3)), []
    keys = np.floor(pts / radius).astype(np.int64)
    order = np.lexsort((keys[:, 2], keys[:, 1], keys[:, 0]))
    ks = keys[order]
    change = np.any(ks[1:] != ks[:-1], axis=1)
    bounds = np.concatenate([[0], np.where(change)[0] + 1, [len(ks)]])
    centers = [pts[order[bounds[i]:bounds[i + 1]]].mean(axis=0) for i in range(len(bounds) - 1)]
    return np.array(centers), []


def cluster_hotspots(pts, radius):
    """3D 점들을 radius 격자의 26이웃 연결로 묶어 (군집중심, 멤버인덱스들) 리스트 반환."""
    pts = np.asarray(pts, float)
    if len(pts) == 0:
        return []
    keys = np.floor(pts / radius).astype(np.int64)
    cell = {}
    for i, k in enumerate(map(tuple, keys)):
        cell.setdefault(k, []).append(i)
    cell_keys = list(cell.keys()); kindex = {k: c for c, k in enumerate(cell_keys)}
    parent = list(range(len(cell_keys)))
    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a
    neigh = [(dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)]
    for c, k in enumerate(cell_keys):
        for d in neigh:
            nk = (k[0] + d[0], k[1] + d[1], k[2] + d[2])
            if nk in kindex:
                ra, rb = find(c), find(kindex[nk])
                if ra != rb:
                    parent[ra] = rb
    groups = {}
    for c, k in enumerate(cell_keys):
        groups.setdefault(find(c), []).extend(cell[k])
    return [(pts[idxs].mean(axis=0), idxs) for idxs in groups.values()]


def dbscan(pts, eps, min_samples):
    """scipy cKDTree 기반 DBSCAN. 라벨 배열 반환(-1=노이즈). sklearn 불필요.
    코어점(이웃 ≥ min_samples)을 통해 밀도 도달 가능한 점을 한 군집으로 묶는다."""
    n = len(pts)
    labels = np.full(n, -1, np.int64)
    if n == 0:
        return labels
    tree = cKDTree(pts)
    neigh = tree.query_ball_tree(tree, eps)                     # 각 점의 eps 이웃 인덱스
    core = np.fromiter((len(neigh[i]) >= min_samples for i in range(n)), bool, n)
    cid = -1
    for i in range(n):
        if labels[i] != -1 or not core[i]:
            continue
        cid += 1
        labels[i] = cid
        stack = list(neigh[i])
        while stack:                                            # 밀도 도달 확장(BFS/DFS)
            q = stack.pop()
            if labels[q] == -1:
                labels[q] = cid
                if core[q]:
                    stack.extend(neigh[q])
    return labels


def _densify(poly, step, max_gap=0.3):
    """폴리라인 정점 사이를 최대 step 간격으로 채운 점 배열(원 정점 포함). DBSCAN 전에 적용하면
    중심선 정점이 성겨도 크랙 내부 연속성이 보장돼, eps 는 순수하게 '갭 브리징'만 담당한다.
    단, 한 스텝이 max_gap 을 넘으면(역투영 오류로 생긴 수십 cm~수십 m 점프) 채우지 않고 끊는다
    — 유령 점 폭발과 빈 공간을 잇는 가짜 선을 방지한다."""
    poly = np.asarray(poly, float).reshape(-1, 3)
    if len(poly) < 2:
        return poly
    out = [poly[0]]
    for a, b in zip(poly[:-1], poly[1:]):
        seg = b - a
        L = float(np.linalg.norm(seg))
        if L > max_gap:
            out.append(b)                       # 비정상 점프 → 채우지 않음(자연스러운 끊김)
        elif L > step:
            k = int(np.ceil(L / step))
            for t in range(1, k + 1):
                out.append(a + seg * (t / k))
        else:
            out.append(b)
    return np.asarray(out)


def graph_length(pts, eps):
    """점군을 eps 이내로 이은 그래프에서 '최장 최단경로'(가중 지름)를 실측 길이로 반환한다.
    여러 뷰가 같은 크랙을 겹쳐 봐도 지름은 끝-끝 거리라 중복이 자동 정리된다(길이 이중계산 없음).
    이중 스윕(임의점→최원점 a→최원점) — 트리형 골격에서 정확, 일반 그래프에서도 좋은 근사."""
    pts = np.asarray(pts, float)
    n = len(pts)
    if n < 2:
        return 0.0
    tree = cKDTree(pts)
    pairs = tree.query_pairs(eps, output_type='ndarray')
    if len(pairs) == 0:
        return 0.0
    d = np.linalg.norm(pts[pairs[:, 0]] - pts[pairs[:, 1]], axis=1)
    rows = np.concatenate([pairs[:, 0], pairs[:, 1]])
    cols = np.concatenate([pairs[:, 1], pairs[:, 0]])
    W = csr_matrix((np.concatenate([d, d]), (rows, cols)), shape=(n, n))
    ncomp, lab = connected_components(W, directed=False)
    best = 0.0
    for c in range(ncomp):
        idx = np.where(lab == c)[0]
        if len(idx) < 2:
            continue
        sub = W[idx][:, idx]
        d0 = dijkstra(sub, indices=0)
        d0[~np.isfinite(d0)] = -1
        a = int(np.argmax(d0))
        d1 = dijkstra(sub, indices=a)
        fin = d1[np.isfinite(d1)]
        if len(fin):
            best = max(best, float(fin.max()))
    return best


def arc_length_mm(poly, max_gap=0.3):
    """순서 있는 3D 폴리라인의 호 길이(mm). 연속 정점 거리 합 — 순서를 알 때 가장 자연스럽다.
    한 구간이 max_gap(=30cm, 역투영 유령 점프)보다 크면 제외한다. graph_length 는 eps 연결성이
    성긴 정점(>eps)에서 끊겨 길이를 과소평가하지만, 호 길이는 순서를 그대로 써서 그 문제가 없다."""
    p = np.asarray(poly, float)
    if len(p) < 2:
        return 0.0
    seg = np.linalg.norm(np.diff(p, axis=0), axis=1)
    return float(seg[seg <= max_gap].sum()) * 1000.0


def fuse_points(pts, ws, ns, voxel):
    """여러 뷰의 중심선 점을 '하나의 크랙 리본'으로 융합한다.
    ① 복셀 다운샘플로 겹친 뷰(평행 중복)를 한 점으로 합치고(폭=최대, 법선=평균),
    ② 최소신장트리(MST)로 뼈대를 잡아 DFS 순회로 '연속 정점이 인접'한 순서 있는 폴리라인을 만든다.
    반환: (poly[[x,y,z]], poly_w[mm], poly_n[[nx,ny,nz]]). 크랙당 리본 1개 → 겹침 없음·클릭 명확."""
    P = np.asarray(pts, float)
    W = np.array([(w if w is not None else 0.0) for w in ws], float)
    haveN = ns is not None and len(ns) == len(P)
    N = np.asarray(ns, float) if haveN else np.zeros((len(P), 3))
    if len(P) == 0:
        return [], [], []
    # ① 복셀 병합
    vox = {}
    for i in range(len(P)):
        k = (int(np.floor(P[i, 0] / voxel)), int(np.floor(P[i, 1] / voxel)), int(np.floor(P[i, 2] / voxel)))
        v = vox.get(k)
        if v is None:
            vox[k] = [P[i].copy(), float(W[i]), N[i].copy(), 1]
        else:
            v[0] += P[i]; v[1] = max(v[1], float(W[i])); v[2] += N[i]; v[3] += 1
    vp = np.array([v[0] / v[3] for v in vox.values()])
    vw = np.array([v[1] for v in vox.values()])
    vn = np.array([v[2] / v[3] for v in vox.values()])
    nl = np.linalg.norm(vn, axis=1, keepdims=True); nl[nl < 1e-9] = 1.0; vn = vn / nl
    n = len(vp)
    if n < 2:
        return vp.round(5).tolist(), vw.round(3).tolist(), vn.round(4).tolist()
    # ② MST → DFS 순서화(모든 가지 포함, 인접 유지)
    tree = cKDTree(vp)
    pairs = tree.query_pairs(voxel * 3.0, output_type='ndarray')
    if len(pairs) == 0:
        return vp.round(5).tolist(), vw.round(3).tolist(), vn.round(4).tolist()
    d = np.linalg.norm(vp[pairs[:, 0]] - vp[pairs[:, 1]], axis=1)
    mst = minimum_spanning_tree(csr_matrix((d, (pairs[:, 0], pairs[:, 1])), shape=(n, n))).tocoo()
    adj = [[] for _ in range(n)]
    for a, b in zip(mst.row.tolist(), mst.col.tolist()):
        adj[a].append(b); adj[b].append(a)
    start = next((i for i in range(n) if len(adj[i]) == 1), 0)   # 끝점(leaf)에서 시작
    order = [start]; seen = [False] * n; seen[start] = True; stack = [start]
    while stack:
        cur = stack[-1]; nxt = next((nb for nb in adj[cur] if not seen[nb]), None)
        if nxt is not None:
            seen[nxt] = True; stack.append(nxt); order.append(nxt)
        else:
            stack.pop()
            if stack:
                order.append(stack[-1])       # 되돌아가는 한 걸음(인접) → 다음 가지로
    idx = np.array(order)
    return vp[idx].round(5).tolist(), vw[idx].round(3).tolist(), vn[idx].round(4).tolist()


# ==================================================================
# 2. 크랙 세그멘테이션 (best.pt · 타일 추론 + FP16 배치 가속)
# ==================================================================
def _tile_coords(size, tile, overlap):
    """한 축을 tile 크기로 겹쳐 덮는 시작좌표들(마지막은 가장자리에 정렬)."""
    step = max(1, tile - overlap)
    if size <= tile:
        return [0]
    cs = list(range(0, size - tile + 1, step))
    if cs[-1] != size - tile:
        cs.append(size - tile)
    return cs


def _hann2d(h, w, floor=0.12):
    """타일 이음매를 부드럽게 하는 2D Hann 가중창(경계 손실 방지용 floor 포함)."""
    wy = np.hanning(h) if h > 1 else np.ones(1)
    wx = np.hanning(w) if w > 1 else np.ones(1)
    return (np.outer(wy, wx).astype(np.float32)) * (1.0 - floor) + floor


class CrackSegmenter:
    """best.pt(4채널, 크랙=채널1)로 raw-BGR 타일 추론해 크랙 확률맵/마스크를 낸다.

    타일 크기 주의: DeepLabV3 의 ASPP 에는 AdaptiveAvgPool2d(1) 전역 풀링 브랜치가 있어
    '타일 크기 = 전역 문맥 창'이다. 즉 tile 을 바꾸면 모델 동작 자체가 바뀐다(단순 분할이 아님).
    512→1024 는 실제 콘크리트 크랙 이미지에서 검증했다: 엄격 IoU 는 0.86 이지만 크랙 두께가
    중앙값 2.8px 라 경계 1px 흔들림만으로 그 정도가 빠진다. ±1px 허용 시 IoU 0.991 로 사실상 동일.
    처리 픽셀은 36.7M→25.2M, 타일 수는 140→24 로 줄어든다."""
    def __init__(self, model, device, thr=CRACK_THR, tile=1024, overlap=128, crack_index=1):
        import torch  # noqa
        self.model = model.eval().to(device)
        self.device = device
        self.dtype = next(self.model.parameters()).dtype     # FP16/FP32 자동 대응
        self.thr, self.tile, self.overlap, self.crack_index = thr, tile, overlap, crack_index

    def _predict_tile(self, tile_bgr):
        """단일 타일(가변 크기) 추론. 16의 배수로 반사패딩 후 크랙 확률맵 반환."""
        import torch
        x = tile_bgr.astype(np.float32); h, w = x.shape[:2]
        ph, pw = (16 - h % 16) % 16, (16 - w % 16) % 16
        if ph or pw:
            x = np.pad(x, ((0, ph), (0, pw), (0, 0)), mode="reflect")
        t = torch.from_numpy(np.ascontiguousarray(x.transpose(2, 0, 1)[None])).to(self.device, self.dtype)
        with torch.no_grad():
            out = self.model(t); out = out["out"] if isinstance(out, dict) else out
            prob = torch.softmax(out.float(), dim=1)[0, self.crack_index]
        return prob.detach().cpu().numpy()[:h, :w]

    def predict_prob(self, img_bgr):
        """이미지를 타일로 나눠 확률맵을 Hann 가중 평균으로 합성(폴백 경로)."""
        H, W = img_bgr.shape[:2]
        if H <= self.tile and W <= self.tile:
            return self._predict_tile(img_bgr)
        acc = np.zeros((H, W), np.float32); wsum = np.zeros((H, W), np.float32)
        for y in _tile_coords(H, self.tile, self.overlap):
            for x in _tile_coords(W, self.tile, self.overlap):
                th, tw = min(self.tile, H - y), min(self.tile, W - x)
                prob = self._predict_tile(img_bgr[y:y + th, x:x + tw]); win = _hann2d(th, tw)
                acc[y:y + th, x:x + tw] += prob * win; wsum[y:y + th, x:x + tw] += win
        wsum[wsum == 0] = 1e-6
        return acc / wsum

    def predict_prob_batched(self, img_bgr, batch_size=CRACK_BATCH):
        """타일을 배치로 묶어 한 번에 forward(가속). 큰 이미지에서 균일 512 타일에 유효."""
        import torch
        H, W = img_bgr.shape[:2]
        if H <= self.tile or W <= self.tile:
            return self.predict_prob(img_bgr)
        coords = [(y, x) for y in _tile_coords(H, self.tile, self.overlap)
                         for x in _tile_coords(W, self.tile, self.overlap)]
        acc = np.zeros((H, W), np.float32); wsum = np.zeros((H, W), np.float32); win = _hann2d(self.tile, self.tile)
        for i in range(0, len(coords), batch_size):
            ch = coords[i:i + batch_size]
            batch = [img_bgr[y:y + self.tile, x:x + self.tile].astype(np.float32).transpose(2, 0, 1) for (y, x) in ch]
            t = torch.from_numpy(np.ascontiguousarray(np.stack(batch))).to(self.device, self.dtype)
            with torch.no_grad():
                out = self.model(t); out = out["out"] if isinstance(out, dict) else out
                prob = torch.softmax(out.float(), dim=1)[:, self.crack_index].cpu().numpy()
            for j, (y, x) in enumerate(ch):
                acc[y:y + self.tile, x:x + self.tile] += prob[j] * win; wsum[y:y + self.tile, x:x + self.tile] += win
        wsum[wsum == 0] = 1e-6
        return acc / wsum

    def predict_mask(self, img_bgr):
        """이진 크랙 마스크(0/255) 반환."""
        return (self.predict_prob_batched(img_bgr) >= self.thr).astype(np.uint8) * 255


# ==================================================================
# 3. 크랙 정량화 (중심선 길이 + 거리변환 기반 직교 폭)
# ==================================================================
SQRT2 = np.sqrt(2.0)
_OFF = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
_OFF_D = [SQRT2, 1.0, SQRT2, 1.0, 1.0, SQRT2, 1.0, SQRT2]


def _neighbor_count(skel):
    """각 스켈레톤 픽셀의 8이웃 중 스켈레톤 개수 맵."""
    k = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], np.uint8)
    return ndi.convolve(skel.astype(np.uint8), k, mode="constant") * skel


def nc_at(skel, y, x):
    """(y,x)의 8이웃 스켈레톤 개수."""
    H, W = skel.shape
    return sum(1 for dy, dx in _OFF if 0 <= y + dy < H and 0 <= x + dx < W and skel[y + dy, x + dx])


def _order_skeleton(skel, max_pts=256):
    """1px 중심선을 '모든 가지를 포함해' 순서화한 [x,y] 리스트로 반환.
    분기점에서 한 갈래만 걷고 멈추던 옛 방식은 거미줄/분기 크랙의 가지를 통째로 버렸다(측정: 웹형
    이미지에서 골격의 최대 26% 유실). 여기서는 DFS 순회로 골격 전체를 덮되, 되돌아갈 때 부모를
    다시 담아 '연속 정점이 항상 인접'하도록 한다(가지 왕복은 같은 자리를 겹쳐 지나 시각적으로 무해).
    길이는 이 순회가 아니라 골격 자체(_scaled_length)로 재므로 왕복 중복이 길이를 부풀리지 않는다."""
    ys, xs = np.nonzero(skel)
    if len(xs) == 0:
        return []
    yl, xl = ys.tolist(), xs.tolist()
    pts = set(zip(yl, xl))
    start = next(((y, x) for y, x in zip(yl, xl) if nc_at(skel, y, x) == 1), (yl[0], xl[0]))

    def _unvisited(p, seen):
        return next(((p[0] + dy, p[1] + dx) for dy, dx in _OFF
                     if (p[0] + dy, p[1] + dx) in pts and (p[0] + dy, p[1] + dx) not in seen), None)

    visited = {start}; order = [start]; stack = [start]
    while stack:
        nxt = _unvisited(stack[-1], visited)
        if nxt is not None:
            visited.add(nxt); stack.append(nxt); order.append(nxt)
        else:
            stack.pop()
            if stack:
                order.append(stack[-1])          # 되돌아가는 한 걸음(항상 인접) → 다음 가지로 이어짐
    if len(order) > max_pts:
        order = [order[i] for i in np.linspace(0, len(order) - 1, max_pts).astype(int)]
    return [[int(x), int(y)] for (y, x) in order]


def _elong(ys, xs):
    """픽셀 좌표 PCA 로 길쭉함(주축/부축 표준편차비). 얇은 선=큼, 뭉툭한 blob=1 근처."""
    if len(xs) < 3:
        return 1.0
    cov = np.cov(np.vstack([xs, ys]).astype(float))
    ev = np.linalg.eigvalsh(cov)
    return float(np.sqrt(max(ev[1], 1e-9) / max(ev[0], 1e-9)))


def _skeleton_length(skel):
    """중심선 총 길이(px): 인접 스켈레톤 쌍의 유클리드 거리 합(대각선 √2 반영)."""
    total = 0.0; H, W = skel.shape
    ys, xs = np.nonzero(skel)
    for y, x in zip(ys, xs):
        for (dy, dx), d in zip(_OFF, _OFF_D):
            ny, nx = y + dy, x + dx
            if 0 <= ny < H and 0 <= nx < W and skel[ny, nx] and (ny * W + nx) > (y * W + x):
                total += d
    return float(total)


def _prune_spurs(skel, min_len):
    """끝점에서 분기점까지 걸어가며 길이가 min_len 미만인 짧은 가지(burr)를 제거한다."""
    skel = skel.copy()
    if min_len <= 0:
        return skel
    H, W = skel.shape
    for _ in range(20):
        nc = _neighbor_count(skel)
        endpoints = list(zip(*np.nonzero((nc == 1) & skel)))
        if not endpoints:
            break
        removed = False
        for (ey, ex) in endpoints:
            if not skel[ey, ex]:
                continue
            path = [(ey, ex)]; py, px = -1, -1; cy, cx = ey, ex; length = 0.0
            hit_junction = False
            while True:
                nbrs = [(cy + dy, cx + dx, d) for (dy, dx), d in zip(_OFF, _OFF_D)
                        if 0 <= cy + dy < H and 0 <= cx + dx < W and skel[cy + dy, cx + dx]
                        and not (cy + dy == py and cx + dx == px)]
                if len(nbrs) != 1:
                    break                                  # 반대 끝점(고립된 선) — 분기 아님
                ny, nx, d = nbrs[0]; length += d
                if nc_at(skel, ny, nx) >= 3:
                    hit_junction = True                    # 분기점에 닿음 = 진짜 스퍼
                    break
                path.append((ny, nx)); py, px = cy, cx; cy, cx = ny, nx
            # 분기점에 붙은 짧은 가지(스퍼)만 제거한다. 고립된 짧은 선(=짧은 얇은 크랙)은 통째로
            # 지우지 않는다 — 옛 방식은 min_len 미만 고립선을 삭제해 짧은 크랙을 잃었다.
            if length < min_len and hit_junction:
                for (ry, rx) in path:
                    skel[ry, rx] = False
                removed = True
        if not removed:
            break
    return skel & (_neighbor_count(skel) >= 1)


def _scaled_length(skel, scale_map):
    """픽셀별 스케일(mm/px)이 있을 때의 길이: 각 간선 길이 × 두 끝점 평균 스케일."""
    total = 0.0; H, W = skel.shape
    ys, xs = np.nonzero(skel)
    for y, x in zip(ys, xs):
        for (dy, dx), d in zip(_OFF, _OFF_D):
            ny, nx = y + dy, x + dx
            if 0 <= ny < H and 0 <= nx < W and skel[ny, nx] and (ny * W + nx) > (y * W + x):
                total += d * 0.5 * (scale_map[y, x] + scale_map[ny, nx])
    return float(total)


class CrackMetrics:
    """정량화 결과 컨테이너(속성 접근용)."""
    def __init__(self, d):
        self.__dict__.update(d)


ROI_PAD = 2      # 컴포넌트 bbox 여유. ≥1 이면 EDT·컨볼루션 경계가 전체 프레임과 동일해진다.


def quantify_crack(mask, mm_per_px=None, min_branch_px=10, min_object_px=30, bridge_px=0):
    """이진 마스크 → 크랙 인스턴스별 길이·최대/평균 폭. mm_per_px 가 (H,W) 배열이면
    픽셀별 스케일(실척)로 mm 단위 산출, None 이면 px 단위.

    크랙은 화면의 0.01% 수준(2000만 픽셀 중 2천 개)이라 전체 프레임에 EDT/스켈레톤을 돌리면
    필요한 일의 수천 배를 하게 된다 → 컴포넌트 bbox 로 잘라서 처리한다. 컴포넌트끼리는 최소 1px
    배경으로 떨어져 있으므로(8이웃 라벨링) 패딩만 주면 결과는 전체 프레임과 동일하다.

    bridge_px>0 이면 모폴로지 클로징으로 그 반경 안에서 끊긴 크랙을 하나로 잇는다(직선 부분 두께는
    보존되고 갭만 메워짐). 검출 마스크 오버레이는 원본 그대로 두고 여기(3D 인스턴스화)에서만 적용."""
    mask = np.asarray(mask)
    if mask.ndim == 3:
        mask = mask[..., 0]
    binm = mask > 0
    empty = CrackMetrics(dict(unit="mm" if mm_per_px is not None else "px", n_instances=0,
                              total_length=0.0, max_width=0.0, mean_width=0.0, p95_width=0.0,
                              max_width_xy=(0, 0), instances=[]))
    if not binm.any():
        return empty
    if bridge_px and bridge_px > 0:
        k = 2 * int(bridge_px) + 1
        ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        binm = cv2.morphologyEx(binm.astype(np.uint8), cv2.MORPH_CLOSE, ker).astype(bool)

    s3 = np.ones((3, 3), int)
    lbl0, n0 = ndi.label(binm, structure=s3)
    counts = np.bincount(lbl0.ravel()); counts[0] = 0
    keep = set(np.nonzero(counts >= int(min_object_px))[0].tolist())      # 큰 것: 모양 무관 유지
    # 작아도 '길쭉한'(얇은 크랙) 컴포넌트는 살리고, 작고 뭉툭한(노이즈 blob) 것만 버린다.
    cand = np.nonzero((counts >= MIN_THIN_PX) & (counts < int(min_object_px)))[0]
    if len(cand):
        objs0 = ndi.find_objects(lbl0)
        for ci in cand:
            sl = objs0[ci - 1]
            yy, xx = np.nonzero(lbl0[sl] == ci)
            if _elong(yy, xx) >= THIN_ELONG:
                keep.add(int(ci))
    if not keep:
        keep = set(range(1, n0 + 1))                                     # 안전장치(기존 동작)

    is_mm = mm_per_px is not None
    scale_map = mm_per_px.astype(np.float64) if isinstance(mm_per_px, np.ndarray) else None
    scale_scalar = float(mm_per_px) if (is_mm and scale_map is None) else 1.0

    H, W = binm.shape
    slices = ndi.find_objects(lbl0)
    instances = []; widths_all = []; total_len_u = 0.0
    for i in range(1, n0 + 1):
        sl = slices[i - 1]
        if i not in keep or sl is None:
            continue
        y0, y1 = max(0, sl[0].start - ROI_PAD), min(H, sl[0].stop + ROI_PAD)
        x0, x1 = max(0, sl[1].start - ROI_PAD), min(W, sl[1].stop + ROI_PAD)
        sub = (lbl0[y0:y1, x0:x1] == i)                       # 이 컴포넌트만
        dist, _edt_idx = ndi.distance_transform_edt(sub, return_indices=True)  # 반경 + 최근접 배경픽셀 좌표
        skel = _prune_spurs(skeletonize(sub), min_branch_px)  # 중심선
        if not skel.any():
            continue
        cy, cx = np.nonzero(skel); cw_px = dist[cy, cx] * 2.0
        if scale_map is not None:
            smap = scale_map[y0:y1, x0:x1]
            cw_u = cw_px * smap[cy, cx]; L_u = _scaled_length(skel, smap)
        elif is_mm:
            cw_u = cw_px * scale_scalar; L_u = _skeleton_length(skel) * scale_scalar
        else:
            cw_u = cw_px; L_u = _skeleton_length(skel)
        total_len_u += L_u; widths_all.append(cw_u); j = int(np.argmax(cw_u))
        _ordered = _order_skeleton(skel)                       # [(x,y),...] (크롭 좌표)
        _cl = [[x + x0, y + y0] for (x, y) in _ordered]        # 중심선(원본 프레임 좌표)
        # 각 중심선 점의 '가장 가까운 배경 픽셀'(EDT 특징변환). 중심점과 이 경계점을 각각 메시에
        # 역투영해 3D 거리×2 로 폭을 재는 데 쓴다(3D 폭 프로토타입).
        _edge = [[int(_edt_idx[1][y, x]) + x0, int(_edt_idx[0][y, x]) + y0] for (x, y) in _ordered]
        # 폭 프로파일: 정점별 국소 폭(mm). 중심선과 같은 순서 → 3D 리본으로 표면에 얹고
        # 통계(중앙값·p95)에 쓴다. dist·smap 모두 ROI 좌표라 (x,y) 로 직접 인덱싱.
        if scale_map is not None:
            _wp = [float(dist[y, x] * 2.0 * smap[y, x]) for (x, y) in _ordered]
        elif is_mm:
            _wp = [float(dist[y, x] * 2.0 * scale_scalar) for (x, y) in _ordered]
        else:
            _wp = [float(dist[y, x] * 2.0) for (x, y) in _ordered]
        instances.append(dict(id=i, length=float(L_u), max_width=float(cw_u.max()),
                              mean_width=float(cw_u.mean()),
                              max_width_xy=(int(cx[j] + x0), int(cy[j] + y0)),
                              centerline=_cl, edge=_edge, width_profile=_wp))
    if not instances:
        return empty
    allw = np.concatenate(widths_all)
    best = max(instances, key=lambda d: d["max_width"])       # 전역 최대폭 = 컴포넌트 최대폭들의 최대
    max_w, max_xy = best["max_width"], best["max_width_xy"]
    mean_w, p95_w = float(allw.mean()), float(np.percentile(allw, 95))
    instances.sort(key=lambda d: -d["max_width"])
    for rank, it in enumerate(instances, 1):
        it["id"] = rank
    return CrackMetrics(dict(unit="mm" if is_mm else "px", n_instances=len(instances),
                             total_length=float(total_len_u), max_width=max_w, mean_width=mean_w,
                             p95_width=p95_w, max_width_xy=max_xy, instances=instances))


# ==================================================================
# 4. 실척 측정 헬퍼 (경사 보정 스케일 맵 · 3D 센터라인 길이)
# ==================================================================
def build_scale_map(shape, uv, locs, ray_idx, tri_idx, d_world, C, fx, mesh, fallback):
    """픽셀별 mm/px 스케일 맵. mm/px=(z/f)/cosθ×1000 (깊이 z, 시선-법선 각 θ=경사 보정)."""
    H, W = shape
    scale = np.full((H, W), fallback, np.float32)
    if len(ray_idx) == 0:
        return scale
    fn = mesh.face_normals[tri_idx]
    z = np.linalg.norm(C - locs, axis=1)
    cos_t = np.clip(np.abs(np.einsum('ij,ij->i', d_world[ray_idx], fn)), COS_CLAMP, None)
    mmpp = (z / fx) / cos_t * 1000.0
    yy = uv[ray_idx, 1].astype(int); xx = uv[ray_idx, 0].astype(int)
    ok = (yy >= 0) & (yy < H) & (xx >= 0) & (xx < W)
    scale[yy[ok], xx[ok]] = mmpp[ok].astype(np.float32)
    return scale


def crack_length_3d(skeleton_mask, K, dist, R, C, mesh):
    """[정련용] 중심선 픽셀을 3D로 역투영해 인접 스켈레톤 간 3D 거리를 합산 → 실표면 길이(m).
    원근·경사·곡면을 모두 자동 보정한다(향후 스케일맵 길이 대체/검증용)."""
    ys, xs = np.where(np.asarray(skeleton_mask) > 0)
    if len(xs) == 0:
        return 0.0
    uv = np.column_stack([xs, ys]).astype(float)
    locs, ray_idx, _, _ = backproject(uv, K, dist, R, C, mesh)
    P = {(int(uv[ray_idx[k], 1]), int(uv[ray_idx[k], 0])): locs[k] for k in range(len(ray_idx))}
    total = 0.0
    for (y, x), p in P.items():
        for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
            if (y + dy, x + dx) in P:
                total += float(np.linalg.norm(p - P[(y + dy, x + dx)]))
    return total


# ==================================================================
# 5. 보고서 생성
# ==================================================================
_segmenter = None


def get_segmenter(log):
    """best.pt(전체모델)를 로드해 CrackSegmenter 를 1회 생성/캐시한다.
    GPU 에서는 FP16(.half())로 변환 — 추론 속도↑·VRAM 절반. CPU 는 FP16 이 에뮬레이션이라 오히려
    느리므로 FP32 유지. 추론 시 softmax 는 out.float() 로 계산하므로 수치 안정성은 유지된다."""
    global _segmenter
    if _segmenter is None:
        import torch
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        # 타일이 항상 같은 크기(512×512)라 cuDNN 알고리즘 오토튠이 그대로 재사용된다.
        torch.backends.cudnn.benchmark = True
        log(f"크랙 모델(best.pt) 로드... [{dev}]")
        m = torch.load(CRACK_WEIGHTS, map_location=dev, weights_only=False)
        if dev == "cuda" and USE_FP16:
            m = m.half(); log("  FP16 양자화 적용(GPU)")
        elif dev == "cpu":
            m = m.float()
        _segmenter = CrackSegmenter(m, dev, thr=CRACK_THR)
        log("크랙 모델 준비 완료.")
    return _segmenter


def build_scene_data(folder, ply_path, log, progress, detect=True, thr=CRACK_THR, limit=None):
    """이미지들을 크랙 추론·3D 역투영해 scene_data(메시+카메라+크랙점군+크랙핫스팟)를 만든다.
    핫스팟은 경사보정 스케일맵으로 폭·길이를 mm 산출하고, 병합 시 최적(정면·근접) 뷰를 채택한다."""
    import trimesh
    from PIL import Image, ImageOps
    log("메시(PLY) 로드...")
    # 예전에는 여기서 PLY 를 통째로 base64 문자열로 만들어(1.2GB → 1.6GB str) 함수 끝까지 붙들고
    # 있었다. 메시 로드·이미지 594장 처리 내내 1.6GB 를 점유해 큰 모델에서 MemoryError 가 났다.
    # v2 는 base64 를 쓰지 않고 저장 시점에 PLY 파일을 zip 으로 바로 복사한다.
    mesh = trimesh.load(ply_path, force='mesh')
    if mesh.is_empty or len(mesh.faces) == 0:
        raise RuntimeError("PLY에 삼각형이 없습니다(메시가 아닌 포인트클라우드).")
    diag = float(np.linalg.norm(mesh.bounds[1] - mesh.bounds[0]))
    txt_radius = TEXTPOINT_MERGE_M                                   # 절대값(m) — 모델 크기 무관
    log(f"메시 대각선 {diag:.1f}m · DBSCAN eps {DBSCAN_EPS_M*1000:.0f}mm · 복셀 {txt_radius*1000:.0f}mm")

    all_found = find_pairs(folder)
    no_xmp = [os.path.basename(i) for (i, x) in all_found if not x]
    pairs = [(i, x) for (i, x) in all_found if x]
    if limit:
        pairs = pairs[:limit]
    # 조용한 실패 방지: XMP 없어 제외되는 이미지를 명시한다. 예전엔 말없이 버려서 사용자가
    # 1288장을 넣고도 594장만 분석된 걸 몰랐다.
    if no_xmp:
        preview = ", ".join(no_xmp[:5]) + (f" 외 {len(no_xmp)-5}장" if len(no_xmp) > 5 else "")
        log(f"⚠ XMP(카메라 파라미터) 없어 제외: {len(no_xmp)}장 [{preview}]")
    log(f"이미지 {len(all_found)}장 발견 · 분석 대상 {len(pairs)}장 (detect={'ON' if detect else 'OFF'})")
    seg = get_segmenter(log) if detect else None
    all_cameras, all_hotspots, all_crack_pts = [], [], []
    skipped = list(no_xmp)                       # 분석에서 빠진 이미지(사유 불문) 집계

    # ---- 파이프라이닝: ①로드(스레드 프리페치) → ②GPU 추론(메인) → ③후처리(워커) ----
    # 프로파일(6장 실측): 추론 92% · 후처리 8%(역투영+정량화) · 로드 <1%.
    # GPU 추론이 지배적이라 이득 상한은 '후처리+로드를 추론 뒤에 숨기는' ≈8%지만
    # 무손실 스케줄링 최적화이고, GPU 가 빨라질수록 겹침 비중이 커진다.
    # 스레드 안전: GPU 는 메인 스레드 전용, 메시 레이캐스트는 후처리 워커(1개) 전용.
    # 무거운 구간(PIL 디코드·cv2·embree·skimage)은 GIL 을 놓아 실제로 겹친다.
    from concurrent.futures import ThreadPoolExecutor
    from collections import deque

    def _load_one(n, img_path, xmp_path):
        """①로드: 디코드·방향정렬·JPEG 인코딩 — GPU 와 무관한 준비 작업 전부."""
        name = os.path.basename(img_path)
        K, dist, R, T, calib_wh = parse_xmp(xmp_path)
        pil = ImageOps.exif_transpose(Image.open(img_path))
        disp = cv2.cvtColor(np.array(pil.convert("RGB")), cv2.COLOR_RGB2BGR)
        disp = orient_to_xmp(disp, calib_wh)
        dh, dw = disp.shape[:2]
        C = pick_center(K, R, T)
        cam = camera_pose(K, R, T, dw, dh)
        cam.update({"id": n, "source": name,
                    # 원본 JPEG 바이트. data-URI/base64 로 부풀리지 않는다(저장 시 zip 에 그대로).
                    "_thumb": encode_jpeg_bytes(disp, THUMB_MAX_PX, 75),
                    "_full": encode_jpeg_bytes(disp, FULL_MAX_PX, FULL_JPEG_Q)})
        return {"n": n, "name": name, "K": K, "dist": dist, "R": R, "C": C,
                "cam": cam, "disp": disp, "dh": dh, "dw": dw}

    def _post_one(it, prob, mask):
        """③후처리: 마스크 인코딩·역투영·스케일맵·정량화 — 결과는 메인에서 이미지 순서대로 수확."""
        n = it["n"]; name = it["name"]
        K = it["K"]; dist = it["dist"]; R = it["R"]; C = it["C"]
        dh = it["dh"]; dw = it["dw"]
        crack_pts = []; hots = []
        mask_png = encode_mask_png(mask, FULL_MAX_PX)   # 이미지 탭 오버레이용
        fx = float(K[0, 0])
        ys, xs = np.where(mask > 0); uv = np.column_stack([xs, ys]).astype(float)
        if len(uv) > SCALE_MAP_CAP:
            uv = uv[np.random.default_rng(n).choice(len(uv), SCALE_MAP_CAP, replace=False)]
        locs, ray_idx, tri_idx, dirs = backproject(uv, K, dist, R, C, mesh)
        if len(locs):
            cloud = locs
            if len(cloud) > MAX_CRACK_POINTS_PER_IMG:
                cloud = cloud[np.random.default_rng(n + 1).choice(len(cloud), MAX_CRACK_POINTS_PER_IMG, replace=False)]
            crack_pts.extend(cloud.tolist())
            med_depth = float(np.median(np.linalg.norm(C - locs, axis=1)))
        else:
            med_depth = 1.0
        smap = build_scale_map(mask.shape, uv, locs, ray_idx, tri_idx, dirs, C, fx, mesh, med_depth / fx * 1000.0)
        met = quantify_crack(mask, mm_per_px=smap, min_branch_px=4,   # 웹 가지 보존
                             bridge_px=CRACK_BRIDGE_PX)               # 5px 이내 끊긴 크랙 잇기
        for inst in met.instances:
            px, py = inst["max_width_xy"]
            loc, _r, _t, _d = backproject(np.array([[px, py]], float), K, dist, R, C, mesh)
            if len(loc) == 0:
                continue
            p3 = loc[0]; z = float(np.linalg.norm(C - p3))
            cosq = abs(float(np.dot(_d[0], mesh.face_normals[int(_t[0])]))) if len(_t) else 1.0
            conf = float(prob[int(py), int(px)]) if (0 <= py < dh and 0 <= px < dw) else 1.0
            # 크랙 중심선 → 3D 폴리라인(poly): 메시에 맞은 점만 순서 유지
            cl = inst.get("centerline") or []
            wp = inst.get("width_profile") or []
            poly = []; poly_w = []; poly_n = []; hit = {}
            if cl:
                cuv = np.array(cl, float)
                clocs, cray, _ct, _cd = backproject(cuv, K, dist, R, C, mesh)
                hit = {int(cray[k]): clocs[k] for k in range(len(cray))}
                tri_of = {int(cray[k]): int(_ct[k]) for k in range(len(cray))}  # 정점→맞은 면
                for i in range(len(cuv)):                 # 메시에 맞은 정점만, 순서·폭·법선 유지
                    if i in hit:
                        poly.append([round(float(hit[i][0]), 5), round(float(hit[i][1]), 5), round(float(hit[i][2]), 5)])
                        poly_w.append(round(float(wp[i]), 3) if i < len(wp) else None)
                        nrm = mesh.face_normals[tri_of[i]].astype(float)
                        if float(np.dot(nrm, C - hit[i])) < 0:     # 카메라(바깥) 쪽으로 향하게
                            nrm = -nrm
                        poly_n.append([round(float(nrm[0]), 4), round(float(nrm[1]), 4), round(float(nrm[2]), 4)])
            # [프로토타입] 3D 폭: 중심점 C 와 최근접 경계점 B 를 각각 역투영해 |C3d-B3d|×2.
            # 경사·곡면의 이방성 foreshortening 을 실측으로 반영(2D 방식의 등방 cosθ 근사 대체).
            width_mm_3d = None
            edge = inst.get("edge") or []
            if cl and edge:
                elocs, eray, _et, _ed = backproject(np.array(edge, float), K, dist, R, C, mesh)
                ehit = {int(eray[k]): elocs[k] for k in range(len(eray))}
                w3 = [2000.0 * float(np.linalg.norm(hit[i] - ehit[i]))   # ×2(반→전폭) ×1000(m→mm)
                      for i in range(len(cl)) if i in hit and i in ehit]
                if w3:
                    width_mm_3d = round(max(w3), 3)
            hots.append({"conf": round(conf, 3),
                         "xyz": [float(p3[0]), float(p3[1]), float(p3[2])],
                         "width_mm": round(float(inst["max_width"]), 3),
                         "width_mm_3d": width_mm_3d,   # 프로토타입(사장님 제안 방식)
                         "length_mm": round(float(inst["length"]), 2),
                         "quality": round(cosq / max(z, 1e-6), 6),
                         "poly": poly, "poly_w": poly_w, "poly_n": poly_n, "source": name})
        return {"mask_png": mask_png, "crack_pts": crack_pts, "hotspots": hots}

    with ThreadPoolExecutor(max_workers=2) as loader, ThreadPoolExecutor(max_workers=1) as poster:
        load_q = deque(); next_i = 0

        def _fill():                                   # 프리페치 깊이 3 — 원본 배열 메모리 상한
            nonlocal next_i
            while next_i < len(pairs) and len(load_q) < 3:
                i = next_i; next_i += 1
                load_q.append((i, pairs[i][0], loader.submit(_load_one, i, *pairs[i])))
        _fill()
        post_futs = []                                 # (n, name, cam, future) — 이미지 순서 유지
        while load_q:
            i, img_path, fut = load_q.popleft()
            name = os.path.basename(img_path)
            try:
                it = fut.result()
                all_cameras.append(it["cam"])          # 메인 스레드에서 순서대로 append → id 순 유지
                if detect:
                    disp = it.pop("disp")
                    try:
                        prob = seg.predict_prob_batched(disp, batch_size=CRACK_BATCH)
                    except RuntimeError as e:
                        import torch
                        if "out of memory" in str(e).lower():
                            torch.cuda.empty_cache()
                        log(f"    (배치 추론 폴백: {e})"); prob = seg.predict_prob(disp)
                    del disp
                    mask = (prob >= thr).astype(np.uint8)
                    ncr = int(mask.sum())
                    if ncr > 0:                        # 후처리는 워커에 넘기고 다음 이미지 추론으로
                        post_futs.append((i, name, it["cam"], poster.submit(_post_one, it, prob, mask)))
                    log(f"  [{i+1}/{len(pairs)}] {name}  crack_px={ncr}")
                else:
                    log(f"  [{i+1}/{len(pairs)}] {name} (카메라만)")
            except Exception as e:
                log(f"  [오류] {name}: {e}")
                skipped.append(name)                 # 실패도 집계에 포함
            progress(i + 1, len(pairs))
            _fill()
        # 후처리 결과 수확: 이미지 순서대로 통합 → 직렬 처리와 결과·순서 동일(결정적)
        for i, name, cam, fut in post_futs:
            try:
                r = fut.result()
                cam["_mask"] = r["mask_png"]; cam["has_mask"] = True
                all_crack_pts.extend(r["crack_pts"])
                all_hotspots.extend(r["hotspots"])
            except Exception as e:
                log(f"  [후처리 오류] {name}: {e}")
                skipped.append(name)

    n_ok = len(all_cameras)
    if skipped:
        log(f"⚠ 처리 완료: {n_ok}장 성공 · {len(skipped)}장 제외/실패 (전체 {len(all_found)}장)")
    else:
        log(f"처리 완료: {n_ok}장 전부 성공")

    log("크랙 병합 및 정량화 집계 (DBSCAN)...")
    merged = []; inst2hot = {}
    if all_hotspots:
        eps = DBSCAN_EPS_M
        # 1) 각 핫스팟의 중심선(poly, 없으면 대표점 xyz)을 조밀화하고 소속 핫스팟 idx 를 태깅
        step = max(eps * 0.5, 1e-4)
        parts, owners = [], []
        for hi, h in enumerate(all_hotspots):
            geom = h["poly"] if h.get("poly") else [h["xyz"]]
            dens = _densify(geom, step)
            parts.append(dens)
            owners.append(np.full(len(dens), hi, np.int64))
        P = np.vstack(parts)
        owner = np.concatenate(owners)
        if len(P) > DBSCAN_MAX_PTS:                              # 성능 보호(소속 유지)
            sel = np.random.default_rng(0).choice(len(P), DBSCAN_MAX_PTS, replace=False)
            P, owner = P[sel], owner[sel]
        # 2) DBSCAN 으로 3D 밀도 군집화 → 물리적으로 연속된 크랙이 한 군집
        labels = dbscan(P, eps, DBSCAN_MIN_SAMPLES)
        # 3) 핫스팟을 '자기 점들의 다수결 라벨' 군집에 배정(전부 노이즈면 단독 처리)
        clusters = {}
        solo = 0
        for hi in range(len(all_hotspots)):
            labs = labels[owner == hi]
            labs = labs[labs >= 0]
            if len(labs):
                vals, cnts = np.unique(labs, return_counts=True)
                key = int(vals[cnts.argmax()])
            else:
                key = ("solo", solo); solo += 1
            clusters.setdefault(key, []).append(hi)
        # 4) 군집별 산출: 폭=2D 최대(유지) · 길이=군집 전체 기하의 그래프 최장경로 · views 합침
        for key, member_idxs in clusters.items():
            members = [all_hotspots[i] for i in member_idxs]
            w_max = max(m["width_mm"] for m in members)
            best_view = max(members, key=lambda m: m.get("quality", 0.0))
            w_best = best_view["width_mm"]
            best = max(members, key=lambda m: m["conf"])
            w3_vals = [m["width_mm_3d"] for m in members if m.get("width_mm_3d") is not None]
            w_max_3d = max(w3_vals) if w3_vals else None       # 프로토타입 3D 폭(멤버 최대)
            # 대표 리본 = 최적(정면·근접) 뷰의 폭 프로파일. 폭은 프로파일 중앙값·p95(단일픽셀 max 노이즈 완화).
            # 길이는 '군집 전체(모든 멤버 인스턴스)'의 3D 점을 이은 그래프 끝-끝 거리(가중 지름)로 잰다.
            # 한 뷰의 조각(best_view)만 쓰면 여러 뷰·조각에 걸친 긴 크랙이 짧게 나온다(그래서 교체).
            # 지름은 겹친 뷰가 같은 자리를 지나 중복이 자동 정리되고, 조밀화 점이라 끊김이 없다.
            rep_poly = best_view.get("poly") or []
            rep_w = best_view.get("poly_w") or []
            rep_n = best_view.get("poly_n") or []
            rp = np.asarray(rep_poly, float)
            # 길이: 멤버 인스턴스들의 2D 골격 실척 길이(_scaled_length=length_mm) 중 최대. 골격 합산이라
            # 조각나지 않고(그래프 방식의 프래그먼트 문제 없음) 가지 왕복 중복도 없다. 여러 뷰가 같은
            # 크랙을 보면 각자 length_mm 를 내므로 그중 가장 온전히 본 뷰(=최대)를 대표로 쓴다.
            L = max((m.get("length_mm", 0.0) for m in members), default=0.0)
            wv = [w for w in rep_w if w is not None]
            w_med = float(np.median(wv)) if wv else w_best
            w_p95 = float(np.percentile(wv, 95)) if wv else w_best
            center = rp.mean(axis=0) if len(rp) else np.asarray(best_view["xyz"], float)
            views = {}
            for m in members:
                if m["source"] not in views or m["conf"] > views[m["source"]]["conf"]:
                    views[m["source"]] = {"source": m["source"], "conf": m["conf"]}
            w3_txt = f" · 3D폭 {w_max_3d:.1f}" if w_max_3d is not None else ""
            merged.append({"text": f"크랙 폭 중앙 {w_med:.1f}mm (p95 {w_p95:.1f} · 최대 {w_max:.1f}){w3_txt} · 길이 {L:.0f}mm",
                           "conf": best["conf"], "xyz": [round(float(v), 5) for v in center],
                           "width_mm": round(w_best, 3), "width_mm_max": round(w_max, 3),
                           "width_mm_median": round(w_med, 3), "width_mm_p95": round(w_p95, 3),
                           "width_mm_3d": round(w_max_3d, 3) if w_max_3d is not None else None,
                           "length_mm": round(float(L), 2),
                           "_members": member_idxs,   # 이 크랙(핫스팟)에 속한 뷰별 인스턴스 idx(리본 매핑용)
                           "views": sorted(views.values(), key=lambda v: -v["conf"])})
        merged.sort(key=lambda h: -h["width_mm_max"])
        # 리본 → 자기 핫스팟 매핑(정렬 후 최종 인덱스로). 그 뒤 _members 는 제거.
        inst2hot = {}
        for mi, mh in enumerate(merged):
            for hi in mh.pop("_members"):
                inst2hot[hi] = mi
        log(f"  병합 결과: 핫스팟 {len(all_hotspots)}개(뷰별 인스턴스) → {len(merged)}개 크랙")

    crack_points = []
    if all_crack_pts:
        tcenters, _ = cluster_voxel(np.array(all_crack_pts), txt_radius)
        crack_points = [[round(float(c[0]), 5), round(float(c[1]), 5), round(float(c[2]), 5)] for c in tcenters]

    import datetime
    widths = [h["width_mm_max"] for h in merged] if merged else []
    report_meta = {
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "source_folder": os.path.basename(os.path.normpath(folder)),
        "mesh_file": os.path.basename(ply_path),
        "mesh_diag_m": round(diag, 2),
        "mesh_faces": int(len(mesh.faces)),
        "images_found": len(all_found),
        "images_analyzed": len(all_cameras),
        "images_skipped": len(skipped),
        "skipped_list": skipped[:50],            # 너무 길면 자름(전체 수는 images_skipped)
        "detect": bool(detect),
        "width_mm_range": [round(min(widths), 2), round(max(widths), 2)] if widths else None,
        # 측정 신뢰도 경고: 실척 전제("PLY 1단위=1m")가 깨지면 폭이 비현실적으로 커진다.
        "scale_warning": (max(widths) > 50.0) if widths else False,
    }
    # 리본 = '크랙(핫스팟)당 1개'. 서로 다른 이미지에서 검출한 같은 크랙의 여러 뷰 중심선을
    # fuse_points 로 융합(복셀 병합 + MST 순서화)한다 → 겹침 없는 단일 3D 객체 → 클릭 명확.
    hot2insts = {}
    for hi, mi in inst2hot.items():
        hot2insts.setdefault(mi, []).append(hi)
    all_ribbons = []
    for mi in range(len(merged)):
        pts, ws, ns = [], [], []
        for hi in hot2insts.get(mi, []):
            h = all_hotspots[hi]
            if h.get("poly") and len(h["poly"]) >= 2:
                pts.extend(h["poly"]); ws.extend(h["poly_w"]); ns.extend(h["poly_n"])
        if len(pts) < 2:
            continue
        fp, fw, fn = fuse_points(pts, ws, ns, FUSE_VOXEL_M)
        if len(fp) >= 2:
            all_ribbons.append({"poly": fp, "poly_w": fw, "poly_n": fn, "hot": mi})
    log(f"  리본: 뷰별 인스턴스 {len(all_hotspots)}개 → 크랙당 융합 {len(all_ribbons)}개(핫스팟 {len(merged)})")
    return {"ply_path": ply_path, "text_points": crack_points, "ribbons": all_ribbons,
            "cameras": all_cameras, "hotspots": merged, "report_meta": report_meta}


def save_report(scene_data, out_path):
    """scene_data 를 .twin(v2: zip 컨테이너)로 저장하고 경로를 반환한다.

    파일은 여전히 하나지만 안이 나뉘어 있다:
        meta.json   기하만(수 MB) — 뷰어가 JSON.parse 로 읽는다
        mesh.ply    원본 바이너리 — 뷰어가 ArrayBuffer 로 받는다(문자열 한계와 무관)
        img/*.jpg   개별 스트리밍 — 보이는 것만 로드
    PLY 는 DEFLATE 해도 16%밖에 안 줄면서 30~60초를 먹으므로(실측) STORED 로 넣는다.

    원자적 쓰기: 임시(.tmp)에 다 쓴 뒤 성공하면 rename 한다. 예전엔 곧바로 out_path 에 써서,
    생성이 중간에 실패하면 손상된 .twin 이 남았다(예: 1234.twin = 1.6GB 빈 껍데기)."""
    if not out_path.lower().endswith(".twin"):
        out_path += ".twin"
    cams = scene_data["cameras"]
    tmp = out_path + ".tmp"
    try:
        with zipfile.ZipFile(tmp, "w", allowZip64=True) as z:
            z.write(scene_data["ply_path"], "mesh.ply", compress_type=zipfile.ZIP_STORED)
            for c in cams:
                cid = c["id"]
                z.writestr(f"img/{cid:04d}_thumb.jpg", c.pop("_thumb"), zipfile.ZIP_STORED)
                z.writestr(f"img/{cid:04d}_full.jpg", c.pop("_full"), zipfile.ZIP_STORED)
                mb = c.pop("_mask", None)                        # 검출 마스크(있는 이미지만)
                if mb is not None:
                    z.writestr(f"img/{cid:04d}_mask.png", mb, zipfile.ZIP_STORED)
            meta = {"version": 2,
                    "counts": {"cameras": len(cams), "hotspots": len(scene_data["hotspots"])},
                    "report_meta": scene_data.get("report_meta", {}),
                    "text_points": scene_data["text_points"],
                    "ribbons": scene_data.get("ribbons", []),   # 모든 2D 인스턴스의 3D 리본
                    "cameras": cams,
                    "hotspots": scene_data["hotspots"]}
            z.writestr("meta.json", json.dumps(meta, ensure_ascii=False), zipfile.ZIP_DEFLATED)
        os.replace(tmp, out_path)                # 원자적 교체(성공 시에만 최종 파일이 생김)
    except BaseException:
        if os.path.exists(tmp):
            try: os.remove(tmp)                  # 실패한 임시 파일은 남기지 않는다
            except OSError: pass
        raise
    return out_path


def build_report(folder, ply_path, out_path, log, progress, detect=True, thr=CRACK_THR, limit=None):
    """분석 → scene_data 생성 → .twin 저장까지 한 번에 수행한다."""
    scene = build_scene_data(folder, ply_path, log, progress, detect=detect, thr=thr, limit=limit)
    p = save_report(scene, out_path)
    log(f"보고서 저장: {p} ({os.path.getsize(p)/1e6:.1f} MB) · 카메라 {len(scene['cameras'])} · 크랙 {len(scene['hotspots'])}")
    return p
