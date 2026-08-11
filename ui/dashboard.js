// dashboard.js — 보고서 만들기 / 열기
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let folder = null, ply = null, polling = null;

  // 파일/폴더 선택창을 띄우고 고른 경로를 돌려준다(취소·오류면 null).
  // 브라우저 <input type=file> 은 실제 경로를 주지 않으므로 백엔드가 네이티브 다이얼로그를 띄운다.
  async function pick(kind) {
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
  // 폴더와 PLY 가 둘 다 정해졌을 때만 '보고서 생성'을 활성화한다.
  function checkReady() { $("btnCreate").disabled = !(folder && ply); }

  $("btnCreate").onclick = async () => {
    $("btnCreate").disabled = true;
    $("prog").classList.remove("hidden"); $("log").classList.remove("hidden");
    $("log").textContent = "보고서 생성 시작...\n";
    try {
      const r = await fetch("/api/create", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder, ply, name: $("reportName").value.trim() }) });
      const d = await r.json();
      if (d.error) { alert(d.error); $("btnCreate").disabled = false; return; }
      pollJob(d.job);
    } catch (e) { alert("생성 실패: " + e); $("btnCreate").disabled = false; }
  };

  // 생성 잡을 0.7초마다 폴링해 진행바·로그를 갱신하고, 끝나면 목록을 새로 고친다.
  function pollJob(job) {
    clearInterval(polling);
    polling = setInterval(async () => {
      const d = await (await fetch("/api/job?id=" + job)).json();
      $("progBar").style.width = Math.round((d.progress || 0) * 100) + "%";
      if (d.log) $("log").textContent = d.log.join("\n");
      $("log").scrollTop = $("log").scrollHeight;
      if (d.done) {
        clearInterval(polling); $("btnCreate").disabled = false;
        if (d.error) { alert("생성 오류: " + d.error); }
        else {
          $("log").textContent += "\n✅ 완료: " + d.result;
          loadReports();
          if (confirm("보고서 생성 완료!\n지금 새 창으로 열까요?")) openReport(d.result);
        }
      }
    }, 700);
  }

  // .twin 을 뷰어로 연다 — pywebview 면 새 네이티브 창, 브라우저면 새 창.
  function openReport(path) {
    const a = (window.pywebview && window.pywebview.api) || null;
    if (a && a.open_report) { a.open_report(path); }
    else { window.open("/viewer?report=" + encodeURIComponent(path), "_blank", "width=1360,height=860"); }
  }

  // 탐색기에서 그 파일 위치를 연다(파일 선택 상태). 파일이 없으면 백엔드가 폴더만 연다.
  async function revealPath(path) {
    try {
      const d = await (await fetch("/api/reveal?p=" + encodeURIComponent(path))).json();
      if (d.error) alert("위치 열기 실패: " + d.error);
    } catch (e) { alert("위치 열기 실패: " + e); }
  }

  $("btnOpenFile").onclick = async () => {
    const p = await pick("twin");
    if (p) openReport(p);
  };
  $("btnRefresh").onclick = loadReports;

  // 종료: 확인 대화상자는 main.py 의 closing 핸들러가 띄운다(X 버튼과 같은 경로 → 한 번만 뜸).
  $("btnQuit").onclick = () => {
    const a = (window.pywebview && window.pywebview.api) || null;
    if (a && a.quit_app) a.quit_app();
    else alert("브라우저에서 열려 있어 종료할 수 없습니다. 앱 창에서 사용하세요.");
  };

  // reports/ 의 .twin 목록을 받아 최신순으로 그린다.
  async function loadReports() {
    const list = $("reportList"); list.innerHTML = "";
    try {
      const d = await (await fetch("/api/reports")).json();
      const reports = (d.reports || []).sort((a, b) => b.mtime - a.mtime);
      if (!reports.length) {
        list.innerHTML = '<div class="empty">아직 보고서가 없습니다. ①에서 만들거나 파일에서 여세요.</div>';
      }
      reports.forEach((rp) => {
        const el = document.createElement("div"); el.className = "ritem";
        el.innerHTML = `<svg class="ic ritem-ic"><use href="#i-file"/></svg>`
          + `<span class="nm">${rp.name}<br><span class="meta">${rp.mb} MB · ${new Date(rp.mtime*1000).toLocaleString()}</span></span>`;
        const m = document.createElement("button"); m.className = "ghost";
        m.textContent = "모바일";
        m.title = "폰으로 보낼 파일 준비 — 모바일 뷰어를 만들고 보낼 파일 위치를 엽니다";
        m.onclick = () => prepMobile(rp, m);
        const loc = document.createElement("button"); loc.className = "ghost";
        loc.innerHTML = `<svg class="ic"><use href="#i-folder"/></svg>위치`;
        loc.title = "이 보고서 파일이 있는 폴더를 탐색기에서 엽니다";
        loc.onclick = () => revealPath(rp.path);
        const b = document.createElement("button"); b.className = "primary";
        b.innerHTML = `<svg class="ic"><use href="#i-open"/></svg>열기`;
        b.onclick = () => openReport(rp.path);
        el.appendChild(m); el.appendChild(loc); el.appendChild(b); list.appendChild(el);
      });
    } catch (e) { list.innerHTML = '<div class="empty">목록 로드 실패: ' + e + '</div>'; }
  }

  // 폰으로 보낼 파일 준비 — 뷰어(고정 파일)를 만들고, 보낼 두 파일이 있는 폴더를 연다.
  // 뷰어는 보고서 데이터를 담지 않아 즉시 만들어진다(예전처럼 사진을 굽지 않는다).
  async function prepMobile(rp, btn) {
    const orig = btn.textContent;
    btn.disabled = true; btn.textContent = "준비 중…";
    try {
      const d = await (await fetch("/api/mobile_viewer", { method: "POST" })).json();
      if (d.error) throw new Error(d.error);
      const mb = Math.round(rp.mb);
      if (confirm(
        `폰으로 보낼 파일 2개가 준비됐습니다.\n\n` +
        `① ${d.name}  (뷰어 · 한 번만 보내면 계속 재사용)\n` +
        `② ${rp.name}  (보고서 · ${mb}MB)\n\n` +
        `폰에서 ①을 열고 ②를 고르면 됩니다.\n\n파일 위치를 지금 열까요?`)) {
        revealPath(rp.path);
      }
    } catch (e) { alert("준비 실패: " + e.message); }
    finally { btn.disabled = false; btn.textContent = orig; }
  }

  // ===== 도움말(?) — 카드 제목의 ? 버튼이 <template> 내용을 모달로 띄운다 =====
  // 설명은 HTML 쪽 <template id="help-*"> 에 두어, 문구 수정 시 JS 를 건드릴 필요가 없다.
  const helpTitles = { create: "보고서 만들기 — 사용 설명", open: "보고서 열기 — 사용 설명" };
  function openHelp(key) {
    const tpl = document.getElementById("help-" + key);
    if (!tpl) return;
    $("helpTitle").textContent = helpTitles[key] || "사용 설명";
    const body = $("helpBody");
    body.innerHTML = "";
    body.appendChild(tpl.content.cloneNode(true));
    body.scrollTop = 0;
    $("helpModal").style.display = "flex";
  }
  function closeHelp() { $("helpModal").style.display = "none"; }
  document.querySelectorAll(".help").forEach((b) => {
    b.onclick = () => openHelp(b.dataset.help);
  });
  $("helpClose").onclick = closeHelp;
  // 바깥(어두운 배경) 클릭 · Esc 로 닫기
  $("helpModal").onclick = (e) => { if (e.target === $("helpModal")) closeHelp(); };
  addEventListener("keydown", (e) => {
    if (e.key === "Escape" && $("helpModal").style.display === "flex") closeHelp();
  });

  loadReports();
})();
