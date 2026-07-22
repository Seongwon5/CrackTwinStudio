# -*- coding: utf-8 -*-
"""기존 .twin 에 크랙 유형(type/type_conf)을 재분석 없이 주입한다.

각 크랙(hotspot)에 대해 융합 중심선(ribbons)과 대표 카메라 이미지로 유형을 분류
(크랙 / 조인트·시공선 / 실금 / 얼룩). 뷰어의 유형 필터가 이 값을 사용한다.

사용법:  py tools/patch_twin_type.py <report.twin>
"""
import sys, os, json, zipfile, tempfile
import numpy as np, cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cracktype as CT


def main(twin):
    zin = zipfile.ZipFile(twin)
    meta = json.loads(zin.read("meta.json"))
    cams = {c["source"]: c for c in meta.get("cameras", [])}
    camid = {c["source"]: c["id"] for c in meta.get("cameras", [])}
    rb_by_hot = {}
    for r in meta.get("ribbons", []):
        rb_by_hot.setdefault(r["hot"], []).append(r)
    _gray = {}

    def gray(cid):
        if cid not in _gray:
            try:
                _gray[cid] = cv2.imdecode(np.frombuffer(zin.read(f"img/{cid:04d}_full.jpg"), np.uint8),
                                          cv2.IMREAD_GRAYSCALE).astype(np.float32)
            except Exception:
                _gray[cid] = None
        return _gray[cid]

    hs = meta.get("hotspots", [])
    diag = meta.get("report_meta", {}).get("mesh_diag_m", 100.0)
    # L2 par_neigh: 크랙별 융합 중심선의 (중심·주축)으로 장면 전체 평행 이웃 비율 계산
    polym = {hi: np.array([p for r in rb_by_hot[hi] for p in r["poly"]], float) for hi in rb_by_hot}
    cents = []; axes = []; hidx = []
    for hi in range(len(hs)):
        P = polym.get(hi)
        if P is None or len(P) < 2: continue
        ax, c = CT.principal_axis(P); axes.append(ax); cents.append(c); hidx.append(hi)
    par = CT.parallel_fraction(cents, axes, near=diag * CT.PAR_NEAR_FRAC) if cents else []
    par_of = {hidx[k]: float(par[k]) for k in range(len(hidx))}
    counts = {}
    done = 0
    for hi, h in enumerate(hs):
        polys = rb_by_hot.get(hi)
        if not polys:
            h["type"] = "crack"; h["type_conf"] = 0.0
            counts["crack"] = counts.get("crack", 0) + 1
            continue
        P = np.array([p for r in polys for p in r["poly"]], float)
        # 대표 카메라: 멤버 뷰 중 가장 크게(정면·근접) 보이는 것
        best = None; bestext = -1
        for vw in (h.get("views") or []):
            cam = cams.get(vw["source"])
            if not cam:
                continue
            uv, ok = CT.project_to_cam(cam, P); uv = uv[ok]
            if len(uv) < 4:
                continue
            ext = float(np.linalg.norm(uv.max(0) - uv.min(0)))
            if ext > bestext:
                bestext = ext; best = cam
        if best is None:
            h["type"] = "crack"; h["type_conf"] = 0.0
        else:
            w = h.get("width_mm_median") or h.get("width_mm_max") or h.get("width_mm")
            lab, conf = CT.classify_crack(P, w, h.get("length_mm"), best, gray(camid[best["source"]]), par_of.get(hi, 0.0))
            h["type"] = lab; h["type_conf"] = round(conf, 3)
        counts[h["type"]] = counts.get(h["type"], 0) + 1
        done += 1
        if done % 2000 == 0:
            print(f"  {done}/{len(hs)}...", flush=True)

    # 원자적 재작성
    fd, tmp = tempfile.mkstemp(suffix=".twin", dir=os.path.dirname(os.path.abspath(twin))); os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w") as zout:
            for it in zin.infolist():
                if it.filename == "meta.json":
                    zout.writestr("meta.json", json.dumps(meta, ensure_ascii=False))
                else:
                    zout.writestr(it, zin.read(it.filename))
        zin.close()
        os.replace(tmp, twin)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    print(f"완료: 유형 부여 {done}/{len(hs)} → {twin}")
    print("  분포:", counts)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__); sys.exit(1)
    main(sys.argv[1])
