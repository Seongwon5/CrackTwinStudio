# -*- coding: utf-8 -*-
"""
backend.py — 로컬 백엔드

pywebview 창이 접속하는 localhost 서버. 대시보드·뷰어 정적 파일, 보고서 생성 잡,
보고서 목록, .twin 조각(메타·메시·이미지) 스트리밍을 제공한다.

메시·이미지는 HTML에 인라인하지 않고 바이너리로 스트리밍한다. 인라인하면 V8 최대
문자열(536.9MB)에 걸려 큰 보고서가 열리지 않고, 서버 피크 메모리도 파일 크기에 비례한다.
"""
import os
import re
import sys
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

# 소스 실행과 exe 실행에서 기준 경로가 다르다.
#   자원(ui·libs) : exe 안에 묶여 있으므로 PyInstaller 가 풀어놓은 _MEIPASS
#   보고서(reports): 실행 때마다 지워지면 안 되므로 exe 가 놓인 폴더
_FROZEN = getattr(sys, "frozen", False)
HERE = sys._MEIPASS if _FROZEN else os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(sys.executable) if _FROZEN else HERE
UI_DIR, LIBS_DIR, REPORTS_DIR = os.path.join(HERE, "ui"), os.path.join(HERE, "libs"), os.path.join(BASE, "reports")
_jobs = {}                       # job_id -> {progress, done, error, result, log[]}


def twin_version(path):
    """매직바이트로 .twin 형식을 검증한다(zip 컨테이너)."""
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:4] == b"PK\x03\x04":
        return 2
    raise ValueError("알 수 없는 .twin 형식입니다: " + os.path.basename(path))


def report_meta(path):
    """뷰어용 메타(JSON) — 카메라 자세만 담고 이미지·메시는 별도 스트리밍."""
    twin_version(path)
    with zipfile.ZipFile(path) as z:
        return z.read("meta.json").decode("utf-8")


# PowerShell 은 PATH 에 없을 수도 있으므로(경로가 지워진 PC·제한 환경) 절대 경로를 먼저 쓴다.
_PS_EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
if not os.path.exists(_PS_EXE):
    _PS_EXE = "powershell"
# 콘솔 없는 exe(--windowed)에서 subprocess 를 그냥 띄우면 검은 창이 번쩍인다.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _ps_args(*rest):
    """다이얼로그용 PowerShell 공통 인자."""
    return [_PS_EXE, "-STA", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", *rest]


def _ps_dialog(script):
    """PowerShell(-STA)로 Windows 네이티브 파일/폴더 다이얼로그를 띄우고 선택 경로 반환."""
    try:
        r = subprocess.run(_ps_args("-Command", script), capture_output=True, text=True,
                           timeout=300, creationflags=_NO_WINDOW)
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
        subprocess.run(_ps_args("-ExecutionPolicy", "Bypass", "-File", ps),
                       capture_output=True, timeout=300, creationflags=_NO_WINDOW)
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


def reveal_path(path):
    """탐색기에서 그 파일이 있는 폴더를 열고 파일을 선택 상태로 띄운다.

    보고서 폴더(REPORTS_DIR) 안의 파일만 허용한다. 파일이 없으면(예: 모바일 HTML
    미생성) 폴더만 연다. explorer 는 성공해도 종료코드 1 을 내므로 반환값은 보지 않는다.
    """
    ap = os.path.abspath(path)
    root = os.path.abspath(REPORTS_DIR)
    if os.path.commonpath([ap, root]) != root:      # 보고서 폴더 밖은 거부(경로 우회 방지)
        raise ValueError("허용되지 않은 경로")
    if os.path.exists(ap):
        # '/select,<path>' 는 한 토큰이어야 한다 — 공백이 끼면 파일 선택 대신 문서 폴더가 열린다.
        subprocess.Popen(["explorer", "/select," + ap])
    else:                                            # 파일이 없으면 폴더만
        subprocess.Popen(["explorer", os.path.dirname(ap) or root])
    return ap


def render_viewer_html(twin_path):
    """뷰어 HTML을 반환한다. 보고서 경로만 주입하고 데이터는 뷰어가 API로 가져간다."""
    twin_version(twin_path)
    with open(os.path.join(UI_DIR, "viewer.html"), "r", encoding="utf-8") as f:
        html = f.read()
    return html.replace("__REPORT__", json.dumps(twin_path))   # JS 문자열 리터럴로 이스케이프


def _run_job(job_id, folder, ply, out_path):
    """백그라운드에서 보고서를 생성하고 진행률·로그·결과를 갱신한다."""
    j = _jobs[job_id]
    def log(m): j["log"].append(str(m)); j["log"][:] = j["log"][-200:]   # 최근 200줄만
    def prog(c, t): j["progress"] = (c / t) if t else 0.0
    try:
        j["result"] = P.build_report(folder, ply, out_path, log, prog); j["progress"] = 1.0
    except Exception as e:
        import traceback; traceback.print_exc(); j["error"] = str(e)
    finally:
        j["done"] = True


class Handler(BaseHTTPRequestHandler):
    """대시보드·뷰어의 모든 HTTP 요청을 처리하는 핸들러.

    정적 자원 · 보고서 조각 스트리밍 · 생성 잡 세 갈래를 처리한다.
    응답은 항상 no-store — 보고서를 다시 만들었을 때 낡은 데이터가 보이면 안 된다.
    """

    def log_message(self, *a):
        pass                                     # 기본 액세스 로그를 끈다

    def _send(self, code, body, ctype="application/octet-stream"):
        """작은 응답 전용. 큰 덩어리는 _stream()을 쓸 것."""
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _file(self, path, ctype):
        """정적 파일 하나를 그대로 응답한다."""
        if not os.path.exists(path):
            return self._send(404, "not found", "text/plain")
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)

    def _stream(self, src, size, ctype):
        """1MB씩 흘려보낸다 — 피크 메모리가 콘텐츠 크기와 무관해진다."""
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

    def _report_mesh(self, path):
        """메시를 바이너리로 스트리밍한다."""
        twin_version(path)
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("mesh.ply")
            with z.open("mesh.ply") as src:
                return self._stream(src, info.file_size, "application/octet-stream")

    def _report_img(self, path, cid, kind):
        """카메라 id의 사진 한 장을 스트리밍한다. kind=thumb(축소) · full(≤1920px)."""
        if kind not in ("thumb", "full"):
            return self._send(400, "kind 는 thumb|full", "text/plain; charset=utf-8")
        mime = "image/jpeg"
        twin_version(path)
        name = f"img/{cid:04d}_{kind}.jpg"
        with zipfile.ZipFile(path) as z:
            try:
                info = z.getinfo(name)
            except KeyError:
                return self._send(404, "이미지 없음: " + name, "text/plain; charset=utf-8")
            with z.open(name) as src:
                return self._stream(src, info.file_size, mime)

    def do_GET(self):
        """정적 자원 → 뷰어 HTML → 보고서 조각 → 목록·잡 조회 순으로 매칭한다.

        예외는 500으로 감싼다 — 핸들러가 죽으면 창이 통째로 멈춘다.
        """
        u = urllib.parse.urlparse(self.path); r = u.path; q = urllib.parse.parse_qs(u.query)
        try:
            if r in ("/", "/index.html", "/dashboard.html"):
                return self._file(os.path.join(UI_DIR, "dashboard.html"), "text/html; charset=utf-8")
            if r in ("/dashboard.js", "/style.css", "/viewer.js", "/viewer.css"):
                ct = "text/javascript" if r.endswith(".js") else "text/css"
                return self._file(os.path.join(UI_DIR, r[1:]), ct + "; charset=utf-8")
            if r.startswith("/libs/"):
                p = os.path.normpath(os.path.join(LIBS_DIR, r[len("/libs/"):]))
                if not p.startswith(LIBS_DIR):
                    return self._send(403, "no")
                return self._file(p, "text/javascript; charset=utf-8" if p.endswith(".js") else "application/octet-stream")
            if r == "/viewer":
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
            if r == "/api/reveal":                    # 탐색기에서 그 파일 위치 열기(파일 선택 상태)
                try:
                    return self._json({"ok": True, "path": reveal_path(q.get("p", [""])[0])})
                except Exception as e:
                    return self._json({"error": str(e)}, 400)
            if r == "/api/reports":
                items = []
                if os.path.isdir(REPORTS_DIR):
                    for f in sorted(os.listdir(REPORTS_DIR)):
                        if f.lower().endswith(".twin"):
                            fp = os.path.join(REPORTS_DIR, f)
                            mob = os.path.splitext(fp)[0] + "_mobile.html"
                            items.append({"name": f, "path": fp, "mb": round(os.path.getsize(fp) / 1e6, 1),
                                          "mtime": os.path.getmtime(fp),
                                          "mobile": os.path.exists(mob), "mobile_path": mob})
                return self._json({"reports": items})
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
        """POST 라우트 — /api/create(보고서 생성) · /api/export_mobile(모바일 뷰어 내보내기)."""
        rp = urllib.parse.urlparse(self.path).path
        if rp == "/api/export_mobile":
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
                twin = d.get("path", "")
                if not twin or not os.path.exists(twin):
                    return self._json({"error": "보고서 파일 없음"}, 400)
                out = os.path.splitext(twin)[0] + "_mobile.html"
                jid = uuid.uuid4().hex[:8]
                _jobs[jid] = {"progress": 0.0, "done": False, "error": None, "result": None, "log": []}

                def run():
                    j = _jobs[jid]
                    def log(m): j["log"].append(str(m)); j["log"][:] = j["log"][-200:]
                    try:
                        import export_mobile as EM
                        j["result"] = EM.export_mobile(twin, out, log=log)
                        j["progress"] = 1.0
                    except Exception as e:
                        import traceback; traceback.print_exc(); j["error"] = str(e)
                    finally:
                        j["done"] = True

                threading.Thread(target=run, daemon=True).start()
                return self._json({"job": jid, "out": out})
            except Exception as e:
                import traceback; traceback.print_exc(); return self._json({"error": str(e)}, 500)
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
            jid = uuid.uuid4().hex[:8]
            _jobs[jid] = {"progress": 0.0, "done": False, "error": None, "result": None, "log": []}
            threading.Thread(target=_run_job, args=(jid, folder, ply, out), daemon=True).start()
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
