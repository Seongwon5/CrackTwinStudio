# -*- coding: utf-8 -*-
"""
app.py  ―  크랙 디지털 트윈 스튜디오 (실행 진입점)
====================================================
실행하면 네이티브 창(대시보드)이 열린다.
  ① 보고서 만들기 : 이미지+XMP 폴더 / PLY 선택 → 크랙 분석 → .twin 저장
  ② 보고서 열기   : .twin 을 각각 새 창으로 (여러 개 동시에)

실행:  py app.py
"""
import threading
import urllib.parse
import webview

import backend as B

_main = None            # 대시보드 창
_quitting = False       # 종료 확인을 이미 받았는지(중복 대화상자 방지)


class Api:
    """JS(window.pywebview.api)에서 호출: 보고서를 새 창으로 연다(멀티윈도우)."""
    def __init__(self, port):
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


def main():
    """로컬 백엔드를 띄우고 대시보드 창을 연다."""
    global _main
    httpd = B.make_server(0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _main = webview.create_window("크랙 디지털 트윈 스튜디오", f"http://127.0.0.1:{port}/",
                                  js_api=Api(port), width=1080, height=720, min_size=(820, 560))
    _main.events.closing += _on_closing
    webview.start()
    httpd.shutdown()


if __name__ == "__main__":
    main()
