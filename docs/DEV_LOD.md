# 계층 LOD 타일 — 개발 문서

> 사용법은 README 를 보세요. 이 문서는 **왜 이 구조인지 · 데이터가 어떤 순서로 처리되는지 ·
> 고치려면 어디를 봐야 하는지**를 적은 인수인계용 문서입니다.

## 1. 왜 (Why)

### 문제
원전 촬영본 PLY 가 6.26GB(정점 9,628만 · 면 1억 9,247만 · 좌표 double)로 나왔다.
예전 구조는 `.twin` 에 PLY 를 그대로 넣고 뷰어가 `/api/report/mesh` 로 **통째로**
ArrayBuffer 에 받아 PLYLoader 로 풀었다. 이 파일에서는:

| 단계 | 필요량 | 한계 |
|---|---|---|
| fetch → ArrayBuffer | 6.3GB | WebView2 렌더러 메모리 |
| PLYLoader(일반 JS 배열) | 10GB+ | 〃 |
| GPU 정점+색+인덱스 | 3.7~4.6GB | RTX 5050 8GB 이지만 D3D11 버퍼 하나 2GB 상한 |
| 렌더 | 1억 9천만 면 | lod.js 실측(400만 면 5.7ms) 외삽 → 프레임당 270ms |
| 클릭 BVH | 수 GB · 수 분 | 메인 스레드 |

즉 "로드만 되게" 만들어도 쓸 수 없다.

### 버린 대안
- **생성 시 단일 간략화(500만 면)** — 하루면 되지만 가까이 가도 원본 디테일이 없다.
  크랙 확인은 사진으로 하지만, 위치 탐색에서 표면 디테일이 사라지는 것을 사용자가 원치 않았다.
- **스트리밍 파서 + 조각 렌더로 원본 통째 로드** — 메모리·프레임 한계는 그대로. 불가.
- **open3d/trimesh 로 QEM 간략화** — 의존성 100MB+ 추가, exe 크기·빌드 복잡도 증가. numpy 만으로 해결.

### 채택: 옥트리 계층 LOD (3D Tiles 의 REPLACE 정제와 같은 구조)
- 메시를 공간 옥트리로 나누고, **노드마다 그 영역 전체를 노드 크기에 맞는 해상도로 요약한 메시**를
  미리 만들어 둔다. 잎은 원본 삼각형 그대로.
- 뷰어는 카메라 절두체 안 노드만, 노드의 기하 오차가 화면에서 문턱(px)보다 크면 자식으로 교체해
  그린다. 화면 삼각형 합계가 예산을 넘으면 문턱을 스스로 올린다 → 어떤 PC 든 프레임이 유지된다.
- 첫 화면은 루트 타일 하나(3만 면)라 즉시 뜬다. 가까이 가면 잎(원본)이 그대로 보인다.

### 트레이드오프
- `.twin` 이 원본 PLY 보다 작다(6.26GB → 타일 합계 약 3.4GB: float32 좌표·uint16 인덱스, 내부 노드 +30%).
- 인접 타일의 LOD 단계가 다르면 경계에 미세한 틈이 보일 수 있다(스커트 없음). 점검 용도에서는 허용.
- 내부 노드의 감기(winding)는 요약 과정에서 뒤집힐 수 있어 타일은 **양면**으로 그리고 양면으로 교차한다.
  정점색 메시라 조명이 없어 시각적 영향은 없다.

## 2. 순서 (Flow)

### 생성 — `pipeline.build_report` → `tiler.build_tiles`
```
PLY(binary_little_endian, vertex + face(list uchar int ×3))
 1. read_header      헤더 해석 → 정점/면 블록을 np.memmap 으로 잡음 (6GB 를 통째로 올리지 않음)
 2. load_mesh        정점: double 좌표 − 원점(표본 경계상자 중심, 정수 m) → float32 · 색 uint8
                     면: 전부 삼각형인지 검사하며 int32 (아니면 PlyError → mesh.ply 원본 저장으로 후퇴)
 3. 모튼 정렬        면마다 첫 정점의 모튼 키(12비트×3축) → argsort → 면 배열 재배열
                     정렬 뒤 어떤 옥트리 노드의 면도 연속 구간 → 노드 = (시작, 끝)
 4. build (후위 재귀)  잎(≤ LEAF_MAX_TRIS=100k 또는 깊이 12): 구간의 삼각형 그대로 → 타일
                     내부: 자식 8개 메시를 합쳐 simplify() → 타일
 5. emit             tiles/<id>.bin 을 zip 에 바로 씀(STORED) · 노드 레코드 {b,e,t,v,c} 누적
 → meta.json 의 "tiles": {origin, box, root, color, nodes[...]}
```
- `simplify()` = 격자 병합: 노드 정육면체를 128³ 칸으로 나눠 칸마다 정점 하나(위치·색 평균).
  삼각형은 원본을 옮기지 않고 **칸 인접 그래프의 3-클리크**로 만든다. 원본 삼각형이 칸보다 훨씬
  작으면 네 칸이 만나는 지점마다 대각선 한쪽만 남아 구멍이 생기는데(실측: 돔 루트가 격자 구멍투성이),
  대각선 인접은 항상 생기므로 클리크로 만들면 사각형이 빈틈없이 덮인다. 기하 오차 = 칸 한 변.
- 요약 삼각형 = 3-클리크 ∪ **칸별 부채꼴(dual fan)** ∪ **작은 경계 고리 메움**(≤8칸). 클리크만으로는 접점
  사각형에 대각선 인접이 없으면 구멍이 남아 돔 코스 레벨이 점·선투성이로 보였다(실측: 돔 영역 배경 픽셀
  보통 LOD 25.1% vs 원본 24.5%, 루트 34.5%). 부채꼴은 칸의 이웃을 접평면(이웃 벡터 PCA) 각도 순으로 이어
  사각형을 두 삼각형으로 덮고, 남는 작은 고리는 직접 메운다 → 보통 LOD 24.5%(원본과 같음), 루트 27.1%.
  겹치는 삼각형이 생기지만(변의 20%가 3장 이상 공유) 양면 렌더라 보이는 문제는 없다. 값: `FAN_MAX_GAP`·`FILL_MAX_LOOP`.
- 실측(6.26GB, RTX 5050 노트북·32GB): 읽기 12~58초 · 정렬 108~185초 · 전체 14.4분(3-클리크 방식;
  원본 삼각형만 옮기던 첫 방식은 6.4분이었으나 구멍이 남았다) · 타일 7,071개(잎 5,715) · 타일 합계 3.40GB
  · 루트 3만 면 · 최대 메모리 약 9GB.

### 열기 — 데스크톱 `ui/viewer.js` · 모바일 `ui/mobile_template.html` → 공용 `ui/tiles.js`
```
meta.json
 ├─ tiles 있음 → shiftMetaOrigin(cameras −= origin) → src.load(id) = /api/report/tile?id (데스크톱)
 │                                                    = twin.bytes('tiles/<id>.bin') (모바일, File.slice)
 └─ 없음(옛 보고서) → mesh.ply 통째 → shiftToLocalOrigin → PLYLoader → legacySource()
                      (간략 메시(lod.js) = 루트, 원본 = 자식 인 두 단계 타일셋)
createTileSet(deps, src, opts) → { group, update, raycast, faceNormal, ready, stats }
 매 프레임 update(camera, H, moving):
   traverse(root): 절두체 밖 → 무시
                   sse = err × (H / 2tan(fov/2)) / 상자까지 거리
                   sse > 문턱 && 자식 있음 → 보이는 자식이 전부 로드됐으면 자식으로 내려감
                                             아니면 자식 요청(want) 후 자신을 그림 (구멍 없음)
   삼각형 합계 > triBudget → 문턱 ×1.1 · 절반 미만 → ÷1.05
   요청: sse 큰 순으로 concurrency 개까지 · 해제: byteBudget 초과 시 이번 프레임 미사용 · 오래된 순
   조작 중(moving) 은 문턱 × movingScale → 거칠게 그려 프레임 확보, 멈추면 원복
 raycast: 지금 그려진 타일 중 광선이 상자를 지나는 것만, 가까운 상자 순 · 타일별 BVH 지연 생성·캐시
```
- 예산(데스크톱): 500만 면 · 1.5GB · 문턱 2px · 조작 중 ×2.5 · 동시 6.
  모바일: 150만 면 · 400MB · 3px · ×3 · 동시 4.

### 백엔드 — `backend.py`
- `/api/report/tile?p=&id=` → `tiles/<id>.bin` 스트리밍. `_open_twin()` 이 ZipFile 을 경로별로 캐시
  (요청 수천 개가 중앙 디렉터리를 매번 다시 읽지 않게). 3초 미사용이면 닫는다 — Windows 에서 열린
  파일은 `os.replace` 를 막아 같은 이름으로 보고서를 다시 만들 때 실패했었다.
- `/api/report/mesh` 는 옛 보고서용으로 남아 있다.

## 2-1. main 브랜치 전용 — 측정용 메시와 진행 로그

### 측정용 메시(옥트리 컷)
main 의 분석은 trimesh 로 이미지 픽셀을 메시에 역투영한다. trimesh 는 면마다 float64 삼각형·법선·외적을
캐시해 면 1억 9천만이면 20GB 를 넘긴다(실측 MemoryError). 그래서 `build_report` 는 **타일을 먼저 만들고**,
`tiler.build_tiles(..., measure_max_faces=MEASURE_MAX_FACES)` 가 돌려주는 **옥트리 컷**을 측정용 메시로 쓴다.
- 컷 = 루트→잎 경로마다 "내 칸 ≤ 목표 < 부모 칸" 인 노드 하나씩(잎이면 부모 칸 > 목표). 목표 칸 =
  평균 변 길이 × sqrt(원본 면 수 / 상한) → 면 수가 상한(2천만) 근처로 떨어진다. 6GB 기준 약 3cm.
- 원본 면 수가 상한 이하면 원본 그대로(칸 0).
- 정확도: 폭은 인접 광선 두 개의 착지 차라 국소 평면에서는 요약의 영향이 없다. 돔 반경 20m·3cm 격자의
  현 오차 6µm. report_meta 에 `measure_faces`·`measure_cell_m`·`mesh_faces`(원본) 가 남는다.
- 처리 순서: 메시 타일 → 메시 로드(컷 → trimesh, embree 가속 구조는 광선 1개로 미리 생성) → 모델 로드 →
  이미지 분석 → 병합 → 융합 → (측정용 메시 해제 + gc) → 보고서 저장(이미지·meta). zip(.tmp) 은
  처음부터 열어 두고 채운다.

### embree 가 없으면 멈춘다
`_load_mesh_for_measure` 는 `type(mesh.ray).__module__` 을 로그에 찍고 `pyembree` 가 아니면 RuntimeError 를
낸다. trimesh 는 embreex 임포트가 실패하면 조용히 `ray_triangle`(numpy)로 물러나는데, 이 경로는 광선마다
(광선 수 × 면 수) 배열을 만들어 3천만 면에서는 장당 수십 GB 다(배포 앱 실측: 커밋 111GB, 사진 5장째부터
할당 실패). 번들에는 `--collect-all embreex --copy-metadata embreex` 가 필요하고, `exe --selftest` 로 확인한다.

### 진행 로그 (`pipeline.Progress`)
- 7단계 가중치(타일 25 · 메시 로드 5 · 모델 2 · 이미지 50 · 병합 5 · 융합 3 · 저장 10%)로 전체 %를 만든다.
  `stage(이름)` 이 "▶ [전체 NN%] (k/7) 이름 · 경과 · 메모리", `step(frac, 세부)` 이 5% 또는 10초마다
  "[전체 NN%] 이름 NN% · 세부" 를 찍고, 20초 넘게 보고가 없으면 심장박동 스레드가 "진행 중 · N초 경과 · 메모리"
  를 찍는다. 대시보드 막대는 같은 값을 받는다(backend `prog(c, t=None)`).
- tiler 안쪽은 `_Report` 가 같은 형식으로 "[메시 타일 NN%] PLY 읽기/공간 정렬/타일 생성 NN% · 세부" 를 찍는다.
- 바꾸려면: 가중치 `Progress.STAGES` · 심장박동 간격 `Progress.HEARTBEAT_SEC` · 로그 간격은 `step()`/`_Report.__call__`.

## 3. 어디 (Where)

| 바꾸고 싶은 것 | 위치 |
|---|---|
| 잎 타일 크기 · 격자 분할 수 · 최대 깊이 | `tiler.py` 상단 `LEAF_MAX_TRIS` · `GRID` · `MAX_DEPTH` |
| 요약(간략화) 방식 | `tiler.simplify()` — 입력: 자식 (P,C,I) 목록 + 정육면체, 출력: (P,C,I) |
| PLY 형식 지원 범위(색 이름·ascii 등) | `tiler.read_header` · `_vertex_dtype` · `load_mesh` · `_COLOR_NAMES` |
| 타일 바이너리 형식 | `tiler.pack_tile` ↔ `tiles.js parseTile` (둘을 같이 고칠 것) |
| meta 의 노드 표기 | `tiler.build_tiles.emit` ↔ `tiles.js createTileSet` 의 `nodes.map` |
| 정제 문턱·예산·동시 요청 수 | 데스크톱 `viewer.js` `createTileSet(... opts)` · 모바일 `mobile_template.html` 같은 자리 |
| 정제 규칙(sse 계산·자식 준비 조건) | `tiles.js` `sse()` · `traverse()` |
| 해제 규칙 | `tiles.js update()` 의 "해제" 블록 |
| 클릭 교차 | `tiles.js raycast()` · `bvh.js`(양면 옵션 `bothSides`) |
| 옛 보고서 호환 | `tiles.js legacySource()` · 뷰어 두 곳의 `else` 분기 |
| 타일 스트리밍 라우트 · zip 캐시 | `backend.py` `_report_tile` · `_open_twin` · `_zip_janitor` |
| 진행률 배분(메시 vs 사진) | `pipeline.py` `MESH_PROGRESS` |
| 모바일 뷰어에 새 모듈 인라인 | `export_mobile.py build_viewer` (`__TILES_SRC__`) ↔ 템플릿의 `src-tiles` |

### 디버그
- 데스크톱 뷰어에서 `window.__tiles.stats` → `{tris, drawn, loaded, pending, bytes, sse}`.
- `tiler.py` 는 단독 실행 가능: `py tiler.py in.ply out.zip [--leaf N --grid G]` (사진 없는 타일 zip; 뷰어로 열린다).

### 검증 기록 (2026-09-04)
- chocolate(37만 면, 잎 2만 설정): 타일 73개 · 데스크톱/모바일 · 타일/옛 보고서 4조합 모두 클릭 → 같은 사진(_DSC4525) 선택.
- 울산 원전(6.26GB): 타일 7,071개 · 루트 즉시 표시 · 돔 40m 거리에서 화면 286만 면·82 타일로 정제 ·
  첫 클릭 55ms(타일 BVH 생성 포함) · 두 번째 클릭 1ms.
- 울산 원전 전체 생성(대시보드 API 경로, 사진 1,090장 + 6.26GB PLY): 약 45분 · 결과 4.09GB ·
  뷰어에서 돔 클릭 62ms → 사진 P1100110 + 붉은 원 + 후보 12장 정상. 종전엔 같은 입력이 6.9GB 로
  만들어져 열리지 않았다.
