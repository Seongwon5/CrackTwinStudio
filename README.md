# 원전 디지털 트윈 스튜디오

> 이 브랜치(`viewer-simple`)는 `main` 과 영구적으로 갈라진 단순 뷰어 버전입니다.
> 검출·측정·검토·비교 기능은 없습니다. 그 기능이 필요하면 `main` 브랜치를 쓰세요.

포토그래메트리 산출물(사진 + 카메라 파라미터 XMP + 3D 메시 PLY)을 자체완결형 보고서
(`.twin`)로 묶고, 3D 로 돌려보며 **표면 한 점을 클릭하면 그 지점을 찍은 사진**을
보여주는 프로그램입니다.

```
사진 + 카메라 파라미터(XMP) + 3D 메시(PLY)
        │  (그대로 묶기만 — 별도 처리 없음)
        ▼
   보고서 (.twin)  ──▶  3D 뷰어: 회전·확대 + 클릭 → 원본 사진
```

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
2. **보고서 열기** — 뷰어 창: 드래그 회전 · 휠 확대 · 우드래그 이동 ·
   **표면 클릭 → 우측에 그 지점을 찍은 사진**

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

## 구성

```
main.py           진입점 (대시보드 + 멀티윈도우)
backend.py        로컬 서버 (생성 잡 · .twin 스트리밍: meta/mesh/img)
pipeline.py       보고서 생성 (XMP 파싱 · 방향 정렬 · 카메라 포즈 · 이미지 인코딩)
build.py          배포용 exe 빌드
assets/           앱 아이콘 (make_icon.py 로 생성)
ui/               대시보드(만들기/열기) + 뷰어
libs/             three.js r160 오프라인 번들 (MIT)
reports/          생성된 .twin (Git 미포함)
```

`.twin` 은 `main` 과 같은 v2 zip 컨테이너(`meta.json`/`mesh.ply`/`img/*`)라
**main 에서 만든 보고서도 이 뷰어로 열립니다**(추가 데이터는 무시됨).

## 전제

- PLY 는 삼각형 메시여야 하며, XMP 는 3DF Zephyr 계열 형식(`calibration`/`extrinsics`)을 따릅니다.
- 사진·XMP 와 PLY 는 같은 프로젝트에서 함께 내보낸 세트여야 클릭 지점이 일치합니다.
- 사용자 입력물(사진·XMP·PLY·.twin)은 사용자 소유이며 저장소에 포함되지 않습니다.
- 3D 엔진: [three.js](https://github.com/mrdoob/three.js) r160 (MIT).
