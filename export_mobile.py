# -*- coding: utf-8 -*-
"""export_mobile.py — .twin → 단일 HTML 모바일 뷰어.

산출물은 서버·설치 없이 폰 브라우저에서 바로 열리는 자체완결 파일이다.
three.js·조작 코드·메시·사진을 전부 한 파일에 인라인한다.

다운샘플 없이 원본 그대로 담는다:
  메시  — 전체 정점·면을 컴팩트 바이너리(f32 위치 + u8 색 + u32 인덱스)로 재포장
  사진  — .twin 에 들어 있는 이미지 바이트를 그대로 임베드(썸네일만 작게 재인코딩)
주의: 메시·사진이 크면 결과 HTML 도 커진다(폰 메모리에 따라 로딩이 느릴 수 있음).

ESM 모듈(three)은 단일 파일에서 <script src>로 불러올 수 없으므로,
소스를 text/plain 으로 심어 두고 런타임에 Blob URL 로 동적 import 한다.
"""
import io
import os
import re
import json
import base64
import struct
import zipfile
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
LIBS = os.path.join(HERE, "libs")
UI = os.path.join(HERE, "ui")

THUMB_PX = 320                 # 하단 썸네일 스트립용(작게 재인코딩)
THUMB_Q = 60


# ────────────────────────────────────────────────────────── PLY 파싱
_TYPES = {"char": "i1", "uchar": "u1", "int8": "i1", "uint8": "u1",
          "short": "i2", "ushort": "u2", "int16": "i2", "uint16": "u2",
          "int": "i4", "uint": "u4", "int32": "i4", "uint32": "u4",
          "float": "f4", "float32": "f4", "double": "f8", "float64": "f8"}


def parse_ply(buf):
    """바이너리 리틀엔디언 PLY → (positions f64 (V,3), colors u8 (V,3)|None, faces i64 (F,3))."""
    head_end = buf.find(b"end_header\n")
    if head_end < 0:
        raise ValueError("PLY 헤더 없음")
    header = buf[:head_end].decode("ascii", "replace").splitlines()
    body = memoryview(buf)[head_end + len(b"end_header\n"):]

    fmt = next((l.split()[1] for l in header if l.startswith("format ")), "")
    if "binary_little_endian" not in fmt:
        raise ValueError("지원하지 않는 PLY 형식: " + fmt)

    elems = []                                  # [(name, count, [(prop, dtype)…] | ("list", ct, it))]
    for l in header:
        t = l.split()
        if not t:
            continue
        if t[0] == "element":
            elems.append([t[1], int(t[2]), []])
        elif t[0] == "property" and elems:
            if t[1] == "list":
                elems[-1][2].append(("__list__", _TYPES[t[2]], _TYPES[t[3]]))
            else:
                elems[-1][2].append((t[-1], _TYPES[t[1]]))

    off = 0
    pos = col = faces = None
    for name, count, props in elems:
        if name == "vertex":
            dt = np.dtype([(p[0], "<" + p[1]) for p in props])
            arr = np.frombuffer(body, dtype=dt, count=count, offset=off)
            off += dt.itemsize * count
            pos = np.stack([arr["x"], arr["y"], arr["z"]], axis=1).astype(np.float64)
            if all(k in dt.names for k in ("red", "green", "blue")):
                col = np.stack([arr["red"], arr["green"], arr["blue"]], axis=1).astype(np.uint8)
        elif name == "face":
            ct, it = props[0][1], props[0][2]
            cs, is_ = np.dtype(ct).itemsize, np.dtype(it).itemsize
            stride = cs + 3 * is_
            sub = body[off: off + stride * count]
            # 모든 면이 삼각형(count==3)일 때만 벡터화 파싱 — 사진측량 메시는 삼각형이다
            dt = np.dtype([("n", "<" + ct), ("v", "<" + it, (3,))])
            arr = np.frombuffer(sub, dtype=dt, count=count)
            if not (arr["n"] == 3).all():
                raise ValueError("삼각형이 아닌 면 포함 — 지원하지 않음")
            faces = arr["v"].astype(np.int64)
            off += stride * count
        else:                                    # 알 수 없는 요소 — 고정폭이면 건너뜀
            w = sum(np.dtype(p[1]).itemsize for p in props if p[0] != "__list__")
            off += w * count
    if pos is None or faces is None:
        raise ValueError("vertex/face 요소 없음")
    return pos, col, faces


# ────────────────────────────────────────────────────────── 포장
def pack_mesh(pos, col, faces):
    """[u32 V][u32 F] + f32 pos + u8 col(+4바이트 정렬 패딩) + u32 idx → base64."""
    V, F = len(pos), len(faces)
    if col is None:
        col = np.full((V, 3), 158, np.uint8)
    pad = (4 - (V * 3) % 4) % 4
    blob = (struct.pack("<II", V, F)
            + pos.astype("<f4").tobytes()
            + col.astype("u1").tobytes() + b"\x00" * pad
            + faces.astype("<u4").tobytes())
    return base64.b64encode(blob).decode("ascii"), V, F


# ────────────────────────────────────────────────────────── 사진
def _embed_raw(z, name):
    """.twin 안 이미지 바이트를 재인코딩 없이 그대로 data URI 로 임베드(원본 화질 유지)."""
    with z.open(name) as f:
        raw = f.read()
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


def _reencode(z, name, max_px, q):
    """썸네일 스트립용 — 최대변 max_px 로 축소해 재인코딩(파일 크기 절감)."""
    with z.open(name) as f:
        im = Image.open(io.BytesIO(f.read())).convert("RGB")
    s = max_px / max(im.size)
    if s < 1.0:
        im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=q)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


# ────────────────────────────────────────────────────────── 조립
def _esc(s):
    """인라인 <script> 안에서 닫힘 토큰이 파서를 끊지 않게 이스케이프(런타임에 복원)."""
    return s.replace("</script", "<\\/script")


def _oc_source():
    """OrbitControls 모듈 → 일반 함수 본문(THREE 를 인자로 받는 형태)으로 변환."""
    src = open(os.path.join(LIBS, "addons", "controls", "OrbitControls.js"), encoding="utf-8").read()
    src = re.sub(r"import\s*\{([^}]*)\}\s*from\s*['\"]three['\"];?", r"const {\1} = THREE;", src)
    src = re.sub(r"export\s*\{[^}]*\};?", "", src)
    return src


def _bvh_source():
    """bvh.js → export 를 떼어 인라인 가능한 본문으로. three 의존이 없어 변환은 이것뿐이다."""
    src = open(os.path.join(UI, "bvh.js"), encoding="utf-8").read()
    return re.sub(r"^export\s+", "", src, flags=re.MULTILINE)


def export_mobile(twin_path, out_path=None, log=print):
    """단일 HTML 모바일 뷰어를 생성하고 경로를 반환한다."""
    out_path = out_path or os.path.splitext(twin_path)[0] + "_mobile.html"
    name = os.path.splitext(os.path.basename(twin_path))[0]

    with zipfile.ZipFile(twin_path) as z:
        meta = json.loads(z.read("meta.json").decode("utf-8"))
        log("메시 파싱…")
        pos, col, faces = parse_ply(z.read("mesh.ply"))
        log(f"  전체 메시 그대로 담기 — {len(pos):,} 정점 · {len(faces):,} 면")
        mesh_b64, V, F = pack_mesh(pos, col, faces)

        cams = []
        imgs = {}
        total = 0
        cam_list = meta.get("cameras") or []
        for i, c in enumerate(cam_list):
            cid = c["id"]
            # 뷰어가 쓰는 필드만 추린다(main .twin 의 크랙 데이터 등은 버림)
            cams.append({k: c[k] for k in
                         ("id", "source", "pos", "fwd", "up", "right",
                          "fx", "fy", "cx", "cy", "w", "h", "dist") if k in c})
            full = _embed_raw(z, f"img/{cid:04d}_full.jpg")     # 원본 화질 그대로
            thumb = _reencode(z, f"img/{cid:04d}_thumb.jpg", THUMB_PX, THUMB_Q)
            imgs[str(cid)] = {"f": full, "t": thumb}
            total += len(full) + len(thumb)
            log(f"  사진 {i + 1}/{len(cam_list)}")

    tpl = open(os.path.join(UI, "mobile_template.html"), encoding="utf-8").read()
    html = (tpl
            .replace("__TITLE__", name)
            .replace("__THREE_SRC__", _esc(open(os.path.join(LIBS, "three.module.js"), encoding="utf-8").read()))
            .replace("__OC_SRC__", _esc(_oc_source()))
            .replace("__BVH_SRC__", _esc(_bvh_source()))
            .replace("__META__", _esc(json.dumps({"name": name, "cameras": cams}, ensure_ascii=False)))
            .replace("__IMGS__", _esc(json.dumps(imgs)))
            .replace("__MESH_B64__", mesh_b64))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    mb = os.path.getsize(out_path) / 1e6
    log(f"완료: {os.path.basename(out_path)} ({mb:.1f} MB · 메시 {V:,}v/{F:,}f · 사진 {len(imgs)}장)")
    return out_path


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("twin")
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args()
    export_mobile(a.twin, a.out)
