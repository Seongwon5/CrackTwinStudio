# 계층 LOD 타일 — main 브랜치 포팅 계획 (완료 · 2026-09-04)

> 아래 계획은 전부 반영됐다: `tools/twin_mesh.py`(정점 표본) · `ui/viewer.js` 6개 지점 · `ui/origin.js` 분리 ·
> `KEEP_MESH_PLY = False` · 문서. 검증: choco(타일/옛 보고서) 지점 검사 클릭, 울산 6GB 타일 7,071개 로드.
> 이후 참고용으로 남긴다.

> 원본 구현·설계 근거는 viewer-simple 브랜치의 `docs/DEV_LOD.md` 를 보세요.
> 이 문서는 main 에서 **무엇이 이미 들어왔고, 무엇을 더 해야 하는지**를 적은 작업 계획입니다.

## 배경
원전 촬영본 PLY 가 6.26GB(면 1억 9천만)라 `.twin` 의 `mesh.ply` 를 통째로 받는 뷰어로는
열 수 없다. viewer-simple 에서는 생성 시 메시를 옥트리 타일로 쪼개고(`tiler.py`), 뷰어가
보이는 타일만 거리별 해상도로 스트리밍하는 구조(`ui/tiles.js`)로 해결했다(2026-09-04).

## 이미 들어온 것 (feat/lod-tiles 브랜치, 기계적 복사)
| 파일 | 내용 |
|---|---|
| `tiler.py` | PLY → 타일. viewer-simple 과 동일 파일 |
| `ui/tiles.js` · `ui/bvh.js` · `ui/lod.js` | 타일 런타임 · 레이-메시 BVH · 격자 병합 LOD. 동일 파일 |
| `backend.py` | `_open_twin` zip 캐시(3초 유휴 닫힘) · `/api/report/tile?p=&id=` · 정적 파일 화이트리스트에 bvh/lod/origin/tiles.js |
| `pipeline.py save_report` | 타일을 `tiles/<id>.bin` 으로 쓰고 meta 에 `tiles` 를 넣는다. **`KEEP_MESH_PLY = True`** 라 `mesh.ply` 도 같이 담는다(아래 이유) |

지금 상태에서 main 은 **아무것도 깨지지 않는다**: 뷰어·tools 는 계속 `mesh.ply` 를 읽고,
타일은 옆에 쌓여 있을 뿐이다. 큰 메시 보고서는 크기가 (원본 + 타일 약 55%) 로 커진다.

## 남은 일 (뷰어 포팅) — 순서대로

### 1. `tools/` 의 mesh.ply 의존 제거 (진짜 블로커)
- `tools/compare_twins.py:37-46` `load_twin()` — `z.read("mesh.ply")` 로 정점 표본 → ICP 정합.
- `tools/export_report.py:171-180` — PDF 위치도용 정점 6만 개 표본.
- 둘 다 **표본만** 필요하다. 타일 보고서에서는 루트·상위 타일이 곧 정점 표본이므로,
  `tiler.py` 형식(`TWT1` 헤더, float32 좌표 + `tiles.origin`)을 읽는 헬퍼
  `twin_vertex_sample(path, n)` 을 `tools/` 에 하나 만들어 두 곳에서 쓰면 된다
  (mesh.ply 가 있으면 기존 경로, 없으면 타일 경로).

### 2. `ui/viewer.js` (2,384줄) 포팅
viewer-simple 의 `ui/viewer.js` 로드 블록·클릭 블록을 참고한다.
| 위치 | 할 일 |
|---|---|
| `viewer.js:91-182` 원점 이동 IIFE | `ui/origin.js` 로 추출(viewer-simple 파일 복사) + `shiftMetaOrigin(DATA, O)`. 반환된 원점을 `WORLD_ORIGIN` 에 넣어 `wpt()`(184) · `fmtXYZ()`(186) · 고스트 행렬(791-793)이 그대로 돌게 |
| `viewer.js:66-75` 로드 | `DATA.tiles` 있으면 타일 소스, 없으면 mesh.ply → `legacySource()` |
| `viewer.js:372-398` 메시 생성 | `createTileSet({THREE, buildBVH}, src, {material, triBudget 5e6, byteBudget 1.5e9, ssePx 2})` · `await tiles.ready` · `scene.add(tiles.group)`. 재질은 이미 DoubleSide(387) |
| `viewer.js:1823-1835` `pickMesh()` | `tiles.raycast(origin, dir, Infinity, false)` + `tiles.faceNormal(hit)` + `ray.ray.at()`. 반환 형태 `{point, normal}` 유지. 법선은 시점 쪽으로 뒤집기(타일은 감기 보장 없음) |
| `viewer.js:1569-1594` `fitSizes()` | `Box3.setFromObject(globalMesh)` → `tiles.box`/`tiles.sphere`. `setTimeout(fitSizes, 500)` 제거 |
| `viewer.js:1614-1626` 어두운 메시 토글 | `globalMesh.material` → 타일셋에 넘긴 `mat` 하나 |
| `viewer.js:2374` 렌더 루프 | `camera.updateMatrixWorld(); tiles.update(camera, H(), moving)` — `moving` 은 `anim`/`updateFly` 에서 유도 |
| 가림 검사(선택) | main 은 가림 검사가 없다(1223-1224: 전수 레이캐스트 116초). `tiles.raycast(..., anyHit=true)` 로 viewer-simple 의 `cameraSeesPoint()` 를 가져오면 새로 가능해진다 |

### 3. 진행률
main 은 `progress(c, t)` 규약. 지금은 저장 단계에서 타일 진행률을 0~100 으로 한 번 더 보고한다.
분석 단계와 합쳐 한 번만 차게 하려면 `_run_job`(backend.py) 에서 단계 가중치를 두면 된다.

### 4. `KEEP_MESH_PLY = False` 로 내리기
1·2 가 끝나면 내린다. 옛 보고서(mesh.ply 만 있음)는 `legacySource()` 경로로 계속 열린다.

### 5. 문서
- `docs/DEVELOPER_GUIDE.md` §6(.twin 형식 373-418) · §7-1 라우트(436-454) · §8-1 뷰어 블록(490-503) · 조정표(605-619)
- `README.md` 101-110(.twin 표) · 86-97(파일 목록)
- `docs/USER_GUIDE.md` 125-146 (PLY 면 수 줄이기 안내 삭제)
- `docs/CHANGELOG.md` `[Unreleased]`

## 검증 방법
- 작은 보고서: viewer-simple 의 `reports/choco_tiled.twin` 생성 절차(`tiler.build_tiles` + 기존 img 복사)를 참고.
- 큰 보고서: `py tiler.py in.ply out.zip` 으로 타일만 만든 zip 을 `/viewer?report=` 로 열면 카메라 없이 메시만 확인된다.
- 뷰어 콘솔에서 `window.__tiles.stats` (viewer-simple 처럼 디버그 훅을 달 것).
