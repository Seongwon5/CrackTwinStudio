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
import base64
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

# 구형식(v1: 단일 JSON) 을 백엔드가 통째로 파싱해도 되는 상한. v1 은 이 크기를 넘으면 열지 않고
# 변환을 안내한다(파싱만으로 파일의 4~5배 메모리를 쓰기 때문).
LEGACY_MAX = 400 * 1024 * 1024
_v1_cache = {"path": None, "mtime": None, "data": None}   # v1 은 재파싱이 비싸 1개만 캐시


def twin_version(path):
    """매직바이트로 형식 판별.  2 = zip 컨테이너,  1 = 구형식 단일 JSON."""
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:4] == b"PK\x03\x04":
        return 2
    if head[:1] == b"{":
        return 1
    raise ValueError("알 수 없는 .twin 형식입니다: " + os.path.basename(path))


def _load_v1(path):
    """구형식 .twin 을 파싱해 캐시. 뷰어가 meta/mesh/이미지를 각각 요청하므로 캐시가 없으면
    요청마다 전체를 다시 파싱하게 된다."""
    mt = os.path.getmtime(path)
    if _v1_cache["path"] == path and _v1_cache["mtime"] == mt:
        return _v1_cache["data"]
    size = os.path.getsize(path)
    if size > LEGACY_MAX:
        raise ValueError(
            f"구형식 보고서가 너무 큽니다({size/1e6:.0f}MB). 브라우저 문자열 한계(536.9MB)로 열 수 없습니다.\n"
            f"  py tools/convert_twin.py \"{path}\"  로 새 형식(v2)으로 변환하세요.")
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    _v1_cache.update(path=path, mtime=mt, data=d)
    return d


def _v1_img_bytes(cam, kind):
    """구형식 카메라의 data-URI( data:image/jpeg;base64,... ) → 원본 JPEG 바이트."""
    uri = cam.get(kind) or ""
    i = uri.find(",")
    return base64.b64decode(uri[i + 1:]) if i >= 0 else b""


def report_meta(path):
    """뷰어용 메타(JSON). 이미지·메시 같은 큰 덩어리는 빼고 기하만 담는다.
    v1/v2 어느 쪽이든 같은 모양을 돌려주므로 뷰어 코드는 한 갈래로 유지된다."""
    if twin_version(path) == 2:
        with zipfile.ZipFile(path) as z:
            return z.read("meta.json").decode("utf-8")
    d = _load_v1(path)
    cams = [{k: v for k, v in c.items() if k not in ("thumb", "full")} for c in d.get("cameras", [])]
    for i, c in enumerate(cams):
        c.setdefault("id", i)
    return json.dumps({"version": 1,
                       "counts": {"cameras": len(cams), "hotspots": len(d.get("hotspots", []))},
                       "text_points": d.get("text_points", []),
                       "cameras": cams,
                       "hotspots": d.get("hotspots", [])}, ensure_ascii=False)


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
    def log(m): j["log"].append(str(m)); j["log"][:] = j["log"][-200:]
    def prog(c, t): j["progress"] = (c / t) if t else 0.0
    try:
        j["result"] = P.build_report(folder, ply, out_path, log, prog, detect=detect); j["progress"] = 1.0
    except Exception as e:
        import traceback; traceback.print_exc(); j["error"] = str(e)
    finally:
        j["done"] = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/octet-stream"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _file(self, path, ctype):
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

    # ---- 보고서 조각 API (v1/v2 공통 모양) ----
    def _report_mesh(self, path):
        """메시를 '바이너리'로 내려준다. base64/JSON 을 거치지 않으므로 문자열 한계와 무관."""
        if twin_version(path) == 2:
            with zipfile.ZipFile(path) as z:                     # 스트리밍: 피크 메모리 ≈ 청크
                info = z.getinfo("mesh.ply")
                with z.open("mesh.ply") as src:
                    return self._stream(src, info.file_size, "application/octet-stream")
        raw = base64.b64decode(_load_v1(path).get("mesh_base64", ""))    # v1 은 디코드해서 동일 형태로
        return self._send(200, raw, "application/octet-stream")

    def _report_img(self, path, cid, kind):
        if kind not in ("thumb", "full", "mask"):
            return self._send(400, "kind 는 thumb|full|mask", "text/plain; charset=utf-8")
        ext = "png" if kind == "mask" else "jpg"                  # 마스크는 투명 PNG
        mime = "image/png" if kind == "mask" else "image/jpeg"
        if twin_version(path) == 2:
            name = f"img/{cid:04d}_{kind}.{ext}"
            with zipfile.ZipFile(path) as z:
                try:
                    info = z.getinfo(name)
                except KeyError:
                    return self._send(404, "이미지 없음: " + name, "text/plain; charset=utf-8")
                with z.open(name) as src:
                    return self._stream(src, info.file_size, mime)
        if kind == "mask":
            return self._send(404, "v1 은 마스크 미지원", "text/plain; charset=utf-8")
        cams = _load_v1(path).get("cameras", [])
        cam = next((c for c in cams if c.get("id") == cid), None)
        if cam is None and 0 <= cid < len(cams):
            cam = cams[cid]
        if cam is None:
            return self._send(404, "카메라 없음: " + str(cid), "text/plain; charset=utf-8")
        return self._send(200, _v1_img_bytes(cam, kind), "image/jpeg")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); r = u.path; q = urllib.parse.parse_qs(u.query)
        try:
            if r in ("/", "/index.html", "/dashboard.html"):
                return self._file(os.path.join(UI_DIR, "dashboard.html"), "text/html; charset=utf-8")
            if r in ("/dashboard.js", "/style.css"):
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
        rp = urllib.parse.urlparse(self.path).path
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
