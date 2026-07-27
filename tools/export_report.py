# -*- coding: utf-8 -*-
"""검사 보고서 PDF export — 표지·요약 / 위치도 / 균열 대장 / 사진 대지.

    py tools/export_report.py <report.twin> [-o out.pdf] [--rows 300] [--photos 24]

· 심각도(경미~특이값)·균열 신뢰도는 .twin 저장값과 동일 규칙(meta 경계값)으로 재계산.
· 검토 사이드카(<twin>.review.json)가 있으면 검토 상태·수정 유형·메모를 대장에 반영.
· 주의: 수치는 파이프라인 무보정 mm 기준(뷰어의 2점 캘리브레이션은 로컬 설정이라 미반영)
  — 표지에 명시한다.
"""
import os
import sys
import json
import argparse
import zipfile
import io
import datetime

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.collections import LineCollection

plt.rcParams["font.family"] = "Malgun Gothic"
plt.rcParams["axes.unicode_minus"] = False

A4L = (11.69, 8.27)                    # A4 가로(inch)

CONF_NAME = ["낮음", "보통", "높음"]
CONF_HEX_T = {"낮음": "#cc4038", "보통": "#e08a2e", "높음": "#2e9e63"}
TYPE_LABEL = {"crack": "크랙", "joint": "조인트·시공선", "blob": "얼룩"}
LEGACY_TYPE = {"craze": "crack"}       # 폐지된 '실금' → 크랙(구 .twin 호환)
REV_LABEL = {"confirmed": "확인", "rejected": "오탐", "edited": "수정"}


def _type_of(h):
    """핫스팟의 유형(폐지된 유형은 대체 유형으로 폴백)."""
    t = h.get("type")
    return LEGACY_TYPE.get(t, t)


# 심각도(절대 폭 기준, 단일 체계) — 판정 등급 A~D 는 폐지하고 이것으로 통일했다.
SEV_KEYS = ["S1", "S2", "S3", "S4", "S5", "S6"]
SEV_NAME = {"S1": "경미", "S2": "관찰", "S3": "보통",
            "S4": "심각", "S5": "매우 심각", "S6": "특이값"}
SEV_HEX = {"S1": "#9ccb5b", "S2": "#2e9e5b", "S3": "#f0c419",
           "S4": "#ee8a3c", "S5": "#d9433c", "S6": "#8a6bc4"}


def _sev(h, bounds):
    """대표 폭(p95) → 심각도 S1~S6. meta 의 경계값을 그대로 쓴다."""
    w = h.get("width_mm_p95") or h.get("width_mm_median") or h.get("width_mm_max") or h.get("width_mm")
    if w is None:
        return None
    return SEV_KEYS[sum(w >= b for b in bounds)]


def _conf_band(h, bounds):
    """균열 신뢰도(AI 가 균열이라고 본 확률) → 낮음/보통/높음."""
    c = h.get("conf")
    if c is None:
        return "-"
    return CONF_NAME[2] if c >= bounds[1] else (CONF_NAME[1] if c >= bounds[0] else CONF_NAME[0])


def build_pdf(twin, out_pdf, log=print, max_rows=300, top_photos=24, debug_png=None):
    """.twin 하나로 검사 보고서 PDF 를 만든다(라이브러리 진입점).

    구성: 표지·요약 → 위치도 → 균열 대장(폭 내림차순 max_rows 개) → 사진 대지(top_photos 장).
    검토 사이드카(<twin>.review.json)가 있으면 상태·수정 유형·메모를 대장에 반영한다.
    수치는 파이프라인 무보정 mm 기준이다(뷰어의 2점 캘리브레이션은 로컬 설정이라 미반영).
    log 콜백으로 진행 상황을 전달한다(백엔드 잡 로그용). debug_png 를 주면 각 면을 PNG 로도 남긴다."""
    z = zipfile.ZipFile(twin)
    meta = json.loads(z.read("meta.json"))
    hots = meta.get("hotspots", [])
    rb_by_hot = {}
    for r in meta.get("ribbons", []):
        rb_by_hot.setdefault(r["hot"], []).append(r)
    rm = meta.get("report_meta", {})
    review = {"reviews": {}}
    if os.path.exists(twin + ".review.json"):
        with open(twin + ".review.json", encoding="utf-8") as f:
            review = json.load(f)
    revs = review.get("reviews", {})

    # 검토 유형 수정 반영(뷰어 부트와 동일 규칙)
    for k, v in revs.items():
        try:
            if v.get("type"): hots[int(k)]["type"] = v["type"]
        except (IndexError, ValueError):
            pass
    sev_bounds = rm.get("severity_bounds_mm", [0.5, 1.0, 3.0, 5.0, 10.0])
    sevs = [_sev(h, sev_bounds) for h in hots]
    cbnd = rm.get("conf_bounds", [0.7, 0.9])
    confs = [_conf_band(h, cbnd) for h in hots]
    # 정렬 기준: 심각도(절대 폭)가 1차. 예전의 길이×폭 상대값은 물리적 근거가 약해 폐지.
    sev = np.array([(h.get("width_mm_p95") or h.get("width_mm_median") or h.get("width_mm_max") or 0)
                    for h in hots])
    # 대장·사진 순서: '크랙' 유형 우선 → 심각도. 검사원이 오탐(rejected) 판정한 것은 제외.
    is_crack = np.array([1 if _type_of(h) in (None, "crack") else 0 for h in hots])
    rejected = np.array([revs.get(str(i), {}).get("state") == "rejected" for i in range(len(hots))])
    order = np.lexsort((-sev, -is_crack))
    order = [int(i) for i in order if not rejected[i]]
    n = len(hots)
    pngs = []

    # 완성된 figure 한 장을 PDF 에 넣고 닫는다(debug_png 지정 시 PNG 도 함께 저장).
    def _save(fig, name):
        pdf.savefig(fig)
        if debug_png:
            p = os.path.join(debug_png, name + ".png")
            fig.savefig(p, dpi=110); pngs.append(p)
        plt.close(fig)

    with PdfPages(out_pdf) as pdf:
        # ---------- 1) 표지 · 요약 ----------
        log("[1/4] 표지·요약 …")
        fig = plt.figure(figsize=A4L)
        fig.text(0.07, 0.90, "콘크리트 균열 검사 보고서", fontsize=26, fontweight="bold", color="#12303f")
        fig.text(0.07, 0.855, os.path.basename(twin), fontsize=13, color="#456")
        fig.text(0.07, 0.815, f"생성 {datetime.datetime.now():%Y-%m-%d %H:%M}"
                 + f" · 분석 {rm.get('created','-')} · 이미지 {rm.get('images_analyzed','-')}장"
                 + f" · 메시 {rm.get('mesh_file','-')} ({rm.get('mesh_faces','-'):,}면)"
                 if isinstance(rm.get('mesh_faces'), int) else
                 f"생성 {datetime.datetime.now():%Y-%m-%d %H:%M} · 분석 {rm.get('created','-')}",
                 fontsize=10, color="#567")
        fig.lines.append(plt.Line2D([0.07, 0.93], [0.79, 0.79], transform=fig.transFigure, color="#c9d6df", lw=1.2))
        # 등급 분포 막대
        ax = fig.add_axes([0.07, 0.44, 0.40, 0.30])
        from collections import Counter
        sc = Counter(g for g in sevs if g)
        ax.bar([SEV_NAME[k] for k in SEV_KEYS], [sc.get(k, 0) for k in SEV_KEYS],
               color=[SEV_HEX[k] for k in SEV_KEYS])
        ax.set_title(f"심각도 분포 (폭 p95 기준 · 경계 {sev_bounds} mm)", fontsize=11)
        ax.tick_params(axis="x", labelsize=8)
        for s in ("top", "right"): ax.spines[s].set_visible(False)
        # 신뢰/유형/검토 요약 텍스트
        rc = Counter(confs); tc = Counter(TYPE_LABEL.get(_type_of(h), "크랙") for h in hots)
        rvc = Counter(v.get("state") for v in revs.values())
        ty_txt = " · ".join(f"{k} {v:,}" for k, v in tc.most_common())
        txt = (f"총 균열 {n:,}개\n\n"
               f"균열 신뢰도  높음 {rc.get('높음',0):,} · 보통 {rc.get('보통',0):,} · 낮음 {rc.get('낮음',0):,}\n"
               f"유형        {ty_txt}\n"
               f"검사원 검토 {len(revs):,}/{n:,} (확인 {rvc.get('confirmed',0)} · 수정 {rvc.get('edited',0)} · 오탐 {rvc.get('rejected',0)})")
        fig.text(0.54, 0.68, txt, fontsize=11.5, va="top", linespacing=1.9, color="#234")
        warn = ("※ 본 보고서의 mm 수치는 무보정(메시 1단위=1m 가정) 값입니다. "
                "현장 캘리브레이션 확정 전에는 상대 비교·우선순위 판단용으로만 사용하십시오.")
        if rm.get("scale_warning"):
            warn += "\n※ scale_warning: 비현실적 폭(>50mm)이 검출됨 — 실척 전제 재확인 필요."
        fig.text(0.07, 0.30, warn, fontsize=10, color="#a3541e",
                 bbox=dict(boxstyle="round,pad=0.6", fc="#fdf3e7", ec="#e2b98a"))
        fig.text(0.07, 0.10, "CrackTwin Studio — 드론·AI·3D 디지털트윈 균열 점검", fontsize=9, color="#789")
        _save(fig, "p1_cover")

        # ---------- 2) 위치도 ----------
        log("[2/4] 위치도(메시 실루엣 + 균열 등급색) …")
        try:
            import trimesh
            mesh = trimesh.load(io.BytesIO(z.read("mesh.ply")), file_type="ply", process=False)
            V = np.asarray(mesh.vertices, float)
            if len(V) > 60000:
                V = V[np.random.default_rng(0).choice(len(V), 60000, replace=False)]
            c = V.mean(0)
            _, _, Vt = np.linalg.svd(V - c, full_matrices=False)      # 지배 평면(주성분 1·2축) 투영
            e1, e2 = Vt[0], Vt[1]
            P2 = np.column_stack([(V - c) @ e1, (V - c) @ e2])
            fig, ax = plt.subplots(figsize=A4L)
            ax.scatter(P2[:, 0], P2[:, 1], s=0.5, c="#c9d3da", linewidths=0)
            for gsel in SEV_KEYS:                                      # 심각도별 LineCollection
                segs = []
                for hi in range(n):
                    if sevs[hi] != gsel: continue
                    for r in rb_by_hot.get(hi, []):
                        Q = np.asarray(r["poly"], float)[::4]
                        if len(Q) < 2: continue
                        q2 = np.column_stack([(Q - c) @ e1, (Q - c) @ e2])
                        segs.extend(np.stack([q2[:-1], q2[1:]], axis=1))
                if segs:
                    ax.add_collection(LineCollection(segs, colors=SEV_HEX[gsel],
                                                     linewidths=1.1 if gsel in ("S4", "S5", "S6") else 0.7,
                                                     label=SEV_NAME[gsel]))
            ax.legend(loc="upper right", fontsize=9, framealpha=0.9)
            ax.set_aspect("equal"); ax.axis("off")
            ax.set_title("균열 위치도 — 구조물 지배 평면 투영 (심각도색)", fontsize=13)
            _save(fig, "p2_map")
        except Exception as e:
            log(f"  (위치도 생략: {e})")

        # ---------- 3) 균열 대장 ----------
        rows_all = order[:max_rows]
        log(f"[3/4] 균열 대장(심각도순 상위 {len(rows_all)}행) …")
        PER = 26
        cols = ["ID", "유형", "심각도", "신뢰도", "폭중앙", "p95", "최대", "길이", "총연장", "뷰", "검토", "메모"]
        wcol = [0.065, 0.10, 0.075, 0.05, 0.065, 0.065, 0.065, 0.075, 0.075, 0.035, 0.055, 0.235]
        for pg in range(0, len(rows_all), PER):
            fig, ax = plt.subplots(figsize=A4L); ax.axis("off")
            ax.set_title(f"균열 대장 (심각도순 {pg+1}–{min(pg+PER,len(rows_all))} / 상위 {len(rows_all)} · 전체 {n:,})",
                         fontsize=12, loc="left")
            cell = []
            for hi in rows_all[pg:pg + PER]:
                h = hots[hi]; rv = revs.get(str(hi), {})
                f1 = lambda x: (f"{x:.2f}" if isinstance(x, (int, float)) else "-")
                f0 = lambda x: (f"{x:,.0f}" if isinstance(x, (int, float)) else "-")
                cell.append([f"C-{hi+1:04d}", TYPE_LABEL.get(_type_of(h), "크랙"),
                             SEV_NAME.get(sevs[hi], "-"), confs[hi],
                             f1(h.get("width_mm_median")), f1(h.get("width_mm_p95")), f1(h.get("width_mm_max")),
                             f0(h.get("length_mm")), f0(h.get("length_total_mm")),
                             str(len(h.get("views", []))), REV_LABEL.get(rv.get("state"), "-"),
                             (rv.get("note") or "")[:26]])
            tb = ax.table(cellText=cell, colLabels=cols, colWidths=wcol, loc="upper center", cellLoc="center")
            tb.auto_set_font_size(False); tb.set_fontsize(7.6); tb.scale(1, 1.35)
            for (r0, c0), c_ in tb.get_celld().items():
                c_.set_edgecolor("#d5dde4")
                if r0 == 0:
                    c_.set_facecolor("#eef3f7"); c_.set_text_props(fontweight="bold", color="#234")
                elif c0 == 2:
                    _k = next((k for k, v in SEV_NAME.items() if v == c_.get_text().get_text()), None)
                    if _k: c_.set_text_props(color=SEV_HEX[_k], fontweight="bold")
            _save(fig, f"p3_table_{pg//PER}")

        # ---------- 4) 사진 대지 ----------
        cams = {c_["source"]: c_ for c_ in meta.get("cameras", [])}
        photos = [hi for hi in order if hots[hi].get("views") and rb_by_hot.get(hi)][:top_photos]
        log(f"[4/4] 사진 대지(상위 {len(photos)}개) …")
        if photos:
            import cv2
            import cracktype as CT
            PPP = 6                                             # 페이지당 2행×3열
            for pg in range(0, len(photos), PPP):
                fig = plt.figure(figsize=A4L)
                fig.suptitle(f"균열 사진 대지 (심각도순 {pg+1}–{min(pg+PPP,len(photos))})", fontsize=12)
                for k, hi in enumerate(photos[pg:pg + PPP]):
                    h = hots[hi]
                    cam = cams.get(h["views"][0]["source"])
                    ax = fig.add_subplot(2, 3, k + 1); ax.axis("off")
                    try:
                        img = cv2.imdecode(np.frombuffer(z.read(f"img/{cam['id']:04d}_full.jpg"), np.uint8),
                                           cv2.IMREAD_COLOR)[:, :, ::-1]
                        P = np.array([p for r in rb_by_hot[hi] for p in r["poly"]], float)
                        uv, ok = CT.project_to_cam(cam, P); uv = uv[ok] * (img.shape[1] / cam["w"])
                        if len(uv) < 2: raise RuntimeError("투영 실패")
                        x0, y0 = uv.min(0); x1, y1 = uv.max(0)
                        mrg = max(40, 0.25 * max(x1 - x0, y1 - y0))
                        xa, xb = int(max(0, x0 - mrg)), int(min(img.shape[1], x1 + mrg))
                        ya, yb = int(max(0, y0 - mrg)), int(min(img.shape[0], y1 + mrg))
                        ax.imshow(img[ya:yb, xa:xb])
                        ax.plot(uv[:, 0] - xa, uv[:, 1] - ya, color="#ff3b30", lw=1.0, alpha=0.85)
                    except Exception as e:
                        ax.text(0.5, 0.5, f"이미지 없음\n({e})", ha="center", va="center", fontsize=8, color="#999")
                    sv = sevs[hi]
                    ax.set_title(f"C-{hi+1:04d} · {TYPE_LABEL.get(_type_of(h),'크랙')} · "
                                 f"{SEV_NAME.get(sv,'')}\n폭 {h.get('width_mm_median','-')}mm · "
                                 f"길이 {h.get('length_mm','-')}mm",
                                 fontsize=8.5, color=SEV_HEX.get(sv, "#333"))
                _save(fig, f"p4_photo_{pg//PPP}")

    log(f"저장: {out_pdf} ({os.path.getsize(out_pdf)/1e6:.1f} MB)")
    return out_pdf, pngs


def main():
    """CLI 진입점. 인자를 파싱해 build_pdf() 를 부른다(출력 경로 미지정 시 .twin 옆에 만든다).
    앱에서는 backend 가 build_pdf 를 직접 호출하므로 이 경로를 타지 않는다."""
    ap = argparse.ArgumentParser()
    ap.add_argument("twin")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--rows", type=int, default=300)
    ap.add_argument("--photos", type=int, default=24)
    ap.add_argument("--debug-png", default=None)
    a = ap.parse_args()
    out = a.out or (os.path.splitext(a.twin)[0] + "_report.pdf")
    build_pdf(a.twin, out, max_rows=a.rows, top_photos=a.photos, debug_png=a.debug_png)


if __name__ == "__main__":
    main()
