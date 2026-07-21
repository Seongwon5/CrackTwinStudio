# 설계: .twin v2 — 컨테이너 포맷 + 스트리밍 서빙

작성 2026-07-15 · 상태: **설계(미구현)**

## 1. 해결하려는 것

`3D Model.twin`(2.30GB)이 열리지 않는다. 원인은 **하드 한계 3개**이고, 전부 "메모리를 늘리면 된다"가
아니다.

| # | 벽 | 한계 | 현재 | 위치 |
|---|---|---|---|---|
| 1 | **V8 최대 문자열** | **536.9MB** (`2^29−24`) | 2,300.6MB (**4.29×**) | `viewer.html:203` `const DATA = __DATA__;` |
| 2 | **`window.atob`** | 문자열 한계 + 바이트 루프 | base64 1.6GB | `viewer.html:168` `base64ToArrayBuffer` |
| 3 | Python 피크 메모리 | 여유 13GB | ~11.5GB + json 객체 | `backend.render_viewer_html` |

### 1-1. 왜 인라인이 근본 문제인가
```python
# backend.py — 현재
data_str = open(twin).read()                    # 2.3GB 파일 → str 4.6GB (한글 1,173B 때문에 UCS-2)
scene    = json.loads(data_str)                 # 카메라/핫스팟 '개수'만 쓰면서 2.3GB 전체를 파싱
return html.replace("__DATA__", data_str)       # 또 4.6GB 사본
# → _send() 에서 .encode("utf-8") → 또 2.3GB
```
```js
// viewer.html — 현재
const DATA = __DATA__;                          // .twin 전체가 JS '소스 코드'
const buf = base64ToArrayBuffer(DATA.mesh_base64);   // atob(1.6GB) → 불가능
```

`.twin` 크기가 커지면 **반드시** 터진다. 상한은 약 **530MB**.

### 1-2. 지금 데이터로는 상한을 넘길 방법이 없다
```
이미지 1,288장 × 0.43MB = 554MB   ← 메시가 0이어도 이미 536.9MB 초과
```
**데이터를 줄이는 방식(선택지 A)으로는 원본 확대 이미지를 포기해야만 들어간다.** 구조를 고쳐야 한다.

---

## 1-3. 전제 검증 (실측 완료)

이 설계는 두 가정 위에 서 있다. 둘 다 **이 환경에서 직접 측정해 확인**했다.

**① 브라우저: `ArrayBuffer` 는 문자열 한계와 무관한가?** → **그렇다**
```
ArrayBuffer  512MB : ok
ArrayBuffer  800MB : ok
ArrayBuffer 1200MB : ok
ArrayBuffer 1600MB : ok          ← 메시(1.2GB)를 담을 수 있다
String       800MB : FAIL "Invalid string length"   ← 같은 크기에서 문자열은 죽는다
```
→ 메시를 base64 문자열이 아닌 **바이너리 ArrayBuffer 로 받으면 한계가 사라진다.**

**② Python: `zipfile` 이 멤버를 스트리밍할 수 있는가?** → **그렇다**
```
[ZipFile.open + 1MB 청크] 419MB 멤버 전송 → 피크 메모리   2.2 MB   (크기와 무관)
[ZipFile.read() 통째로 ]  대조군          → 피크 메모리 419.4 MB
                                          → 195배 차이
[namelist()]              멤버 목록       → 0.7ms (중앙 디렉터리만 읽음)
[ZipFile.open(.., "w", force_zip64=True)] 스트리밍 쓰기도 동작
```
→ 백엔드 피크 메모리를 **파일 크기와 무관**하게 만들 수 있다.

---

## 2. 설계 원칙

1. **큰 덩어리는 문자열/JSON 을 절대 거치지 않는다** — 바이너리로 스트리밍
2. **`.twin` 은 여전히 단일 파일** — 제품 요구사항인 "자체완결" 유지
3. 백엔드 피크 메모리는 파일 크기와 **무관**해야 한다
4. 기존 보고서를 깨지 않는다

---

## 3. 포맷: `.twin` v2 = ZIP 컨테이너

```
report.twin                      ← 여전히 파일 하나 (magic: PK\x03\x04)
├── meta.json                    ZIP_DEFLATED  ~40MB   기하만 (이미지·메시 제외)
├── mesh.ply                     ZIP_DEFLATED  ~0.5GB  원본 바이너리 (base64 없음)
└── img/
    ├── 0000_thumb.jpg           ZIP_STORED            JPEG 재압축은 무의미
    ├── 0000_full.jpg            ZIP_STORED
    └── ...                      (1288 × 2 = 2,576 개)
```

**ZIP 을 고른 이유**
- `zipfile` 은 **표준 라이브러리** (오프라인 자체완결 원칙 유지, 새 의존성 0)
- `ZipFile.open(name)` 이 **멤버별 스트리밍 읽기**를 지원 → 시크 후 필요한 부분만
- ZIP64 자동 → 4GB 초과 대응
- 단일 파일 유지 + 사용자가 직접 열어 내용 확인 가능(디버깅)

**압축 선택**
- `mesh.ply` → DEFLATE (바이너리 기하는 잘 줄어듦)
- `img/*.jpg` → **STORED** (JPEG 은 이미 압축됨 → DEFLATE 는 CPU 만 태우고 이득 ~0)
- `meta.json` → DEFLATE (숫자 텍스트라 잘 줄어듦)

### 3-1. `meta.json`
`cameras` 에서 `thumb`/`full` **데이터 URI 를 제거**하고 나머지 기하만 남긴다.
```jsonc
{
  "version": 2,
  "counts": { "cameras": 1288, "hotspots": 4210 },
  "text_points": [[x,y,z], ...],
  "cameras": [
    { "id": 0, "source": "A (1).JPG",
      "pos": [...], "fwd": [...], "up": [...], "right": [...],
      "fov": 41.2, "aspect": 1.5, "w": 5472, "h": 3648,
      "fx": ..., "fy": ..., "cx": ..., "cy": ... }
    // thumb/full 없음 → id 로 /api/report/img 에서 가져온다
  ],
  "hotspots": [ { "text":..., "conf":..., "xyz":..., "width_mm":..., "poly": [...], "views": [...] } ]
}
```
크기 추정: `text_points` 는 복셀 다운샘플(`TEXTPOINT_MERGE_RATIO`)로 상한이 있어
1288장 기준 ~135K점 ≈ 5MB, `hotspots`(poly 포함) ~20MB → **meta.json ≈ 30~50MB.**
`JSON.parse` 에 전혀 문제없다 (한계 536.9MB 대비 여유 10배 이상).

> 가드: `meta.json` 이 300MB 를 넘으면 경고 로그. 현실적으로 도달 불가하지만 한계는 남아 있다.

---

## 4. 백엔드 API

```
GET /viewer?report=<path>                            → viewer.html (경로만 주입, 수 KB)
GET /api/report/meta?p=<path>                        → meta.json           application/json
GET /api/report/mesh?p=<path>                        → mesh.ply            application/octet-stream
GET /api/report/img?p=<path>&id=<N>&kind=thumb|full  → JPEG                image/jpeg
```

전부 **스트리밍**. 피크 메모리는 파일 크기와 무관(청크 크기만큼).

```python
# backend.py — 신규
import zipfile, shutil

def _twin_version(path):
    """매직바이트로 v2(zip) / v1(legacy json) 판별."""
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:4] == b"PK\x03\x04":
        return 2
    if head[:1] == b"{":
        return 1
    raise ValueError("알 수 없는 .twin 형식")

def _send_member(self, twin_path, member, ctype):
    """zip 멤버를 메모리에 올리지 않고 소켓으로 흘려보낸다."""
    with zipfile.ZipFile(twin_path) as z:
        try:
            info = z.getinfo(member)
        except KeyError:
            return self._send(404, f"멤버 없음: {member}", "text/plain; charset=utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(info.file_size))   # 진행률 표시용
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with z.open(member) as src:
            shutil.copyfileobj(src, self.wfile, 1 << 20)           # 1MB 청크
```

`render_viewer_html` 은 **경로만 주입**하도록 축소:
```python
def render_viewer_html(twin_path):
    with open(os.path.join(UI_DIR, "viewer.html"), "r", encoding="utf-8") as f:
        html = f.read()
    return html.replace("__REPORT__", json.dumps(twin_path))   # JS 문자열 리터럴로 안전하게
```
`json.loads(data_str)` 로 개수를 세던 부분은 사라진다 → `__N__`/`__H__` 는 `meta.counts` 에서 온다.

---

## 5. 뷰어 변경

`DATA` 참조는 **5곳뿐**이라 변경 범위가 작다.

| 위치 | 현재 | v2 |
|---|---|---|
| `:216` | `base64ToArrayBuffer(DATA.mesh_base64)` | `await fetch(mesh).arrayBuffer()` |
| `:253,255` | `DATA.text_points` | 동일 (meta 에서) |
| `:319` | `DATA.hotspots` | 동일 (meta 에서) |
| `:372` | `DATA.cameras` | 동일 (meta 에서) |
| `:168` | `base64ToArrayBuffer` | **삭제** |
| `:175` | `openFullImage(base64Data,…)` | URL 기반으로 변경 |

```js
const REPORT = __REPORT__;                  // 경로 문자열 하나만 주입
const Q = encodeURIComponent(REPORT);
const api = (k, extra="") => `/api/report/${k}?p=${Q}${extra}`;

// 1) 메타 (작음)
const DATA = await (await fetch(api("meta"))).json();

// 2) 메시 — ArrayBuffer 는 문자열 한계와 무관, atob 도 불필요
const geo = new PLYLoader().parse(await (await fetch(api("mesh"))).arrayBuffer());
geo.computeVertexNormals();

// 3) 이미지 — 지연 로드 (전부 메모리에 올리지 않는다)
imgEl.src = api("img", `&id=${cam.id}&kind=thumb`);
```

`DATA.cameras[].thumb/full` 을 쓰던 자리는 전부 `api("img", ...)` URL 로 치환.
브라우저가 이미지 캐시/지연로드를 알아서 처리 → **1288장을 한 번에 메모리에 올리던 것도 해소**.

전체 스크립트를 `async` 진입점으로 감싸고, 로딩 중 표시를 둔다.

---

## 6. 생성 단계 (`pipeline.py`)

현재 생성도 메모리를 많이 쓴다:
```python
ply_b64 = base64.b64encode(open(ply,'rb').read()).decode()   # 1.2GB → 1.6GB str
cam["thumb"] = encode_jpeg_datauri(...)                       # 전 카메라 data URI 를 RAM 에 축적
json.dump(scene_data, f)                                      # 직렬화
```

v2 에서는 **base64 자체가 사라진다.**
```python
def save_report_v2(out_path, ply_path, cams, hotspots, text_points):
    with zipfile.ZipFile(out_path, "w", allowZip64=True) as z:
        z.write(ply_path, "mesh.ply", compress_type=zipfile.ZIP_DEFLATED)   # 스트리밍 복사
        for c in cams:
            z.writestr(f"img/{c['id']:04d}_thumb.jpg", c.pop("_thumb"), zipfile.ZIP_STORED)
            z.writestr(f"img/{c['id']:04d}_full.jpg",  c.pop("_full"),  zipfile.ZIP_STORED)
        meta = {"version": 2, "counts": {"cameras": len(cams), "hotspots": len(hotspots)},
                "text_points": text_points, "cameras": cams, "hotspots": hotspots}
        z.writestr("meta.json", json.dumps(meta, ensure_ascii=False), zipfile.ZIP_DEFLATED)
```
`encode_jpeg_datauri` → `encode_jpeg_bytes` 로 교체 (data URI 접두사·base64 제거).

**추가 개선(권장):** zip 을 루프 시작 시 열고 이미지를 **처리하는 즉시 기록** → 피크 메모리가
이미지 1장 수준으로 떨어진다(현재는 전 이미지 누적).

---

## 7. 하위 호환

```
매직바이트 판별
  PK\x03\x04  → v2 (zip)      → 신규 경로
  {           → v1 (legacy)   → 크기에 따라 분기
```
- **v1 & < 400MB** → 기존 인라인 경로 그대로 유지 (`hammer.twin` 128MB, `line_test` 등 계속 열림)
- **v1 & ≥ 400MB** → 열기 거부 + 안내: *"이 보고서는 구형식이고 브라우저 한계(537MB)를 넘습니다.
  `py tools/convert_twin.py <경로>` 로 변환하거나 재생성하세요."*

### 7-1. 변환기 `tools/convert_twin.py` (v1 → v2, 선택)
1288장 재생성은 ~107분이지만 변환은 I/O 바운드라 수 분이다.

스트리밍 스캔으로 처리 가능하다 — **base64/data URI 에는 따옴표도 이스케이프도 없어서**
따옴표만 찾으면 값 경계가 정확히 나온다 (이번 조사에서 이 방식으로 2.3GB 를 안전하게 스캔했다).
또 `save_report` 가 dict 순서대로 쓰므로 최상위 키 순서가 고정이다:
`mesh_base64` → `text_points` → `cameras` → `hotspots`.

```
1. b'"mesh_base64": "' 위치 탐색 → 닫는 따옴표까지 청크 단위 base64 디코드 → zip 에 mesh.ply 기록
2. b'"thumb": "' / b'"full": "' 를 순차 탐색 → data URI 접두사 제거 → 디코드 → img/*.jpg 기록
3. 위 구간을 제외한 나머지를 이어붙여 meta.json 재구성
```

---

## 8. 예상 효과

| | 현재 (v1) | v2 |
|---|---|---|
| `3D Model.twin` | **2.30 GB** | **~1.0 GB** (base64 33% 제거 + deflate) |
| 열림 여부 | ✗ 불가 (4.29× 초과) | ✓ **크기 무관** |
| 백엔드 피크 | ~11.5 GB | **수십 MB** (스트리밍) |
| 브라우저 피크 | 불가 | 메시 ArrayBuffer + 보이는 이미지만 |
| 생성 피크 | 수 GB | 이미지 1장 수준 |
| `hammer.twin` | 128MB ✓ | ~95MB ✓ |

---

## 9. ⚠️ B가 해결하지 **못하는** 것

**포맷을 고쳐도 3,726만 면은 여전히 렌더링 부담이다.**
```
three.js 버퍼 추정:
  position  18.6M × 3 × 4 =  223 MB
  color     18.6M × 3     =   56 MB
  normal    18.6M × 3 × 4 =  223 MB   (computeVertexNormals 로 생성)
  index     37.3M × 3 × 4 =  447 MB
  ─────────────────────────────────
  합계                     ≈ 950 MB   GPU 버퍼
```
**열리기는 하지만 쾌적하진 않을 것이다. 메시 데시메이션은 별개로 필요하다.**

또한 `damcrack.ply` 자체에 **37% 낭비**가 있다 (CHANGELOG 참조):
- 좌표가 `double` → three.js 는 어차피 Float32 로 변환 → **223.6MB 낭비**
- 법선을 저장하는데 뷰어가 `computeVertexNormals()` 로 재계산 → **223.6MB 전부 낭비**

→ `float` 좌표 + 법선 제거로 재저장하면 1.21GB → **764MB** (즉시 37% 감소, 화질 영향 없음)

---

## 10. 작업 순서 (제안)

| 단계 | 내용 | 효과 | 위험 |
|---|---|---|---|
| **1** | 백엔드 스트리밍 엔드포인트 + 매직바이트 분기 | — | 낮음 (기존 경로 유지) |
| **2** | 뷰어를 async fetch 로 전환 (`DATA` 5곳 + `base64ToArrayBuffer` 삭제) | **열림** | 중간 (뷰어 전반 검증 필요) |
| **3** | `save_report_v2` (zip 기록) + `encode_jpeg_bytes` | 생성 메모리·용량 | 낮음 |
| **4** | `tools/convert_twin.py` (v1→v2) | 재생성 불필요 | 낮음 (선택) |
| **5** | 메시 데시메이션 파이프라인 (별건) | **렌더 성능** | 별도 논의 |

1~3 만으로 `3D Model.twin` 이 열린다. 4 는 편의, 5 는 성능.

## 11. 검증 계획

- `hammer.twin`(v1 128MB) → 변환 후 v2 로 열어 **핫스팟/카메라/측정값이 동일**한지 대조
- `3D Model.twin`(v1 2.3GB) → 변환 → 열림 확인 + 백엔드 피크 메모리 측정
- 스트리밍 확인: 요청 중 `backend` 프로세스 RSS 가 파일 크기와 무관하게 유지되는지
- 회귀: 기존 소형 v1 보고서(`line_test`, `pkg_test`)가 계속 열리는지
