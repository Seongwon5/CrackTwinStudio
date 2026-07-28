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
import time
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
DBSCAN_MIN_SAMPLES = 2          # (dbscan 유틸용) 코어 최소 이웃 — 병합은 복셀 성분화로 대체됨
PROB_CACHE_DIR = os.path.join(HERE, "cache", "prob")   # 추론(확률맵) 캐시 — 이미지·모델·타일 동일 시 GPU 스킵
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
WIDTH_CAP_PX = 60                 # 수직 폭 프로파일의 편측 최대 탐색 반경(px). 옆 크랙으로 새는 것 방지
WIDTH_STEP_PX = 0.5               # 수직 폭 샘플 간격(px). 서브픽셀 보간 → 실효 분해능
# ---- 판정 등급(절대 폭 기준, mm) — ACI 349.3R 계열 허용폭 관행을 참고한 기본 프레임 ----
# A(허용) < 0.3 ≤ B(관찰) < 0.5 ≤ C(보수 검토) < 1.0 ≤ D(보수 필요). p95 폭 기준(단일픽셀
# 최대의 노이즈와 중앙값의 국소확폭 둔감을 절충한 보수적 대표값). 임계값은 meta 에 기록해
# 추적 가능하게 하고, 뷰어는 캘리브레이션 보정 mm 로 실시간 재계산한다.
# ---- 심각도(절대 폭 기준, 단일 체계) ----
# 예전엔 '심각도(상대 순위)'와 '판정 등급 A~D'가 따로 있어 화면에 기준이 둘이었다 → 하나로 통일.
# 경계(mm): 0.5 / 1 / 3 / 5 / 10.  10mm 초과는 콘크리트 균열로는 비현실적이라 '특이값'으로 분리해
# 측정 오류(실척 미보정·경사 폭주)를 눈에 띄게 한다.
SEV_BOUNDS_MM = (0.5, 1.0, 3.0, 5.0, 10.0)
SEV_LABELS = ("S1", "S2", "S3", "S4", "S5", "S6")
SEV_NAMES = ("경미", "관찰", "보통", "심각", "매우 심각", "특이값")


def severity_of(w_mm):
    """대표 폭(mm) → 심각도 S1~S6."""
    if w_mm is None:
        return None
    return SEV_LABELS[sum(w_mm >= b for b in SEV_BOUNDS_MM)]
# ---- 균열 신뢰도(conf) 구간 ----
# conf = AI 가 그 지점을 균열이라고 본 확률(0~1). 검출 임계값 CRACK_THR 미만은 애초에 마스크가 아니다.
# 예전엔 '측정 신뢰 A/B/C'(실척 의심·뷰 불일치·단일뷰)를 따로 두었으나, 화면에 '신뢰'가 둘이라
# 혼동이 커서 균열 신뢰도 하나로 통일했다. 실척 의심은 report_meta.scale_warning 이 담당한다.
CONF_BOUNDS = (0.7, 0.9)          # 낮음 < 0.7 ≤ 보통 < 0.9 ≤ 높음


# ==================================================================
# 1. 이미지 / 카메라 기하
# ==================================================================
def find_pairs(folder):
    """폴더에서 (이미지, XMP) 짝을 찾아 파일명 순으로 돌려준다.

    명명 규칙 두 가지를 모두 받는다: `photo.jpg.xmp`(Zephyr 기본) 와 `photo.xmp`.
    짝이 없는 이미지도 (경로, None) 으로 포함한다 — 호출부가 '제외됨'으로 집계해
    사용자에게 알리기 위해서다(예전엔 말없이 버려 1,288장 중 594장만 분석된 걸 몰랐다)."""
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
    """[폴백 전용] 이미지를 XMP 보정 방향(가로/세로)에 맞도록 90° 단위로 회전해 반환.

    ⚠ 90° 시계/반시계는 회전 후 모양이 같아 이 방법으로는 구분할 수 없다(첫 매치 k=1 채택).
    따라서 방향이 모호한 상황에서는 180° 뒤집힐 수 있다 — 정상 경로는 load_oriented() 가
    치수 근거로 판정하며, 여기는 치수가 아예 안 맞을 때의 최후 수단이다."""
    base_k = 0
    if calib_wh:
        target_hw = (calib_wh[1], calib_wh[0])
        for k in (0, 1, 2, 3):
            if np.rot90(file_img, k).shape[:2] == target_hw:
                base_k = k
                break
    return (np.rot90(file_img, base_k).copy() if base_k else file_img.copy())


def load_oriented(img_path, calib_wh, log=None):
    """이미지를 'XMP 보정과 같은 방향'의 BGR 배열로 로드한다 — 기하 정합의 출발점.

    사진측량 SW 가 EXIF 회전을 적용했는지는 도구·버전마다 다르다. Zephyr 는 무시하고
    저장된 원시 픽셀로 보정한다(실측: interior 데이터셋 — EXIF ori=8 인 172장의 calib 이
    전부 원시 가로 치수). 그래서 추측 대신 치수로 판정한다:
      ① calib == 원시 치수      → 원시 그대로 (보정 SW 가 EXIF 미적용)
      ② calib == EXIF 적용 치수 → exif_transpose (태그가 회전 방향을 정확히 아므로
                                   90°CW/CCW/180°/플립 전부 모호성 없음)
      ③ 그 외                  → 종횡비 폴백(orient_to_xmp) + 경고 로그
    예전엔 무조건 exif_transpose 후 종횡비로 추가 회전을 '추측'했는데, 90° 시계/반시계가
    구분되지 않아 EXIF ori=8 이미지가 calib 기준 180° 뒤집힌 채 분석됐다(전 검출 오배치).

    반환: (disp BGR, 방향태그). 태그('raw'/'exifN'/'fb')는 추론 캐시 키에 들어간다 —
    같은 파일·같은 치수라도 방향 판정이 다르면 픽셀 배치가 달라 캐시를 공유하면 안 된다
    (실측: 방향 수정 직후 구 방향에서 만든 확률맵이 그대로 히트해 마스크가 180° 어긋났다)."""
    from PIL import Image, ImageOps
    pil = Image.open(img_path)
    raw_wh = pil.size
    ori = pil.getexif().get(274, 1)
    exif_wh = (raw_wh[1], raw_wh[0]) if ori in (5, 6, 7, 8) else raw_wh
    if calib_wh and tuple(calib_wh) == raw_wh:                       # ① 원시 방향 보정
        rgb = pil.convert("RGB"); tag = "raw"
    elif calib_wh and tuple(calib_wh) == exif_wh:                    # ② EXIF 방향 보정
        rgb = ImageOps.exif_transpose(pil).convert("RGB"); tag = f"exif{ori}"
    else:                                                            # ③ 최후 폴백(모호)
        if log:
            log(f"    ⚠ {os.path.basename(img_path)}: calib {calib_wh} 가 원시 {raw_wh}·"
                f"EXIF 적용 {exif_wh} 와 모두 불일치 — 종횡비로 추정(방향 오류 가능)")
        disp = cv2.cvtColor(np.array(ImageOps.exif_transpose(pil).convert("RGB")), cv2.COLOR_RGB2BGR)
        return orient_to_xmp(disp, calib_wh), "fb"
    return cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2BGR), tag


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
    # ① 복셀 병합 — 폭은 복셀 내 '중앙값'(과거 최대값은 겹친 뷰 중 최대 오검출 폭을 채택해 부풀렸다)
    vox = {}
    for i in range(len(P)):
        k = (int(np.floor(P[i, 0] / voxel)), int(np.floor(P[i, 1] / voxel)), int(np.floor(P[i, 2] / voxel)))
        v = vox.get(k)
        if v is None:
            vox[k] = [P[i].copy(), [float(W[i])], N[i].copy(), 1]
        else:
            v[0] += P[i]; v[1].append(float(W[i])); v[2] += N[i]; v[3] += 1
    vp = np.array([v[0] / v[3] for v in vox.values()])
    vw = np.array([float(np.median(v[1])) for v in vox.values()])
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
    # ②-b 라플라시안 스무딩: 각 정점을 MST 이웃 평균 쪽으로 당겨 정합 오차로 생긴 측면 지그재그를
    #     접는다(뷰 간 수십 cm 어긋남이 리본을 실폭보다 뚱뚱하게 만들던 원인). 그래프 기반이라
    #     순서·분기와 무관하고, 크랙의 완만한 곡률(저주파)은 보존한다.
    LAM, ITER = 0.5, 6
    for _ in range(ITER):
        acc = np.zeros_like(vp)
        for i in range(n):
            nb = adj[i]
            acc[i] = vp[nb].mean(0) if nb else vp[i]
        vp = vp * (1.0 - LAM) + acc * LAM
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
        """로드된 모델을 추론 전용으로 준비한다(eval + 지정 device 이동).

        model 은 이미 FP16/FP32 로 변환된 상태로 들어온다 — dtype 을 파라미터에서 읽어
        입력 텐서를 맞추므로 어느 쪽이든 동작한다. crack_index 는 출력 4채널 중 크랙 채널(=1)."""
        import torch  # noqa
        self.model = model.eval().to(device)
        self.device = device
        self.dtype = next(self.model.parameters()).dtype     # FP16/FP32 자동 대응
        self.thr, self.tile, self.overlap, self.crack_index = thr, tile, overlap, crack_index
        # channels_last(NHWC): FP16 텐서코어 커널 활성화 — 3060 Ti 실측 +8%, 마스크 IoU 0.9996.
        # (cudnn.benchmark 는 실측 1.00x 라 미적용)
        self._chlast = str(device).startswith("cuda")
        if self._chlast:
            self.model = self.model.to(memory_format=torch.channels_last)

    def _predict_tile(self, tile_bgr):
        """단일 타일(가변 크기) 추론. 16의 배수로 반사패딩 후 크랙 확률맵 반환."""
        import torch
        x = tile_bgr.astype(np.float32); h, w = x.shape[:2]
        ph, pw = (16 - h % 16) % 16, (16 - w % 16) % 16
        if ph or pw:
            x = np.pad(x, ((0, ph), (0, pw), (0, 0)), mode="reflect")
        t = torch.from_numpy(np.ascontiguousarray(x.transpose(2, 0, 1)[None])).to(self.device, self.dtype)
        if self._chlast:
            t = t.contiguous(memory_format=torch.channels_last)
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
            if self._chlast:
                t = t.contiguous(memory_format=torch.channels_last)
            with torch.no_grad():
                out = self.model(t); out = out["out"] if isinstance(out, dict) else out
                prob = torch.softmax(out.float(), dim=1)[:, self.crack_index].cpu().numpy()
            for j, (y, x) in enumerate(ch):
                acc[y:y + self.tile, x:x + self.tile] += prob[j] * win; wsum[y:y + self.tile, x:x + self.tile] += win
        wsum[wsum == 0] = 1e-6
        return acc / wsum


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

    # 점 p 의 8이웃 중 아직 안 밟은 골격 픽셀 하나(없으면 None) — DFS 로 중심선을 잇는 데 쓴다.
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
    """정량화 결과 컨테이너(속성 접근용).

    quantify_crack() 이 만든 dict 를 그대로 속성으로 노출한다.
      unit          "mm"(스케일맵 있음) 또는 "px"
      n_instances   균열 인스턴스 수
      total_length  전체 총연장 · max/mean/p95_width  집계 폭 · max_width_xy 최대폭 지점(px)
      instances[]   인스턴스별 dict — id · centerline · width_profile · edge_l/edge_r ·
                    max_width · max_width_xy · length(총연장) · length_path(최장경로).
                    폭 내림차순으로 정렬돼 있고 id 는 그 순위(1부터)다."""
    def __init__(self, d):
        """dict 의 키를 그대로 속성으로 펼친다(m.instances 처럼 쓰기 위함)."""
        self.__dict__.update(d)


ROI_PAD = 2      # 컴포넌트 bbox 여유. ≥1 이면 EDT·컨볼루션 경계가 전체 프레임과 동일해진다.


def _skeleton_lengths(skel, scale_map=None, scalar=1.0):
    """스켈레톤에서 (총연장, 최장경로) 를 실척으로 반환한다.
      · 총연장  = 모든 간선(픽셀 인접) 길이의 합 — 가지까지 포함한 '총 크랙 연장'.
      · 최장경로 = 그래프 지름(끝-끝 최장 측지) — 분기를 왕복하지 않는 '대표 길이'.
    scale_map(ROI 크롭)가 있으면 픽셀별 mm/px, 아니면 scalar 배율. 대각 간선은 √2 로 가중."""
    ys, xs = np.nonzero(skel)
    n = len(ys)
    if n < 2:
        return 0.0, 0.0
    H, W = skel.shape
    pos = -np.ones((H, W), np.int64)
    pos[ys, xs] = np.arange(n)
    ra, rb, rw = [], [], []
    for (dy, dx), d in (((0, 1), 1.0), ((1, 0), 1.0), ((1, 1), SQRT2), ((1, -1), SQRT2)):
        y2, x2 = ys + dy, xs + dx
        ok = (y2 >= 0) & (y2 < H) & (x2 >= 0) & (x2 < W)
        ok[ok] = skel[y2[ok], x2[ok]]                 # 양끝 모두 스켈레톤인 간선만
        if not ok.any():
            continue
        ay, ax = ys[ok], xs[ok]; by, bx = y2[ok], x2[ok]
        if scale_map is not None:
            w = d * 0.5 * (scale_map[ay, ax] + scale_map[by, bx])
        else:
            w = np.full(ok.sum(), d * scalar, float)
        ra.append(pos[ay, ax]); rb.append(pos[by, bx]); rw.append(w)
    if not ra:
        return 0.0, 0.0
    a = np.concatenate(ra); b = np.concatenate(rb); w = np.concatenate(rw)
    total = float(w.sum())
    # 최장경로: 이중 다익스트라(트리에서 정확, 소수 루프가 있어도 좋은 근사)
    g = csr_matrix((np.r_[w, w], (np.r_[a, b], np.r_[b, a])), shape=(n, n))
    d0 = dijkstra(g, indices=0); d0[~np.isfinite(d0)] = -1.0
    u = int(np.argmax(d0))
    du, pred = dijkstra(g, indices=u, return_predecessors=True)
    du[~np.isfinite(du)] = -1.0
    far = int(np.argmax(du)); L_naive = float(max(du[far], 0.0))
    # 계단(디지털화) 편향 보정: 8-이웃 간선 합은 30/60° 직선에서 최대 +10% 과대(실측).
    # 지름 경로의 픽셀 좌표를 이동평균(창5)으로 스무딩해 유클리드 합산 → 계단이 접혀
    # 각도 무관 ~±1%. 총연장은 같은 스켈레톤의 계단 통계를 공유하므로 동일 비율로 보정.
    path = []
    v = far
    while v >= 0 and len(path) <= n:
        path.append(v)
        v = int(pred[v])
    if len(path) >= 3 and L_naive > 0:
        pc = np.column_stack([xs[path], ys[path]]).astype(float)
        k = 5 if len(pc) >= 5 else 3            # 홀수 커널만(짝수면 출력 길이가 어긋남)
        ker = np.ones(k) / k
        sm = np.column_stack([np.convolve(np.pad(pc[:, j], (k // 2, k // 2), mode="edge"),
                                          ker, mode="valid") for j in range(2)])
        seg = np.linalg.norm(np.diff(sm, axis=0), axis=1)
        if scale_map is not None:
            sc = scale_map[ys[path], xs[path]]
            L_path = float((seg * 0.5 * (sc[:-1] + sc[1:])).sum())
        else:
            L_path = float(seg.sum()) * scalar
        ratio = min(1.0, max(0.85, L_path / L_naive))     # 안전 클램프(스무딩 과축소 방지)
        return total * ratio, L_path
    return total, L_naive


def _perp_width_profile(ordered, field, thr, edt, cap_px, ds=WIDTH_STEP_PX):
    """중심선 각 점에서 '접선의 수직'으로 양쪽 경계를 찾아 전체폭(px)을 잰다.

    한쪽 EDT×2(내접원 지름) 방식은 중심선이 실제 중심에 있다는 보장이 없어 과소 편향이 있다.
    여기서는 접선을 구해 그 수직선을 따라 field(확률맵)가 thr 를 지나는 지점을 양쪽에서
    서브픽셀 보간으로 찾는다 → 중심 가정 불필요·진짜 수직·양자화 완화.
    경계를 못 찾은 점(프레임 밖 등)은 EDT×2 로 폴백한다.

    ordered: [(x,y),...] ROI 좌표 · field: ROI 크롭(확률 또는 0/1) · thr: 경계 레벨
    edt:     각 점의 EDT 반경(px, 폴백용) · 반환: w_px(P,), bl(P,2), br(P,2) 경계점 ROI좌표"""
    xy = np.asarray(ordered, float); P = len(xy)
    if P == 0:
        return np.zeros(0), np.zeros((0, 2)), np.zeros((0, 2))
    if P < 2:
        r = float(edt[0]); nx, ny = 0.0, 1.0
        return (np.array([2.0 * r]),
                np.array([[xy[0, 0] - nx * r, xy[0, 1] - ny * r]]),
                np.array([[xy[0, 0] + nx * r, xy[0, 1] + ny * r]]))
    tx = np.gradient(xy[:, 0]); ty = np.gradient(xy[:, 1])
    tn = np.hypot(tx, ty); tn[tn < 1e-9] = 1.0
    nx = -ty / tn; ny = tx / tn                        # 법선(접선의 수직)
    offs = np.arange(-cap_px, cap_px + ds, ds)         # (S,)
    sx = xy[:, 0][:, None] + nx[:, None] * offs[None, :]
    sy = xy[:, 1][:, None] + ny[:, None] * offs[None, :]
    vals = ndi.map_coordinates(field, [sy.ravel(), sx.ravel()], order=1,
                               mode="constant", cval=0.0).reshape(P, len(offs))
    c = int(np.argmin(np.abs(offs)))                   # 중심(offset≈0) 인덱스
    w_px = np.empty(P); bl = np.empty((P, 2)); br = np.empty((P, 2))

    def _cross(v, direction):                          # 중심에서 direction 방향 첫 thr 하향교차 offset
        prev = c; i = c + direction
        while 0 <= i < len(offs):
            if v[i] < thr:
                v0, v1 = v[prev], v[i]; den = v0 - v1
                f = (v0 - thr) / den if den > 1e-9 else 0.0
                return offs[prev] + f * (offs[i] - offs[prev])
            prev = i; i += direction
        return None

    for p in range(P):
        v = vals[p]; pc = _cross(v, +1); nc = _cross(v, -1)
        # 편측 한도: 접선이 분기·꼬임에서 잘못 잡히면 '수직'이 크랙을 따라가 폭이 폭주한다.
        # 각 반쪽은 그 점의 내접원 반경(EDT)의 3배+4px 를 넘을 수 없다(넘으면 폴백).
        lim = float(edt[p]) * 3.0 + 4.0
        if v[c] < thr or pc is None or nc is None or pc > lim or -nc > lim:   # 폴백: 내접원 지름
            r = float(edt[p]); w_px[p] = 2.0 * r
            bl[p] = [xy[p, 0] - nx[p] * r, xy[p, 1] - ny[p] * r]
            br[p] = [xy[p, 0] + nx[p] * r, xy[p, 1] + ny[p] * r]
        else:
            w_px[p] = pc - nc
            br[p] = [xy[p, 0] + nx[p] * pc, xy[p, 1] + ny[p] * pc]
            bl[p] = [xy[p, 0] + nx[p] * nc, xy[p, 1] + ny[p] * nc]
    return w_px, bl, br


def quantify_crack(mask, mm_per_px=None, min_branch_px=10, min_object_px=30, bridge_px=0,
                   prob=None, prob_thr=CRACK_THR):
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
        dist = ndi.distance_transform_edt(sub)                # EDT 반경(폴백·탐색반경용)
        skel = _prune_spurs(skeletonize(sub), min_branch_px)  # 중심선
        if not skel.any():
            continue
        smap = scale_map[y0:y1, x0:x1] if scale_map is not None else None
        # 길이: 총연장 + 최장경로(그래프 지름) 둘 다
        if scale_map is not None:
            L_tot, L_path = _skeleton_lengths(skel, scale_map=smap)
        elif is_mm:
            L_tot, L_path = _skeleton_lengths(skel, scalar=scale_scalar)
        else:
            L_tot, L_path = _skeleton_lengths(skel)
        _ordered = _order_skeleton(skel)                       # [(x,y),...] (크롭 좌표)
        if not _ordered:
            continue
        oa = np.array(_ordered, float); oxi = oa[:, 0].astype(int); oyi = oa[:, 1].astype(int)
        edt_ord = dist[oyi, oxi]
        # 폭: 중심선 접선의 '수직'으로 양쪽 경계를 확률맵 thr 레벨셋에서 서브픽셀로 찾아 전체폭.
        # (한쪽 EDT×2 의 중심-편향 과소측정을 제거) 폴백은 EDT×2.
        field = prob[y0:y1, x0:x1].astype(np.float32) if prob is not None else sub.astype(np.float32)
        thr_f = float(prob_thr) if prob is not None else 0.5
        cap = float(min(WIDTH_CAP_PX, max(6.0, float(edt_ord.max()) * 3.0 + 4.0)))
        wpx, bl, br = _perp_width_profile(_ordered, field, thr_f, edt_ord, cap)
        # 실척(mm) 변환 — 중심선 점의 스케일
        if scale_map is not None:
            sc_ord = smap[oyi, oxi]
        elif is_mm:
            sc_ord = np.full(len(wpx), scale_scalar)
        else:
            sc_ord = np.ones(len(wpx))
        wmm = wpx * sc_ord
        _cl = [[x + x0, y + y0] for (x, y) in _ordered]        # 중심선(원본 프레임 좌표)
        _edge_l = [[float(bl[k, 0] + x0), float(bl[k, 1] + y0)] for k in range(len(bl))]
        _edge_r = [[float(br[k, 0] + x0), float(br[k, 1] + y0)] for k in range(len(br))]
        _wp = [float(v) for v in wmm]                          # 폭 프로파일(mm, 중심선 순서)
        total_len_u += L_tot; widths_all.append(wmm); j = int(np.argmax(wmm))
        instances.append(dict(id=i, length=float(L_tot), length_path=float(L_path),
                              max_width=float(wmm.max()), mean_width=float(wmm.mean()),
                              max_width_xy=(int(oxi[j] + x0), int(oyi[j] + y0)),
                              centerline=_cl, edge_l=_edge_l, edge_r=_edge_r, width_profile=_wp))
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


# ==================================================================
# 추론(확률맵) 캐시 — 같은 사진은 두 번 추론하지 않는다
# ==================================================================
# 병합·정량화·융합 파라미터만 바꾼 재실행에서 GPU 추론(전체 시간의 75~92%)을 통째로
# 건너뛴다. 키는 이미지 내용(부분 해시)+방향+모델(best.pt)+타일 설정 — 어느 하나라도
# 바뀌면 자동 미스. 값은 uint8(1/255) 양자화 PNG(장당 1~4MB). thr 는 확률맵과 무관하므로
# 키에 넣지 않는다(마스크가 아니라 확률맵을 캐시). 캐시 실패는 전부 무해(추론으로 폴백).

def _prob_cache_key(img_path, dw, dh, tile, overlap, otag=""):
    """확률맵 캐시의 키(sha1). 이미지 내용·표시해상도·방향태그·모델 파일·타일 설정을 모두 섞는다.
    어느 하나라도 바뀌면 키가 달라져 자동으로 미스가 되므로, 낡은 결과를 재사용할 위험이 없다.
    otag(방향 판정 결과)가 필요한 이유: 같은 파일이라도 방향 로직이 바뀌면 픽셀 배치가
    180° 다를 수 있는데 치수는 동일하다 — 태그 없이는 구 방향 캐시가 그대로 히트한다(실측 사고).
    전체 해시는 비싸서 앞 256KB + 끝 64KB 만 읽는다(크기·이름과 함께라면 충돌은 사실상 없다)."""
    import hashlib
    st = os.stat(img_path)
    ms = os.stat(CRACK_WEIGHTS)
    h = hashlib.sha1()
    h.update(f"{os.path.basename(img_path)}|{st.st_size}|{dw}x{dh}|o{otag}|"
             f"{ms.st_size}|{int(ms.st_mtime)}|t{tile}o{overlap}".encode())
    with open(img_path, "rb") as f:                    # 내용 부분 해시(앞 256KB + 끝 64KB)
        h.update(f.read(262144))
        if st.st_size > 262144 + 65536:
            f.seek(-65536, 2); h.update(f.read())
    return h.hexdigest()


def _prob_cache_load(key, shape):
    """캐시된 확률맵을 float32(0~1)로 되살린다. 없거나 형태가 다르면 None(→ 호출부가 추론).
    미스·손상 모두 조용히 None 을 돌려준다 — 캐시는 어디까지나 가속 수단이라 실패가 무해해야 한다."""
    try:
        p = os.path.join(PROB_CACHE_DIR, key + ".png")
        if not os.path.isfile(p):                      # 미스는 조용히(경고 없이)
            return None
        u8 = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
        if u8 is not None and u8.shape == tuple(shape[:2]):
            return u8.astype(np.float32) / 255.0
    except Exception:
        pass
    return None


def _prob_cache_save(key, prob):
    """확률맵을 uint8(1/255) 양자화 PNG 로 저장한다(장당 1~4MB).
    tmp 에 쓴 뒤 os.replace 로 교체 — 저장 중 중단돼도 반쪽짜리 캐시가 남지 않는다.
    로더 스레드에서 비동기로 불리며, 실패해도 조용히 넘어간다."""
    try:
        os.makedirs(PROB_CACHE_DIR, exist_ok=True)
        u8 = np.clip(np.rint(prob * 255.0), 0, 255).astype(np.uint8)
        ok, buf = cv2.imencode(".png", u8, [cv2.IMWRITE_PNG_COMPRESSION, 3])
        if ok:
            tmp = os.path.join(PROB_CACHE_DIR, key + ".tmp")
            with open(tmp, "wb") as f:
                f.write(buf.tobytes())
            os.replace(tmp, os.path.join(PROB_CACHE_DIR, key + ".png"))   # 원자적 교체
    except Exception:
        pass


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
        disp, otag = load_oriented(img_path, calib_wh, log=log)   # XMP 보정과 같은 방향으로(치수 근거 판정)
        dh, dw = disp.shape[:2]
        C = pick_center(K, R, T)
        cam = camera_pose(K, R, T, dw, dh)
        # 왜곡계수(k1,k2,p1,p2,k3)도 저장 — 뷰어가 3D→이미지 되투영 시 왜곡을 적용해야
        # 원본(왜곡된) 사진 위에 정확히 얹힌다(핀홀만 쓰면 가장자리에서 수십 px 어긋남).
        cam["dist"] = [float(x) for x in dist]
        cam.update({"id": n, "source": name,
                    # 원본 JPEG 바이트. data-URI/base64 로 부풀리지 않는다(저장 시 zip 에 그대로).
                    "_thumb": encode_jpeg_bytes(disp, THUMB_MAX_PX, 75),
                    "_full": encode_jpeg_bytes(disp, FULL_MAX_PX, FULL_JPEG_Q)})
        it = {"n": n, "name": name, "K": K, "dist": dist, "R": R, "C": C,
              "cam": cam, "disp": disp, "dh": dh, "dw": dw}
        if seg is not None:                            # 추론 캐시 조회(로드 스레드에서 — 메인 루프 비용 0)
            it["ckey"] = _prob_cache_key(img_path, dw, dh, seg.tile, seg.overlap, otag)
            it["cprob"] = _prob_cache_load(it["ckey"], disp.shape)
        return it

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
                             bridge_px=CRACK_BRIDGE_PX,               # 5px 이내 끊긴 크랙 잇기
                             prob=prob, prob_thr=CRACK_THR)           # 수직 폭은 확률맵 thr 레벨셋에서
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
            # 3D 폭: 좌·우 경계점을 각각 역투영해 |L3d-R3d| (한쪽×2 대칭가정 없이 양쪽 실측).
            # 경사·곡면의 이방성 foreshortening 을 실측으로 반영(2D 방식의 등방 cosθ 근사 대체).
            width_mm_3d = None
            el = inst.get("edge_l") or []; er = inst.get("edge_r") or []
            if cl and el and er:
                ll, lray, _lt, _ld = backproject(np.array(el, float), K, dist, R, C, mesh)
                rl, rray, _rt, _rd = backproject(np.array(er, float), K, dist, R, C, mesh)
                lhit = {int(lray[k]): ll[k] for k in range(len(lray))}
                rhit = {int(rray[k]): rl[k] for k in range(len(rray))}
                w3 = [1000.0 * float(np.linalg.norm(lhit[i] - rhit[i]))   # 좌↔우 전폭 ×1000(m→mm)
                      for i in range(min(len(el), len(er))) if i in lhit and i in rhit]
                if w3:
                    width_mm_3d = round(max(w3), 3)
            # 이 뷰에서의 GSD(픽셀당 mm, 중심선 중앙값) — '측정 바닥' 판정 근거.
            # 폭이 2×GSD 미만이면 그 폭은 분해능 안에서 잰 값이 아니라 상한값이다(뷰어·PDF 가 "≤" 표기).
            gsd = None
            if cl:
                cyx = np.array(cl, float).astype(int)
                cyx[:, 0] = np.clip(cyx[:, 0], 0, dw - 1); cyx[:, 1] = np.clip(cyx[:, 1], 0, dh - 1)
                gv = smap[cyx[:, 1], cyx[:, 0]]
                gv = gv[np.isfinite(gv) & (gv > 0)]
                if len(gv):
                    gsd = round(float(np.median(gv)), 4)
            hots.append({"conf": round(conf, 3),
                         "xyz": [float(p3[0]), float(p3[1]), float(p3[2])],
                         "width_mm": round(float(inst["max_width"]), 3),
                         "width_mm_3d": width_mm_3d,
                         "length_mm": round(float(inst.get("length_path", inst["length"])), 2),  # 최장경로(대표)
                         "length_total_mm": round(float(inst["length"]), 2),                     # 총연장(가지 포함)
                         "quality": round(cosq / max(z, 1e-6), 6),
                         "gsd_mm": gsd,
                         "poly": poly, "poly_w": poly_w, "poly_n": poly_n, "source": name})
        return {"mask_png": mask_png, "crack_pts": crack_pts, "hotspots": hots}

    with ThreadPoolExecutor(max_workers=2) as loader, ThreadPoolExecutor(max_workers=1) as poster:
        load_q = deque(); next_i = 0

        # 로드 대기열을 3개까지 채운다(프리페치). 깊이를 늘리면 디코드된 원본 배열이
        # 그만큼 메모리에 쌓이므로, GPU 를 놀리지 않을 최소값으로 잡았다.
        def _fill():
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
                    prob = it.pop("cprob", None)       # 추론 캐시 히트 시 GPU 스킵
                    hitc = prob is not None
                    if not hitc:
                        try:
                            prob = seg.predict_prob_batched(disp, batch_size=CRACK_BATCH)
                        except RuntimeError as e:
                            import torch
                            if "out of memory" in str(e).lower():
                                torch.cuda.empty_cache()
                            log(f"    (배치 추론 폴백: {e})"); prob = seg.predict_prob(disp)
                        loader.submit(_prob_cache_save, it["ckey"], prob)   # 비동기 저장(실패 무해)
                    del disp
                    mask = (prob >= thr).astype(np.uint8)
                    ncr = int(mask.sum())
                    if ncr > 0:                        # 후처리는 워커에 넘기고 다음 이미지 추론으로
                        post_futs.append((i, name, it["cam"], poster.submit(_post_one, it, prob, mask)))
                        # 배압: 캐시 히트면 추론이 0초라 메인 루프가 후처리보다 빨라져
                        # 대기 작업마다 prob(~80MB/장)를 붙든 채 무한히 쌓인다(→ OOM).
                        # 미완료 후처리가 2개를 넘으면 가장 오래된 것을 기다린다.
                        pend = [f for *_pf, f in post_futs if not f.done()]
                        if len(pend) > 2:
                            try:
                                pend[0].result()
                            except Exception:
                                pass                   # 실제 오류는 수확 단계에서 이미지명과 함께 보고
                    log(f"  [{i+1}/{len(pairs)}] {name}  crack_px={ncr}" + (" (캐시)" if hitc else ""))
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
    reg_med = reg_p90 = None; _reg_view_errs = []      # 뷰 간 정합 오차(핫스팟 없으면 표본 없음)
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
        # 2) eps 복셀 연결 성분으로 군집화 — 물리적으로 연속된 크랙이 한 군집.
        #    (기존: DBSCAN_MAX_PTS=8만 전역 랜덤 서브샘플 후 DBSCAN. 전체 데이터(594장)에선
        #     수백만 점이 3%로 깎여 점 간격이 eps 를 훨씬 넘고 연쇄가 전부 끊겨
        #     뷰 간 병합이 전멸했다(핫스팟 5.3만 · avg views 1.1). 복셀 성분화는 상한이
        #     필요 없어(전 구간 벡터화 O(N logN)) 규모와 무관하게 밀도·소속을 보존한다.)
        B = np.int64(1) << 20                          # 좌표 오프셋(음수 방지) — 복셀 인덱스 ±2^20 여유
        ix = np.floor(P / eps).astype(np.int64) + B
        key = (ix[:, 0] << 42) | (ix[:, 1] << 21) | ix[:, 2]
        uniq, vid = np.unique(key, return_inverse=True)          # 점 → 복셀 id
        import scipy.sparse as _sp
        rows = [np.arange(len(uniq))]; cols = [np.arange(len(uniq))]
        offs = [(dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)][14:]
        for dx, dy, dz in offs:                        # 26-이웃 중 +방향 13개만(대칭이라 충분)
            nb = uniq + ((np.int64(dx) << 42) + (np.int64(dy) << 21) + np.int64(dz))
            pos = np.searchsorted(uniq, nb)
            ok = (pos < len(uniq)) & (uniq[np.minimum(pos, len(uniq) - 1)] == nb)
            rows.append(np.nonzero(ok)[0]); cols.append(pos[ok])
        g = _sp.coo_matrix((np.ones(sum(len(r) for r in rows), np.int8),
                            (np.concatenate(rows), np.concatenate(cols))), shape=(len(uniq), len(uniq)))
        _, vlab = connected_components(g, directed=False)
        labels = vlab[vid]                             # 점 → 군집 라벨(서브샘플 없음 → 노이즈 없음)
        # 3) 핫스팟을 '자기 점들의 다수결 라벨' 군집에 배정
        clusters = {}
        order = np.argsort(owner, kind="stable")
        so, sl = owner[order], labels[order]
        bounds = np.flatnonzero(np.r_[True, so[1:] != so[:-1], True])
        for bi in range(len(bounds) - 1):
            a, b = bounds[bi], bounds[bi + 1]
            vals, cnts = np.unique(sl[a:b], return_counts=True)
            clusters.setdefault(int(vals[cnts.argmax()]), []).append(int(so[a]))
        # 3.5) 뷰 간 정합 오차 실측 — "이 포토그래메트리를 믿어도 되는가"를 숫자로.
        # 같은 군집(=같은 균열)에 들어온 서로 다른 뷰의 중심선이 3D 에서 얼마나 어긋나는지 잰다:
        # 각 뷰의 중심선(서브샘플 ≤50점)에서 '다른 뷰들의 점'까지 최근접 거리의 중앙값 = 그 뷰의 어긋남.
        # 주의(생존 편향): eps 를 넘게 어긋난 뷰는 애초에 다른 군집으로 갈라져 여기 안 잡힌다 —
        # 그래서 이 값은 하한 추정이며, 아래 '조각남 의심 쌍'이 그 사각지대를 보완한다.
        _reg_view_errs = []
        for _mis in clusters.values():
            by_src = {}
            for _i in _mis:
                _h = all_hotspots[_i]
                if _h.get("poly"):
                    by_src.setdefault(_h["source"], []).extend(_h["poly"])
            _srcs = list(by_src)
            if len(_srcs) < 2:
                continue
            for _s in _srcs:
                P_ = np.asarray(by_src[_s], float)
                O_ = np.asarray([p for t in _srcs if t != _s for p in by_src[t]], float)
                if len(P_) < 2 or len(O_) < 2:
                    continue
                d_, _ = cKDTree(O_).query(P_[::max(1, len(P_) // 50)])
                _reg_view_errs.append(float(np.median(d_)) * 1000.0)   # m → mm
        reg_med = round(float(np.median(_reg_view_errs)), 1) if _reg_view_errs else None
        reg_p90 = round(float(np.percentile(_reg_view_errs, 90)), 1) if _reg_view_errs else None
        if reg_med is not None:
            log(f"  뷰 간 정합 오차: 중앙 {reg_med}mm · p90 {reg_p90}mm (뷰 표본 {len(_reg_view_errs)}개"
                f" · 병합 eps {DBSCAN_EPS_M*1000:.0f}mm)")
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
            # 길이: 멤버 인스턴스들의 2D 골격 실척 길이 중 최대(뷰별로 재고 최대 채택). 두 지표를 함께 산출 —
            # length_mm=최장경로(그래프 지름, 대표 길이) · length_total_mm=총연장(가지 포함). 둘 다 뷰별
            # 골격에서 재므로 융합 형상과 무관하고, 여러 뷰 중 가장 온전히 본 뷰(=최대)를 대표로 쓴다.
            L = max((m.get("length_mm", 0.0) for m in members), default=0.0)          # 최장경로(대표)
            L_tot = max((m.get("length_total_mm", 0.0) for m in members), default=0.0)  # 총연장(가지 포함)
            wv = [w for w in rep_w if w is not None]
            w_med = float(np.median(wv)) if wv else w_best
            w_p95 = float(np.percentile(wv, 95)) if wv else w_best
            center = rp.mean(axis=0) if len(rp) else np.asarray(best_view["xyz"], float)
            views = {}
            for m in members:
                if m["source"] not in views or m["conf"] > views[m["source"]]["conf"]:
                    # 뷰별 측정치(w/L)도 보관 — 시계열 비교의 '측정 노이즈 바닥'(뷰 간 산포) 추정용
                    views[m["source"]] = {"source": m["source"], "conf": m["conf"],
                                          "w": round(float(m["width_mm"]), 3),
                                          "L": round(float(m.get("length_mm", 0.0)), 1)}
            # 심각도(절대 폭 p95 기준, 단일 체계)
            sev = severity_of(w_p95)
            # 측정 바닥: 대표 폭(w_med·w_p95)은 best_view 프로파일에서 나오므로 그 뷰의 GSD 를 싣는다.
            gsd = best_view.get("gsd_mm")
            # 뷰 간 산포 → 불확실도(±): 뷰별 최대폭의 IQR/2. 뷰 3개 이상일 때만 의미가 있다.
            wlist = [v["w"] for v in views.values() if v.get("w") is not None]
            w_unc = None
            if len(wlist) >= 3:
                q1, q3 = np.percentile(wlist, [25, 75])
                w_unc = round(float(q3 - q1) / 2.0, 3)
            w3_txt = f" · 3D폭 {w_max_3d:.1f}" if w_max_3d is not None else ""
            tot_txt = f" (총연장 {L_tot:.0f})" if L_tot > L + 0.5 else ""
            merged.append({"text": f"크랙 폭 중앙 {w_med:.1f}mm (p95 {w_p95:.1f} · 최대 {w_max:.1f}){w3_txt} · 길이 {L:.0f}mm{tot_txt}",
                           "conf": best["conf"], "xyz": [round(float(v), 5) for v in center],
                           "width_mm": round(w_best, 3), "width_mm_max": round(w_max, 3),
                           "width_mm_median": round(w_med, 3), "width_mm_p95": round(w_p95, 3),
                           "width_mm_3d": round(w_max_3d, 3) if w_max_3d is not None else None,
                           "length_mm": round(float(L), 2),               # 최장경로(대표)
                           "length_total_mm": round(float(L_tot), 2),      # 총연장(가지 포함)
                           "sev": sev,                                     # 심각도 S1~S6(무보정 mm 기준 — 뷰어는 보정 재계산)
                           "gsd_mm": gsd,                                  # 대표 뷰 GSD — 폭<2×GSD 면 "≤" 검열 표기
                           "w_unc_mm": w_unc,                              # 뷰 간 산포(IQR/2, 뷰≥3)
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
        log(f"  텍스트 포인트 군집화: 점 {len(all_crack_pts)}개 …")
        tcenters, _ = cluster_voxel(np.array(all_crack_pts), txt_radius)
        crack_points = [[round(float(c[0]), 5), round(float(c[1]), 5), round(float(c[2]), 5)] for c in tcenters]
        log(f"  텍스트 포인트: {len(crack_points)}개 라벨 위치")

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
        # 심각도 기준값 기록(추적 가능성) — 뷰어·보고서가 보정 mm 로 재계산할 때 같은 기준을 쓴다
        "severity_bounds_mm": list(SEV_BOUNDS_MM),
        "severity_names": list(SEV_NAMES),
        "conf_bounds": list(CONF_BOUNDS),          # 균열 신뢰도 구간(낮음/보통/높음)
        # 뷰 간 정합 오차(실측) — 병합·위치 신뢰의 근거. eps 대비로 읽는다(주의: 생존 편향 하한).
        "merge_eps_mm": round(DBSCAN_EPS_M * 1000, 1),
        "view_reg_mm_median": reg_med,
        "view_reg_mm_p90": reg_p90,
        "view_reg_n": len(_reg_view_errs),
    }
    # 리본 = '크랙(핫스팟)당 1개'. 서로 다른 이미지에서 검출한 같은 크랙의 여러 뷰 중심선을
    # fuse_points 로 융합(복셀 병합 + MST 순서화)한다 → 겹침 없는 단일 3D 객체 → 클릭 명확.
    hot2insts = {}
    for hi, mi in inst2hot.items():
        hot2insts.setdefault(mi, []).append(hi)
    log(f"  리본 융합 시작: 크랙 {len(merged)}개(뷰별 중심선 복셀 병합·스무딩) …")
    all_ribbons = []
    _t_fuse = time.time(); _nfuse = len(merged)
    for mi in range(_nfuse):
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
        if _nfuse >= 2000 and (mi + 1) % 2000 == 0:
            log(f"    융합 {mi + 1}/{_nfuse} ({(mi + 1) / _nfuse * 100:.0f}%) · {time.time() - _t_fuse:.0f}s")
    log(f"  리본: 뷰별 인스턴스 {len(all_hotspots)}개 → 크랙당 융합 {len(all_ribbons)}개"
        f"(핫스팟 {len(merged)}) · {time.time() - _t_fuse:.0f}s")

    # 조각남 의심 쌍: 서로 다른 '병합된 크랙' 둘이 3×eps 안까지 접근해 있으면, 정합 오차가
    # eps 급일 때 같은 균열이 갈라졌을 가능성이 있다 — 위 정합 오차 실측의 생존 편향(eps 를
    # 넘게 어긋난 뷰는 아예 다른 군집이 됨) 사각지대를 보완하는 신호다.
    split_suspects = 0
    if len(all_ribbons) >= 2:
        _pts = []; _ids = []
        for _r in all_ribbons:
            _P = np.asarray(_r["poly"], float)
            _P = _P[::max(1, len(_P) // 80)]           # 리본당 ≤80점이면 근접 판정에 충분
            _pts.append(_P); _ids.append(np.full(len(_P), _r["hot"]))
        _pts = np.vstack(_pts); _ids = np.concatenate(_ids)
        _pairs = cKDTree(_pts).query_pairs(r=DBSCAN_EPS_M * 3, output_type="ndarray")
        if len(_pairs):
            _a, _b = _ids[_pairs[:, 0]], _ids[_pairs[:, 1]]
            _diff = _a != _b
            if _diff.any():
                _pr = np.sort(np.stack([_a[_diff], _b[_diff]], axis=1), axis=1)
                split_suspects = int(len(np.unique(_pr, axis=0)))
        if split_suspects:
            log(f"  조각남 의심: 3×eps({DBSCAN_EPS_M*3000:.0f}mm) 안까지 접근한 크랙 쌍 {split_suspects}개")
    report_meta["split_suspect_pairs"] = split_suspects

    # ---- 크랙 유형 분류 (크랙 / 조인트·시공선 / 실금 / 얼룩) ----
    # 융합 중심선(기하) + 대표 뷰 이미지(겉모습)로 각 크랙에 type/type_conf 부여.
    # 뷰어의 유형 필터가 사용. 모델 없거나 실패해도 무해(전부 'crack' 로 폴백).
    try:
        import cracktype as _ct
        cam_by_src = {c["source"]: c for c in all_cameras}
        rbh = {}
        for r in all_ribbons: rbh.setdefault(r["hot"], []).append(r)
        # 크랙별 융합 중심선 + L2 par_neigh(장면 전체의 평행 이웃 비율 — 시공선 가족 신호)
        polym = {mi: np.array([p for r in rbh[mi] for p in r["poly"]], float) for mi in rbh}
        cents = []; axes = []; midx = []
        for mi in range(len(merged)):
            P = polym.get(mi)
            if P is None or len(P) < 2: continue
            ax, c = _ct.principal_axis(P); axes.append(ax); cents.append(c); midx.append(mi)
        log(f"  유형 분류 시작: 크랙 {len(merged)}개 (L2 평행이웃 계산 완료) …")
        par = _ct.parallel_fraction(cents, axes, near=diag * _ct.PAR_NEAR_FRAC) if cents else []
        par_of = {midx[k]: float(par[k]) for k in range(len(midx))}
        _t_cls = time.time(); _ncls = len(merged)
        # ① 대표 뷰 선택(이미지 불필요) — '화면에서 가장 크게 보이는 뷰' 비교라 중심선을
        #    ≤48점으로 서브샘플해도 순위가 같다. 크랙을 대표 뷰(source)별로 그룹핑.
        by_src = {}                                    # source → [(mi, P, cam)]
        for mi, h in enumerate(merged):
            h["type"] = "crack"; h["type_conf"] = 0.0  # 기본값(분류 불가 시 그대로)
            P = polym.get(mi)
            if P is None or len(P) < 4:
                continue
            Ps = P[::max(1, len(P) // 48)]
            best = None; bestext = -1
            for vw in h.get("views", []):
                c = cam_by_src.get(vw["source"])
                if not c: continue
                uv, ok = _ct.project_to_cam(c, Ps); uv = uv[ok]
                if len(uv) < 4: continue
                ext = float(np.linalg.norm(uv.max(0) - uv.min(0)))
                if ext > bestext: bestext = ext; best = c
            if best is not None:
                by_src.setdefault(best["source"], []).append((mi, P, best))
        # ② 뷰별 그룹 처리 — 이미지당 디코드 딱 1회, 메모리엔 그 한 장(float32 ~11MB)만.
        #    (예전: 크랙 순서대로 접근 + 전 이미지 float32 캐시 → 594장 ≈ 6.5GB 로 페이징 유발)
        feats = []; fmi = []; done = 0
        for src, items in by_src.items():
            c0 = items[0][2]; buf = c0.get("_full")
            arr = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_GRAYSCALE) if buf else None
            gray = arr.astype(np.float32) if arr is not None else None
            for mi, P, cam in items:
                h = merged[mi]
                w = h.get("width_mm_median") or h.get("width_mm_max") or h.get("width_mm")
                x = _ct.crack_features(P, w, h.get("length_mm"), cam, gray, par_of.get(mi, 0.0))
                if x is not None:
                    feats.append(x); fmi.append(mi)
                done += 1
                if _ncls >= 2000 and done % 2000 == 0:
                    log(f"    분류 {done}/{_ncls} ({done / _ncls * 100:.0f}%) · {time.time() - _t_cls:.0f}s")
        # ③ 일괄 분류 — numpy 벡터화 랜덤포레스트(샘플당 파이썬 트리순회 제거)
        for k, (lab, conf) in enumerate(_ct.classify_batch(feats)):
            h = merged[fmi[k]]; h["type"] = lab; h["type_conf"] = round(float(conf), 3)
        tcnt = {}
        for h in merged:
            tcnt[h["type"]] = tcnt.get(h["type"], 0) + 1
        log(f"  크랙 유형 분류: {tcnt} · {time.time() - _t_cls:.0f}s")
    except Exception as e:
        log(f"  (유형 분류 건너뜀: {e})")

    log(f"후처리 완료: 크랙 {len(merged)} · 리본 {len(all_ribbons)} · 텍스트 {len(crack_points)}")
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
    log(f"보고서 저장 중: {os.path.basename(out_path)} (메시·이미지 압축) …")
    _t_save = time.time()
    p = save_report(scene, out_path)
    log(f"보고서 저장: {p} ({os.path.getsize(p)/1e6:.1f} MB) · 카메라 {len(scene['cameras'])} · "
        f"크랙 {len(scene['hotspots'])} · {time.time() - _t_save:.0f}s")
    return p
