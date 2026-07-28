(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let folder = null, ply = null, polling = null;

  // 파일/폴더 선택창을 띄우고 고른 경로를 돌려준다(취소·오류면 null).
  // 브라우저 <input type=file> 은 실제 경로를 주지 않으므로 백엔드가 Windows 네이티브 다이얼로그를 띄운다.
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
  // 폴더와 PLY 가 둘 다 정해졌을 때만 '분석 시작'을 활성화한다.
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

  // 분석 잡을 0.7초마다 폴링해 진행바·로그를 갱신하고, 끝나면 목록을 새로 고친다.
  // (분석은 수 분~수십 분이라 요청 하나로 붙들 수 없다 → 백엔드가 job_id 만 주고 여기서 따라간다)
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

  // .twin 을 뷰어로 연다 — pywebview 면 새 네이티브 창, 브라우저면 새 창(여러 개 동시에).
  function openReport(path) {
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

  let _reports = [];
  // reports/ 의 .twin 목록을 받아 최신순으로 그린다(항목마다 열기·PDF 버튼).
  // 비교 셀렉트도 같은 목록을 쓰므로 마지막에 함께 채운다.
  async function loadReports() {
    const list = $("reportList"); list.innerHTML = "";
    try {
      const d = await (await fetch("/api/reports")).json();
      _reports = (d.reports || []).sort((a, b) => b.mtime - a.mtime);
      if (!_reports.length) {
        list.innerHTML = '<div class="empty">아직 보고서가 없습니다. ①에서 만들거나 파일에서 여세요.</div>';
      }
      _reports.forEach((rp) => {
        const el = document.createElement("div"); el.className = "ritem";
        el.innerHTML = `<svg class="ic ritem-ic"><use href="#i-file"/></svg>`
          + `<span class="nm">${rp.name}<br><span class="meta">${rp.mb} MB · ${new Date(rp.mtime*1000).toLocaleString()}</span></span>`;
        const b = document.createElement("button"); b.className = "primary";
        b.innerHTML = `<svg class="ic"><use href="#i-open"/></svg>열기`;
        b.onclick = () => openReport(rp.path);
        const pb = document.createElement("button"); pb.className = "ghost";
        pb.innerHTML = `<svg class="ic"><use href="#i-download"/></svg>PDF`;
        pb.title = "검사 보고서 PDF 생성(표지·위치도·균열 대장·사진 대지)";
        pb.onclick = () => exportPdf(rp.path, pb);
        el.appendChild(pb); el.appendChild(b); list.appendChild(el);
      });
      fillCompareSelects();
    } catch (e) { list.innerHTML = '<div class="empty">목록 로드 실패: ' + e + '</div>'; }
  }

  // ===== 검사 비교 (시간축) =====
  function fillCompareSelects() {
    const mk = (sel, defIdx) => {
      sel.innerHTML = '<option value="">선택…</option>';
      // 시간순(오래된 → 최신)으로 나열해 '이전→이번' 선택이 자연스럽게
      [..._reports].sort((a, b) => a.mtime - b.mtime).forEach((rp) => {
        const o = document.createElement("option");
        o.value = rp.path;
        o.textContent = `${rp.name} · ${new Date(rp.mtime*1000).toLocaleDateString()}`;
        o.dataset.mtime = rp.mtime;
        sel.appendChild(o);
      });
      if (defIdx != null && sel.options.length > 1)
        sel.selectedIndex = Math.max(1, sel.options.length - 1 + defIdx);
    };
    mk($("cmpA"), _reports.length >= 2 ? -1 : null);   // 기본: 이전=두 번째 최신
    mk($("cmpB"), _reports.length >= 1 ? 0 : null);    //       이번=최신
    cmpCheck();
  }
  // 셀렉트에서 고른 보고서를 {path, mtime} 로 꺼낸다(미선택이면 null).
  function cmpSel(sel) { const o = sel.selectedOptions[0]; return o && o.value ? {path: o.value, mtime: +o.dataset.mtime} : null; }
  // 두 선택을 검사해 경고를 띄우고 '비교 실행' 활성 여부를 정한다.
  // 같은 보고서는 막고, 시간 역순은 막지 않되 자동 정렬됨을 알린다.
  function cmpCheck() {
    const a = cmpSel($("cmpA")), b = cmpSel($("cmpB"));
    const note = $("cmpNote"); let msg = "";
    if (a && b && a.path === b.path) msg = "같은 보고서끼리는 비교할 수 없습니다.";
    else if (a && b && a.mtime > b.mtime) msg = "⚠ 이전(T1)이 이번(T2)보다 최신입니다 — 실행 시 시간순으로 자동 정렬됩니다.";
    note.textContent = msg; note.classList.toggle("hidden", !msg);
    $("btnCompare").disabled = !(a && b && a.path !== b.path);
  }
  $("cmpA").onchange = cmpCheck; $("cmpB").onchange = cmpCheck;

  $("btnCompare").onclick = async () => {
    let a = cmpSel($("cmpA")), b = cmpSel($("cmpB"));
    if (!a || !b) return;
    if (a.mtime > b.mtime) [a, b] = [b, a];            // 시간순 보정(이전→이번)
    $("btnCompare").disabled = true;
    $("cmpProg").classList.remove("hidden"); $("cmpLog").classList.remove("hidden");
    $("cmpLog").textContent = "비교 시작(3D 정합 → 균열 매칭 → 판정)...\n";
    try {
      const r = await fetch("/api/compare", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ a: a.path, b: b.path }) });
      const d = await r.json();
      if (d.error) { alert(d.error); $("btnCompare").disabled = false; return; }
      pollCompare(d.job, d.out);
    } catch (e) { alert("비교 실패: " + e); $("btnCompare").disabled = false; }
  };

  let cmpPolling = null;
  // 비교 잡 폴링(0.5초). 완료되면 자산 목록을 갱신하고 비교 뷰어 열기를 제안한다.
  function pollCompare(job, out) {
    clearInterval(cmpPolling);
    cmpPolling = setInterval(async () => {
      const d = await (await fetch("/api/job?id=" + job)).json();
      $("cmpProgBar").style.width = Math.round((d.progress || 0) * 100) + "%";
      if (d.log) $("cmpLog").textContent = d.log.join("\n");
      $("cmpLog").scrollTop = $("cmpLog").scrollHeight;
      if (d.done) {
        clearInterval(cmpPolling); $("btnCompare").disabled = false;
        if (d.error) { alert("비교 오류: " + d.error); return; }
        $("cmpLog").textContent += "\n✅ 완료: " + d.result;
        loadAssets();
        if (confirm("비교 완료!\n비교 뷰어(상태색·고스트)로 열까요?")) openAsset(d.result);
      }
    }, 500);
  }

  // PDF 생성 잡을 띄우고 그 버튼 자체를 진행 표시로 쓴다(단계 표기 [n/4]).
  // 완료되면 /api/download 로 새 창을 열어 내려받는다.
  async function exportPdf(path, btn) {
    const old = btn.innerHTML;
    btn.disabled = true; btn.innerHTML = "생성 중…";
    try {
      const r = await fetch("/api/export", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ p: path }) });
      const d = await r.json();
      if (d.error) { alert(d.error); return; }
      const t = setInterval(async () => {
        const s = await (await fetch("/api/job?id=" + d.job)).json();
        if (s.log && s.log.length) btn.innerHTML = "생성 중… " + (s.log[s.log.length-1].match(/^\[\d\/\d\]/)||[""])[0];
        if (!s.done) return;
        clearInterval(t);
        btn.disabled = false; btn.innerHTML = old;
        if (s.error) { alert("PDF 생성 오류: " + s.error); return; }
        window.open("/api/download?path=" + encodeURIComponent(s.result), "_blank");
      }, 700);
    } catch (e) { alert("PDF 생성 실패: " + e); btn.disabled = false; btn.innerHTML = old; }
  }

  // asset.json 을 비교(자산) 모드 뷰어로 연다 — 상태색·이전 검사 고스트가 켜진 채로 뜬다.
  function openAsset(path) {
    const a = (window.pywebview && window.pywebview.api) || null;
    if (a && a.open_asset) { a.open_asset(path); }
    else { window.open("/viewer?asset=" + encodeURIComponent(path), "_blank", "width=1360,height=860"); }
  }

  // 영속 균열 대장(asset.json) 목록을 최신순으로 그린다. 아직 없으면 조용히 비워 둔다.
  async function loadAssets() {
    const list = $("assetList"); list.innerHTML = "";
    try {
      const d = await (await fetch("/api/assets")).json();
      if (!d.assets || !d.assets.length) return;
      d.assets.sort((a, b) => b.mtime - a.mtime).forEach((as) => {
        const el = document.createElement("div"); el.className = "ritem";
        el.innerHTML = `<svg class="ic ritem-ic"><use href="#i-clock"/></svg>`
          + `<span class="nm">${as.name}<br><span class="meta">${(as.ids||[]).join(" → ")} · 영속 균열 ${as.n} · ${new Date(as.mtime*1000).toLocaleString()}</span></span>`;
        const b = document.createElement("button"); b.className = "primary";
        b.innerHTML = `<svg class="ic"><use href="#i-open"/></svg>비교 열기`;
        b.onclick = () => openAsset(as.path);
        el.appendChild(b); list.appendChild(el);
      });
    } catch (e) { /* 자산 없음은 조용히 */ }
  }

  // (제거됨) AI 유형 재학습 카드 — 유형 분류 폐지. 검토 라벨(확인·오탐)은 계속 쌓이며
  // 검출(세그멘테이션) 모델 재학습에 쓰인다.

  loadReports();
  loadAssets();
})();
