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


def main():
    """로컬 백엔드를 띄우고 대시보드 창을 연다."""
    global _main
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
    main()
