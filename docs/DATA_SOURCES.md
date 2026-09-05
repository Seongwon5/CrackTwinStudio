# 데이터 · 구성요소 출처 (Data Sources & Attribution)

CrackTwinStudio 에 포함되거나 사용되는 모델·코드·데이터의 출처를 정리한다.
재배포·상용 이용 전에는 각 항목의 **원본 라이선스**를 반드시 확인할 것.

---

## 1. 크랙 검출 모델 — `model/conglomerate.pt`

- **정체**: DeepLabV3+ (ResNet-101 백본, output_stride=8) 기반 콘크리트 크랙
  세그멘테이션 모델. 출력 4채널 중 **채널 1 = 크랙**.
- **계보**: Virginia Tech / beric7 `concrete_crack_detection` 의
  **Conglomerate** 콘크리트 크랙 가중치.
  - 레포: https://github.com/beric7/concrete_crack_detection
  - 가중치(Conglomerate): https://doi.org/10.7294/16628596
  - (참고, 전역 장면용 LCW 가중치: https://doi.org/10.7294/16628707 )
- **본 프로젝트 처리**: 파일은 FP32 원본(약 236MB) 그대로 쓰고, GPU 추론에서만
  실행 중 `.half()` 로 FP16 변환한다(속도↑·VRAM↓, 파일은 그대로).
- **이력**: 2.0.3 까지는 `model/best.pt`(FP16, 약 118MB)를 썼다. 2.0.4 에서
  `conglomerate.pt` 로 교체했다. 두 파일은 **FP16 반올림으로 설명되지 않는 차이**가
  있으므로(파라미터 최대 편차 0.023 ≈ FP16 ulp 의 10배 이상) 같은 체크포인트가 아니다 —
  교체하면 검출 결과가 달라진다. 임계값 `pipeline.CRACK_THR` 는 재검증 대상이다.

## 2. 신경망 아키텍처 — `network/` 패키지

- 가중치(전체 모델 객체) 언피클에 필요한 DeepLabV3+ 구현.
- **출처**: VainF, *DeepLabV3Plus-Pytorch*
  - https://github.com/VainF/DeepLabV3Plus-Pytorch (MIT License)
  - beric7 레포의 `Training - Testing/network` 로 재배포된 것을 그대로 포함.

## 3. 3D 렌더링 엔진 — `libs/`

- **three.js r160** (오프라인 번들: `three.module.js`, `OrbitControls`, `PLYLoader`)
  - https://github.com/mrdoob/three.js (MIT License)
  - 출처: unpkg CDN (`three@0.160.0`).

## 4. 학습 데이터 출처 (모델 훈련에 쓰인 데이터)

Conglomerate 데이터셋은 여러 공개 콘크리트 크랙 데이터셋을 합친 것이다.
- 통합 데이터셋(Conglomerate): https://doi.org/10.7294/16625056
- 구성에 포함된 대표 공개 데이터셋(일부): **CRACK500, GAPs384, CFD, AEL,
  cracktree200, DeepCrack** 등. 각 데이터셋은 원저자의 라이선스·인용 요건을 따른다.
- 정확한 구성·라이선스는 위 figshare 데이터셋 페이지 및 각 원본 데이터셋을 참조.

## 5. 파이프라인 입력 데이터 (사용자 제공, 미포함)

- **이미지 + XMP**: 사진측량 소프트웨어 **3Dflow Zephyr** 등에서 내보낸 카메라
  내부/외부 파라미터(XMP)와 원본 이미지. https://www.3dflow.net/
- **PLY 메시**: 동일 사진측량 결과물. 좌표는 **1 unit = 1 m(미터)** 로 스케일되어
  있어야 실척(mm) 폭·길이 산출이 성립한다.
- 이 입력물은 본 패키지에 포함되지 않으며, 사용자의 데이터·권리에 속한다.

## 6. 파이썬 라이브러리 (런타임 의존성, 미포함 · pip 설치)

| 라이브러리 | 용도 | 라이선스(대략) |
|---|---|---|
| PyTorch / torchvision | 크랙 추론 | BSD |
| trimesh | 메시 로드·광선 교차 | MIT |
| OpenCV (opencv-python) | 이미지 처리·왜곡보정 | Apache-2.0 |
| scikit-image | 스켈레톤화 | BSD |
| scipy / numpy | 수치 연산 | BSD |
| Pillow | 이미지·EXIF | HPND(PIL) |
| pywebview | 네이티브 창 | BSD |

---

### 요약
- 포함된 학습 산출물(`conglomerate.pt`)·아키텍처(`network`)는 **VT/beric7 + VainF** 계열,
  3D 엔진은 **three.js(MIT)** 이다.
- 학습 데이터의 근본 출처는 **Conglomerate(figshare) 및 그 구성 공개 데이터셋들**.
- 사용자 입력(이미지/XMP/PLY)은 별도 소유물이며 미포함.
- **재배포 시**: conglomerate.pt·network(원 라이선스), three.js(MIT), 구성 데이터셋의
  인용/라이선스 조건을 확인·준수할 것.
