# -*- coding: utf-8 -*-
"""
app.py  ―  크랙 디지털 트윈 스튜디오 (실행 진입점)
====================================================
실행하면 네이티브 창(대시보드)이 열린다.
  ① 보고서 만들기 : 이미지+XMP 폴더 / PLY 선택 → 크랙 분석 → .twin 저장
  ② 보고서 열기   : .twin 을 각각 새 창으로 (여러 개 동시에)

실행:  py app.py          (개발)
       크랙 디지털 트윈 스튜디오.exe   (배포 — build.py 로 생성, 폴더 배포)
"""
import os
import sys
import threading
import urllib.parse
import webview

import backend as B

APP_TITLE = "크랙 디지털 트윈 스튜디오"
_main = None            # 대시보드 창
_quitting = False       # 종료 확인을 이미 받았는지(중복 대화상자 방지)


class Api:
    """JS(window.pywebview.api)에서 호출: 보고서를 새 창으로 연다(멀티윈도우)."""
    def __init__(self, port):
        """백엔드가 실제로 물린 포트를 기억한다(0 으로 요청해 OS 가 정해준 값)."""
        self.port = port

    def open_report(self, path):
        """.twin 경로를 받아 새 뷰어 창을 띄운다."""
        import os
        if not path or not os.path.exists(path):
            return {"error": "파일 없음"}
        url = f"http://127.0.0.1:{self.port}/viewer?report=" + urllib.parse.quote(path)
        webview.create_window("뷰어 · " + os.path.basename(path), url,
                              width=1360, height=860, min_size=(900, 600))
        return {"ok": True}

    def open_asset(self, path):
        """asset.json(검사 간 비교) 경로를 받아 비교 뷰어 창을 띄운다."""
        import os
        if not path or not os.path.exists(path):
            return {"error": "파일 없음"}
        url = f"http://127.0.0.1:{self.port}/viewer?asset=" + urllib.parse.quote(path)
        webview.create_window("검사 비교 · " + os.path.basename(path), url,
                              width=1360, height=860, min_size=(900, 600))
        return {"ok": True}

    def quit_app(self):
        """대시보드의 빨간 종료 버튼. 확인 대화상자는 띄우지 않는다 —
        destroy() 가 창의 closing 이벤트를 발생시키므로 _on_closing 이 한 번만 물어본다."""
        if _main:
            _main.destroy()
        return {"ok": True}


def _on_closing():
    """대시보드가 닫히려 할 때(X 버튼·Alt+F4·종료 버튼 모두 여기로 모인다).
    False 를 돌려주면 pywebview 가 종료를 취소한다
    (winforms: should_cancel = closing.set() → args.Cancel = True).
    주의: create_window(confirm_close=True) 를 같이 쓰면 내장 영문 대화상자가 겹쳐 뜬다."""
    global _quitting
    if _quitting:
        return True
    if not _main.create_confirmation_dialog("종료 확인", "크랙 디지털 트윈 스튜디오를 종료하시겠습니까?"):
        return False
    _quitting = True
    for w in list(webview.windows):          # 열려 있는 뷰어 창들도 함께 정리
        if w is not _main:
            try:
                w.destroy()
            except Exception:
                pass
    return True


def _icon_path():
    """창 아이콘 경로 — exe 로 묶였으면 번들 안, 소스 실행이면 assets/."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, "assets", "icon.ico")
    return p if os.path.exists(p) else None


def _msgbox(text, title=APP_TITLE, style=0x10):
    """콘솔이 없는 exe 에서도 보이는 안내창(기본: 오류 아이콘)."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, style)
    except Exception:
        print(text)


def _has_webview2():
    """Edge WebView2 런타임 설치 여부. 없으면 창이 비거나 3D 가 전혀 그려지지 않아 원인을 찾기
    어렵다. EdgeUpdate 가 기록하는 버전 값(pv)으로 판별한다."""
    import winreg
    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    targets = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients" "\\" + guid),
               (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients" "\\" + guid),
               (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients" "\\" + guid)]
    for root, path in targets:
        try:
            with winreg.OpenKey(root, path) as k:
                pv = winreg.QueryValueEx(k, "pv")[0]
            if pv and pv != "0.0.0.0":
                return True
        except OSError:
            continue
    return False


def main():
    """로컬 백엔드를 띄우고 대시보드 창을 연다."""
    global _main
    if os.name == "nt" and not _has_webview2():
        _msgbox("이 프로그램은 Microsoft Edge WebView2 런타임이 필요합니다.\n\n"
                "아래 주소에서 'Evergreen Standalone Installer'(x64)를 내려받아\n"
                "설치한 뒤 다시 실행해 주세요.\n\n"
                "https://developer.microsoft.com/microsoft-edge/webview2/")
        return
    httpd = B.make_server(0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _main = webview.create_window(APP_TITLE, f"http://127.0.0.1:{port}/",
                                  js_api=Api(port), width=1080, height=720, min_size=(820, 560))
    _main.events.closing += _on_closing
    icon = _icon_path()
    webview.start(icon=icon) if icon else webview.start()
    httpd.shutdown()


def selftest():
    """--selftest: 배포 번들 자가진단. 창 없이 exe 옆 selftest.txt 에 결과를 쓰고 끝난다.

    빌드가 바뀔 때마다 17분짜리 실제 분석을 돌리지 않고도 '번들에 빠진 것'을 수 초 만에 잡기 위한 것.
    확인 항목: torch CUDA · trimesh 의 embree 인터페이스(없으면 numpy 교차로 물러나 큰 메시에서
    메모리 고갈) · embreex/cv2/skimage/scipy/PIL 임포트 · 모델 파일.
    """
    import traceback
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(base, "selftest.txt")
    lines = [f"python {sys.version.split()[0]} · frozen={getattr(sys, 'frozen', False)}"]
    def check(name, fn):
        try:
            lines.append(f"ok   {name}: {fn()}")
        except Exception as e:
            lines.append(f"FAIL {name}: {type(e).__name__}: {str(e)[:300]}")
    check("torch", lambda: __import__("torch").__version__ + " cuda=" + str(__import__("torch").cuda.is_available()))
    check("embreex", lambda: __import__("embreex").__version__)
    check("embreex.rtcore_scene", lambda: __import__("embreex.rtcore_scene").__name__)
    def _embree():
        import trimesh.ray as R
        if not R.has_embree:
            R.ray_pyembree.RayMeshIntersector          # ExceptionWrapper — 원인 예외를 다시 던진다
        return "has_embree=" + str(R.has_embree)
    check("trimesh.ray embree", _embree)
    check("cv2", lambda: __import__("cv2").__version__)
    check("skimage.morphology", lambda: __import__("skimage.morphology").__name__)
    check("scipy.ndimage", lambda: __import__("scipy.ndimage").__name__)
    check("PIL", lambda: __import__("PIL.Image").__name__)
    def _weights():                                # 실제로 쓰는 가중치 파일을 pipeline 에서 읽는다
        import pipeline
        p = pipeline.CRACK_WEIGHTS
        return f"{os.path.basename(p)} {os.path.getsize(p) / 1e6:.0f}MB"
    check("크랙 가중치", _weights)
    check("tiler", lambda: __import__("tiler").__name__)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0 if all(l.startswith("ok") for l in lines[1:]) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    # 콘솔 없는 exe 는 예외가 나도 아무 것도 보이지 않는다 → 항상 창으로 알린다.
    try:
        main()
    except Exception:
        import traceback
        _msgbox("실행 중 오류가 발생했습니다.\n\n" + traceback.format_exc()[-1500:])
        raise
