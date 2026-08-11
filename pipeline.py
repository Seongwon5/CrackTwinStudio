# -*- coding: utf-8 -*-
"""
pipeline.py — 보고서 생성

이미지+XMP 폴더와 3D 메시(PLY)를 받아 사진·카메라 포즈·메시를 담은 .twin(zip)을 만든다.
XMP의 포즈는 뷰어가 '클릭한 3D 점을 찍은 사진'을 찾는 데 쓴다.
"""
import os
import json
import zipfile
import numpy as np
import cv2

IMG_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")
FULL_MAX_PX, FULL_JPEG_Q = 1920, 82   # 내장 원본 최대변 / 품질
THUMB_MAX_PX = 900                    # 썸네일 최대변


# ---------------------------------------------------------------- 입력 · 기하

def find_pairs(folder):
    """폴더에서 (이미지, XMP) 짝을 파일명 순으로 반환한다.

    `photo.jpg.xmp`와 `photo.xmp` 두 규칙을 모두 받는다. 짝이 없는 이미지도
    (경로, None)으로 포함해 호출부가 '제외됨'으로 집계하게 한다.
    """
    files = os.listdir(folder)
    xmps = {f.lower(): f for f in files if f.lower().endswith(".xmp")}
    pairs = []
    for f in files:
        if not f.lower().endswith(IMG_EXTS):
            continue
        base, _ = os.path.splitext(f)
        matched = xmps.get((f + ".xmp").lower()) or xmps.get((base + ".xmp").lower())
        pairs.append((os.path.join(folder, f),
                      os.path.join(folder, matched) if matched else None))
    return sorted(pairs, key=lambda p: p[0])


def parse_xmp(path):
    """XMP에서 내부파라미터 K·왜곡 dist·회전 R·평행이동 T·보정해상도를 읽는다."""
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    cal, ext = root.find(".//calibration"), root.find(".//extrinsics")
    if cal is None or ext is None:
        raise RuntimeError("calibration/extrinsics 없음")
    K = np.array([[float(cal.get("fx")), 0, float(cal.get("cx"))],
                  [0, float(cal.get("fy")), float(cal.get("cy"))],
                  [0, 0, 1]], float)
    dist = np.array([float(cal.get("k1", 0)), float(cal.get("k2", 0)),
                     float(cal.get("p1", 0)), float(cal.get("p2", 0)),
                     float(cal.get("k3", 0))], float)
    w, h = cal.get("w"), cal.get("h")
    calib_wh = (int(w), int(h)) if (w and h) else None
    R = np.array([float(x) for x in ext.find("rotation").text.split()], float).reshape(3, 3)
    T = np.array([float(x) for x in ext.find("translation").text.split()], float)
    return K, dist, R, T, calib_wh


def orient_to_xmp(file_img, calib_wh):
    """[폴백] 이미지를 XMP 보정 방향에 맞도록 90° 단위로 회전한다.

    90° 시계/반시계는 회전 후 모양이 같아 구분할 수 없다(첫 매치 채택).
    정상 경로는 load_oriented()가 치수로 판정하며, 여기는 최후 수단이다.
    """
    base_k = 0
    if calib_wh:
        target_hw = (calib_wh[1], calib_wh[0])
        for k in (0, 1, 2, 3):
            if np.rot90(file_img, k).shape[:2] == target_hw:
                base_k = k
                break
    return np.rot90(file_img, base_k).copy() if base_k else file_img.copy()


def load_oriented(img_path, calib_wh, log=None):
    """이미지를 XMP 보정과 같은 방향의 BGR 배열로 로드한다.

    사진측량 도구가 EXIF 회전을 적용했는지는 제각각이므로 치수로 판정한다.
      calib == 원시 치수      → 원시 그대로
      calib == EXIF 적용 치수 → exif_transpose
      그 외                  → 종횡비 폴백 + 경고
    """
    from PIL import Image, ImageOps
    pil = Image.open(img_path)
    raw_wh = pil.size
    ori = pil.getexif().get(274, 1)
    exif_wh = (raw_wh[1], raw_wh[0]) if ori in (5, 6, 7, 8) else raw_wh
    if calib_wh and tuple(calib_wh) == raw_wh:
        rgb = pil.convert("RGB")
    elif calib_wh and tuple(calib_wh) == exif_wh:
        rgb = ImageOps.exif_transpose(pil).convert("RGB")
    else:
        if log:
            log(f"    ⚠ {os.path.basename(img_path)}: calib {calib_wh} 가 원시 {raw_wh}·"
                f"EXIF 적용 {exif_wh} 와 불일치 — 종횡비로 추정")
        disp = cv2.cvtColor(np.array(ImageOps.exif_transpose(pil).convert("RGB")), cv2.COLOR_RGB2BGR)
        return orient_to_xmp(disp, calib_wh)
    return cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2BGR)


def camera_pose(K, R, T, dw, dh):
    """월드 좌표계 카메라 자세: 중심·전방·상·우·화각·종횡비·내부파라미터.

    X_cam = R(X-C) 규약 → C = -Rᵀ T, fwd = Rᵀ e_z, up = -Rᵀ e_y, right = Rᵀ e_x.
    """
    C = -R.T @ T
    fwd = R.T @ np.array([0.0, 0.0, 1.0])
    up = -(R.T @ np.array([0.0, 1.0, 0.0]))
    right = R.T @ np.array([1.0, 0.0, 0.0])
    fx, fy = float(K[0, 0]), float(K[1, 1])
    cx, cy = float(K[0, 2]), float(K[1, 2])
    unit = lambda v: v / np.linalg.norm(v) if np.linalg.norm(v) > 1e-12 else v
    return {"pos": [float(x) for x in C],
            "fwd": [float(x) for x in unit(fwd)],
            "up": [float(x) for x in unit(up)],
            "right": [float(x) for x in unit(right)],
            "fov": float(np.degrees(2.0 * np.arctan((dh / 2.0) / fy))),
            "aspect": float(dw) / float(dh), "w": int(dw), "h": int(dh),
            "fx": fx, "fy": fy, "cx": cx, "cy": cy}


def encode_jpeg_bytes(img_bgr, max_px, quality):
    """BGR 이미지를 최대변 max_px로 축소해 JPEG 바이트로 인코딩한다."""
    h, w = img_bgr.shape[:2]
    s = min(1.0, float(max_px) / max(h, w))
    im = cv2.resize(img_bgr, (0, 0), fx=s, fy=s) if s < 1.0 else img_bgr
    ok, buf = cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    return buf.tobytes()


# ---------------------------------------------------------------- 보고서 생성

def build_report(folder, ply_path, out_path, log, progress):
    """이미지+XMP 폴더와 PLY를 .twin(zip) 하나로 묶는다.

    구성은 meta.json / mesh.ply / img/*. 이미지는 처리 즉시 기록해 피크 메모리를
    한 장 수준으로 유지하고, .tmp에 다 쓴 뒤 교체해 중간 실패 시 손상 파일을 남기지 않는다.
    """
    if not out_path.lower().endswith(".twin"):
        out_path += ".twin"

    all_found = find_pairs(folder)
    no_xmp = [os.path.basename(i) for i, x in all_found if not x]
    pairs = [(i, x) for i, x in all_found if x]
    if no_xmp:
        preview = ", ".join(no_xmp[:5]) + (f" 외 {len(no_xmp) - 5}장" if len(no_xmp) > 5 else "")
        log(f"⚠ XMP 없어 제외: {len(no_xmp)}장 [{preview}]")
    log(f"이미지 {len(all_found)}장 발견 · 대상 {len(pairs)}장")

    tmp = out_path + ".tmp"
    cams, skipped = [], list(no_xmp)
    try:
        with zipfile.ZipFile(tmp, "w", allowZip64=True) as z:
            # JPEG·PLY는 STORED — 이미 압축돼 있어 DEFLATE는 시간만 든다
            z.write(ply_path, "mesh.ply", compress_type=zipfile.ZIP_STORED)
            for n, (img_path, xmp_path) in enumerate(pairs):
                name = os.path.basename(img_path)
                try:
                    K, dist, R, T, calib_wh = parse_xmp(xmp_path)
                    disp = load_oriented(img_path, calib_wh, log=log)
                    dh, dw = disp.shape[:2]
                    cam = camera_pose(K, R, T, dw, dh)
                    cam["dist"] = [float(x) for x in dist]   # 뷰어 투영 시 왜곡 재적용
                    cam["id"], cam["source"] = n, name
                    z.writestr(f"img/{n:04d}_thumb.jpg",
                               encode_jpeg_bytes(disp, THUMB_MAX_PX, 75), zipfile.ZIP_STORED)
                    z.writestr(f"img/{n:04d}_full.jpg",
                               encode_jpeg_bytes(disp, FULL_MAX_PX, FULL_JPEG_Q), zipfile.ZIP_STORED)
                    cams.append(cam)
                    log(f"  [{n + 1}/{len(pairs)}] {name}")
                except Exception as e:
                    log(f"  [오류] {name}: {e}")
                    skipped.append(name)
                progress(n + 1, len(pairs))

            import datetime
            meta = {
                "version": 2,
                "counts": {"cameras": len(cams)},
                "report_meta": {
                    "created": datetime.datetime.now().isoformat(timespec="seconds"),
                    "source_folder": os.path.basename(os.path.normpath(folder)),
                    "mesh_file": os.path.basename(ply_path),
                    "images_found": len(all_found),
                    "images_used": len(cams),
                    "images_skipped": len(skipped),
                    "skipped_list": skipped[:50],
                },
                "cameras": cams,
            }
            # 무압축 — 모바일 뷰어가 브라우저에서 압축 해제 없이 바로 읽는다(구형 폰 대응).
            # 카메라 수백 대라도 수백 KB 수준이라 용량 이득이 의미 없다.
            z.writestr("meta.json", json.dumps(meta, ensure_ascii=False), zipfile.ZIP_STORED)
        os.replace(tmp, out_path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise

    log(f"저장: {out_path} ({os.path.getsize(out_path) / 1e6:.1f} MB) · 사진 {len(cams)}장")
    return out_path
