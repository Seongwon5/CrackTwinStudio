# 원전 디지털 트윈 스튜디오

> 이 브랜치(`viewer-simple`)는 `main` 과 영구적으로 갈라진 단순 뷰어 버전입니다.
> 검출·측정·검토·비교 기능은 없습니다. 그 기능이 필요하면 `main` 브랜치를 쓰세요.

포토그래메트리 산출물(사진 + 카메라 파라미터 XMP + 3D 메시 PLY)을 자체완결형 보고서
(`.twin`)로 묶고, 3D 로 돌려보며 **표면 한 점을 클릭하면 그 지점을 찍은 사진**을
보여주는 프로그램입니다.

```
사진 + 카메라 파라미터(XMP) + 3D 메시(PLY)
        │  (사진 축소 · 메시는 계층 LOD 타일로 분할)
        ▼
   보고서 (.twin)  ──▶  3D 뷰어: 보이는 타일만 스트리밍 + 클릭 → 원본 사진
```

메시 크기 제한이 없습니다. 6GB(정점 9,600만 · 면 1억 9천만) PLY 도 그대로 넣으면
생성 시 옥트리 타일(약 7,000개)로 쪼개지고, 뷰어는 카메라에 보이는 타일만 거리에 맞는
해상도로 받아 화면에는 늘 수백만 면만 올립니다. 설계·구현은 `docs/DEV_LOD.md` 참조.

## 실행

배포본은 단일 실행파일입니다.

```
원전 디지털 트윈 스튜디오.exe
```

소스에서 직접 실행하려면:

```bash
py -m pip install -r requirements.txt
```

```bash
py main.py
```

1. **보고서 만들기** — 이미지+XMP 폴더와 PLY 선택 → 생성
2. **보고서 열기** — 뷰어 창: **WASD 이동 · 좌드래그 시점 회전**(Space/Ctrl 위아래 ·
   Shift 가속 · 휠 전진후진 · 우클릭 홀드 확대) · **표면 클릭 → 우측에 그 지점을 찍은 사진**
3. **모바일 내보내기** — 보고서 목록의 `모바일` 버튼 → 뷰어 HTML(고정 파일) 생성.
   뷰어와 `.twin` 을 폰으로 보내(메신저·메일·USB) 브라우저로 열면 서버·설치 없이 동작합니다
   (터치 회전·핀치 줌 · 탭 → 사진). 타일 보고서는 폰에서도 보이는 타일만 파일에서
   조각조각 읽으므로 수 GB 보고서를 폰 메모리에 올리지 않습니다.

큰 메시(6GB) 기준 생성 시간은 타일 약 15분 + 사진(2,000장 약 15~20분)입니다.
생성 PC 메모리는 16GB 이상을 권장합니다(타일 생성 중 최대 약 9GB 사용).

사진 선택 규칙: 클릭한 3D 점이 ① 그 사진의 화면 안에 들어오고(왜곡 포함 투영)
② 카메라가 표면 앞쪽에 있는 것 중 ③ 정면·근접( cosθ/거리 ) 순으로 고릅니다.
조건을 만족하는 사진이 없으면 단계적으로 완화합니다.

## 빌드

```bash
py -m pip install pyinstaller
```

```bash
py build.py
```

`dist/원전 디지털 트윈 스튜디오.exe` 가 만들어집니다. `ui`·`libs`·아이콘은 exe 안에
들어가고, `reports/` 는 exe 가 놓인 폴더에 생성됩니다.

## 배포 — 받는 PC 에 필요한 것

exe 하나만 복사하면 됩니다. **Python 도, 어떤 패키지도 설치할 필요가 없습니다.**
파이썬 런타임·VC 런타임·NumPy·OpenCV·three.js 가 모두 exe 안에 들어 있고, 실행할 때만
`%TEMP%` 에 잠시 풀렸다가 종료 시 지워집니다.

유일한 외부 요구사항은 **Microsoft Edge WebView2 런타임**(화면을 그리는 엔진)입니다.
Windows 11 과 최근 Windows 10 에는 기본으로 들어 있습니다. 없는 PC 에서는 실행 시
안내창이 뜨며, [WebView2 페이지](https://developer.microsoft.com/microsoft-edge/webview2/)에서
*Evergreen Standalone Installer (x64)* 를 설치하면 됩니다.

- 관리자 권한 불필요 · 64비트 Windows 10 이상
- 서명하지 않은 exe 라 처음 실행 시 SmartScreen 이 뜰 수 있습니다
  (`추가 정보 ▸ 실행`). 경고창에는 exe 에 새긴 앱 이름이 표시됩니다.
- 백신이 `%TEMP%` 실행을 막는 환경이면 exe 를 예외로 등록해야 할 수 있습니다.

## 구성

```
main.py           진입점 (대시보드 + 멀티윈도우)
backend.py        로컬 서버 (생성 잡 · .twin 스트리밍: meta/tile/mesh/img)
pipeline.py       보고서 생성 (XMP 파싱 · 방향 정렬 · 카메라 포즈 · 이미지 인코딩)
tiler.py          PLY → 계층 LOD 타일(옥트리) 분할 (numpy 만 사용)
export_mobile.py  모바일 뷰어 HTML 생성
build.py          배포용 exe 빌드
assets/           앱 아이콘 (make_icon.py 로 생성)
ui/               대시보드(만들기/열기) + 뷰어 (tiles.js = 타일 스트리밍 런타임, 데스크톱·모바일 공용)
libs/             three.js r160 오프라인 번들 (MIT)
docs/             개발 문서 (DEV_LOD.md — 계층 LOD 설계·처리 순서·수정 지점)
reports/          생성된 .twin (Git 미포함)
```

`.twin` 은 v2 zip 컨테이너입니다: `meta.json` / `tiles/<id>.bin` / `img/*`.
타일이 없는 옛 보고서(`mesh.ply`)와 **main 에서 만든 보고서도 그대로 열립니다** — 뷰어가
통째로 받아 간략 메시 + 원본 두 단계 타일셋으로 감쌉니다(메시 크기가 브라우저 한계 안일 때).

## 전제

- PLY 는 삼각형 메시여야 하며, XMP 는 3DF Zephyr 계열 형식(`calibration`/`extrinsics`)을 따릅니다.
- 사진·XMP 와 PLY 는 같은 프로젝트에서 함께 내보낸 세트여야 클릭 지점이 일치합니다.
- 사용자 입력물(사진·XMP·PLY·.twin)은 사용자 소유이며 저장소에 포함되지 않습니다.
- 3D 엔진: [three.js](https://github.com/mrdoob/three.js) r160 (MIT).
