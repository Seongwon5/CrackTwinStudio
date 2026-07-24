# -*- coding: utf-8 -*-
"""분류기에서 한 클래스를 다른 클래스로 병합한다(재학습 없이).

    py tools/merge_class.py --from craze --to crack

랜덤포레스트의 각 리프는 클래스별 표본 수 벡터를 갖는다. 두 클래스를 합치는 것은
그 두 열을 더하는 것과 같아, 원 학습 신호를 그대로 보존한 유효한 분류기가 된다
(재학습이 필요 없고, 원본 라벨이 없어도 가능).

용도: '실금(craze)'을 '크랙(crack)'으로 흡수 — 실금도 결국 가는 균열이라 별도 유형으로
두지 않기로 한 결정을 모델에 반영.
"""
import os
import sys
import json
import shutil
import argparse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = os.path.join(HERE, "model", "crack_type.json")


def merge(model_path, src, dst, out=None, log=print):
    with open(model_path, encoding="utf-8") as f:
        m = json.load(f)
    cls = m["classes"]
    if src not in cls:
        log(f"'{src}' 클래스가 모델에 없습니다 (현재: {cls}) — 변경 없음"); return False
    if dst not in cls:
        log(f"'{dst}' 클래스가 모델에 없습니다 (현재: {cls}) — 중단"); return False
    si, di = cls.index(src), cls.index(dst)
    for t in m["trees"]:
        for v in t["va"]:                      # 리프별 클래스 표본 수
            v[di] += v[si]                     # src 표본을 dst 로 흡수
            v[si] = 0
    # 열 제거(순서 유지)
    keep = [k for k in range(len(cls)) if k != si]
    m["classes"] = [cls[k] for k in keep]
    for t in m["trees"]:
        t["va"] = [[v[k] for k in keep] for v in t["va"]]
    m["note"] = (m.get("note", "") + f" | merged {src}->{dst}").strip(" |")
    out = out or model_path
    with open(out, "w", encoding="utf-8") as f:
        json.dump(m, f)
    log(f"병합 완료: {cls} → {m['classes']}  ({src} → {dst})")
    log(f"저장: {out}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--to", dest="dst", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-backup", action="store_true")
    a = ap.parse_args()
    if not a.no_backup and (a.out in (None, a.model)):
        b = os.path.splitext(a.model)[0] + ".premerge.json"
        shutil.copy(a.model, b); print("백업:", b)
    merge(a.model, a.src, a.dst, a.out)


if __name__ == "__main__":
    main()
