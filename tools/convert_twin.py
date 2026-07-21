# -*- coding: utf-8 -*-
"""
convert_twin.py  ―  구형식(v1) .twin 을 v2(zip 컨테이너)로 변환
================================================================================
v1 은 단일 JSON 이고 메시·이미지가 base64 로 박혀 있다. 그래서 뷰어가 파일 전체를 JS 소스에
인라인해야 했고, V8 최대 문자열(536.9MB)을 넘는 순간 열 수 없게 된다.
v2 는 같은 내용을 zip 안에 meta.json / mesh.ply / img/*.jpg 로 나눠 담는다(파일은 여전히 하나).
백엔드가 멤버를 스트리밍하므로 크기 제한이 사라진다.

파일 전체를 메모리에 올리지 않는다:
  base64 와 data-URI 에는 따옴표도 이스케이프도 없다 → 여는 따옴표부터 다음 따옴표까지가 값이다.
  이 성질 덕에 스트리밍 스캔으로 값의 바이트 구간을 정확히 찾을 수 있다.

사용:
    py tools/convert_twin.py "reports/3D Model.twin"
    py tools/convert_twin.py "reports/3D Model.twin" --decimate 3000000
    py tools/convert_twin.py "reports/3D Model.twin" -o "reports/out.twin"
"""
import os
import io
import sys
import json
import time
import base64
import zipfile
import argparse
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

CHUNK = 1 << 24                     # 24MB 스캔 청크
MESH_KEY = b'"mesh_base64": "'
IMG_KEYS = (b'"thumb": "', b'"full": "')


def _find_ranges(path, log):
    """스트리밍 스캔으로 큰 base64 값들의 (종류, 시작, 끝) 바이트 구간을 찾는다.
    끝 = 닫는 따옴표 위치(값에 따옴표가 없으므로 안전)."""
    size = os.path.getsize(path)
    keys = (MESH_KEY,) + IMG_KEYS
    maxk = max(len(k) for k in keys)
    ranges = []                     # (kind, start, end)
    pos = 0
    pending = None                  # 값 안을 지나는 중이면 (kind, start)
    tail = b""
    t0 = time.perf_counter()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            buf = tail + chunk
            base = pos - len(tail)
            i = 0
            while i < len(buf):
                if pending is None:
                    hit, kind = -1, None
                    for k in keys:
                        j = buf.find(k, i)
                        if j >= 0 and (hit < 0 or j < hit):
                            hit, kind = j, k
                    if hit < 0:
                        break
                    pending = (kind, base + hit + len(kind))
                    i = hit + len(kind)
                else:
                    j = buf.find(b'"', i)
                    if j < 0:
                        i = len(buf)
                        break
                    ranges.append((pending[0], pending[1], base + j))
                    pending = None
                    i = j + 1
            pos += len(chunk)
            # 값 안을 지나는 중이면 tail 을 남길 필요가 없다(따옴표만 찾으면 됨)
            tail = b"" if pending is not None else buf[-(maxk - 1):]
    log(f"  스캔 {time.perf_counter()-t0:.1f}s  ({size/(time.perf_counter()-t0)/1e6:.0f} MB/s)"
        f"  → 값 {len(ranges)}개")
    return ranges, size


def _iter_b64(f, start, end, skip_datauri=False):
    """[start,end) 의 base64 를 4문자 정렬 청크로 읽어 디코드하며 yield."""
    f.seek(start)
    if skip_datauri:                         # data:image/jpeg;base64,XXXX → 접두사 건너뜀
        head = f.read(min(64, end - start))
        c = head.find(b",")
        if c < 0:
            raise ValueError("data-URI 에 ',' 가 없습니다")
        start += c + 1
        f.seek(start)
    remain = end - start
    carry = b""
    while remain > 0:
        n = min(CHUNK, remain)
        buf = carry + f.read(n)
        remain -= n
        cut = (len(buf) // 4) * 4 if remain > 0 else len(buf)
        carry = buf[cut:]
        if cut:
            yield base64.b64decode(buf[:cut])
    if carry:
        yield base64.b64decode(carry)


def _reduced_json(path, ranges, log):
    """큰 값 구간을 ""로 치환한 축소 JSON 을 만든다(수 MB 수준 → 안전하게 파싱 가능)."""
    out = io.BytesIO()
    with open(path, "rb") as f:
        prev = 0
        for kind, s, e in ranges:
            keep = s - prev - 1                       # 여는 따옴표 직전까지
            f.seek(prev)
            left = keep
            while left > 0:
                b = f.read(min(CHUNK, left))
                out.write(b); left -= len(b)
            out.write(b'""')                          # 값 자리에 빈 문자열
            prev = e + 1                              # 닫는 따옴표 다음
        f.seek(prev)
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            out.write(b)
    raw = out.getvalue()
    log(f"  축소 JSON {len(raw)/1e6:.1f}MB → 파싱")
    return json.loads(raw.decode("utf-8"))


def _decimate(ply_path, target, log):
    """뷰어용으로 면 수를 줄인다. 분석(폭·길이)은 서버에서 원본으로 이미 끝나 있으므로
    여기서 줄여도 측정값에는 영향이 없다(뷰어의 클릭 측정 정밀도만 낮아진다)."""
    import trimesh
    t0 = time.perf_counter()
    m = trimesh.load(ply_path, force="mesh")
    n0 = len(m.faces)
    if n0 <= target:
        log(f"  데시메이션 생략: 이미 {n0:,}면 ≤ 목표 {target:,}")
        return ply_path, n0, n0
    log(f"  데시메이션 {n0:,} → {target:,}면 ...")
    r = m.simplify_quadric_decimation(face_count=target)
    out = ply_path + ".dec.ply"
    r.export(out, file_type="ply", encoding="binary")
    log(f"  데시메이션 완료 {len(r.faces):,}면  {time.perf_counter()-t0:.1f}s  "
        f"({os.path.getsize(ply_path)/1e6:.0f}MB → {os.path.getsize(out)/1e6:.0f}MB)")
    return out, n0, len(r.faces)


def convert(src, dst=None, decimate=None, log=print):
    if dst is None:
        base, ext = os.path.splitext(src)
        dst = base + "_v2" + ext
    log(f"변환: {os.path.basename(src)}  ({os.path.getsize(src)/1e9:.2f} GB)")

    ranges, size = _find_ranges(src, log)
    mesh_r = [r for r in ranges if r[0] == MESH_KEY]
    img_r = [r for r in ranges if r[0] != MESH_KEY]
    if len(mesh_r) != 1:
        raise ValueError(f"mesh_base64 를 {len(mesh_r)}개 찾았습니다(1개여야 함)")

    scene = _reduced_json(src, ranges, log)
    cams = scene.get("cameras", [])
    if len(img_r) != 2 * len(cams):
        log(f"  ⚠ 이미지 값 {len(img_r)}개 vs 카메라 {len(cams)}대×2 — 순서 가정 확인 필요")

    tmpd = tempfile.mkdtemp(prefix="twin_")
    ply = os.path.join(tmpd, "mesh.ply")
    try:
        # ---- 메시 ----
        t0 = time.perf_counter()
        with open(src, "rb") as f, open(ply, "wb") as w:
            for b in _iter_b64(f, mesh_r[0][1], mesh_r[0][2]):
                w.write(b)
        log(f"  메시 추출 {os.path.getsize(ply)/1e6:.0f}MB  {time.perf_counter()-t0:.1f}s")
        n0 = n1 = None
        if decimate:
            ply, n0, n1 = _decimate(ply, decimate, log)

        # ---- zip 기록 ----
        t0 = time.perf_counter()
        with zipfile.ZipFile(dst, "w", allowZip64=True) as z:
            # PLY 는 DEFLATE 해도 16%밖에 안 줄면서 30~60s 를 먹는다(실측) → STORED
            z.write(ply, "mesh.ply", compress_type=zipfile.ZIP_STORED)
            with open(src, "rb") as f:
                for k, (kind, s, e) in enumerate(img_r):
                    cid, tag = k // 2, ("thumb" if kind == IMG_KEYS[0] else "full")
                    with z.open(f"img/{cid:04d}_{tag}.jpg", "w") as w:
                        for b in _iter_b64(f, s, e, skip_datauri=True):
                            w.write(b)
            for i, c in enumerate(cams):
                c.pop("thumb", None); c.pop("full", None)
                c["id"] = i                                   # 파일명(img/{id:04d}_*)과 일치시킴
            meta = {"version": 2,
                    "counts": {"cameras": len(cams), "hotspots": len(scene.get("hotspots", []))},
                    "text_points": scene.get("text_points", []),
                    "cameras": cams,
                    "hotspots": scene.get("hotspots", [])}
            if n0:
                meta["mesh"] = {"faces": n1, "faces_original": n0, "decimated": True}
            z.writestr("meta.json", json.dumps(meta, ensure_ascii=False), zipfile.ZIP_DEFLATED)
        log(f"  zip 기록 {time.perf_counter()-t0:.1f}s")
    finally:
        for p in (ply, os.path.join(tmpd, "mesh.ply")):
            if os.path.exists(p):
                try: os.remove(p)
                except OSError: pass
        try: os.rmdir(tmpd)
        except OSError: pass

    o = os.path.getsize(dst)
    log(f"완료: {dst}\n  {size/1e9:.2f} GB → {o/1e9:.2f} GB ({100*o/size:.0f}%)")
    return dst


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="구형식 .twin → v2(zip) 변환")
    ap.add_argument("src")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--decimate", type=int, default=None,
                    help="뷰어용 목표 면 수(예: 3000000). 분석 결과(mm)에는 영향 없음")
    a = ap.parse_args()
    t = time.perf_counter()
    convert(a.src, a.out, a.decimate)
    print(f"총 {time.perf_counter()-t:.1f}s")
