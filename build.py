# -*- coding: utf-8 -*-
"""
build.py — 배포용 실행파일 빌드

  py build.py

결과: dist/원전 디지털 트윈 스튜디오.exe (단일 파일, 콘솔 창 없음)

exe 안에 ui·libs·아이콘을 함께 넣는다. 보고서(reports/)는 번들이 아니라
exe 가 놓인 폴더에 만들어지므로 재빌드해도 지워지지 않는다(backend.BASE 참고).

사전 준비:  py -m pip install -r requirements.txt pyinstaller
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "원전 디지털 트윈 스튜디오"
ICON = os.path.join(HERE, "assets", "icon.ico")


def main():
    if not os.path.exists(ICON):                    # 아이콘이 없으면 먼저 만든다
        subprocess.check_call([sys.executable, os.path.join(HERE, "assets", "make_icon.py")])

    sep = ";" if os.name == "nt" else ":"           # --add-data 구분자는 OS 마다 다르다
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", NAME,
        "--icon", ICON,
        "--add-data", f"{os.path.join(HERE, 'ui')}{sep}ui",
        "--add-data", f"{os.path.join(HERE, 'libs')}{sep}libs",
        "--add-data", f"{ICON}{sep}assets",
        "--exclude-module", "tkinter",              # 쓰지 않는 무거운 모듈은 제외
        "--exclude-module", "matplotlib",
        os.path.join(HERE, "main.py"),
    ]
    subprocess.check_call(args, cwd=HERE)

    exe = os.path.join(HERE, "dist", NAME + ".exe")
    print("\n완료:", exe, f"({os.path.getsize(exe) / 1024 / 1024:.0f} MB)")


if __name__ == "__main__":
    main()
