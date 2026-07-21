# CrackTwinStudio — 크랙 디지털 트윈 스튜디오

드론·카메라로 촬영한 콘크리트 구조물 사진에서 **AI로 균열을 픽셀 단위로 검출**하고,
포토그래메트리로 복원한 **3D 표면에 정확히 역투영**하여, 실척(mm) 폭·길이를 산출한
**자체완결형 보고서(`.twin`)** 를 만들고 웹 3D 뷰어로 점검하는 프로그램입니다.

> 2D 이미지 균열 검출 → 3D 표면 매핑 → 다시점 융합 → 인터랙티브 3D 뷰어

---

## 파이프라인 개요

데이터는 한 방향으로 흐르며, 각 단계의 출력이 다음 단계의 입력이 됩니다.

| 단계 | 동작 | 입력 → 출력 |
|---|---|---|
| ① 촬영·재구성 | 포토그래메트리(3DF Zephyr 등) | 다시점 사진 → 3D 메시(PLY) + 카메라 포즈(XMP) |
| ② AI 검출 | 타일 추론 세그멘테이션(DeepLabV3+, FP16) | 원본 사진 → 균열 확률맵·이진 마스크 |
| ③ 정량화 | 컴포넌트·골격·직교 폭 | 마스크 → 중심선 + 폭 프로파일 + 길이 |
| ④ 3D 역투영 | ray-casting(왜곡 보정) | 중심선 uv + 포즈 + 메시 → 3D 좌표·실척 |
| ⑤ 융합·병합 | 다시점 융합 + DBSCAN | 여러 사진의 크랙 → 3D 객체 1개 + 핫스팟 |
| ⑥ 시각화 | 웹 3D 뷰어(three.js) | 형상 + 메시 + 이미지 → 인터랙티브 뷰어 |

좌표 규약: `X_cam = R·(X_world − C)`, `C = −Rᵀ·T`. 역투영은 `undistort(K,dist) → 카메라 광선 → Rᵀ → ray–mesh 교차`.

> **핵심 전제:** PLY의 `1 unit = 1 m`. 모든 mm 측정(폭·길이·스케일맵)이 이 스케일 전제에 의존합니다.

---

## 설치

**필요 패키지 (한 번만):**

```bash
pip install -r requirements.txt
```

- GPU 사용 시 CUDA 지원 `torch`를 설치하세요. GPU가 없어도 CPU로 동작하나 느립니다.
- **`embreex`는 사실상 필수입니다.** 없으면 trimesh가 순수 numpy 브루트포스로 조용히 폴백해
  역투영에서 `Unable to allocate ... MiB`로 전 이미지가 실패하고 **빈 보고서**가 생성됩니다.
  확인: `python -c "import trimesh.ray; print(trimesh.ray.has_embree)"` → `True` 여야 정상.
  (`pyembree`는 trimesh 3.x 시절의 폐기된 이름이라 4.x에선 소용없습니다.)

**모델 가중치 (`model/best.pt`):**
이 저장소는 **Git LFS**로 가중치를 포함합니다. clone 전에 LFS를 설치하세요.

```bash
git lfs install
git clone <repo-url>
```

가중치의 원본 출처(VT/beric7 Conglomerate)와 FP16 변환 내역은 [`DATA_SOURCES.md`](DATA_SOURCES.md)를 참조하세요.

---

## 실행

```bash
python app.py
```

대시보드 창이 열립니다.

1. **보고서 만들기** — "이미지+XMP 폴더" + "PLY 메시" 선택 → 분석 시작 → `reports/*.twin`
2. **보고서 열기** — 목록에서 열기(또는 `.twin` 파일 선택) → 각각 새 창으로(여러 개 동시)

### 뷰어 조작

| 조작 | 동작 |
|---|---|
| 드래그 / 휠 / 우클릭 | 회전 / 확대 / 이동 |
| 파란 카메라 클릭 | 그 사진 시점으로 정확 정렬(이동 시 물체중심 회전으로 전환) |
| 크랙(형상) 클릭 | 대표 크랙 이미지 확대 + 실척 평균 폭·총 길이(최대·p95) |
| 크랙 호버 | 평균 폭·총 길이 말풍선, 해당 크랙 흰색 강조 |
| 우측 이미지 | 좌클릭=시점 이동 · 우클릭=원본 확대(점 클릭 시 3D 좌표) |
| 측정 도구 | 거리 / 반경 (1 unit = 1 m) |

균열 형상은 **심각도(상대 순위)에 따라 색이 다르며**, 검출 마스크 오버레이를 켜고 끌 수 있습니다.

---

## 프로젝트 구성

```
app.py            실행 진입점 (대시보드 + 멀티윈도우)
backend.py        로컬 서버 (대시보드/뷰어/보고서 API)
pipeline.py       분석 파이프라인 (기하·크랙추론·정량화·융합·보고서) 통합 모듈
ui/               대시보드 + three.js 뷰어(viewer.html)
libs/             three.js r160 오프라인 번들 (인터넷 불필요)
network/          best.pt 언피클용 DeepLabV3+ 신경망 패키지
model/best.pt     크랙 검출 가중치 (FP16, 약 113MB · Git LFS)
tools/            보조 스크립트 (convert_twin.py 등)
reports/          생성된 .twin 보고서 (Git 미포함)
```

### `.twin` 포맷

단일 ZIP 컨테이너입니다(손상 방지 원자적 쓰기).

| 파일/키 | 내용 |
|---|---|
| `mesh.ply` | 원본 메시 |
| `img/NNNN_thumb.jpg` / `_full.jpg` | 썸네일 / 원본(≤1920px) |
| `img/NNNN_mask.png` | 검출 마스크 RGBA(균열만 채색), full과 픽셀 일치 |
| `meta.json` | 카메라 포즈 · 크랙 형상(ribbons) · 핫스팟(통계) 등 기하·메타 |

---

## 알려진 한계

- **메트릭 스케일 캘리브레이션 (최우선 과제).** 폭·길이가 모두 메시 실척(1 unit = 1 m)
  전제에 의존합니다. 알려진 실측 치수로 검증하기 전까지 mm 절대값은 신뢰 불가하며
  `meta.json`에 `scale_warning`으로 표시됩니다. (상대 순위·형상은 이미 유효합니다.)
- 뷰 간 정합 오차가 병합·융합 거리 파라미터(eps·복셀)의 한계를 결정합니다.

---

## 라이선스 · 출처

이 저장소에는 제3자 구성요소가 포함됩니다 — 재배포·상용 이용 전 각 원본 라이선스를 확인하세요.

- **크랙 검출 모델 `model/best.pt`** — DeepLabV3+ 기반, VT/beric7 *Conglomerate* 가중치 계열(FP16 변환).
- **신경망 `network/`** — [VainF/DeepLabV3Plus-Pytorch](https://github.com/VainF/DeepLabV3Plus-Pytorch) (MIT).
- **3D 엔진 `libs/`** — [three.js](https://github.com/mrdoob/three.js) r160 (MIT).

전체 출처·계보·학습 데이터셋과 라이선스 요건은 **[`DATA_SOURCES.md`](DATA_SOURCES.md)** 를 참조하세요.
파이프라인 단계별 상세 설계 근거는 함께 배포된 상세 보고서를 참조하세요.

> 사용자 입력물(이미지·XMP·PLY 및 생성된 `.twin` 보고서)은 사용자 소유이며 이 저장소에 포함되지 않습니다.
