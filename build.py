# -*- coding: utf-8 -*-
"""
build.py — 배포용 실행파일 빌드

  py build.py

결과: dist/원전 디지털 트윈 스튜디오.exe (단일 파일, 콘솔 창 없음)

Python 이 설치되지 않은 PC 에서도 그대로 실행된다. 파이썬 런타임(python3xx.dll)과
VC 런타임, ui·libs·아이콘을 모두 exe 안에 넣기 때문이다. 실행 시 %TEMP% 에 잠시
풀렸다가 종료할 때 지워진다. 보고서(reports/)만 exe 가 놓인 폴더에 남는다.

유일한 외부 요구사항은 Edge WebView2 런타임으로, Windows 11 과 최근 Windows 10 에는
기본 탑재되어 있다. 없으면 main.py 가 안내창을 띄운다.

사전 준비:  py -m pip install -r requirements.txt pyinstaller
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "원전 디지털 트윈 스튜디오"
VERSION = (1, 0, 9, 0)
COMPANY = "PRIMEPANO"
ICON = os.path.join(HERE, "assets", "icon.ico")

# exe 속성창·SmartScreen 경고에 표시되는 정보. 없으면 '알 수 없는 게시자'만 뜬다.
_VERSION_FILE = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041204B0', [
        StringStruct('CompanyName', '{company}'),
        StringStruct('FileDescription', '{name}'),
        StringStruct('FileVersion', '{vs}'),
        StringStruct('InternalName', '{name}'),
        StringStruct('OriginalFilename', '{name}.exe'),
        StringStruct('ProductName', '{name}'),
        StringStruct('ProductVersion', '{vs}')])]),
    VarFileInfo([VarStruct('Translation', [0x0412, 1200])])
  ]
)
"""


def main():
    if not os.path.exists(ICON):                    # 아이콘이 없으면 먼저 만든다
        subprocess.check_call([sys.executable, os.path.join(HERE, "assets", "make_icon.py")])

    verfile = os.path.join(HERE, "build", "version_info.txt")
    os.makedirs(os.path.dirname(verfile), exist_ok=True)
    with open(verfile, "w", encoding="utf-8") as f:
        f.write(_VERSION_FILE.format(v=VERSION, vs=".".join(map(str, VERSION)),
                                     name=NAME, company=COMPANY))

    sep = ";" if os.name == "nt" else ":"           # --add-data 구분자는 OS 마다 다르다
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", NAME,
        "--icon", ICON,
        "--version-file", verfile,
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
