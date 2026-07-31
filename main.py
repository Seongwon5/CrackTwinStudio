# -*- coding: utf-8 -*-
"""
main.py — 원전 디지털 트윈 스튜디오 진입점

실행하면 네이티브 창(대시보드)이 열린다.
  ① 보고서 만들기 : 이미지+XMP 폴더 / PLY 선택 → .twin 저장
  ② 보고서 열기   : .twin 을 각각 새 창으로 (여러 개 동시에)

실행:  py main.py          (개발)
       원전 디지털 트윈 스튜디오.exe   (배포 — build.py 로 생성)
"""
import os
import sys
import threading
import urllib.parse
import webview

import backend as B

APP_TITLE = "원전 디지털 트윈 스튜디오"

_main = None            # 대시보드 창
_quitting = False       # 종료 확인을 이미 받았는지(중복 대화상자 방지)


class Api:
    """JS(window.pywebview.api)에서 호출하는 창 제어 API."""

    def __init__(self, port):
        self.port = port                      # 백엔드가 실제로 물린 포트

    def open_report(self, path):
        """.twin 경로를 받아 새 뷰어 창을 띄운다."""
        if not path or not os.path.exists(path):
            return {"error": "파일 없음"}
        url = f"http://127.0.0.1:{self.port}/viewer?report=" + urllib.parse.quote(path)
        webview.create_window("뷰어 · " + os.path.basename(path), url,
                              width=1360, height=860, min_size=(900, 600))
        return {"ok": True}

    def quit_app(self):
        """종료 버튼. destroy()가 closing 이벤트를 발생시키므로 확인은 _on_closing이 한 번만 한다."""
        if _main:
            _main.destroy()
        return {"ok": True}


def _on_closing():
    """대시보드 종료 시도(X·Alt+F4·종료 버튼)를 한곳에서 확인한다.

    False를 반환하면 pywebview가 종료를 취소한다.
    create_window(confirm_close=True)를 같이 쓰면 내장 대화상자가 겹쳐 뜨므로 쓰지 않는다.
    """
    global _quitting
    if _quitting:
        return True
    if not _main.create_confirmation_dialog("종료 확인", f"{APP_TITLE}를 종료하시겠습니까?"):
        return False
    _quitting = True
    for w in list(webview.windows):           # 열려 있는 뷰어 창도 함께 정리
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
    """Edge WebView2 런타임 설치 여부.

    Windows 11 과 최근 Windows 10 에는 기본 탑재지만 LTSC·Server·정리된 이미지에는
    없을 수 있다. 없으면 창이 비거나 3D 가 전혀 그려지지 않아 원인을 찾기 어렵다.
    EdgeUpdate 가 기록하는 버전 값(pv)으로 판별한다.
    """
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
    httpd = B.make_server(0)                  # 포트 0 → OS가 빈 포트를 준다
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _main = webview.create_window(APP_TITLE, f"http://127.0.0.1:{port}/",
                                  js_api=Api(port), width=1080, height=720, min_size=(820, 560))
    _main.events.closing += _on_closing
    icon = _icon_path()
    webview.start(icon=icon) if icon else webview.start()
    httpd.shutdown()


if __name__ == "__main__":
    # 콘솔 없는 exe 는 예외가 나도 아무 것도 보이지 않는다 → 항상 창으로 알린다.
    try:
        main()
    except Exception:
        import traceback
        _msgbox("실행 중 오류가 발생했습니다.\n\n" + traceback.format_exc()[-1500:])
        raise
