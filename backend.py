# -*- coding: utf-8 -*-
"""
backend.py  ―  로컬 백엔드 (대시보드/뷰어/three.js 제공 + 보고서 생성/열람 API)
================================================================================
브라우저(pywebview 창)가 접속하는 localhost 서버. 원본 이미지 분석(크랙 검출) 작업,
.twin 보고서 목록, 그리고 .twin 을 조각내어(메타/메시/이미지) 스트리밍 제공한다.

보고서를 HTML 에 인라인하지 않는 이유(중요):
  예전에는 .twin 전체를 viewer.html 의 `const DATA = __DATA__;` 자리에 끼워넣었다. 이 방식은
  파일이 커지면 반드시 터진다 —
    · V8 최대 문자열 = 536,870,888 자(536.9MB). 넘으면 "Invalid string length" (RAM 과 무관한 하드 한계)
    · window.atob(거대 base64) 도 같은 한계에 걸림
    · 파이썬도 read → json.loads → replace → encode 로 파일 크기의 5배 안팎을 씀
  그래서 큰 덩어리(메시·이미지)는 문자열/JSON 을 거치지 않고 바이너리로 스트리밍한다.
  ArrayBuffer 는 문자열 한계와 무관해 GB 급도 담긴다(실측 확인).
"""
import os
import re
import json
import uuid
import shutil
import zipfile
import threading
import argparse
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pipeline as P

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR, LIBS_DIR, REPORTS_DIR = os.path.join(HERE, "ui"), os.path.join(HERE, "libs"), os.path.join(HERE, "reports")
_jobs = {}                       # job_id -> {progress, done, error, result, log[]}

import sys as _sys
_sys.path.insert(0, os.path.join(HERE, "tools"))
import compare_twins as CT2      # 검사 간 비교(정합·매칭·판정) — 대시보드 '검사 비교' 가 사용


# (제거됨) 유형 분류기 재학습 잡 — 유형 분류 폐지와 함께 삭제. 검토 사이드카의
# 확인/오탐 라벨은 계속 쌓이며, 검출(세그멘테이션) 모델 재학습의 학습 데이터가 된다.


def _run_export_job(job_id, twin, out_pdf, rows, photos):
    """백그라운드 PDF 보고서 생성(표지·요약/위치도/대장/사진 대지)."""
    j = _jobs[job_id]
    # 잡 로그에 한 줄 추가. 최근 200줄만 남긴다(장시간 분석에서 무한 증가 방지).
    def log(m): j["log"].append(str(m)); j["log"][:] = j["log"][-200:]
    try:
        import export_report as ER
        ER.build_pdf(twin, out_pdf, log=log, max_rows=rows, top_photos=photos)
        j["result"] = out_pdf; j["progress"] = 1.0
    except Exception as e:
        import traceback; traceback.print_exc(); j["error"] = str(e)
    finally:
        j["done"] = True

def twin_version(path):
    """매직바이트로 형식 검증. 2 = zip 컨테이너(현행 유일 형식).
    구형식(v1: 단일 JSON)은 더 이상 지원하지 않는다 — 재분석해 새로 생성해야 한다."""
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:4] == b"PK\x03\x04":
        return 2
    if head[:1] == b"{":
        raise ValueError(
            "구형식(v1) 보고서는 더 이상 지원하지 않습니다: " + os.path.basename(path) + "\n"
            "  대시보드에서 원본 이미지·PLY 로 다시 분석해 새로 생성하세요.")
    raise ValueError("알 수 없는 .twin 형식입니다: " + os.path.basename(path))


def report_meta(path):
    """뷰어용 메타(JSON). 이미지·메시 같은 큰 덩어리는 빼고 기하만 담는다."""
    twin_version(path)                           # 형식 검증(구형식이면 여기서 예외)
    with _open_twin(path) as z:
        return z.read("meta.json").decode("utf-8")


# 타일 보고서(계층 LOD, tiler.py)는 뷰어가 수백~수천 개의 작은 요청을 보낸다. 요청마다 zip 중앙
# 디렉터리(항목 수천 개)를 다시 읽지 않도록 열어 둔 ZipFile 을 경로별로 재사용한다. 파일이 바뀌면
# (크기·수정시각) 새로 연다. ZipFile 은 open() 들을 락으로 직렬화하므로 여러 스레드가 함께 읽어도 된다.
# 열어 둔 파일은 Windows 에서 교체(os.replace)를 막으므로, 몇 초 안 쓰이면 닫는다 — 같은 이름으로
# 보고서를 다시 만들 때 생성이 실패하지 않게. (viewer-simple 브랜치와 동일 코드)
_ZIP_IDLE_SEC = 3.0
_zip_cache, _zip_lock = {}, threading.Lock()      # path -> [key, ZipFile, last_used, in_use]


class _Shared:
    """with 문으로 써도 닫지 않는 ZipFile 대리자 — 캐시가 소유하고, 쓰는 동안은 청소하지 않는다."""
    def __init__(self, ent): self.ent = ent
    def __enter__(self):
        with _zip_lock: self.ent[3] += 1
        return self.ent[1]
    def __exit__(self, *a):
        import time
        with _zip_lock: self.ent[3] -= 1; self.ent[2] = time.monotonic()
        return False


def _open_twin(path):
    import time
    st = os.stat(path)
    key = (st.st_size, st.st_mtime_ns)
    with _zip_lock:
        ent = _zip_cache.get(path)
        if ent and ent[0] == key:
            ent[2] = time.monotonic()
            return _Shared(ent)
        if ent and ent[3] == 0:
            _zip_cache.pop(path)
            try: ent[1].close()
            except Exception: pass
        ent = [key, zipfile.ZipFile(path), time.monotonic(), 0]
        _zip_cache[path] = ent
        return _Shared(ent)


def _zip_janitor():
    import time
    while True:
        time.sleep(1.0)
        with _zip_lock:
            now = time.monotonic()
            for p in [p for p, e in _zip_cache.items() if e[3] == 0 and now - e[2] > _ZIP_IDLE_SEC]:
                try: _zip_cache.pop(p)[1].close()
                except Exception: pass


threading.Thread(target=_zip_janitor, daemon=True).start()


def _ps_dialog(script):
    """PowerShell(-STA)로 Windows 네이티브 파일/폴더 다이얼로그를 띄우고 선택 경로 반환."""
    try:
        r = subprocess.run(["powershell", "-STA", "-NoProfile", "-NonInteractive",
                            "-WindowStyle", "Hidden", "-Command", script],
                           capture_output=True, text=True, timeout=300)
        return (r.stdout or "").strip() or None
    except Exception:
        return None


# 최신(Vista+) 탐색기 스타일 폴더 선택 — IFileOpenDialog + FOS_PICKFOLDERS.
# WinForms FolderBrowserDialog 는 .NET Framework(=Windows PowerShell)에서 구형 트리(작은 창)로만
# 떠서, 일반 파일 탐색기처럼 주소창·즐겨찾기·검색이 있는 공통 항목 대화상자를 COM 으로 띄운다.
_FOLDER_PS = r'''
$ErrorActionPreference='Stop'
$outFile = '__OUT__'
$code = @"
using System;
using System.Runtime.InteropServices;
namespace FS {
  [ComImport, Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")] internal class Dlg { }
  [ComImport, Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  internal interface ISI {
    void BindToHandler(IntPtr a, ref Guid b, ref Guid c, out IntPtr d); void GetParent(out ISI a);
    void GetDisplayName(uint a, out IntPtr b); void GetAttributes(uint a, out uint b); void Compare(ISI a, uint b, out int c);
  }
  [ComImport, Guid("d57c7288-d4ad-4768-be02-9d969532d960"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  internal interface IFOD {
    [PreserveSig] int Show(IntPtr parent);
    void SetFileTypes(uint a, IntPtr b); void SetFileTypeIndex(uint a); void GetFileTypeIndex(out uint a);
    void Advise(IntPtr a, out uint b); void Unadvise(uint a);
    void SetOptions(uint a); void GetOptions(out uint a);
    void SetDefaultFolder(IntPtr a); void SetFolder(IntPtr a); void GetFolder(out IntPtr a); void GetCurrentSelection(out IntPtr a);
    void SetFileName(string a); void GetFileName(out string a); void SetTitle(string a); void SetOkButtonLabel(string a);
    void SetFileNameLabel(string a); void GetResult(out ISI a); void AddPlace(IntPtr a, int b); void SetDefaultExtension(string a);
    void Close(int a); void SetClientGuid(ref Guid a); void ClearClientData(); void SetFilter(IntPtr a);
    void GetResults(out IntPtr a); void GetSelectedItems(out IntPtr a);
  }
  public static class P {
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    public static string Pick(string title) {
      var d = (IFOD)(new Dlg());
      uint o; d.GetOptions(out o);
      d.SetOptions(o | 0x20u | 0x40u);            // FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM
      if (!string.IsNullOrEmpty(title)) d.SetTitle(title);
      if (d.Show(GetForegroundWindow()) != 0) return null;
      ISI si; d.GetResult(out si);
      IntPtr pp; si.GetDisplayName(0x80058000u, out pp);   // SIGDN_FILESYSPATH
      string s = Marshal.PtrToStringUni(pp); Marshal.FreeCoTaskMem(pp); return s;
    }
  }
}
"@
$path = $null
try {
  Add-Type -TypeDefinition $code -Language CSharp
  $path = [FS.P]::Pick("이미지+XMP 폴더 선택")
} catch {
  Add-Type -AssemblyName System.Windows.Forms
  $f=New-Object System.Windows.Forms.Form; $f.TopMost=$true
  $b=New-Object System.Windows.Forms.FolderBrowserDialog
  if($b.ShowDialog($f) -eq [System.Windows.Forms.DialogResult]::OK){ $path=$b.SelectedPath }
  $f.Dispose()
}
if ($path) { [IO.File]::WriteAllText($outFile, $path, [System.Text.Encoding]::UTF8) }
'''


def pick_folder():
    """최신 탐색기 스타일 폴더 선택 다이얼로그 → 선택 경로. 실패 시 구형으로 폴백."""
    import tempfile
    fd, out = tempfile.mkstemp(suffix=".txt"); os.close(fd)
    fd, ps = tempfile.mkstemp(suffix=".ps1"); os.close(fd)
    try:
        with open(ps, "w", encoding="utf-8-sig") as f:      # BOM: PowerShell 이 한글 리터럴을 정확히 읽게
            f.write(_FOLDER_PS.replace("__OUT__", out.replace("'", "''")))
        subprocess.run(["powershell", "-STA", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", ps],
                       capture_output=True, timeout=300)
        with open(out, "r", encoding="utf-8-sig") as f:   # .NET UTF8 은 BOM 을 붙인다 → sig 로 제거
            return f.read().strip() or None
    except Exception:
        return None
    finally:
        for p in (out, ps):
            try: os.remove(p)
            except OSError: pass


def pick_file(kind):
    """파일 선택 다이얼로그(kind=ply|twin) → 선택 경로."""
    flt = {"ply": "Mesh (*.ply;*.obj)|*.ply;*.obj|All files (*.*)|*.*",
           "twin": "Report (*.twin)|*.twin|All files (*.*)|*.*"}.get(kind, "All files (*.*)|*.*")
    return _ps_dialog(
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$f=New-Object System.Windows.Forms.Form; $f.TopMost=$true;"
        "$d=New-Object System.Windows.Forms.OpenFileDialog;"
        f"$d.Filter='{flt}'; $d.Multiselect=$false;"
        "if($d.ShowDialog($f) -eq [System.Windows.Forms.DialogResult]::OK){[Console]::Out.Write($d.FileName)};$f.Dispose()")


def render_viewer_html(twin_path, asset=None):
    """뷰어 HTML 반환. 보고서 '경로'만 주입하고 데이터는 뷰어가 API 로 가져간다.
    (예전처럼 .twin 을 통째로 끼워넣으면 V8 문자열 한계 536.9MB 에서 터진다 — 모듈 docstring 참조)
    asset: compare_twins 의 asset.json(dict) — 주면 뷰어가 검사 간 비교 모드로 뜬다."""
    twin_version(twin_path)                      # 형식 검증(이상하면 여기서 예외)
    with open(os.path.join(UI_DIR, "viewer.html"), "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__REPORT__", json.dumps(twin_path))   # JS 문자열 리터럴로 안전하게 이스케이프
    return html.replace("__ASSET__", json.dumps(asset, ensure_ascii=False) if asset else "null")


def _run_compare_job(job_id, twin_a, twin_b, ida, idb, out_path, extend_asset=None):
    """백그라운드에서 검사 간 비교(asset.json 생성/확장)를 수행하고 로그/결과를 갱신한다.
    extend_asset 이 주어지면 새 자산을 만들지 않고 그 자산에 twin_b 를 N번째 검사로 승계."""
    j = _jobs[job_id]
    # 잡 로그에 한 줄 추가. 최근 200줄만 남긴다(장시간 분석에서 무한 증가 방지).
    def log(m): j["log"].append(str(m)); j["log"][:] = j["log"][-200:]
    try:
        if extend_asset:
            asset = CT2.extend(extend_asset, twin_b, idn=idb, log=log)
            j["result"] = extend_asset
        else:
            asset = CT2.run(twin_a, twin_b, out_path, ida=ida, idb=idb, log=log)
            j["result"] = out_path
        j["progress"] = 1.0
        j["stats"] = asset.get("_stats")
    except CT2.RegistrationError as e:
        j["error"] = str(e)
    except Exception as e:
        import traceback; traceback.print_exc(); j["error"] = str(e)
    finally:
        j["done"] = True


def _run_job(job_id, folder, ply, out_path, detect):
    """백그라운드에서 크랙 분석 보고서를 생성하고 진행률/로그/결과를 갱신한다."""
    j = _jobs[job_id]
    # 잡 로그에 한 줄 추가. 최근 200줄만 남긴다(장시간 분석에서 무한 증가 방지).
    def log(m): j["log"].append(str(m)); j["log"][:] = j["log"][-200:]
    # 진행률 갱신(0.0~1.0). 파이프라인이 이미지 c/t 를 넘겨준다.
    def prog(c, t): j["progress"] = (c / t) if t else 0.0
    try:
        j["result"] = P.build_report(folder, ply, out_path, log, prog, detect=detect); j["progress"] = 1.0
    except Exception as e:
        import traceback; traceback.print_exc(); j["error"] = str(e)
    finally:
        j["done"] = True


class Handler(BaseHTTPRequestHandler):
    """대시보드·뷰어의 모든 HTTP 요청을 처리하는 핸들러.

    크게 세 갈래다:
      · 정적 자원 — dashboard/viewer 의 HTML·JS·CSS 와 three.js 번들
      · 보고서 조각 API — .twin 안의 meta/mesh/이미지를 스트리밍(_report_*)
      · 잡 API — 분석·비교·PDF·재학습을 백그라운드로 띄우고 진행률을 폴링
    응답은 항상 no-store 다(로컬 앱이라 캐시 이득이 없고, 보고서 교체 시 낡은 데이터가 보이면 곤란)."""

    def log_message(self, *a):
        """기본 액세스 로그를 끈다 — 분석 로그가 콘솔에서 묻히지 않게."""
        pass

    def _send(self, code, body, ctype="application/octet-stream"):
        """본문을 메모리에 다 올린 채 한 번에 응답한다(작은 응답 전용).
        큰 덩어리는 반드시 _stream() 을 쓸 것."""
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(body)

    def _json(self, obj, code=200):
        """dict/list 를 UTF-8 JSON 으로 응답한다(한글을 이스케이프하지 않는다)."""
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _file(self, path, ctype):
        """디스크의 파일 하나를 그대로 응답한다(정적 자원용). 없으면 404."""
        if not os.path.exists(path):
            return self._send(404, "not found", "text/plain")
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)

    def _stream(self, src, size, ctype):
        """파일류 객체를 1MB 씩 흘려보낸다. 피크 메모리가 콘텐츠 크기와 무관해진다."""
        self.send_response(200); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size)); self.send_header("Cache-Control", "no-store")
        self.end_headers()
        shutil.copyfileobj(src, self.wfile, 1 << 20)

    def _twin_arg(self, q):
        """?p= 로 받은 .twin 경로 검증."""
        p = q.get("p", [""])[0]
        if not p or not os.path.exists(p):
            self._send(404, "보고서 파일 없음: " + p, "text/plain; charset=utf-8"); return None
        return p

    # ---- 보고서 조각 API ----
    def _report_mesh(self, path):
        """메시를 '바이너리'로 내려준다. base64/JSON 을 거치지 않으므로 문자열 한계와 무관."""
        twin_version(path)                                       # 형식 검증
        with _open_twin(path) as z:                              # 스트리밍: 피크 메모리 ≈ 청크
            info = z.getinfo("mesh.ply")
            with z.open("mesh.ply") as src:
                return self._stream(src, info.file_size, "application/octet-stream")

    def _report_tile(self, path, tid):
        """계층 LOD 타일 하나(tiles/<id>.bin)를 스트리밍한다. 뷰어가 보이는 것만 골라 요청한다."""
        twin_version(path)
        name = f"tiles/{tid}.bin"
        with _open_twin(path) as z:
            try:
                info = z.getinfo(name)
            except KeyError:
                return self._send(404, "타일 없음: " + name, "text/plain; charset=utf-8")
            with z.open(name) as src:
                return self._stream(src, info.file_size, "application/octet-stream")

    def _report_img(self, path, cid, kind):
        """카메라 id 의 이미지 한 장을 스트리밍한다.
        kind=thumb(목록용 축소) · full(원본 ≤1920px) · mask(검출 마스크, full 과 픽셀 일치).
        뷰어가 보이는 것만 요청하므로 1,000장짜리 보고서도 메모리에 다 올리지 않는다."""
        if kind not in ("thumb", "full", "mask"):
            return self._send(400, "kind 는 thumb|full|mask", "text/plain; charset=utf-8")
        ext = "png" if kind == "mask" else "jpg"                  # 마스크는 투명 PNG
        mime = "image/png" if kind == "mask" else "image/jpeg"
        twin_version(path)                                        # 형식 검증
        name = f"img/{cid:04d}_{kind}.{ext}"
        with zipfile.ZipFile(path) as z:
            try:
                info = z.getinfo(name)
            except KeyError:
                return self._send(404, "이미지 없음: " + name, "text/plain; charset=utf-8")
            with z.open(name) as src:
                return self._stream(src, info.file_size, mime)

    def do_GET(self):
        """GET 라우팅. 정적 자원 → 뷰어 HTML → 보고서 조각 API → 목록·잡 조회 순으로 매칭한다.
        어떤 예외든 500 으로 감싸 응답한다(핸들러가 죽으면 창이 통째로 멈추기 때문)."""
        u = urllib.parse.urlparse(self.path); r = u.path; q = urllib.parse.parse_qs(u.query)
        try:
            if r in ("/", "/index.html", "/dashboard.html"):
                return self._file(os.path.join(UI_DIR, "dashboard.html"), "text/html; charset=utf-8")
            if r in ("/dashboard.js", "/style.css", "/viewer.js", "/viewer.css",
                     "/bvh.js", "/lod.js", "/origin.js", "/tiles.js"):
                ct = "text/javascript" if r.endswith(".js") else "text/css"
                return self._file(os.path.join(UI_DIR, r[1:]), ct + "; charset=utf-8")
            if r.startswith("/libs/"):
                p = os.path.normpath(os.path.join(LIBS_DIR, r[len("/libs/"):]))
                if not p.startswith(LIBS_DIR):
                    return self._send(403, "no")
                return self._file(p, "text/javascript; charset=utf-8" if p.endswith(".js") else "application/octet-stream")
            if r == "/viewer":
                ap = q.get("asset", [""])[0]
                if ap:                                    # 자산(검사 간 비교) 모드
                    if not os.path.exists(ap):
                        return self._send(404, "자산 파일 없음: " + ap, "text/plain; charset=utf-8")
                    with open(ap, encoding="utf-8") as f:
                        asset = json.load(f)
                    tw = asset["inspections"][-1]["twin"]  # 최신 검사를 본체로, 이전은 고스트
                    if not os.path.exists(tw):
                        return self._send(404, "보고서 파일 없음: " + tw, "text/plain; charset=utf-8")
                    return self._send(200, render_viewer_html(tw, asset=asset), "text/html; charset=utf-8")
                tw = q.get("report", [""])[0]
                if not tw or not os.path.exists(tw):
                    return self._send(404, "보고서 파일 없음: " + tw, "text/plain; charset=utf-8")
                return self._send(200, render_viewer_html(tw), "text/html; charset=utf-8")
            if r == "/api/report/meta":
                p = self._twin_arg(q)
                if p is None: return
                return self._send(200, report_meta(p), "application/json; charset=utf-8")
            if r == "/api/report/mesh":
                p = self._twin_arg(q)
                if p is None: return
                return self._report_mesh(p)
            if r == "/api/report/tile":
                p = self._twin_arg(q)
                if p is None: return
                return self._report_tile(p, int(q.get("id", ["-1"])[0]))
            if r == "/api/report/img":
                p = self._twin_arg(q)
                if p is None: return
                return self._report_img(p, int(q.get("id", ["-1"])[0]), q.get("kind", ["thumb"])[0])
            if r == "/api/pick_folder":
                return self._json({"path": pick_folder() or ""})
            if r == "/api/pick_file":
                return self._json({"path": pick_file(q.get("kind", ["ply"])[0]) or ""})
            if r == "/api/reports":
                items = []
                if os.path.isdir(REPORTS_DIR):
                    for f in sorted(os.listdir(REPORTS_DIR)):
                        if f.lower().endswith(".twin"):
                            fp = os.path.join(REPORTS_DIR, f)
                            items.append({"name": f, "path": fp, "mb": round(os.path.getsize(fp) / 1e6, 1),
                                          "mtime": os.path.getmtime(fp)})
                return self._json({"reports": items})
            if r == "/api/download":
                # 생성된 PDF 만, reports 폴더 안만 서빙(경로 탈출 방지)
                p = q.get("path", [""])[0]
                ap = os.path.abspath(p)
                if not (ap.startswith(os.path.abspath(REPORTS_DIR)) and ap.lower().endswith(".pdf")
                        and os.path.exists(ap)):
                    return self._send(404, "not found", "text/plain")
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", 'inline; filename="%s"'
                                 % os.path.basename(ap).encode("ascii", "ignore").decode() )
                self.send_header("Content-Length", str(os.path.getsize(ap)))
                self.end_headers()
                with open(ap, "rb") as f:
                    shutil.copyfileobj(f, self.wfile)
                return
            if r == "/api/review":
                # 검토 사이드카(<twin>.review.json) 조회 — .twin(GB급)은 절대 재작성하지 않는다
                p = self._twin_arg(q)
                if p is None: return
                rp2 = p + ".review.json"
                if os.path.exists(rp2):
                    return self._file(rp2, "application/json; charset=utf-8")
                return self._json({"version": 1, "reviews": {}})
            if r == "/api/assets":
                items = []
                if os.path.isdir(REPORTS_DIR):
                    for f in sorted(os.listdir(REPORTS_DIR)):
                        if f.lower().endswith(".json") and f.lower().startswith("asset"):
                            fp = os.path.join(REPORTS_DIR, f)
                            try:
                                with open(fp, encoding="utf-8") as fh:
                                    a = json.load(fh)
                                ids = [i["id"] for i in a.get("inspections", [])]
                                items.append({"name": a.get("name") or f, "path": fp,
                                              "ids": ids, "n": len(a.get("cracks", [])),
                                              "mtime": os.path.getmtime(fp)})
                            except Exception:
                                pass
                return self._json({"assets": items})
            if r == "/api/job":
                j = _jobs.get(q.get("id", [""])[0])
                if not j:
                    return self._json({"error": "no job"}, 404)
                return self._json({"progress": j["progress"], "done": j["done"], "error": j["error"],
                                   "result": j["result"], "log": j["log"][-12:]})
            return self._send(404, "not found", "text/plain")
        except Exception as e:
            import traceback; traceback.print_exc(); self._send(500, "error: " + str(e), "text/plain; charset=utf-8")

    def do_POST(self):
        """POST 라우팅. 상태를 바꾸는 요청만 여기로 온다.

          /api/review   검토 저장 ┐ 즉시 처리(사이드카 파일 쓰기 한 번)
          /api/calib    캘리브레이션 저장 ┘
          /api/create   보고서 생성 ┐
          /api/compare  검사 간 비교 ├ 모두 백그라운드 잡 → job_id 만 즉시 응답하고
          /api/export   PDF 내보내기 ┘ 진행률은 GET /api/job 으로 폴링한다"""
        rp = urllib.parse.urlparse(self.path).path
        if rp == "/api/calib":
            # 스케일 캘리브레이션 저장: {p, scale, measured_m, real_mm} — scale 없거나 0/음수면 해제.
            # 검토와 같은 사이드카(<twin>.review.json)의 "calib" 키에 영속 → 뷰어·PDF 가 같은 값을 본다.
            # (예전엔 브라우저 localStorage 에만 있어 PDF 와 화면 수치가 달랐다.)
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
                p = d.get("p", "")
                if not p or not os.path.exists(p):
                    return self._json({"error": "보고서 없음"}, 400)
                rp2 = p + ".review.json"
                data = {"version": 1, "reviews": {}}
                if os.path.exists(rp2):
                    with open(rp2, encoding="utf-8") as f:
                        data = json.load(f)
                sc = d.get("scale")
                if not sc or float(sc) <= 0:
                    data.pop("calib", None); ent = None
                else:
                    import datetime
                    ent = {"scale": float(sc),
                           "measured_m": d.get("measured_m"), "real_mm": d.get("real_mm"),
                           "at": datetime.datetime.now().isoformat(timespec="seconds")}
                    data["calib"] = ent
                tmp = rp2 + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                os.replace(tmp, rp2)
                return self._json({"ok": True, "calib": ent})
            except Exception as e:
                import traceback; traceback.print_exc()
                return self._json({"error": str(e)}, 500)
        if rp == "/api/review":
            # 검토 저장: {p, idx, state?, type?, note?} — state="clear" 는 해당 균열 검토 삭제.
            # 사이드카에 원자적(tmp→replace) 기록. 이 파일이 곧 분류기 재학습 라벨의 축적처다.
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
                p = d.get("p", "")
                if not p or not os.path.exists(p):
                    return self._json({"error": "보고서 없음"}, 400)
                idx = str(int(d["idx"]))
                rp2 = p + ".review.json"
                data = {"version": 1, "reviews": {}}
                if os.path.exists(rp2):
                    with open(rp2, encoding="utf-8") as f:
                        data = json.load(f)
                if d.get("state") == "clear":
                    data["reviews"].pop(idx, None); ent = None
                else:
                    import datetime
                    ent = data["reviews"].get(idx, {})
                    for k in ("state", "type", "note"):
                        if k in d:
                            if d[k] in (None, ""):
                                ent.pop(k, None)
                            else:
                                ent[k] = d[k]
                    ent["at"] = datetime.datetime.now().isoformat(timespec="seconds")
                    data["reviews"][idx] = ent
                tmp = rp2 + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                os.replace(tmp, rp2)
                return self._json({"ok": True, "review": ent})
            except Exception as e:
                import traceback; traceback.print_exc()
                return self._json({"error": str(e)}, 500)
        if rp == "/api/export":
            # PDF 보고서 생성 잡: {p, rows?, photos?} → 결과 pdf 는 reports/ 에 저장
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
                p = d.get("p", "")
                if not p or not os.path.exists(p):
                    return self._json({"error": "보고서 없음"}, 400)
                base = re.sub(r'[\\/:*?"<>|]+', "_", os.path.splitext(os.path.basename(p))[0])
                out = os.path.join(REPORTS_DIR, base + "_report.pdf")
                os.makedirs(REPORTS_DIR, exist_ok=True)
                jid = uuid.uuid4().hex[:8]
                _jobs[jid] = {"progress": 0.0, "done": False, "error": None, "result": None, "log": []}
                threading.Thread(target=_run_export_job,
                                 args=(jid, p, out, int(d.get("rows", 300)), int(d.get("photos", 24))),
                                 daemon=True).start()
                return self._json({"job": jid, "out": out})
            except Exception as e:
                import traceback; traceback.print_exc()
                return self._json({"error": str(e)}, 500)
        if rp == "/api/compare":
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
                ta, tb = d.get("a", ""), d.get("b", "")
                if not (os.path.exists(ta) and os.path.exists(tb)):
                    return self._json({"error": "보고서 파일 없음"}, 400)
                if os.path.abspath(ta) == os.path.abspath(tb):
                    return self._json({"error": "같은 보고서끼리는 비교할 수 없습니다"}, 400)
                import datetime
                ida = (d.get("ida") or "").strip() or datetime.datetime.fromtimestamp(os.path.getmtime(ta)).strftime("%Y-%m-%d")
                idb = (d.get("idb") or "").strip() or datetime.datetime.fromtimestamp(os.path.getmtime(tb)).strftime("%Y-%m-%d")
                if ida == idb:
                    idb += "(2)"
                # 자동 승계: 이전(T1)으로 고른 보고서가 어느 자산의 '최신 검사'면, 새 자산을 만들지
                # 않고 그 자산에 이번(T2)을 N번째 검사로 이어붙인다(영속 ID 유지 = 시간축 축적).
                ext_asset = None
                if os.path.isdir(REPORTS_DIR):
                    for f in os.listdir(REPORTS_DIR):
                        if f.lower().endswith(".json") and f.lower().startswith("asset"):
                            fp = os.path.join(REPORTS_DIR, f)
                            try:
                                with open(fp, encoding="utf-8") as fh:
                                    a0 = json.load(fh)
                                if os.path.abspath(a0["inspections"][-1]["twin"]) == os.path.abspath(ta):
                                    ext_asset = fp; break
                            except Exception:
                                pass
                na = os.path.splitext(os.path.basename(ta))[0]; nb = os.path.splitext(os.path.basename(tb))[0]
                safe = lambda s: re.sub(r'[\\/:*?"<>|]+', "_", s)
                out = os.path.join(REPORTS_DIR, f"asset_{safe(na)}__{safe(nb)}.json")
                os.makedirs(REPORTS_DIR, exist_ok=True)
                jid = uuid.uuid4().hex[:8]
                _jobs[jid] = {"progress": 0.0, "done": False, "error": None, "result": None, "log": []}
                threading.Thread(target=_run_compare_job,
                                 args=(jid, ta, tb, ida, idb, out, ext_asset), daemon=True).start()
                return self._json({"job": jid, "out": ext_asset or out, "mode": "extend" if ext_asset else "new"})
            except Exception as e:
                import traceback; traceback.print_exc()
                return self._json({"error": str(e)}, 500)
        if rp != "/api/create":
            return self._send(404, "not found", "text/plain")
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
            folder, ply = d["folder"], d["ply"]
            name = (d.get("name") or "").strip() or os.path.basename(os.path.normpath(folder))
            name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "report"
            if name.lower().endswith(".twin"):
                name = name[:-5]
            if not os.path.isdir(folder):
                return self._json({"error": "폴더 없음"}, 400)
            if not os.path.exists(ply):
                return self._json({"error": "PLY 없음"}, 400)
            os.makedirs(REPORTS_DIR, exist_ok=True)
            out = os.path.join(REPORTS_DIR, name + ".twin")
            detect = bool(d.get("detect", d.get("do_ocr", True)))
            jid = uuid.uuid4().hex[:8]
            _jobs[jid] = {"progress": 0.0, "done": False, "error": None, "result": None, "log": []}
            threading.Thread(target=_run_job, args=(jid, folder, ply, out, detect), daemon=True).start()
            return self._json({"job": jid, "out": out})
        except Exception as e:
            import traceback; traceback.print_exc(); return self._json({"error": str(e)}, 500)


def make_server(port=0):
    """보고서 폴더 준비 후 로컬 HTTP 서버를 생성한다."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8920)
    args = ap.parse_args()
    httpd = make_server(args.port)
    print(f"[backend] http://127.0.0.1:{httpd.server_address[1]}")
    httpd.serve_forever()
