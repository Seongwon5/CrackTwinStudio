# -*- coding: utf-8 -*-
"""자산(검사 간 비교) 뷰어를 브라우저로 연다.

    py tools/open_asset.py <asset.json>

백엔드를 임시 포트로 띄우고 기본 브라우저에서 /viewer?asset=... 을 연다.
(compare_twins.py 로 만든 asset.json — 상태색·이전검사 고스트·Δ이력 확인용)
Ctrl+C 로 종료."""
import os
import sys
import threading
import urllib.parse
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backend as B


def main():
    if len(sys.argv) != 2 or not os.path.exists(sys.argv[1]):
        print(__doc__); sys.exit(1)
    asset = os.path.abspath(sys.argv[1])
    httpd = B.make_server(0)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/viewer?asset=" + urllib.parse.quote(asset)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("비교 뷰어:", url)
    print("(Ctrl+C 로 종료)")
    webbrowser.open(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
