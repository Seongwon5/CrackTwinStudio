# -*- coding: utf-8 -*-
"""크랙 유형 분류 (크랙 / 조인트·시공선 / 실금 / 얼룩).

특징 = 3D 기하(직선성·꺾임·축정렬·폭) + 2D 겉모습(단면 골 깊이·엣지 선명도·골폭·
어두움·대비·종횡비). 학습(offline)과 추론(pipeline)이 '같은' 이 함수를 써서 분포가
어긋나지 않게 한다. 분류기는 model/crack_type.json (순수 파이썬으로 평가하는 랜덤포레스트)
— 런타임에 scikit-learn 이 필요 없다. 라벨 107개로 학습한 예비 모델이다.
"""
import os, json
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "model", "crack_type.json")

# 특징 순서(학습·추론 공통) · 로그 변환 대상
# circ_resid/turn_consist/curv_cv: 직선뿐 아니라 '원호형' 조인트(매끄러운 규칙 곡선)를
# 불규칙 크랙과 구분하기 위한 곡률-규칙성 특징.
# par_neigh(L2, 장면 수준): 근방에서 방향이 평행한 이웃의 비율. 거푸집 판선 같은 시공선은
# 대규모 평행 가족을 이뤄 값이 높고(중앙 검증 조인트≈평행 다수), 크랙은 제멋대로라 낮다.
FEATS = ["w", "straight", "bend", "axl",
         "circ_resid", "turn_consist", "curv_cv", "par_neigh",
         "ap_depth", "ap_depth_p90", "ap_sharp", "ap_fwhm", "ap_val", "ap_contrast", "ap_consist", "ap_aspect"]
LOG_FEATS = {"w", "straight", "circ_resid"}
PAR_ANG_DEG = 12.0     # 평행 판정 각도
PAR_NEAR_FRAC = 0.15   # 근방 거리 = 메시 대각선의 15%
CLASSES = ["crack", "joint", "craze", "blob"]
_PROF_R = 11   # 단면 표본 반폭(px, full 해상도 기준)


# ---------------- 특징 추출 ----------------
def geom_features(poly):
    """3D 중심선(Nx3) → 직선잔차·평균꺾임각·축정렬각 + 곡률-규칙성(원적합잔차·회전일관성·곡률변동)."""
    P = np.asarray(poly, float)
    if len(P) < 5:
        return dict(straight=0.02, bend=30.0, axl=25.0, circ_resid=0.05, turn_consist=0.5, curv_cv=1.0)
    dv = np.diff(P, axis=0); seg = np.linalg.norm(dv, axis=1); path = seg.sum()
    u = dv / np.maximum(seg[:, None], 1e-9)
    cosb = np.clip((u[:-1] * u[1:]).sum(1), -1, 1)
    bend = float(np.degrees(np.arccos(cosb)).mean()) if len(cosb) else 0.0
    c = P.mean(0); _, _, Vt = np.linalg.svd(P - c, full_matrices=False); axis = Vt[0]
    resid = np.linalg.norm((P - c) - np.outer((P - c) @ axis, axis), axis=1)
    straight = float(resid.mean() / max(path, 1e-6))
    axl = float(np.degrees(np.arccos(min(1.0, max(abs(axis[0]), abs(axis[1]), abs(axis[2]))))))
    # 최적 평면(주성분 2축)에 투영 → 2D
    e1, e2 = Vt[0], Vt[1]
    xy = np.column_stack([(P - c) @ e1, (P - c) @ e2]); x = xy[:, 0]; y = xy[:, 1]
    # 원 적합(Kåsa): 직선·원호는 잔차↓, 불규칙 크랙은 잔차↑
    circ_resid = 0.05
    try:
        A = np.column_stack([2 * x, 2 * y, np.ones(len(x))]); bb = x * x + y * y
        sol, *_ = np.linalg.lstsq(A, bb, rcond=None)
        cx, cy, cc = sol; r = float(np.sqrt(max(cc + cx * cx + cy * cy, 0.0)))
        d = np.abs(np.hypot(x - cx, y - cy) - r)
        circ_resid = float(d.mean() / max(path, 1e-6))
    except Exception:
        pass
    # 회전 방향 일관성(원호=한 방향으로 일관 회전=1, 크랙=좌우 무작위=0) + 곡률 변동계수
    d2 = np.diff(xy, axis=0)
    cross = d2[:-1, 0] * d2[1:, 1] - d2[:-1, 1] * d2[1:, 0]
    dot = (d2[:-1] * d2[1:]).sum(1)
    turn = np.arctan2(cross, dot)                     # 정점별 부호 있는 회전각
    at = np.abs(turn)
    turn_consist = float(abs(turn.sum()) / (at.sum() + 1e-9))
    curv_cv = float(at.std() / (at.mean() + 1e-9))
    return dict(straight=straight, bend=bend, axl=axl,
                circ_resid=circ_resid, turn_consist=turn_consist, curv_cv=curv_cv)


def appearance_features(gray, uv_px):
    """원본 grayscale(H×W float) + 중심선 픽셀좌표(Nx2, [x,y]) → 단면 겉모습 특징."""
    from scipy.ndimage import map_coordinates
    H, W = gray.shape
    uv = np.asarray(uv_px, float)
    inb = (uv[:, 0] >= _PROF_R + 1) & (uv[:, 0] < W - _PROF_R - 1) & \
          (uv[:, 1] >= _PROF_R + 1) & (uv[:, 1] < H - _PROF_R - 1)
    uv = uv[inb]
    if len(uv) < 5:
        return None
    idx = np.linspace(1, len(uv) - 2, min(40, len(uv) - 2)).astype(int)
    ts = np.arange(-_PROF_R, _PROF_R + 1)
    depth = []; sharp = []; fwhm = []; valint = []; bgstd = []
    for i in idx:
        tan = uv[min(len(uv) - 1, i + 1)] - uv[max(0, i - 1)]; nrm = np.hypot(*tan)
        if nrm < 1e-6:
            continue
        tan = tan / nrm; perp = np.array([-tan[1], tan[0]])
        pts = uv[i][None, :] + ts[:, None] * perp[None, :]
        prof = map_coordinates(gray, [pts[:, 1], pts[:, 0]], order=1, mode="nearest")
        c = len(prof) // 2
        val = prof[c - 2:c + 3].min()
        shoulder = (prof[:3].mean() + prof[-3:].mean()) / 2
        d = shoulder - val
        depth.append(d); valint.append(val); sharp.append(np.abs(np.diff(prof)).max())
        fwhm.append(int((prof <= shoulder - d / 2).sum())); bgstd.append(prof.std())
    if not depth:
        return None
    depth = np.array(depth)
    return dict(ap_depth=float(np.median(depth)), ap_depth_p90=float(np.percentile(depth, 90)),
                ap_sharp=float(np.median(sharp)), ap_fwhm=float(np.median(fwhm)),
                ap_val=float(np.median(valint)),
                ap_contrast=float(np.median(depth) / (np.median(bgstd) + 1e-3)),
                ap_consist=float((depth > 6).mean()))


def feature_vector(poly, width_mm, length_mm, gray, uv_px, par_neigh=0.0):
    """전체 특징 dict → FEATS 순서 벡터. par_neigh(L2)는 장면 문맥에서 계산해 주입. 겉모습 실패 시 None."""
    f = dict(w=float(width_mm or 1.0), par_neigh=float(par_neigh))
    f.update(geom_features(poly))
    ap = appearance_features(gray, uv_px)
    if ap is None:
        return None
    f.update(ap)
    f["ap_aspect"] = float(np.log10(max(length_mm or 1.0, 1.0) / max(width_mm or 1.0, 1.0) + 1e-3))
    x = np.array([f[k] for k in FEATS], float)
    for j, k in enumerate(FEATS):
        if k in LOG_FEATS:
            x[j] = np.log10(max(x[j], 1e-6))
    return x


def principal_axis(poly):
    """중심선(Nx3) → (주축 단위벡터, 중심점)."""
    P = np.asarray(poly, float)
    c = P.mean(0)
    if len(P) < 2:
        return np.array([1.0, 0.0, 0.0]), c
    _, _, Vt = np.linalg.svd(P - c, full_matrices=False)
    return Vt[0], c


def parallel_fraction(centroids, axes, near, ang_deg=PAR_ANG_DEG):
    """각 후보에 대해 '근방 이웃 중 방향이 평행한 비율'(L2). 시공선 가족은 높고 크랙은 낮음.
    cKDTree 로 근방만 추려 O(N²) 회피."""
    from scipy.spatial import cKDTree
    C = np.asarray(centroids, float); A = np.asarray(axes, float); n = len(C)
    out = np.zeros(n)
    if n < 3 or near <= 0:
        return out
    tree = cKDTree(C); cm = float(np.cos(np.radians(ang_deg)))
    nb = tree.query_ball_point(C, near)
    for i in range(n):
        idx = [j for j in nb[i] if j != i]
        if not idx:
            continue
        ai = A[i]
        par = sum(1 for j in idx if abs(float(ai @ A[j])) >= cm)
        out[i] = par / len(idx)
    return out


# ---------------- 순수 파이썬 랜덤포레스트 평가 ----------------
_MODEL = None
def _load():
    global _MODEL
    if _MODEL is None:
        try:
            with open(MODEL_PATH, encoding="utf-8") as fp:
                _MODEL = json.load(fp)
        except Exception:
            _MODEL = False
    return _MODEL


def classify(x):
    """특징 벡터 → (label, confidence). 모델/입력 없으면 ('crack', 0.0)."""
    mdl = _load()
    if not mdl or x is None:
        return "crack", 0.0
    classes = mdl["classes"]
    prob = np.zeros(len(classes), float)
    for t in mdl["trees"]:
        cl, cr, fe, th, va = t["cl"], t["cr"], t["fe"], t["th"], t["va"]
        n = 0
        while cl[n] != -1:
            n = cl[n] if x[fe[n]] <= th[n] else cr[n]
        v = va[n]; prob += np.array(v, float) / max(sum(v), 1)
    prob /= len(mdl["trees"])
    k = int(prob.argmax())
    return classes[k], float(prob[k])


def project_to_cam(cam, P):
    """3D 점(Nx3) → 카메라 픽셀좌표(cam.w×cam.h 기준, 왜곡 포함) + 유효마스크.
    뷰어 projCamRaw / backproject 와 동일 규약(왜곡 재적용)."""
    pos = np.array(cam["pos"]); fwd = np.array(cam["fwd"]); up = np.array(cam["up"]); rt = np.array(cam["right"])
    rel = P - pos; Zc = rel @ fwd; ok = Zc > 1e-6
    x = (rel @ rt) / np.where(ok, Zc, 1); y = -(rel @ up) / np.where(ok, Zc, 1)
    d = (list(cam.get("dist") or []) + [0] * 5)[:5]; k1, k2, p1, p2, k3 = d
    r2 = x * x + y * y; rad = 1 + k1 * r2 + k2 * r2**2 + k3 * r2**3
    xd = x * rad + 2 * p1 * x * y + p2 * (r2 + 2 * x * x)
    yd = y * rad + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y
    return np.column_stack([cam["fx"] * xd + cam["cx"], cam["fy"] * yd + cam["cy"]]), ok


def classify_crack(poly, width_mm, length_mm, cam, gray, par_neigh=0.0):
    """크랙 중심선(3D) + 대표 카메라 + 그 카메라 grayscale (+ L2 par_neigh) → (type, conf).
    gray 는 full 이미지(≤1920) grayscale, cam.w 는 원본폭 → 스케일 보정."""
    P = np.asarray(poly, float)
    if len(P) < 4 or gray is None:
        return "crack", 0.0
    uv, ok = project_to_cam(cam, P)
    uv = uv[ok] * (gray.shape[1] / cam["w"])
    x = feature_vector(P, width_mm, length_mm, gray, uv, par_neigh)
    return classify(x)
