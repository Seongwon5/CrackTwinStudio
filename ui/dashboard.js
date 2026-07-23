(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let folder = null, ply = null, polling = null;

  async function pick(kind) {
    // 백엔드가 Windows 네이티브 다이얼로그를 띄우고 경로를 반환
    const url = kind === "folder" ? "/api/pick_folder" : "/api/pick_file?kind=" + encodeURIComponent(kind);
    try {
      const d = await (await fetch(url)).json();
      return d.path || null;
    } catch (e) { alert("다이얼로그 오류: " + e); return null; }
  }

  $("btnFolder").onclick = async () => {
    const p = await pick("folder");
    if (p) {
      folder = p; $("folderLbl").textContent = p; checkReady();
      // 이름란이 비어 있으면 폴더명으로 자동 채움
      if (!$("reportName").value.trim()) {
        $("reportName").value = p.replace(/[\\/]+$/, "").split(/[\\/]/).pop();
      }
    }
  };
  $("btnPly").onclick = async () => {
    const p = await pick("ply");
    if (p) { ply = p; $("plyLbl").textContent = p; checkReady(); }
  };
  function checkReady() { $("btnCreate").disabled = !(folder && ply); }

  $("btnCreate").onclick = async () => {
    $("btnCreate").disabled = true;
    $("prog").classList.remove("hidden"); $("log").classList.remove("hidden");
    $("log").textContent = "분석 시작...\n";
    try {
      const r = await fetch("/api/create", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder, ply, do_ocr: $("ckOcr").checked, name: $("reportName").value.trim() }) });
      const d = await r.json();
      if (d.error) { alert(d.error); $("btnCreate").disabled = false; return; }
      pollJob(d.job, d.out);
    } catch (e) { alert("생성 실패: " + e); $("btnCreate").disabled = false; }
  };

  function pollJob(job, out) {
    clearInterval(polling);
    polling = setInterval(async () => {
      const d = await (await fetch("/api/job?id=" + job)).json();
      $("progBar").style.width = Math.round((d.progress || 0) * 100) + "%";
      if (d.log) $("log").textContent = d.log.join("\n");
      $("log").scrollTop = $("log").scrollHeight;
      if (d.done) {
        clearInterval(polling); $("btnCreate").disabled = false;
        if (d.error) { alert("분석 오류: " + d.error); }
        else {
          $("log").textContent += "\n✅ 완료: " + d.result;
          loadReports();
          if (confirm("보고서 생성 완료!\n지금 새 창으로 열까요?")) openReport(d.result);
        }
      }
    }, 700);
  }

  function openReport(path) {
    // pywebview: 새 네이티브 창 / 브라우저: 새 창 (여러 개 동시에)
    const a = (window.pywebview && window.pywebview.api) || null;
    if (a && a.open_report) { a.open_report(path); }
    else { window.open("/viewer?report=" + encodeURIComponent(path), "_blank", "width=1360,height=860"); }
  }

  $("btnOpenFile").onclick = async () => {
    const p = await pick("twin");
    if (p) openReport(p);
  };
  $("btnRefresh").onclick = loadReports;

  // 종료: 확인 대화상자는 app.py 의 closing 핸들러가 띄운다(X 버튼과 같은 경로 → 한 번만 뜸).
  // 브라우저로 띄운 경우엔 pywebview API 가 없으므로 안내만 한다.
  $("btnQuit").onclick = () => {
    const a = (window.pywebview && window.pywebview.api) || null;
    if (a && a.quit_app) a.quit_app();
    else alert("브라우저에서 열려 있어 종료할 수 없습니다. 앱 창에서 사용하세요.");
  };

  async function loadReports() {
    const list = $("reportList"); list.innerHTML = "";
    try {
      const d = await (await fetch("/api/reports")).json();
      if (!d.reports || !d.reports.length) {
        list.innerHTML = '<div class="empty">아직 보고서가 없습니다. ①에서 만들거나 파일에서 여세요.</div>';
        return;
      }
      d.reports.sort((a, b) => b.mtime - a.mtime).forEach((rp) => {
        const el = document.createElement("div"); el.className = "ritem";
        el.innerHTML = `<svg class="ic ritem-ic"><use href="#i-file"/></svg>`
          + `<span class="nm">${rp.name}<br><span class="meta">${rp.mb} MB · ${new Date(rp.mtime*1000).toLocaleString()}</span></span>`;
        const b = document.createElement("button"); b.className = "primary";
        b.innerHTML = `<svg class="ic"><use href="#i-open"/></svg>열기`;
        b.onclick = () => openReport(rp.path);
        el.appendChild(b); list.appendChild(el);
      });
    } catch (e) { list.innerHTML = '<div class="empty">목록 로드 실패: ' + e + '</div>'; }
  }

  loadReports();
})();
