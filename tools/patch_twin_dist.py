# -*- coding: utf-8 -*-
"""기존 .twin 에 렌즈 왜곡계수(dist)를 주입한다 — 재분석 없이 뷰어 하이라이트 정합 수정용.

사용법:  py tools/patch_twin_dist.py <report.twin> <XMP 폴더>

meta.json 의 cameras[*] 에 "dist":[k1,k2,p1,p2,k3] 를 source 이름 매칭으로 추가한다.
(뷰어는 dist 가 있으면 3D→이미지 되투영에 왜곡을 적용한다. 실측: 핀홀만 33px → 적용 0.2px)
"""
import sys, os, json, zipfile, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import parse_xmp


def xmp_for(folder, source):
    base = os.path.splitext(source)[0]
    for cand in (os.path.join(folder, base + ".xmp"), os.path.join(folder, source + ".xmp")):
        if os.path.isfile(cand):
            return cand
    return None


def main(twin, folder):
    zin = zipfile.ZipFile(twin)
    meta = json.loads(zin.read("meta.json"))
    patched = missing = skipped = 0
    for cam in meta.get("cameras", []):
        if cam.get("dist"):
            skipped += 1
            continue
        xp = xmp_for(folder, cam.get("source", ""))
        if not xp:
            missing += 1
            continue
        _K, dist, _R, _T, _wh = parse_xmp(xp)
        cam["dist"] = [float(x) for x in dist]
        patched += 1
    if not patched:
        print(f"패치할 카메라 없음 (이미 있음 {skipped} · XMP 없음 {missing})")
        return
    # 원자적 재작성: 임시 파일에 전체 복사(meta.json 만 교체) 후 교체
    fd, tmp = tempfile.mkstemp(suffix=".twin", dir=os.path.dirname(os.path.abspath(twin)))
    os.close(fd)
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
    print(f"완료: dist 추가 {patched}대 (이미 있음 {skipped} · XMP 없음 {missing}) → {twin}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
