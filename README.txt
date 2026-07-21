크랙 디지털 트윈 스튜디오 (CrackTwinStudio)
=============================================

원전 격납 콘크리트 등의 이미지+XMP(카메라 파라미터)와 3D 메시(PLY)를 받아
best.pt(DeepLabV3)로 크랙을 검출하고, 3D로 역투영해 실척(mm) 폭·길이를 산출한
자체완결형 보고서(.twin)를 만들고 3D 뷰어로 점검하는 프로그램.

■ 실행
    py app.py
  → 대시보드 창이 열림.
    ① 보고서 만들기 : "이미지+XMP 폴더" + "PLY 메시" 선택 → 분석 시작 → reports/*.twin
    ② 보고서 열기   : 목록에서 열기(또는 .twin 파일 선택) → 각각 새 창으로 (여러 개 동시)

■ 필요 패키지 (한 번만)
    pip install pywebview numpy opencv-python torch torchvision trimesh pillow scikit-image scipy embreex rtree
  (GPU 사용 시 CUDA 지원 torch. GPU 없으면 CPU로도 동작하나 느림)

  ※ embreex 는 필수. 없으면 trimesh 가 순수 numpy 브루트포스로 조용히 폴백해
     역투영에서 "Unable to allocate ... MiB" 로 전 이미지가 실패한다(빈 보고서가 생성됨).
     확인:  py -c "import trimesh.ray; print(trimesh.ray.has_embree)"  → True 여야 정상.
     (pyembree 는 trimesh 3.x 시절의 폐기된 이름이라 4.x 에선 소용없음)

■ 폴더 구성 (전부 이 폴더 안에 자체완결)
    app.py        실행 진입점 (대시보드 + 멀티윈도우)
    backend.py    로컬 서버 (대시보드/뷰어/보고서 API)
    pipeline.py   분석 파이프라인 (기하·크랙추론·정량화·보고서) 통합 모듈
    ui/           대시보드 + three.js 뷰어(viewer.html)
    libs/         three.js 오프라인 번들 (인터넷 불필요)
    network/      best.pt 언피클용 신경망 패키지
    model/best.pt 크랙 검출 가중치 (FP16, 약 118MB)
    reports/      생성된 .twin 보고서

■ 뷰어 조작
    드래그: 회전 · 휠: 확대 · 우클릭: 이동
    파란 카메라 클릭 → 그 사진 시점으로 정확 정렬(움직이면 물체중심 회전으로 전환)
    빨간 크랙 핫스팟 클릭 → 대표 크랙 이미지 확대 + 실척 폭/길이
    우측 이미지: 좌클릭=시점 이동 · 우클릭=원본 크게 보기(점 클릭 시 3D 좌표)
    측정 도구: 거리/반경 (1 단위 = 1 m)

■ 데이터·구성요소 출처
    포함된 크랙 모델(best.pt)·신경망·three.js·학습 데이터의 출처와 라이선스는
    DATA_SOURCES.md 참조. (재배포 전 각 원본 라이선스 확인 필수)
