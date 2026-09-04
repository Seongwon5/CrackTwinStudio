# -*- coding: utf-8 -*-
"""두 검사(.twin)의 균열을 정합·매칭해 영속 균열 대장(asset.json)을 만든다.

    py tools/compare_twins.py <이전.twin> <이번.twin> -o asset.json [--ida 2025-06] [--idb 2026-07]

원리(설계 문서의 축): '위치는 신원, 측정은 상태' — 절대 섞지 않는다.
  ① 정합   : 메시-메시 7-DoF(스케일 포함) ICP. 잔차(reg_rms)가 매칭 반경의 근거.
              포토그래메트리는 검사마다 좌표계·스케일이 다르므로 필수.
  ② 매칭   : 두 검사의 중심선 점을 한 데 섞어 복셀 연결요소(파이프라인 병합과 동일 계열)로
              군집 → 한 성분에 양쪽이 함께 있으면 같은 균열. 조각남(1:N/N:1)이 자동 처리됨.
  ③ 판정   : 같은 균열의 측정치 비교. 노이즈 바닥(뷰 간 산포 IQR, 없으면 보수적 기본값)을
              넘는 변화만 '성장'. 3D 위치 변화는 성장 판정에 쓰지 않는다(정합 오차 cm ≫ 성장 mm).

한계(정직): 정합 실패 시(잔차 게이트) 결과를 신뢰할 수 없어 중단한다 · '미검출'은 보수/미관측을
구분하지 못한다 · 실측 2차 데이터 검증 전까지는 합성 검증만 이뤄진 상태다.
"""
import os
import sys
import json
import argparse
import zipfile
import io

import numpy as np

REG_SAMPLES = 20000        # 정합용 메시 정점 서브샘플
REG_GATE_FRAC = 0.005      # 정합 잔차 게이트: 메시 대각선의 0.5% 초과 시 불신
MATCH_VOXEL_MIN = 0.10     # 매칭 복셀 하한(m) — 정합 잔차의 3배와 큰 쪽
POLY_SUB = 200             # 균열당 중심선 서브샘플 상한
NOISE_W_FRAC = 0.20        # 뷰별 폭이 없을 때 기본 노이즈 바닥: 폭의 20%
NOISE_W_MIN = 0.15         # (mm)
NOISE_L_FRAC = 0.10        # 길이 기본 노이즈 바닥: 10%
GROW_W_MIN = 0.10          # 성장 최소 문턱(mm) — 노이즈 바닥과 큰 쪽 사용
GROW_L_MIN = 20.0          # (mm)


def load_twin(path):
    """meta(dict) + 메시 정점 서브샘플(Nx3, 절대좌표)을 반환.
    mesh.ply 원본 보고서와 계층 LOD 타일 보고서 둘 다 읽는다(twin_mesh.vertex_sample)."""
    import twin_mesh
    z = zipfile.ZipFile(path)
    meta = json.loads(z.read("meta.json"))
    V = twin_mesh.vertex_sample(z, REG_SAMPLES, seed=0, meta=meta)
    return meta, V


def register(Va, Vb, diag):
    """B→A 7-DoF 정합: PCA+중심 조대 정렬 → 스케일 추정 → ICP 정밀. (T4x4, rms) 반환.
    trimesh.registration.icp 는 점군-점군에 동작하므로 메시 정점 서브샘플로 충분."""
    import trimesh.registration as reg
    ca, cb = Va.mean(0), Vb.mean(0)
    # 조대: 주성분 축 정렬(부호 모호성 4가지 시도) + RMS 스케일비
    _, _, Wa = np.linalg.svd(Va - ca, full_matrices=False)
    _, _, Wb = np.linalg.svd(Vb - cb, full_matrices=False)
    sa = np.sqrt(((Va - ca) ** 2).sum(1).mean()); sb = np.sqrt(((Vb - cb) ** 2).sum(1).mean())
    s0 = sa / max(sb, 1e-12)
    best = (None, np.inf)
    from scipy.spatial import cKDTree
    ta = cKDTree(Va)
    for fx in (1, -1):
        for fy in (1, -1):
            F = np.diag([fx, fy, fx * fy])          # det=+1 유지(반사 배제)
            R0 = Wa.T @ F @ Wb
            Vb0 = (Vb - cb) @ (s0 * R0).T + ca
            d, _ = ta.query(Vb0[::10], k=1)
            m = float(np.sqrt((d ** 2).mean()))
            if m < best[1]:
                best = ((R0, s0), m)
    (R0, s0), _ = best
    T0 = np.eye(4); T0[:3, :3] = s0 * R0; T0[:3, 3] = ca - (s0 * R0) @ cb
    # 정밀: ICP (trimesh — scale 미지원이라 스케일은 조대 추정을 신뢰, 잔차로 검증)
    Vb1 = Vb @ T0[:3, :3].T + T0[:3, 3]
    M, _, cost = reg.icp(Vb1, Va, max_iterations=50, scale=False)
    T = M @ T0
    Vb2 = Vb @ T[:3, :3].T + T[:3, 3]
    d, _ = ta.query(Vb2[::5], k=1)
    rms = float(np.sqrt((d ** 2).mean()))
    return T, rms


def apply_T(T, P):
    """4×4 동차 변환 T 를 점군 P(N×3)에 적용한다 — 회전·스케일(3×3) 후 평행이동.
    정합으로 구한 T 로 이전 검사의 좌표를 이번 검사 좌표계에 얹을 때 쓴다."""
    P = np.asarray(P, float)
    return P @ T[:3, :3].T + T[:3, 3]


def crack_points(meta, T=None):
    """핫스팟별 중심선 점(서브샘플). 리본이 없으면 xyz 한 점이라도."""
    rb = {}
    for r in meta.get("ribbons", []):
        rb.setdefault(r["hot"], []).append(r)
    out = {}
    for hi, h in enumerate(meta.get("hotspots", [])):
        pts = [p for r in rb.get(hi, []) for p in r["poly"]]
        P = np.asarray(pts if pts else [h["xyz"]], float)
        if len(P) > POLY_SUB:
            P = P[np.linspace(0, len(P) - 1, POLY_SUB).astype(int)]
        out[hi] = apply_T(T, P) if T is not None else P
    return out


def voxel_cc(points_list, voxel):
    """[(라벨, Nx3), ...] → 성분 id 배열(라벨 순서). 파이프라인 병합과 동일한
    복셀 26-이웃 연결요소. 각 크랙은 자기 점들의 성분 다수결에 배정."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    allp = np.concatenate([p for _, p in points_list])
    owner = np.concatenate([np.full(len(p), k) for k, (_, p) in enumerate(points_list)])
    ijk = np.floor(allp / voxel).astype(np.int64)
    key = (ijk[:, 0] << 42) + (ijk[:, 1] << 21) + ijk[:, 2]
    uniq, vid = np.unique(key, return_inverse=True)
    rows = []; cols = []
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                if dx == dy == dz == 0:
                    continue
                nb = uniq + ((np.int64(dx) << 42) + (np.int64(dy) << 21) + np.int64(dz))
                pos = np.searchsorted(uniq, nb)
                ok = (pos < len(uniq)) & (uniq[np.minimum(pos, len(uniq) - 1)] == nb)
                rows.append(np.nonzero(ok)[0]); cols.append(pos[ok])
    g = coo_matrix((np.ones(sum(len(r) for r in rows), np.int8),
                    (np.concatenate(rows), np.concatenate(cols))), shape=(len(uniq), len(uniq)))
    _, vlab = connected_components(g, directed=False)
    plab = vlab[vid]
    comp = np.zeros(len(points_list), np.int64)
    for k in range(len(points_list)):
        vals, cnts = np.unique(plab[owner == k], return_counts=True)
        comp[k] = vals[cnts.argmax()]
    return comp


def hot_measure(h):
    """핫스팟 → 대표 측정치 + 노이즈 바닥. 뷰별 w/L 이 있으면(신형) IQR, 없으면 기본값."""
    w = h.get("width_mm_median") or h.get("width_mm_max") or h.get("width_mm") or 0.0
    L = h.get("length_mm") or 0.0
    vw = [v["w"] for v in h.get("views", []) if isinstance(v, dict) and v.get("w") is not None]
    vL = [v["L"] for v in h.get("views", []) if isinstance(v, dict) and v.get("L")]
    nw = float(np.subtract(*np.percentile(vw, [75, 25]))) if len(vw) >= 3 else max(NOISE_W_FRAC * w, NOISE_W_MIN)
    nL = float(np.subtract(*np.percentile(vL, [75, 25]))) if len(vL) >= 3 else NOISE_L_FRAC * L
    return dict(w=round(float(w), 3), p95=h.get("width_mm_p95"), L=round(float(L), 1),
                L_tot=h.get("length_total_mm"), noise_w=round(max(nw, 1e-3), 3),
                noise_L=round(max(nL, 1.0), 1), type=h.get("type"), views=len(h.get("views", [])))


def agg(ms):
    """같은 균열의 조각 여러 개 → 검사 대표값(파이프라인 병합 규칙과 동일 계열: 폭 중앙 최대·길이 최대)."""
    return dict(w=max(m["w"] for m in ms), L=max(m["L"] for m in ms),
                noise_w=max(m["noise_w"] for m in ms), noise_L=max(m["noise_L"] for m in ms),
                n_frag=len(ms))


class RegistrationError(RuntimeError):
    """정합 잔차가 게이트를 넘음 — 결과를 신뢰할 수 없다."""


def _match_pair(ma, mb, T, rms):
    """A(기준 좌표계) vs B(T 로 A 좌표계로 변환) 매칭 → (groups, voxel).
    복셀 연결요소 = 후보 블록. 이웃 균열이 신원 반경 안에서 뭉치는 오병합을 막기 위해,
    블록 안에서 B 각각을 '중심선 챔퍼 거리'가 가장 가까운 A 에 배정한다(조각 1:N 유지).
    가까운 A 가 없으면(> voxel×1.5) 신규로 분리. A 끼리는 절대 병합하지 않는다."""
    from scipy.spatial import cKDTree
    pa = crack_points(ma); pb = crack_points(mb, T)
    voxel = max(3.0 * rms, MATCH_VOXEL_MIN)
    items = [("A", hi, P) for hi, P in pa.items()] + [("B", hi, P) for hi, P in pb.items()]
    comp = voxel_cc([(s, P) for s, _, P in items], voxel)
    coarse = {}
    for k, (side, hi, _) in enumerate(items):
        coarse.setdefault(int(comp[k]), {"A": [], "B": []})[side].append(hi)
    groups = {}
    gid = 0
    for g in coarse.values():
        A_ids, B_ids = g["A"], g["B"]
        trees = {ai: cKDTree(pa[ai]) for ai in A_ids}
        assign = {ai: [] for ai in A_ids}; lone_b = []
        for bi in B_ids:
            best_ai, best_d = None, np.inf
            for ai in A_ids:
                d, _ = trees[ai].query(pb[bi], k=1)
                cd = float(np.mean(d))                       # B점 → A중심선 평균 최근접(챔퍼)
                if cd < best_d:
                    best_d, best_ai = cd, ai
            if best_ai is not None and best_d <= voxel * 1.5:
                assign[best_ai].append(bi)
            else:
                lone_b.append(bi)
        for ai in A_ids:
            gid += 1; groups[gid] = {"A": [ai], "B": sorted(assign[ai])}
        for bi in lone_b:
            gid += 1; groups[gid] = {"A": [], "B": [bi]}
    return groups, voxel


def _judge(prev, cur):
    """이전/현재 대표 측정(agg dict) → (status, delta). 노이즈 바닥 초과 변화만 성장."""
    dw = cur["w"] - prev["w"]; dL = cur["L"] - prev["L"]
    thw = max(2 * max(prev["noise_w"], cur["noise_w"]), GROW_W_MIN)
    thL = max(2 * max(prev["noise_L"], cur["noise_L"]), GROW_L_MIN)
    if dw > thw or dL > thL:
        st = "grown"
    elif dw < -thw:
        st = "shrunk"          # 대개 측정 변동 — 보수적 해석 필요
    else:
        st = "stable"
    return st, {"dw": round(dw, 3), "dL": round(dL, 1), "th_w": round(thw, 3), "th_L": round(thL, 1)}


def run(twin_a, twin_b, out, ida="T1", idb="T2", name=None, log=print):
    """비교 본체(라이브러리 진입점). asset dict 를 반환하고 out 에 저장한다.
    정합 실패 시 RegistrationError. log 콜백으로 진행 로그 전달(백엔드 잡 로그용)."""
    class _A:
        """argparse 네임스페이스 대역. CLI 와 라이브러리 호출이 같은 코드를 타게 한다."""
        pass
    a = _A(); a.twin_a = twin_a; a.twin_b = twin_b; a.out = out
    a.ida = ida; a.idb = idb; a.name = name

    log(f"[1/4] 로드: {os.path.basename(a.twin_a)} · {os.path.basename(a.twin_b)}")
    ma, Va = load_twin(a.twin_a)
    mb, Vb = load_twin(a.twin_b)
    diag = float(np.linalg.norm(Va.max(0) - Va.min(0)))

    log(f"[2/4] 정합(7-DoF ICP) …")
    T, rms = register(Va, Vb, diag)
    gate = diag * REG_GATE_FRAC
    log(f"  잔차 RMS {rms*100:.1f}cm (게이트 {gate*100:.1f}cm · 대각 {diag:.0f}m)")
    if rms > gate:
        raise RegistrationError(
            f"정합 실패: 잔차 {rms*100:.1f}cm > 게이트 {gate*100:.1f}cm — 두 메시가 같은 구조물이 아니거나 "
            "복원 품질 차이가 큽니다. (수동 시드 정합은 추후 지원)")

    log(f"[3/4] 매칭(복셀 연결요소 + 성분 내 정밀 배정) …")
    groups, voxel = _match_pair(ma, mb, T, rms)

    log(f"[4/4] 판정 …")
    cracks = []; pid = 0
    stats = {"matched": 0, "new": 0, "missing": 0, "grown": 0, "stable": 0, "shrunk": 0}
    for g in groups.values():
        pid += 1
        entry = {"pid": f"P-{pid:04d}", "matches": {}}
        if g["A"]:
            entry["matches"][a.ida] = sorted(g["A"])
        if g["B"]:
            entry["matches"][a.idb] = sorted(g["B"])
        hist = {}
        if g["A"]:
            hist[a.ida] = agg([hot_measure(ma["hotspots"][i]) for i in g["A"]])
        if g["B"]:
            hist[a.idb] = agg([hot_measure(mb["hotspots"][i]) for i in g["B"]])
        entry["history"] = hist
        entry["born"] = a.ida if g["A"] else a.idb
        if g["A"] and g["B"]:
            st, entry["delta"] = _judge(hist[a.ida], hist[a.idb])
            stats["matched"] += 1; stats[st] += 1
        elif g["B"]:
            st = "new"; stats["new"] += 1
        else:
            st = "missing"; stats["missing"] += 1
        entry["status"] = st
        cracks.append(entry)

    asset = {"name": a.name or os.path.splitext(os.path.basename(a.twin_a))[0],
             "inspections": [
                 {"id": a.ida, "twin": os.path.abspath(a.twin_a), "T_to_ref": np.eye(4).tolist(), "reg_rms_m": 0.0},
                 {"id": a.idb, "twin": os.path.abspath(a.twin_b), "T_to_ref": T.tolist(), "reg_rms_m": round(rms, 4)}],
             "match_voxel_m": round(voxel, 3),
             "cracks": cracks}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(asset, f, ensure_ascii=False, indent=1)
    na, nb = len(ma["hotspots"]), len(mb["hotspots"])
    log(f"결과: {a.ida} {na}개 · {a.idb} {nb}개 → 영속 균열 {len(cracks)}개")
    log(f"  매칭 {stats['matched']} (성장 {stats['grown']} · 불변 {stats['stable']} · 감소? {stats['shrunk']})"
        f" · 신규 {stats['new']} · 미검출 {stats['missing']}")
    log(f"저장: {a.out}")
    asset["_stats"] = stats
    return asset


def extend(asset_path, twin_new, idn="T?", log=print):
    """기존 자산에 새 검사(N번째)를 이어붙인다 — 영속 ID(pid) 승계.

    · 정합: '직전(최신) 검사 메시' 기준(시간상 가장 가까워 구조 변화 최소) →
      기준좌표계 변환은 체인: T_to_ref(new) = T_to_ref(prev) @ T(new→prev)
    · 매칭: 직전 검사에서 관측된 균열( matches[prev] 보유 pid )과만 짝짓는다.
      한계(정직): 직전에 미검출이던 균열이 다시 나타나면 '재등장'이 아니라 신규 pid 가
      된다(재등장 판정은 다중 시점 역탐색 필요 — 추후).
    · 판정: pid 별 직전 대표측정 vs 이번 대표측정( _judge 동일 규칙 ).
    자산 파일을 갱신 저장하고 asset dict(_stats 포함) 반환."""
    with open(asset_path, encoding="utf-8") as f:
        asset = json.load(f)
    prev = asset["inspections"][-1]
    prev_id = prev["id"]
    log(f"[1/4] 자산 '{asset.get('name')}' ({len(asset['inspections'])}회차) ← 새 검사 {os.path.basename(twin_new)}")
    ma, Va = load_twin(prev["twin"])
    mb, Vb = load_twin(twin_new)
    diag = float(np.linalg.norm(Va.max(0) - Va.min(0)))

    log(f"[2/4] 정합(직전 검사 기준 7-DoF ICP) …")
    T, rms = register(Va, Vb, diag)          # new → prev 프레임
    gate = diag * REG_GATE_FRAC
    log(f"  잔차 RMS {rms*100:.1f}cm (게이트 {gate*100:.1f}cm)")
    if rms > gate:
        raise RegistrationError(f"정합 실패: 잔차 {rms*100:.1f}cm > 게이트 {gate*100:.1f}cm")
    T_ref = np.asarray(prev["T_to_ref"], float) @ T      # 체인: new → ref

    log(f"[3/4] 매칭(직전 검사 균열과 pid 승계) …")
    groups, voxel = _match_pair(ma, mb, T, rms)          # prev 프레임에서 매칭
    # 직전 검사 핫 → pid 엔트리 매핑(1 pid 가 조각 여럿을 가질 수 있음)
    ent_of_prevhot = {}
    for ent in asset["cracks"]:
        for ai in ent["matches"].get(prev_id, []):
            ent_of_prevhot[ai] = ent
    b_of_ent = {}                                         # pid 엔트리 → 이번 검사 B 핫 집합
    lone_b = []
    for g in groups.values():
        if g["A"]:
            ent = ent_of_prevhot.get(g["A"][0])
            if ent is not None:
                b_of_ent.setdefault(id(ent), (ent, set()))[1].update(g["B"])
            elif g["B"]:
                lone_b.extend(g["B"])                     # 직전 검사에 없던(구 missing) 자리 → 신규 취급
        elif g["B"]:
            lone_b.extend(g["B"])

    log(f"[4/4] 판정 · 대장 갱신 …")
    stats = {"matched": 0, "new": 0, "missing": 0, "grown": 0, "stable": 0, "shrunk": 0}
    idn_ = idn
    for ent in asset["cracks"]:
        got = b_of_ent.get(id(ent))
        if got and got[1]:
            bl = sorted(got[1])
            ent["matches"][idn_] = bl
            cur = agg([hot_measure(mb["hotspots"][i]) for i in bl])
            ent["history"][idn_] = cur
            pv = ent["history"].get(prev_id)
            if pv:
                st, ent["delta"] = _judge(pv, cur)
            else:
                st = "new"                                # 직전 회차 측정이 없던 pid 의 재등장(드묾)
            stats["matched"] += 1; stats[st] = stats.get(st, 0) + 1
        else:
            st = "missing"
            if ent["matches"].get(prev_id):               # 직전엔 있었는데 이번에 사라짐
                stats["missing"] += 1
        ent["status"] = st
    npid = max((int(e["pid"][2:]) for e in asset["cracks"]), default=0)
    for bi in sorted(set(lone_b)):
        npid += 1
        asset["cracks"].append({"pid": f"P-{npid:04d}", "born": idn_,
                                "matches": {idn_: [bi]},
                                "history": {idn_: agg([hot_measure(mb["hotspots"][bi])])},
                                "status": "new"})
        stats["new"] += 1
    asset["inspections"].append({"id": idn_, "twin": os.path.abspath(twin_new),
                                 "T_to_ref": T_ref.tolist(), "reg_rms_m": round(rms, 4)})
    with open(asset_path, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in asset.items() if k != "_stats"}, f, ensure_ascii=False, indent=1)
    log(f"결과: {len(asset['inspections'])}회차 자산 · pid {len(asset['cracks'])}개")
    log(f"  매칭 {stats['matched']} (성장 {stats['grown']} · 불변 {stats['stable']} · 감소? {stats['shrunk']})"
        f" · 신규 {stats['new']} · 미검출 {stats['missing']}")
    log(f"저장: {asset_path}")
    asset["_stats"] = stats
    return asset


def main():
    """CLI 진입점. 인자를 파싱해 run()(신규 비교) 또는 extend()(기존 자산에 이어 붙이기)를 부른다.
    앱에서는 backend 가 run/extend 를 직접 호출하므로 이 경로를 타지 않는다."""
    ap = argparse.ArgumentParser()
    ap.add_argument("twin_a", help=".twin(이전) 또는 --extend 시 asset.json")
    ap.add_argument("twin_b", help=".twin(이번/새 검사)")
    ap.add_argument("-o", "--out", default="asset.json")
    ap.add_argument("--ida", default="T1"); ap.add_argument("--idb", default="T2")
    ap.add_argument("--name", default=None)
    ap.add_argument("--extend", action="store_true", help="twin_a=기존 asset.json 에 twin_b 를 이어붙임")
    a = ap.parse_args()
    try:
        if a.extend:
            extend(a.twin_a, a.twin_b, idn=a.idb)
        else:
            run(a.twin_a, a.twin_b, a.out, a.ida, a.idb, a.name)
    except RegistrationError as e:
        print("  ✗", e); sys.exit(2)


if __name__ == "__main__":
    main()
