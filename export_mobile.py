# -*- coding: utf-8 -*-
"""export_mobile.py — 모바일 뷰어 HTML 만들기

산출물은 보고서와 **무관한 고정 파일** 하나다. 폰에서 열면 `.twin` 을 고르는 화면이
뜨고, 사진·메시는 그 `.twin` 에서 직접 읽는다. 그래서 보고서마다 새로 만들 필요가
없고, 다음부터는 `.twin` 만 폰으로 보내면 된다.

왜 이렇게 바꿨나 — 예전에는 사진과 메시를 base64 로 HTML 안에 전부 심었다. 사진
594장짜리 보고서에서 결과가 651MB(사진이 83%)가 나왔고, base64 는 +33% 인 데다
브라우저가 문서 전체를 파싱해야 해서 폰에서 열기 어려웠다. 지금은 뷰어가 2MB 남짓이고,
`.twin` 은 <input type="file"> 로 받아 필요한 바이트 범위만 읽는다(twin_reader.js 참조).
실측: 615MB 보고서를 여는 데 약 50ms, 사진은 보는 1~2장만 디스크에서 읽힌다.

ESM 모듈(three)은 단일 파일에서 <script src>로 불러올 수 없으므로,
소스를 text/plain 으로 심어 두고 런타임에 Blob URL 로 동적 import 한다.
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
LIBS = os.path.join(HERE, "libs")
UI = os.path.join(HERE, "ui")

VIEWER_NAME = "모바일뷰어.html"


def _esc(s):
    """인라인 <script> 안에서 닫힘 토큰이 파서를 끊지 않게 이스케이프(런타임에 복원)."""
    return s.replace("</script", "<\\/script")


def _fn_source(path):
    """three 를 import 하는 애드온 → THREE 를 인자로 받는 함수 본문으로 변환."""
    src = open(path, encoding="utf-8").read()
    src = re.sub(r"import\s*\{([^}]*)\}\s*from\s*['\"]three['\"];?", r"const {\1} = THREE;", src)
    src = re.sub(r"export\s*\{[^}]*\};?", "", src)
    return src


def _module_source(path):
    """그대로 ES 모듈로 import 할 소스(three 의존 없음)."""
    return open(path, encoding="utf-8").read()


def _plain_source(path):
    """export 만 떼어 인라인 가능한 본문으로(three 의존 없음)."""
    return re.sub(r"^export\s+", "", open(path, encoding="utf-8").read(), flags=re.MULTILINE)


def build_viewer(out_path=None, log=print):
    """모바일 뷰어 HTML 을 만들고 경로를 반환한다. 보고서 데이터는 담지 않는다."""
    out_path = out_path or os.path.join(HERE, "reports", VIEWER_NAME)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    tpl = open(os.path.join(UI, "mobile_template.html"), encoding="utf-8").read()
    html = (tpl
            .replace("__THREE_SRC__", _esc(_module_source(os.path.join(LIBS, "three.module.js"))))
            .replace("__OC_SRC__", _esc(_fn_source(os.path.join(LIBS, "addons", "controls", "OrbitControls.js"))))
            .replace("__PLY_SRC__", _esc(_fn_source(os.path.join(LIBS, "addons", "loaders", "PLYLoader.js"))))
            .replace("__BVH_SRC__", _esc(_plain_source(os.path.join(UI, "bvh.js"))))
            .replace("__LOD_SRC__", _esc(_plain_source(os.path.join(UI, "lod.js"))))
            .replace("__ORIGIN_SRC__", _esc(_plain_source(os.path.join(UI, "origin.js"))))
            .replace("__ZIP_SRC__", _esc(_module_source(os.path.join(UI, "twin_reader.js")))))
    left = re.findall(r"__(?:THREE_SRC|OC_SRC|PLY_SRC|BVH_SRC|LOD_SRC|ORIGIN_SRC|ZIP_SRC)__", html)
    if left:
        raise RuntimeError("치환되지 않은 자리표시자: " + ", ".join(sorted(set(left))))

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"모바일 뷰어 생성: {os.path.basename(out_path)} ({os.path.getsize(out_path) / 1e6:.1f} MB)")
    return out_path


# 예전 이름 — 호출부가 남아 있어도 깨지지 않게 둔다(보고서 인자는 무시된다).
def export_mobile(twin_path=None, out_path=None, log=print):
    return build_viewer(out_path, log)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="모바일 뷰어 HTML 생성(보고서와 무관한 고정 파일)")
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args()
    build_viewer(a.out)
