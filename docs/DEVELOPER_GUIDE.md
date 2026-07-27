# 개발자 문서 (Developer Guide)

CrackTwinStudio를 **이어받아 개발·유지보수**하기 위한 문서다. 사용법은 [사용자 매뉴얼](USER_GUIDE.md)에 있다.

이 문서는 세 가지를 답하는 것을 목표로 한다:

| | 질문 | 어디에 |
|---|---|---|
| **왜** | 왜 이 방식으로 만들었고, 어떤 대안을 버렸나 | 각 절의 "왜 이렇게 했나" + [§10 설계 결정 기록](#10-설계-결정-기록) |
| **순서** | 데이터가 어떤 순서로 처리되고 각 단계 입출력은 무엇인가 | [§3 전체 경로](#3-요청-한-번이-도는-전체-경로) + [§5 파이프라인](#5-파이프라인-6단계-상세) |
| **어디** | 무엇을 고치려면 어느 파일·함수를 봐야 하나 | 각 절의 "수정 지점" + [§11 수정 지점 지도](#11-수정-지점-지도) |

> **전제:** 코드 주석이 1차 자료다. 특히 `pipeline.py` 상단 설정값과 `backend.py` 모듈 독스트링에는
> 실측 수치와 함께 판단 근거가 남아 있다. 이 문서는 그 조각들을 잇는 지도 역할을 한다.

---

## 1. 무엇을 하는 프로그램인가

드론·카메라로 찍은 콘크리트 구조물 사진에서 AI로 균열을 픽셀 단위 검출하고,
포토그래메트리로 복원한 3D 표면에 역투영해 **실척(mm) 폭·길이**를 산출한다.
결과는 자체완결형 보고서 `.twin` 한 파일로 떨어지고, 웹 3D 뷰어로 점검한다.

핵심 아이디어는 **"2D에서 재고, 3D로 옮긴다"** 가 아니라 **"3D 표면 위에서 잰다"** 는 것이다.
사진 한 장의 픽셀 크기는 촬영 거리·경사에 따라 제각각이라, 픽셀 폭에 상수를 곱하는 방식으로는
실척이 나오지 않는다. 그래서 픽셀마다 3D 표면에 광선을 쏴 **그 픽셀이 실제로 덮는 mm**를 구한다
(→ [§5-4 스케일 맵](#5-4-실척-스케일-맵)).

---

## 2. 큰 그림 — 프로세스와 모듈

### 2-1. 실행 구조

한 프로세스 안에 **네이티브 창(pywebview) + 로컬 HTTP 서버(스레드)** 가 함께 뜬다.

```
py app.py
  │
  ├─ B.make_server(0)          임의 포트로 로컬 HTTP 서버 (데몬 스레드)
  │    └─ ThreadingHTTPServer  요청마다 스레드
  │
  └─ webview.create_window()   네이티브 창 → http://127.0.0.1:<port>/
       │
       ├─ 대시보드 (ui/dashboard.html)      보고서 만들기/열기/비교/PDF/재학습
       └─ 뷰어 창 (ui/viewer.html) × N개    보고서마다 새 창 (멀티윈도우)
```

- **포트는 0으로 요청**해 OS가 빈 포트를 준다 (`app.py:77`). 고정 포트 충돌을 피한다.
- 뷰어 창은 JS에서 `window.pywebview.api.open_report(path)` 로 요청하면
  파이썬 쪽 `Api.open_report` 가 `webview.create_window` 를 호출해 띄운다 (`app.py:26`).
- 종료 확인 대화상자는 `_on_closing` 한 곳으로 모인다 (X 버튼·Alt+F4·종료 버튼 전부).
  `create_window(confirm_close=True)` 를 같이 쓰면 내장 영문 대화상자가 겹쳐 뜨므로 쓰지 않는다.

### 2-2. 모듈 지도

| 파일 | 역할 | 크기 감각 |
|---|---|---|
| [`app.py`](../app.py) | 진입점. 창 생성·멀티윈도우·종료 처리 | 89줄 |
| [`backend.py`](../backend.py) | 로컬 HTTP 서버. 라우팅 · `.twin` 스트리밍 · 백그라운드 잡 | 약 600줄 |
| [`pipeline.py`](../pipeline.py) | **분석 파이프라인 전체**. 기하·추론·정량화·융합·저장 | 약 1,370줄 |
| [`cracktype.py`](../cracktype.py) | 균열 유형 분류 (특징 추출 + 랜덤포레스트 평가) | 약 250줄 |
| `network/` | `best.pt` 언피클에 필요한 DeepLabV3+ 구현 (외부 코드) | — |
| `ui/viewer.js` | **뷰어 본체**. 3D 렌더링·상호작용·측정·검토 | 약 3,000줄 |
| `ui/dashboard.js` | 대시보드 로직 (잡 폴링·목록·비교 선택) | 약 380줄 |
| `libs/` | three.js r160 오프라인 번들 | — |
| `tools/compare_twins.py` | 검사 간 비교 (정합·매칭·성장 판정) | backend가 import |
| `tools/export_report.py` | PDF 보고서 생성 | backend가 import |
| `tools/retrain_type.py` | 유형 분류기 재학습 | backend가 import |

> `tools/` 3개는 **이름과 달리 보조 스크립트가 아니라 기능 모듈**이다. `backend.py`가 직접 import하며
> 대시보드 버튼이 호출한다. CLI로도 단독 실행 가능한 이중 구조다.

### 2-3. 의존 방향

```
app.py ──▶ backend.py ──▶ pipeline.py ──▶ cracktype.py
                    │                 └─▶ network/ (best.pt 언피클)
                    ├──▶ tools/compare_twins.py
                    ├──▶ tools/export_report.py
                    └──▶ tools/retrain_type.py ──▶ cracktype.py
```

역방향 의존은 없다. `pipeline.py`는 HTTP를 모르고, `backend.py`는 3D 렌더링을 모른다.
`cracktype.py`는 **학습(offline)과 추론(pipeline)이 같은 특징 추출 함수를 공유**하도록 분리돼 있다
(분포 불일치 방지 — `cracktype.py` 독스트링 참조).

---

## 3. 요청 한 번이 도는 전체 경로

### 3-1. 보고서 만들기

```
[대시보드]  폴더/PLY 선택 → "분석 시작"
   │  POST /api/create  {folder, ply, out, detect}
   ▼
[backend]  do_POST → job_id 발급 → threading.Thread(_run_job) → 즉시 202 응답
   │                                    │
   │  GET /api/job?id=…  (0.7초 폴링)   ▼
   │  ◀── {progress, log[-12:], done}  [_run_job]
   │                                    │  P.build_report(folder, ply, out, log, prog)
   │                                    ▼
   │                              [pipeline] build_scene_data → save_report
   ▼                                    │
[대시보드]  done → 보고서 목록 갱신 ◀────┘  reports/<이름>.twin
```

- **왜 잡 구조인가:** 분석은 수 분~수십 분이다. HTTP 요청 하나로 붙들면 타임아웃되고 진행률도 못 준다.
  `_jobs` 딕셔너리에 상태를 두고 폴링한다 (`backend.py:33`).
- 로그는 마지막 200줄만 유지(`j["log"][:] = j["log"][-200:]`), 폴링 응답엔 12줄만 실어 보낸다.

### 3-2. 보고서 열기

```
[대시보드]  "열기" → window.pywebview.api.open_report(path)
   ▼
[app.py]   webview.create_window("뷰어 · …", "/viewer?report=<path>")
   ▼
[backend]  GET /viewer → render_viewer_html(path)
             viewer.html 을 읽어 __REPORT__ 자리에 '경로 문자열만' 주입 → 수 KB 응답
   ▼
[뷰어 JS]  GET /api/report/meta?p=…   → meta.json      (JSON.parse, 수 MB)
           GET /api/report/mesh?p=…   → mesh.ply       (ArrayBuffer, 스트리밍)
           GET /api/report/img?p=…&id=N&kind=thumb|full|mask  (지연 로드)
```

**여기가 이 프로젝트에서 가장 중요한 설계 결정이다.** 예전에는 `.twin` 전체를
`viewer.html` 의 `const DATA = __DATA__;` 자리에 통째로 끼워 넣었고, 파일이 커지면 반드시 터졌다:

| 벽 | 하드 한계 | 실제 |
|---|---|---|
| V8 최대 문자열 | 536,870,888자 (536.9MB) | 2.3GB 보고서 = 4.29배 초과 |
| `window.atob` | 문자열 한계와 동일 | base64 1.6GB |
| 파이썬 피크 메모리 | — | read→json.loads→replace→encode ≈ 파일의 5배 |

지금은 **큰 덩어리가 문자열/JSON을 절대 거치지 않는다.** 자세한 근거와 실측치는
[`DESIGN_twin_v2.md`](DESIGN_twin_v2.md)에 있다 (이 문서보다 먼저 쓰인 설계 문서).

---

## 4. 좌표 규약과 실척 전제

**이 두 가지가 틀리면 모든 mm 값이 틀린다.** 기하 코드를 만질 때 먼저 확인할 것.

### 4-1. 카메라 좌표 규약

```
X_cam = R · (X_world − C)          C = −Rᵀ · T      (C = 카메라 중심)
f  =  Rᵀ · e_z      (전방)
up = −Rᵀ · e_y      (위)
right = Rᵀ · e_x    (오른쪽)
```

구현: `pipeline.camera_pose()`. XMP에서 읽은 `R`, `T`는 world→cam 변환이고, 뷰어가 쓰는 것은
카메라의 world 상 위치·자세라 위처럼 뒤집는다.

### 4-2. 역투영 (2D 픽셀 → 3D 표면)

```
undistort(K, dist)  →  카메라 광선  →  Rᵀ 로 world 회전  →  ray–mesh 교차
```

구현: `pipeline.backproject()`. **왜곡 보정이 빠지면 안 된다** — 실측으로 이미지 가장자리에서
33px 어긋났고, 왜곡 적용 후 0.2px가 됐다 (커밋 `하이라이트 투영 어긋남 수정`).

역방향(3D → 이미지 픽셀)은 **왜곡을 다시 입혀야** 원본 사진 위에 정확히 얹힌다.
구현이 두 곳에 있고 규약이 같아야 한다:
- 파이썬: `cracktype.project_to_cam()`
- JS: `ui/viewer.js` 의 `projCamRaw`

### 4-3. 실척 전제 — `1 unit = 1 m`

PLY 메시 좌표가 **미터 단위로 정규화돼 있어야** 한다. 이 전제가 깨지면:

- 폭·길이 mm가 전부 틀린다
- 병합 거리(`DBSCAN_EPS_M`), 복셀 크기(`FUSE_VOXEL_M`)가 물리적으로 무의미해진다

**병합 반경을 메시 대각선 비율로 잡으면 안 된다.** 균열은 mm 단위의 절대 크기 특징이라
모델이 커진다고 같이 커지지 않는다. 비율 방식이던 시절 실측:

```
망치 메시 50m  → 병합 503mm  / 복셀 101mm     (그럭저럭 동작)
댐   메시 622m → 병합 6,225mm / 복셀 1,245mm  (6.2m 안의 크랙이 전부 1개로 뭉침)
                 → 594장에서 핫스팟 51개(장당 0.09)
```

그래서 지금은 **절대값(m)** 이다 (`pipeline.py:30-41` 주석).

> **알려진 최우선 과제:** 실측 치수로 검증하기 전까지 mm **절대값**은 신뢰 불가다.
> `report_meta.scale_warning`(최대 폭 > 50mm)이 이를 표시한다. 상대 순위·형상은 이미 유효하다.

---

## 5. 파이프라인 6단계 상세

진입점은 `pipeline.build_report()` → `build_scene_data()` → `save_report()`.
`build_scene_data()`가 사실상 전부이며, 아래 단계는 그 함수 안에서 순서대로 일어난다.

### 5-1. 입력 수집 — 이미지 + XMP 짝짓기

**입력** 폴더 경로 · **출력** `[(img_path, xmp_path)]`

`find_pairs()` 가 두 가지 명명 규칙을 모두 받는다:
- `photo.jpg.xmp` (Zephyr 기본)
- `photo.xmp`

`parse_xmp()` 가 XMP에서 `K`(내부파라미터) · `dist`(k1,k2,p1,p2,k3) · `R` · `T` · 보정해상도를 읽는다.

**왜 이렇게 했나 — 조용한 실패 방지.** 예전엔 XMP 없는 이미지를 말없이 버려서,
사용자가 1,288장을 넣고도 594장만 분석된 걸 몰랐다. 지금은 제외 목록을 로그와
`report_meta.skipped_list` 에 남긴다.

**수정 지점** 새 포토그래메트리 SW의 XMP를 지원하려면 `parse_xmp()`.

### 5-2. AI 검출 — 타일 추론 세그멘테이션

**입력** 원본 사진 · **출력** 균열 확률맵 `prob` (float32, 원본 해상도) → 이진 마스크

`CrackSegmenter` (`pipeline.py:385`). DeepLabV3+ (ResNet-101, output_stride=8), 출력 4채널 중 **채널 1 = 크랙**.

```
이미지 → 1024px 타일 분할(overlap 128) → 배치 추론(FP16) → Hann 창 가중 합성 → 확률맵
```

설계 포인트:

| 항목 | 값 | 왜 |
|---|---|---|
| 타일 크기 | 1024 | 원본(5472×3648)을 통째로 넣으면 VRAM 초과 |
| overlap | 128 | 타일 경계 이음매 제거 |
| 창 함수 | `_hann2d(floor=0.12)` | 경계 가중치를 0으로 만들면 나눗셈이 불안정 → 하한 0.12 |
| 배치 | `CRACK_BATCH=4` | 처리량은 배치에 거의 무관(GPU가 타일 하나로 이미 포화). VRAM 여유를 남기는 값 |
| FP16 | GPU만 | CPU는 FP16이 에뮬레이션이라 오히려 느림. softmax는 `.float()` 로 계산해 수치 안정성 유지 |
| `channels_last` | 적용 | FP16 텐서코어 +8% |
| `cudnn.benchmark` | True | 타일 크기가 항상 같아 오토튠이 재사용됨 |

**확률맵 캐시** (`_prob_cache_*`): 이미지·모델·타일 설정이 같으면 GPU를 건너뛴다.
키는 `_prob_cache_key(img_path, dw, dh, tile, overlap)`. 재분석 반복 시 크게 빨라진다.

**수정 지점** 다른 세그멘테이션 모델로 바꾸려면 `CrackSegmenter.__init__`의 `crack_index`와
`get_segmenter()`의 로드 방식. 임계값은 `CRACK_THR`.

### 5-3. 정량화 — 중심선 + 직교 폭

**입력** 마스크 + 확률맵 + 스케일맵 · **출력** `CrackMetrics.instances[]`
(각각 `centerline`, `width_profile`, `edge_l/edge_r`, `max_width`, `length`, `length_path`)

`quantify_crack()` (`pipeline.py:694`). 순서:

```
① 컴포넌트 분리        연결요소 + bridge_px(5px) 클로징으로 끊긴 크랙 잇기
② 노이즈 걸러내기      min_object_px 미만이어도 길쭉하면(THIN_ELONG=3.0) 살림
③ 골격화              skeletonize → _prune_spurs(짧은 가지 제거)
④ 중심선 순서화        _order_skeleton (끝점에서 출발해 경로 추적)
⑤ 폭 측정             _perp_width_profile — 중심선 직교 방향 서브픽셀 프로파일
⑥ 길이                _skeleton_lengths — 스케일맵 가중, 최장경로 & 총연장 2지표
```

**폭 측정이 왜 이렇게 복잡한가.** 단순히 거리변환(EDT) 최대값×2를 쓰면 골격이 중심에서
살짝 벗어나거나 분기점에서 폭이 과대평가된다. 지금 방식은:

- **확률맵의 `thr` 레벨셋**에서 잰다 — 이진 마스크의 계단(디지털화) 편향을 피한다
- **양방향 실측** — 중심선에서 좌·우로 각각 첫 하향교차까지. 한쪽×2 대칭 가정을 쓰지 않는다
- **서브픽셀** — `WIDTH_STEP_PX=0.5` 간격 보간
- **`WIDTH_CAP_PX=60`** — 편측 최대 탐색 반경. 옆 크랙으로 새는 것 방지

**길이 지표가 왜 둘인가.** 가지가 있는 크랙에서 "길이"의 의미가 갈린다:
- `length_mm` = **최장경로**(그래프 지름) — 대표 길이
- `length_total_mm` = **총연장**(가지 포함)

뷰어와 PDF는 최장경로를 기본으로 쓰고, 총연장이 유의미하게 크면 함께 표시한다.

> 길이의 **계단(디지털화) 편향** 보정이 들어가 있다 (GT 검증으로 발견·수정, 커밋 `measure: 길이의 계단…`).
> 격자 위 골격을 그대로 세면 대각선이 과대평가된다.

**수정 지점** 폭 알고리즘 → `_perp_width_profile()`. 길이 → `_skeleton_lengths()`.
가지 제거 강도 → `_prune_spurs(min_len)` 와 `quantify_crack(min_branch_px=4)`.

### 5-4. 실척 스케일 맵

**입력** 마스크 픽셀 좌표 + 역투영 결과 · **출력** `scale_map` (픽셀당 mm, 2D 배열)

`build_scale_map()`. 이것이 **실척의 핵심**이다.

```
픽셀 하나가 덮는 실제 크기 = (깊이 z / 초점거리 fx) × (1 / cosθ) × 1000  [mm]
                              └ 원근 보정 ┘   └ 경사 보정 ┘
```

- `θ` = 광선과 면 법선의 각도. 비스듬히 볼수록 픽셀이 표면에서 넓게 퍼진다
- `COS_CLAMP = 0.2` — 거의 스치듯 보는(grazing) 각도에서 `1/cosθ` 가 폭주하는 것을 막는 하한
- 역투영이 실패한 픽셀은 `med_depth / fx * 1000` 폴백

**성능 가드:** `SCALE_MAP_CAP = 60000` — 크랙 픽셀이 많으면 랜덤 서브샘플. 시드를 이미지 번호로
고정해 **결정적**이다.

### 5-5. 3D 역투영과 뷰별 핫스팟

**입력** 인스턴스별 중심선(2D) · **출력** 뷰별 핫스팟 `{xyz, poly, poly_w, poly_n, width_mm, ...}`

`_post_one()` 안에서 일어난다. 각 인스턴스마다:

1. 최대 폭 지점을 역투영 → 대표 3D 좌표 `xyz`
2. 중심선 전체를 역투영 → **3D 폴리라인 `poly`** (메시에 맞은 점만, 순서 유지)
3. 각 점의 면 법선 `poly_n` (카메라 쪽으로 향하게 뒤집음) — 뷰어가 리본을 표면 위로 띄울 때 사용
4. **3D 폭 `width_mm_3d`** — 좌·우 경계점을 각각 역투영해 `|L3d − R3d|`.
   2D 방식의 등방 `cosθ` 근사와 달리 경사·곡면의 **이방성 foreshortening**을 실측 반영
5. `quality = cosθ / z` — "얼마나 정면에서, 가까이 봤나". 대표 뷰 선택 기준

### 5-6. 다시점 융합과 병합

**입력** 모든 뷰의 핫스팟 · **출력** 크랙 1개당 객체 1개 (`merged[]` + `ribbons[]`)

같은 균열이 여러 사진에 찍히므로, 뷰별 인스턴스를 **하나의 3D 크랙**으로 합쳐야 한다.

```
① 조밀화     각 핫스팟의 poly 를 eps/2 간격으로 _densify
② 복셀 연결성분  eps(=DBSCAN_EPS_M=20mm) 격자 → 26이웃 중 +방향 13개로 희소 그래프
                 → scipy connected_components
③ 다수결 배정   핫스팟을 '자기 점들의 최빈 라벨' 군집에 배정
④ 군집별 집계   폭=멤버 최대/중앙/p95 · 길이=멤버 최대 · views 합침
⑤ 리본 융합    fuse_points() — 복셀 병합 + MST 순서화 + 라플라시안 스무딩
```

**왜 DBSCAN이 아니라 복셀 연결성분인가.** 원래 DBSCAN이었고 `DBSCAN_MAX_PTS=80000` 전역
랜덤 서브샘플을 썼다. 전체 데이터(594장)에서 수백만 점이 3%로 깎이자 점 간격이 `eps`를 훨씬 넘고
연쇄가 전부 끊겨 **뷰 간 병합이 전멸**했다 (핫스팟 5.3만 개, 평균 views 1.1).
복셀 성분화는 상한이 필요 없고(전 구간 벡터화 O(N log N)) 규모와 무관하게 밀도·소속을 보존한다.
`dbscan()` 함수 자체는 유틸로 남아 있지만 병합에는 쓰지 않는다.

**리본이 왜 따로 있나.** `merged[]` 는 **통계**(폭·길이·유형), `ribbons[]` 는 **형상**(3D 곡선)이다.
뷰어는 리본을 튜브 메시로 그리고, 클릭하면 대응하는 핫스팟 통계를 띄운다. `ribbon.hot` 이 인덱스다.

> **버린 방식:** '가장 대표하는 가상 곡선'(이방성 주곡선) 융합을 시도했다가 폐기하고
> 라플라시안 스무딩으로 복구했다 (커밋 2건). 주곡선은 여러 뷰의 오차를 평균 내는 대신
> 실제 크랙 형상을 왜곡했다.

### 5-7. 유형 분류

**입력** 융합 중심선 + 대표 뷰 이미지 · **출력** 각 크랙의 `type`, `type_conf`

`cracktype.py`. 유형은 **크랙 / 조인트·시공선 / 얼룩** 3종
(`craze`(실금)는 폐지 — 실금도 가는 균열이라 크랙으로 흡수. `LEGACY_TYPE` 로 폴백).

특징 = **3D 기하**(직선성·꺾임·곡률규칙성·축정렬·폭) + **2D 겉모습**(단면 골 깊이·엣지 선명도·
골폭·어두움·대비·종횡비) + **L2 장면 규칙성**(`par_neigh` = 주변에 평행한 이웃이 얼마나 있나
→ 시공선 가족 판별).

분류기는 `model/crack_type.json` — **순수 파이썬/numpy로 평가하는 랜덤포레스트**라
런타임에 scikit-learn이 필요 없다. 학습할 때만 sklearn을 쓴다.

**성능 설계 2가지** (둘 다 실측 기반):
- **대표 뷰 그룹핑**: 크랙 순서대로 이미지에 접근하면 594장 float32 캐시 ≈ 6.5GB로 페이징이 일어났다.
  지금은 크랙을 대표 뷰(source)별로 묶어 **이미지당 디코드 1회**, 메모리엔 그 한 장(~11MB)만 둔다
- **일괄 분류**: `classify_batch()` 가 numpy 벡터화로 트리를 평가 (샘플당 파이썬 루프 제거)

전체가 `try/except`로 감싸여 있어 **모델이 없거나 실패해도 무해**하다 (전부 `crack` 폴백).

**수정 지점** 특징 추가 → `cracktype.feature_vector()` (학습·추론이 공유하므로 **재학습 필수**).
유형 추가/삭제 → `CRACK_TYPES`(viewer.js) + 모델 재학습.

---

## 6. `.twin` v2 포맷

단일 **ZIP 컨테이너**다. 매직바이트 `PK\x03\x04` 로 판별한다 (`backend.twin_version()`).

```
report.twin
├── meta.json            DEFLATE   기하·통계만 (이미지·메시 제외)
├── mesh.ply             STORED    원본 바이너리
└── img/
    ├── 0000_thumb.jpg   STORED    썸네일 (≤900px)
    ├── 0000_full.jpg    STORED    원본 (≤1920px, q82)
    └── 0000_mask.png    STORED    검출 마스크 RGBA (full과 픽셀 일치)
```

**압축 선택의 근거:**
- JPEG/PNG는 이미 압축돼 있어 DEFLATE가 CPU만 태우고 이득이 없다 → `STORED`
- PLY도 `STORED`. DEFLATE해도 **16%밖에 안 줄면서 30~60초**를 먹는다 (실측)
- `meta.json` 만 DEFLATE — 숫자 텍스트라 잘 줄어든다

**`meta.json` 구조:**

```jsonc
{
  "version": 2,
  "counts": { "cameras": N, "hotspots": M },
  "report_meta": { "created", "mesh_diag_m", "images_found/analyzed/skipped",
                   "width_mm_range", "scale_warning",
                   "severity_bounds_mm", "severity_names", "conf_bounds" },
  "text_points": [[x,y,z], ...],          // 크랙 점군(복셀 다운샘플)
  "ribbons":  [{ "poly", "poly_w", "poly_n", "hot" }],   // 형상
  "cameras":  [{ "id", "source", "pos","fwd","up","right", "fov","aspect","w","h",
                 "fx","fy","cx","cy", "dist":[k1,k2,p1,p2,k3], "has_mask" }],
  "hotspots": [{ "text", "conf", "xyz", "width_mm", "width_mm_max/median/p95",
                 "width_mm_3d", "length_mm", "length_total_mm",
                 "sev", "type", "type_conf", "views":[{source,conf,w,L}] }]
}
```

**원자적 쓰기.** `save_report()` 는 `.tmp` 에 다 쓴 뒤 `os.replace()` 로 교체한다.
예전엔 곧바로 최종 경로에 써서, 생성이 중간에 실패하면 손상된 `.twin` 이 남았다
(예: 1.6GB짜리 빈 껍데기).

**사이드카.** 검사원 검토는 `.twin` 을 건드리지 않고 `<report>.twin.review.json` 에 따로 쌓인다.
`.twin` 은 불변으로 두고, 이 사이드카가 곧 **분류기 재학습 라벨의 축적처**가 된다.

> `.twin` v1(단일 JSON)은 더 이상 지원하지 않는다. 변환 도구도 제거했다.
> v1 파일을 열면 `twin_version()` 이 재분석을 안내하는 예외를 던진다.

---

## 7. 백엔드

### 7-1. 라우트

| 메서드 | 경로 | 하는 일 |
|---|---|---|
| GET | `/`, `/dashboard.html` | 대시보드 |
| GET | `/dashboard.js`, `/style.css`, `/viewer.js`, `/viewer.css` | 정적 자원 |
| GET | `/libs/*` | three.js 번들 (경로 탈출 방어 있음) |
| GET | `/viewer?report=<path>` | 뷰어 HTML (경로만 주입) |
| GET | `/viewer?asset=<path>` | 비교(자산) 모드 뷰어 |
| GET | `/api/report/meta?p=` | `meta.json` |
| GET | `/api/report/mesh?p=` | `mesh.ply` (스트리밍) |
| GET | `/api/report/img?p=&id=&kind=` | thumb / full / mask (스트리밍) |
| GET | `/api/reports` | `reports/*.twin` 목록 |
| GET | `/api/assets` | `asset.json` 목록 |
| GET | `/api/job?id=` | 잡 진행률·로그·결과 |
| GET | `/api/pick_folder`, `/api/pick_file?kind=` | 네이티브 파일 대화상자 |
| GET | `/api/download` | 파일 내려받기 |
| GET | `/api/retrain/status` | 축적된 라벨 수 |
| POST | `/api/create` | 보고서 생성 잡 시작 |
| POST | `/api/review` | 검토 저장 (사이드카) |
| POST | `/api/compare` | 검사 간 비교 잡 |
| POST | `/api/export` | PDF 생성 잡 |
| POST | `/api/retrain` | 분류기 재학습 잡 |

### 7-2. 스트리밍이 원칙이다

`_stream(src, size, ctype)` 은 zip 멤버를 **1MB 청크로 소켓에 흘려보낸다**.
피크 메모리가 파일 크기와 무관해진다 — 실측으로 419MB 멤버 전송 시 피크 2.2MB
(`z.read()` 통째로 읽으면 419MB, **195배 차이**).

`Content-Length` 를 `info.file_size` 로 미리 넣어 브라우저가 진행률을 표시할 수 있게 한다.

### 7-3. 잡 시스템

```python
_jobs = {}   # job_id -> {progress, done, error, result, log[], stats?}
```

네 종류가 같은 패턴을 쓴다: `_run_job`(분석) · `_run_compare_job`(비교) ·
`_run_export_job`(PDF) · `_run_retrain_job`(재학습).

공통 규약:
- `threading.Thread(daemon=True)` 로 띄우고 즉시 `job_id` 응답
- 진행률 `0.0~1.0`, 로그는 마지막 200줄 보관
- 예외는 `j["error"]` 에 문자열로. `finally` 에서 반드시 `j["done"]=True`
- **재학습 성공 시** `cracktype._MODEL`/`_MODEL_NP` 를 `None` 으로 리셋 → 앱 재시작 없이 다음 분석부터 새 모델

> `_jobs` 는 메모리에만 있다. 앱을 재시작하면 진행 중이던 잡 상태는 사라진다
> (생성된 `.twin` 파일 자체는 남는다).

### 7-4. 네이티브 파일 대화상자

`_ps_dialog()` 가 PowerShell 스크립트를 띄운다. 주의할 점 둘:

- **WinForms `FolderBrowserDialog` 는 .NET Framework에서 구형 트리(작은 창)로만 뜬다.**
  그래서 최신 탐색기 스타일 다이얼로그를 우선 시도하고 실패 시 폴백한다
- **출력의 BOM** 때문에 "폴더 없음"이 나던 버그가 있었다 → `utf-8-sig` 로 읽는다

---

## 8. 뷰어 (`ui/viewer.js`)

`viewer.html` 은 껍데기(아이콘 스프라이트 + DOM 구조)이고 **동작은 전부 `viewer.js`** 에 있다.
`type="module"` 로 로드되므로 함수가 전역에 노출되지 않는다 (인라인 `onclick` 에서 호출 불가).

### 8-1. 주요 블록

| 블록 | 내용 |
|---|---|
| FPS 자유 이동 카메라 | OrbitControls 대체. 좌드래그=시점 회전, 우클릭 홀드=스코프 확대(FOV 60→22) |
| 보고서 로딩 | `meta` → `mesh`(ArrayBuffer) → 이미지 지연 로드 |
| 크랙 리본 | 실폭 반영 가변-반경 튜브. 표면 위로 `RIBBON_OFFSET_M=0.06` 띄움 |
| **병합 렌더** | 수만 개 리본을 **메시 1개(드로우콜 1회)** 로 — 대규모 FPS의 핵심 |
| 유형/심각도/신뢰도 필터 | 세 조건 AND. 유형별 병합 버퍼를 나눠 통째로 on/off |
| 카메라 글리프 | 파란 프러스텀. 클릭하면 그 사진 시점으로 정렬 |
| 2D 하이라이트 | 3D 중심선을 사진에 투영해 노란 발광 폴리라인 |
| 지점 검사 모드 | 임의 표면 점 클릭 → 그 점이 찍힌 사진들을 찾아 표시 |
| 검토 UI | 확인/오탐/유형수정/메모 → `/api/review` |
| 측정 도구 | 거리 / 반경 / 캘리브레이션 |
| 비교(자산) 모드 | 상태색·이전 검사 고스트·Δ이력 |

### 8-2. 알아둘 트릭

- **히트박스 분리**: `HIT_MAT` 는 `material.visible=false` 다. 객체를 `visible=false` 로 두면
  이 three 버전에서 피킹 대상에서 빠지기 때문. 화면상 `HITBOX_PX=14` 여유를 줘 덜 정확히
  갖다대도 잡힌다
- **화면상 크기 고정**: 마커·라벨·핫스팟은 줌과 무관하게 항상 같은 픽셀 크기 (정밀 검사용)
- **2D 하이라이트는 투영만 믿지 않는다**: 멤버 뷰인지 확인하고 검출 마스크에 스냅한다.
  투영만 쓰면 정합 오차로 빗나간다
- **스케일 캘리브레이션**: 뷰어에서 2점 실측 거리를 입력하면 모든 mm를 보정한다.
  `localStorage`(`CALIB_KEY`)에 **보고서별로** 저장되는 **로컬 설정**이라
  `.twin` 이나 PDF에는 반영되지 않는다

### 8-3. 심각도·신뢰도는 뷰어가 재계산한다

`.twin` 에 저장된 `sev` 는 무보정 mm 기준이다. 뷰어는 캘리브레이션 보정 후
`meta.report_meta.severity_bounds_mm` 로 **다시 계산**한다. 기준값을 메타에 기록해 둔 이유가 이것이다.

```
심각도 6구간  경계(mm): 0.5 / 1 / 3 / 5 / 10
  S1 경미  S2 관찰  S3 보통  S4 심각  S5 매우 심각  S6 특이값
```

`S6(특이값)` 은 10mm 초과 — 콘크리트 균열로는 비현실적이라 **측정 오류를 눈에 띄게 하는 장치**다.

균열 신뢰도는 `conf`(AI가 균열이라고 본 확률) 하나로 통일했다. 예전엔 '측정 신뢰 A/B/C'가
따로 있어 화면에 '신뢰'가 둘이라 혼동이 컸다. 실척 의심은 `report_meta.scale_warning` 이 담당한다.

---

## 9. 부가 기능 3종

### 9-1. 검사 간 비교 (`tools/compare_twins.py`)

설계 축은 **"위치는 신원, 측정은 상태"** — 절대 섞지 않는다.

```
① 정합  메시–메시 7-DoF(스케일 포함) ICP. 잔차 reg_rms 가 매칭 반경의 근거
        (포토그래메트리는 검사마다 좌표계·스케일이 다르므로 필수)
② 매칭  두 검사의 중심선 점을 섞어 복셀 연결요소로 군집
        → 한 성분에 양쪽이 있으면 같은 균열 (1:N/N:1 조각남이 자동 처리)
③ 판정  노이즈 바닥(뷰 간 산포 IQR)을 넘는 변화만 '성장'
        3D 위치 변화는 성장 판정에 쓰지 않는다 (정합 오차 cm ≫ 성장 mm)
```

출력은 `asset.json` (영속 균열 대장). `extend()` 로 3번째·4번째 검사를 이어 붙이면 영속 ID가 승계된다.
정합 실패(잔차 게이트) 시 **결과를 신뢰할 수 없어 중단**한다 (`RegistrationError`).

### 9-2. PDF 내보내기 (`tools/export_report.py`)

표지·요약 / 위치도 / 균열 대장 / 사진 대지. matplotlib `Agg` 백엔드 + `PdfPages`.
검토 사이드카가 있으면 상태·수정 유형·메모를 대장에 반영한다.
**뷰어의 2점 캘리브레이션은 로컬 설정이라 반영되지 않으며, 표지에 그 사실을 명시한다.**

### 9-3. 유형 재학습 (`tools/retrain_type.py`)

검사원 검토가 곧 학습 데이터가 되는 **액티브 러닝 루프**다.

```
유형 수정(type)  → 그 유형이 정답 라벨
확인(confirmed)  → 화면의 유형이 정답 라벨
오탐(rejected)   → 'blob' 라벨
메모만           → 라벨 아님(건너뜀)
```

안전장치:
- 라벨 < 40개 또는 클래스 < 2면 **모델을 바꾸지 않는다**
- 현재 모델의 어떤 유형에 라벨이 하나도 없으면 **중단** (재학습하면 그 유형이 영영 사라짐 — 4→2 클래스 축소 사고 방지)
- 교체 전 신·구 모델 판정 일치율을 보고하고, 기존 모델은 `crack_type.bak.json` 으로 백업
- `model/train_set.npz` 에 특징을 누적 저장 → 원본 `.twin` 이 삭제돼도 과거 라벨이 계속 반영

---

## 10. 설계 결정 기록

이미 본문에서 다룬 것은 링크만 남긴다. **버린 대안**이 특히 중요하다 — 같은 실수를 반복하지 않기 위해.

| # | 결정 | 버린 대안 | 이유 |
|---|---|---|---|
| 1 | `.twin` = ZIP + 스트리밍 | JSON에 base64 인라인 | V8 문자열 한계 536.9MB. 데이터를 줄여도 이미지만으로 554MB → 구조를 고쳐야 했다 ([상세](DESIGN_twin_v2.md)) |
| 2 | 병합 반경 절대값(m) | 메시 대각선 비율 | 균열은 절대 크기 특징. 댐(622m)에서 6.2m 안이 전부 1개로 뭉쳤다 |
| 3 | 복셀 연결성분 병합 | DBSCAN + 전역 서브샘플 | 594장에서 점이 3%로 깎여 뷰 간 병합 전멸 |
| 4 | 라플라시안 스무딩 | 이방성 주곡선 융합 | 주곡선이 실제 크랙 형상을 왜곡했다 |
| 5 | 확률맵 레벨셋에서 폭 측정 | 이진 마스크 EDT×2 | 디지털화 편향 + 분기점 과대평가 |
| 6 | 심각도 = 절대 폭 6구간 | 상대 순위(길이×폭) | 보고서 간 비교가 불가능했다. 기준이 화면에 둘(심각도+판정등급)이라 혼란 |
| 7 | 균열 신뢰도 = conf 하나 | + 측정 신뢰 A/B/C | 화면에 '신뢰'가 둘이라 혼동. 실척 의심은 `scale_warning` 으로 분리 |
| 8 | `craze`(실금) 폐지 | 4클래스 유지 | 실금도 결국 가는 균열. 3클래스로 병합 |
| 9 | 검토는 사이드카 | `.twin` 직접 수정 | `.twin` 을 불변으로 유지 → 재생성·비교·재학습이 안전 |
| 10 | 순수 numpy 랜덤포레스트 | 런타임 sklearn 의존 | 오프라인 자체완결 원칙. 학습할 때만 sklearn |
| 11 | `embreex` 사실상 필수 | 순수 numpy 폴백 허용 | 폴백이 **조용히** 일어나 전 이미지 실패 → 빈 보고서. [§13](#13-흔한-함정) |

---

## 11. 수정 지점 지도

"이걸 고치고 싶다" → "여기를 보라".

| 하고 싶은 것 | 어디 |
|---|---|
| 검출 임계값 조정 | `pipeline.CRACK_THR` (0.4) |
| 다른 세그멘테이션 모델 적용 | `pipeline.get_segmenter()` + `CrackSegmenter(crack_index=)` |
| 타일/배치/FP16 튜닝 | `pipeline.CRACK_BATCH`, `USE_FP16`, `CrackSegmenter.__init__(tile, overlap)` |
| 끊긴 크랙 잇는 거리 | `pipeline.CRACK_BRIDGE_PX` (5px, 2D) / `DBSCAN_EPS_M` (20mm, 3D) |
| 폭 측정 알고리즘 | `pipeline._perp_width_profile()` |
| 길이 측정 알고리즘 | `pipeline._skeleton_lengths()` |
| 경사 보정 한계 | `pipeline.COS_CLAMP` (0.2) |
| 다시점 융합 해상도 | `pipeline.FUSE_VOXEL_M` (20mm) |
| 심각도 경계 | `pipeline.SEV_BOUNDS_MM` — **뷰어·PDF가 meta에서 읽으므로 여기만 고치면 전파된다** |
| 균열 신뢰도 구간 | `pipeline.CONF_BOUNDS` (0.7 / 0.9) |
| 내장 이미지 화질·용량 | `pipeline.FULL_MAX_PX`, `FULL_JPEG_Q`, `THUMB_MAX_PX` |
| XMP 파서 (다른 SW 지원) | `pipeline.parse_xmp()` |
| `.twin` 에 필드 추가 | `pipeline.save_report()` 의 `meta` dict + 뷰어 소비처 |
| API 라우트 추가 | `backend.Handler.do_GET` / `do_POST` |
| 새 백그라운드 잡 | `backend._run_*_job` 패턴 복제 + `_jobs` |
| 대시보드 UI | `ui/dashboard.html` + `ui/dashboard.js` |
| 뷰어 색상·굵기 | `ui/viewer.js` 상단 상수 (`RIBBON_*`, `SEV_HEX`, `TYPE_COLOR`, `CMP_COLOR`) |
| 뷰어 조작감 | `LOOK_SENS`, `HOLD_MS`, `MOVE_PX`, `BASE_FOV`/`SCOPE_FOV` |
| 유형 특징 추가 | `cracktype.feature_vector()` → **재학습 필수** |
| 유형 종류 변경 | `ui/viewer.js` `CRACK_TYPES`/`TYPE_LABEL` + 모델 재학습 |
| PDF 레이아웃 | `tools/export_report.py` `build_pdf()` |
| 비교 판정 기준 | `tools/compare_twins.py` (노이즈 바닥·정합 게이트) |
| 재학습 안전장치 | `tools/retrain_type.py` `--min-labels`, 클래스 손실 가드 |

---

## 12. 성능·메모리 설계

이 프로젝트에서 터졌던 것들은 대부분 **메모리**였다. 새 기능을 넣을 때 아래 원칙을 깨지 말 것.

### 12-1. 이미지 루프 파이프라이닝

```
①로드(스레드 2개, 프리페치 3) → ②GPU 추론(메인 스레드 전용) → ③후처리(워커 1개)
```

프로파일(6장 실측): **추론 92% · 후처리 8% · 로드 <1%**. GPU가 지배적이라 이득 상한은 ≈8%지만
무손실 스케줄링이고 GPU가 빨라질수록 겹침 비중이 커진다.

스레드 안전 규약:
- GPU는 **메인 스레드 전용**
- 메시 레이캐스트(embree)는 **후처리 워커(1개) 전용**
- 결과 수확은 이미지 순서대로 → 직렬 처리와 **결과·순서가 동일(결정적)**

### 12-2. 배압(backpressure)

확률맵 캐시가 히트하면 추론이 0초라 메인 루프가 후처리보다 빨라진다. 그러면 대기 작업마다
`prob`(장당 ~80MB)를 붙든 채 무한히 쌓여 **OOM**이 난다. 미완료 후처리가 2개를 넘으면
가장 오래된 것을 기다린다 (`pipeline.py:1068-1076`).

### 12-3. 상한과 결정성

| 상수 | 의미 |
|---|---|
| `SCALE_MAP_CAP = 60000` | 스케일맵용 역투영 픽셀 상한 |
| `MAX_CRACK_POINTS_PER_IMG = 30000` | 이미지당 크랙 점군 상한 |
| `TEXTPOINT_MERGE_M = 0.02` | 점군 복셀 다운샘플 |

서브샘플에는 **이미지 번호를 시드로 고정**한 RNG를 쓴다 (`np.random.default_rng(n)`).
같은 입력이면 같은 출력이 나와야 하기 때문이다.

### 12-4. 렌더 성능

수만 개 리본을 개별 메시로 그리면 드로우콜이 폭발해 6fps가 나왔다.
지금은 **유형별 병합 버퍼 1개씩**으로 묶어 드로우콜을 최소화하고, 필터는 버퍼 통째로 on/off 한다.
개별 강조는 정점 범위(`ribVRange`)를 기억해 색상 속성만 갈아끼운다.

---

## 13. 흔한 함정

### `embreex` 누락 — 가장 위험한 함정

없으면 `trimesh` 4.x가 순수 numpy 브루트포스로 **조용히 폴백**한다. 199만 면 메시에 6만 rays를
쏘면 후보 쌍이 7,535만 개로 불어나 `Unable to allocate 575 MiB` 로 **전 이미지가 실패하고
빈 보고서가 생성된다.**

```bash
python -c "import trimesh.ray; print(trimesh.ray.has_embree)"
```

`True` 여야 한다. `pyembree` 는 trimesh 3.x 시절의 폐기된 이름이라 4.x에선 소용없다.
설치 후 실측: 2000 rays 기준 1회차 1.592s(BVH 빌드) → 2회차 이후 **0.001s**.

### 기타

- **PLY가 포인트클라우드**면 즉시 예외 (`삼각형이 없습니다`). 메시여야 한다
- **`.twin` v1**은 열리지 않는다. 재분석해서 새로 생성해야 한다
- **`cache/prob`** 는 파생 데이터다. 지워도 되지만 다음 분석이 느려진다
- **`_jobs` 는 휘발성**이다. 앱 재시작 시 진행률 추적이 끊긴다
- **캘리브레이션은 브라우저 `localStorage`** 에 있다. PC를 바꾸거나 캐시를 지우면 사라지고,
  PDF에도 반영되지 않는다

---

## 14. 개발 환경과 검증

### 설치

```bash
pip install -r requirements.txt
```

GPU를 쓰려면 CUDA 지원 torch를 별도 인덱스로 설치한다.
모델 가중치는 **Git LFS**로 관리한다:

```bash
git lfs install && git lfs pull
```

`model/best.pt` 가 134바이트면 LFS 포인터만 받은 상태다 (실제 크기 ≈ 118MB).

### 검증 방법

이 저장소에는 자동화 테스트가 없다. 변경 후 최소한 아래를 확인할 것:

**① 파이썬 문법·import**
```bash
py -c "import ast,sys; [ast.parse(open(f,encoding='utf-8').read()) for f in ['app.py','backend.py','pipeline.py','cracktype.py']]"
```

**② 뷰어 JS 파싱** — node가 없어도 브라우저로 확인할 수 있다.
백엔드를 띄우고(`B.make_server(포트)`), 대시보드 페이지에서 `viewer.html` 과 동일한 importmap을
주입한 뒤 `import('/viewer.js')` 를 실행한다. `SyntaxError` 가 아니라면 파싱은 통과한 것이다.

**③ 실제 보고서로 확인** — 가장 확실하다. 작은 데이터셋(이미지 5~10장)으로 `.twin` 을 만들어
뷰어에서 열어 본다. `build_report(..., limit=N)` 으로 장수를 제한할 수 있다.

### 코딩 규약

- 주석은 **한국어**, "무엇"보다 **"왜"** 를 적는다. 실측 수치가 있으면 함께 남긴다
- 파라미터를 바꿀 땐 그 값이어야 하는 이유를 주석에 남긴다 (나중에 되돌릴 근거가 된다)
- 새 의존성은 신중히. **오프라인 자체완결**이 원칙이다 (three.js도 CDN이 아니라 `libs/` 번들)

---

## 관련 문서

- [사용자 매뉴얼](USER_GUIDE.md) — 설치·조작·문제 해결
- [`.twin` v2 설계 문서](DESIGN_twin_v2.md) — 포맷 전환의 상세 근거와 실측 데이터
- [데이터·구성요소 출처](../DATA_SOURCES.md) — 모델 계보와 라이선스
- [변경 이력](../CHANGELOG.md)
