# -*- coding: utf-8 -*-
"""검토 라벨 재학습 루프 — 검사원 검토(사이드카)가 곧 학습 데이터가 된다.

    py tools/retrain_type.py                    # reports/ 의 모든 *.twin.review.json 수집 → 재학습
    py tools/retrain_type.py --dry-run          # 라벨 수집·집계만(모델 미변경)

라벨 규칙(뷰어 검토 워크플로와 1:1):
    · 유형 수정(type)  → 그 유형이 정답 라벨
    · 확인(confirmed)  → 화면의 유형(=AI 판정 or 이전 수정)이 정답 라벨
    · 오탐(rejected)   → 'blob'(비크랙) 라벨
    · 메모만 있는 항목  → 라벨 아님(건너뜀)

학습셋 스냅샷(model/train_set.npz): 추출한 특징(X,y,출처)을 누적 저장 —
원본 .twin 이 삭제돼도 과거 라벨이 다음 재학습에 계속 반영된다.

안전장치: 라벨 합계 < --min-labels(기본 40) 또는 클래스 < 2 면 모델을 바꾸지 않는다.
교체 시 기존 모델은 crack_type.bak.json 으로 백업하고, 교체 전 신·구 모델의 판정
일치율을 보고한다. (앱 재시작 시 새 모델 적용 — 파이프라인이 시작에 1회 로드)
"""
import os
import sys
import json
import argparse
import zipfile
from collections import Counter

import numpy as np
import cv2

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import cracktype as CT

REPORTS_DIR = os.path.join(HERE, "reports")
MODEL_JSON = os.path.join(HERE, "model", "crack_type.json")
TRAIN_NPZ = os.path.join(HERE, "model", "train_set.npz")


def extract_from_twin(twin, log=print):
    """한 .twin + 사이드카 → (X 리스트, y 리스트, 출처태그 리스트)."""
    revp = twin + ".review.json"
    if not os.path.exists(revp):
        return [], [], []
    with open(revp, encoding="utf-8") as f:
        revs = json.load(f).get("reviews", {})
    z = zipfile.ZipFile(twin)
    meta = json.loads(z.read("meta.json"))
    hots = meta.get("hotspots", [])
    lab = {}
    for k, v in revs.items():
        try:
            i = int(k)
        except ValueError:
            continue
        if i >= len(hots):
            continue
        if v.get("state") == "rejected":
            lab[i] = "blob"
        elif v.get("type"):
            lab[i] = v["type"]
        elif v.get("state") == "confirmed":
            lab[i] = hots[i].get("type") or "crack"
    if not lab:
        return [], [], []
    rb_by_hot = {}
    for r in meta.get("ribbons", []):
        rb_by_hot.setdefault(r["hot"], []).append(r)
    cams = {c["source"]: c for c in meta.get("cameras", [])}
    camid = {c["source"]: c["id"] for c in meta.get("cameras", [])}
    if not cams:
        log(f"  {os.path.basename(twin)}: 카메라 없음(합성 twin?) → 건너뜀")
        return [], [], []
    diag = meta.get("report_meta", {}).get("mesh_diag_m", 100.0)
    # L2 par_neigh — 장면 '전체' 균열 문맥에서 계산(학습·추론 분포 일치)
    polym = {hi: np.array([p for r in rb_by_hot[hi] for p in r["poly"]], float) for hi in rb_by_hot}
    cents = []; axes = []; hidx = []
    for hi in range(len(hots)):
        P = polym.get(hi)
        if P is None or len(P) < 2:
            continue
        ax, c = CT.principal_axis(P); axes.append(ax); cents.append(c); hidx.append(hi)
    par = CT.parallel_fraction(cents, axes, near=diag * CT.PAR_NEAR_FRAC) if cents else []
    par_of = {hidx[k]: float(par[k]) for k in range(len(hidx))}
    # 라벨 균열의 대표 뷰 선택(≤48점 서브샘플) → 뷰별 그룹으로 이미지 1회 디코드
    by_src = {}
    for i in sorted(lab):
        P = polym.get(i)
        if P is None or len(P) < 4:
            continue
        Ps = P[::max(1, len(P) // 48)]
        best = None; bestext = -1
        for vw in hots[i].get("views", []):
            c = cams.get(vw["source"])
            if not c:
                continue
            uv, ok = CT.project_to_cam(c, Ps); uv = uv[ok]
            if len(uv) < 4:
                continue
            ext = float(np.linalg.norm(uv.max(0) - uv.min(0)))
            if ext > bestext:
                bestext = ext; best = c
        if best is not None:
            by_src.setdefault(best["source"], []).append((i, P, best))
    X = []; y = []; src = []
    base = os.path.basename(twin)
    for s, items in by_src.items():
        try:
            gray = cv2.imdecode(np.frombuffer(z.read(f"img/{camid[s]:04d}_full.jpg"), np.uint8),
                                cv2.IMREAD_GRAYSCALE).astype(np.float32)
        except Exception:
            continue
        for i, P, cam in items:
            h = hots[i]
            w = h.get("width_mm_median") or h.get("width_mm_max") or h.get("width_mm")
            uv, ok = CT.project_to_cam(cam, P); uv = uv[ok] * (gray.shape[1] / cam["w"])
            x = CT.feature_vector(P, w, h.get("length_mm"), gray, uv, par_of.get(i, 0.0))
            if x is not None:
                X.append(x); y.append(lab[i]); src.append(f"{base}#{i}")
    log(f"  {base}: 검토 {len(revs)}건 → 라벨 {len(lab)}건 → 특징 추출 {len(X)}건")
    return X, y, src


MIN_PER_CLASS = 8          # 클래스당 최소 라벨 수 — 이보다 적으면 그 클래스는 학습이 무의미


def _current_classes(path):
    """지금 쓰고 있는 모델의 클래스 목록(없으면 빈 리스트)."""
    try:
        with open(path, encoding="utf-8") as f:
            return list(json.load(f).get("classes", []))
    except Exception:
        return []


def run(reports_dir=REPORTS_DIR, out=MODEL_JSON, trainset=TRAIN_NPZ,
        min_labels=40, dry_run=False, log=print):
    """재학습 본체(라이브러리 진입점) — 대시보드 '재학습' 버튼(백엔드 잡)과 CLI 가 공유.
    반환: {trained: bool, labels: int, dist: {...}, note: str}"""
    log("[1/3] 검토 라벨 수집 …")
    X = []; y = []; src = []
    for f in sorted(os.listdir(reports_dir)):
        if f.lower().endswith(".twin") and os.path.exists(os.path.join(reports_dir, f) + ".review.json"):
            xs, ys, ss = extract_from_twin(os.path.join(reports_dir, f), log=log)
            X += xs; y += ys; src += ss
    log(f"  신규 라벨 특징: {len(X)}건 · 분포 {dict(Counter(y))}")

    # 스냅샷 병합(같은 출처는 새 검토가 덮음 — 검토를 고치면 라벨도 갱신)
    if os.path.exists(trainset):
        old = np.load(trainset, allow_pickle=True)
        keep = [k for k in range(len(old["y"])) if str(old["src"][k]) not in set(src)]
        X = list(old["X"][keep]) + X
        y = list(old["y"][keep]) + y
        src = list(old["src"][keep]) + src
        log(f"  스냅샷 병합: 총 {len(X)}건 · 분포 {dict(Counter(y))}")
    if not X:
        log("라벨이 없습니다 — 뷰어에서 검토(확인/유형수정/오탐)를 먼저 남기세요.")
        return {"trained": False, "labels": 0, "dist": {}, "note": "no-labels"}
    Xa = np.array(X, float); ya = np.array(y)

    cnt = Counter(ya)
    if len(Xa) < min_labels or len(cnt) < 2:
        log(f"[중단] 라벨 {len(Xa)}건 · 클래스 {len(cnt)}종 — 최소 {min_labels}건·2클래스 필요."
            f" 모델은 변경하지 않습니다.")
        return {"trained": False, "labels": int(len(Xa)), "dist": dict(cnt), "note": "below-threshold"}
    # 가드 ①: 클래스당 최소 건수 — 몇 건짜리 클래스는 학습이 무의미하고 분포만 망가뜨린다.
    thin = {c: k for c, k in cnt.items() if k < MIN_PER_CLASS}
    if thin:
        log(f"[중단] 라벨이 너무 적은 유형: {thin} — 유형마다 최소 {MIN_PER_CLASS}건 필요."
            f" 해당 유형의 검토를 더 남긴 뒤 다시 실행하세요. 모델은 변경하지 않습니다.")
        return {"trained": False, "labels": int(len(Xa)), "dist": dict(cnt), "note": "thin-class"}
    # 가드 ②: 기존 모델이 알던 유형을 잃지 않는다 — 새 라벨에 없는 유형이 있으면 그 유형은
    # 앞으로 절대 분류될 수 없게 되므로(4→2 클래스 축소 사고) 중단한다.
    cur = _current_classes(out)
    lost = [c for c in cur if c not in cnt]
    if lost:
        log(f"[중단] 현재 모델의 유형 {lost} 에 대한 라벨이 없습니다 — 재학습하면 그 유형이 사라집니다."
            f" (현재 {cur} → 새 {sorted(cnt)}) 해당 유형의 검토를 남겨 라벨을 확보하세요."
            f" 모델은 변경하지 않습니다.")
        return {"trained": False, "labels": int(len(Xa)), "dist": dict(cnt), "note": "class-loss"}
    if dry_run:
        log("[dry-run] 여기서 종료 — 모델·스냅샷 미변경.")
        return {"trained": False, "labels": int(len(Xa)), "dist": dict(cnt), "note": "dry-run"}

    log("[2/3] 재학습(RandomForest) …")
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import cross_val_predict, StratifiedKFold
    from sklearn.metrics import precision_recall_fscore_support
    rf = RandomForestClassifier(n_estimators=100, max_depth=8, min_samples_leaf=3,
                                class_weight="balanced", random_state=0)
    folds = min(5, min(cnt.values()))
    if folds >= 2:
        yp = cross_val_predict(rf, Xa, ya, cv=StratifiedKFold(folds, shuffle=True, random_state=0))
        log(f"  CV({folds}) 정확도 {(yp == ya).mean()*100:.0f}%")
        p, r, _, s = precision_recall_fscore_support(ya, yp, labels=sorted(cnt), zero_division=0)
        for k, l in enumerate(sorted(cnt)):
            log(f"    {l:>6} 정밀도 {p[k]:.2f} 재현율 {r[k]:.2f} (n={s[k]})")
    rf.fit(Xa, ya)
    # 신·구 판정 일치율(교체 전 비교)
    try:
        old_pred = [CT.classify(x)[0] for x in Xa]
        agree = float(np.mean(np.array(old_pred) == rf.predict(Xa)))
        log(f"  기존 모델과 일치율: {agree*100:.0f}% (라벨 표본 기준)")
    except Exception:
        pass

    log("[3/3] 모델 교체 + 스냅샷 저장 …")
    if os.path.exists(out):
        import shutil
        shutil.copy(out, os.path.splitext(out)[0] + ".bak.json")
    classes = [str(c) for c in rf.classes_]; trees = []
    for est in rf.estimators_:
        t = est.tree_
        trees.append(dict(cl=t.children_left.tolist(), cr=t.children_right.tolist(), fe=t.feature.tolist(),
                          th=[round(float(v), 5) for v in t.threshold],
                          va=[[int(round(c)) for c in nd[0]] for nd in t.value]))
    with open(out, "w", encoding="utf-8") as f:
        json.dump(dict(classes=classes, feats=CT.FEATS, trees=trees,
                       note=f"retrain: {len(Xa)} labels ({dict(cnt)})"), f)
    np.savez_compressed(trainset, X=Xa, y=ya, src=np.array(src, dtype=object))
    log(f"  저장: {out} (백업 .bak.json) · 스냅샷 {trainset} ({len(Xa)}건)")
    return {"trained": True, "labels": int(len(Xa)), "dist": dict(cnt), "note": "ok"}


def main():
    """CLI 진입점. 인자를 파싱해 run() 을 부른다(--dry-run 이면 라벨 집계만).
    앱에서는 backend 가 run 을 직접 호출하므로 이 경로를 타지 않는다."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--reports-dir", default=REPORTS_DIR)
    ap.add_argument("--out", default=MODEL_JSON)
    ap.add_argument("--trainset", default=TRAIN_NPZ)
    ap.add_argument("--min-labels", type=int, default=40)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run(a.reports_dir, a.out, a.trainset, a.min_labels, a.dry_run)


if __name__ == "__main__":
    main()
