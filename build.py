# -*- coding: utf-8 -*-
"""
build.py — 배포용 실행파일 빌드 (main 브랜치 · AI 검출 포함)

  py build.py

결과: dist/크랙 디지털 트윈 스튜디오/  (폴더 배포 · 콘솔 창 없음)
      └ 크랙 디지털 트윈 스튜디오.exe  + _internal/ (파이썬·torch·모델·ui·libs·tools)

viewer-simple 브랜치는 단일 exe(--onefile)지만 여기는 폴더(--onedir)다. torch(CUDA 12.8)
때문에 번들이 수 GB 라 onefile 이면 실행할 때마다 %TEMP% 에 그만큼 풀어야 해서 첫 화면까지
분 단위로 걸린다. 폴더 배포는 그대로 실행되고, 통째로 복사하거나 zip 으로 보내면 된다.

번들에 들어가는 것
  ui/ libs/ assets/    대시보드·뷰어·three.js·아이콘
  model/<가중치>.pt    크랙 검출 모델 — WEIGHTS 하나만 넣는다(model/ 폴더째 넣으면 안 쓰는
                       가중치까지 따라 들어가 번들이 그만큼 커진다). pipeline.CRACK_WEIGHTS 와 같아야 한다.
  network/             가중치 언피클에 필요한 패키지 (pipeline 이 sys.path 에 번들 경로를 넣는다)
  tools/               compare_twins · export_report · twin_mesh (backend 가 sys.path 에 넣는다)
보고서(reports/)·추론 캐시(cache/)는 exe 가 놓인 폴더에 생긴다(backend.BASE · pipeline.BASE).

첫 빌드는 torch·scipy·matplotlib 의존 분석 때문에 30분 안팎이 걸린다. 이후에는
  py build.py --fast     (--clean 생략 → 분석 캐시 재사용, 몇 분)
  py build.py --dist D:\out   (다른 폴더에 빌드 — 실행 중인 앱 폴더를 덮어쓰지 않을 때)
빌드 뒤 `크랙 디지털 트윈 스튜디오.exe --selftest` 를 돌리면 exe 옆 selftest.txt 에 번들 자가진단이
남는다(torch CUDA · embree · 모델 등). 배포 전에 전부 ok 인지 확인할 것.
ui/·libs/·tools/·model/ 같은 자원만 고쳤다면 재빌드 없이 dist/…/_internal/ 아래 파일을
바꿔치기해도 된다(파이썬 파일은 번들에 컴파일돼 들어가므로 재빌드 필요).

사전 준비:  py -m pip install -r requirements.txt pyinstaller
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "크랙 디지털 트윈 스튜디오"
VERSION = (2, 0, 4, 0)          # 2.0.4: 타일 경계 균열 수정 · 검출 모델 conglomerate.pt · 심각도 5등급
WEIGHTS = "conglomerate.pt"     # 번들에 넣을 크랙 검출 가중치 — pipeline.CRACK_WEIGHTS 와 같아야 한다
COMPANY = "PRIMEPANO"
ICON = os.path.join(HERE, "assets", "icon.ico")

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

    sep = ";" if os.name == "nt" else ":"
    data = [("ui", "ui"), ("libs", "libs"), ("assets/icon.ico", "assets"),
            (os.path.join("model", WEIGHTS), "model"), ("network", "network"), ("tools", "tools")]
    fast = "--fast" in sys.argv                    # 분석 캐시 재사용(재빌드용)
    dist = os.path.join(HERE, "dist")
    if "--dist" in sys.argv:                       # 실행 중인 앱 폴더를 덮어쓰지 않고 다른 곳에 빌드
        dist = os.path.abspath(sys.argv[sys.argv.index("--dist") + 1])
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", *([] if fast else ["--clean"]), "--onedir", "--windowed", "--distpath", dist,
        "--name", NAME,
        "--icon", ICON,
        "--version-file", verfile,
        "--exclude-module", "tkinter",
        # 동적으로만 불리는 것들 — 분석기가 못 본다
        "--hidden-import", "network", "--hidden-import", "network.modeling",
        "--hidden-import", "network._deeplab", "--hidden-import", "network.utils",
        "--hidden-import", "network.backbone",
        "--hidden-import", "compare_twins", "--hidden-import", "export_report", "--hidden-import", "twin_mesh",
        "--hidden-import", "tiler",
        # embreex(embree 광선 교차): Cython 모듈 rtcore 는 컴파일된 rtcore_scene 안에서만 불려 분석기가
        # 못 보고, __init__ 이 importlib.metadata 로 버전을 읽어 dist-info 도 있어야 한다. 둘 중 하나라도
        # 빠지면 trimesh 가 조용히 numpy 교차(ray_triangle)로 물러나 큰 메시에서 메모리를 다 쓴다
        # (실측: 배포 앱에서 사진 5장 만에 메모리 고갈). collect-all + copy-metadata 로 전부 넣는다.
        "--collect-all", "embreex", "--copy-metadata", "embreex",
        "--collect-submodules", "network",
        "--collect-submodules", "skimage", "--collect-submodules", "scipy",
        "--collect-data", "trimesh", "--collect-data", "matplotlib",
        "--paths", HERE, "--paths", os.path.join(HERE, "tools"),
    ]
    for src, dst in data:
        args += ["--add-data", f"{os.path.join(HERE, src)}{sep}{dst}"]
    args.append(os.path.join(HERE, "app.py"))
    subprocess.check_call(args, cwd=HERE)

    out = os.path.join(dist, NAME)
    total = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(out) for f in fs)
    print("\n완료:", out, f"({total / 1024 / 1024:.0f} MB)")


if __name__ == "__main__":
    main()
