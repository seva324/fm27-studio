(() => {
  "use strict";

  // Extract session auth token from query string (and clean it out of address bar)
  const params = new URLSearchParams(window.location.search);
  const TOKEN = params.get("token") || "";
  if (TOKEN && window.history && window.history.replaceState) {
    window.history.replaceState({}, document.title, window.location.pathname);
  }

  const SLASH_COMMANDS = [
    { key: "/analyze",   ico: "+", desc: "Analyze squad depth & morale",  insert: "/analyze " },
    { key: "/compare",   ico: "⇄", desc: "Compare with target player",    insert: "/compare " },
    { key: "/bestxi",    ico: "◈", desc: "Optimal starting XI & roles",   insert: "/bestxi " },
    { key: "/shortlist", ico: "⌕", desc: "Scout U23 transfer targets",    insert: "/shortlist " },
    { key: "/train",     ico: "◷", desc: "Weekly training schedule",      insert: "/train " },
    { key: "/heal",      ico: "✦", desc: "Heal player & clear injury",    insert: "/heal",    instant: true },
    { key: "/inspire",   ico: "⚡", desc: "Set morale 100, jadedness 0",  insert: "/inspire", instant: true },
  ];

  const state = {
    boot: null,
    status: null,
    aiSettings: null,
    tabs: [],             // [{ key, kind, id, title, subtab, meta }]
    activeTab: null,
    recent: [],           // [{ key, kind, id, title, tag, pinned }]
    dockOpen: false,
    aiMode: "chat",
    aiBusy: false,
    cmdIndex: 0,
    compareOtherUid: null,
    compareSub: "overview",
    searchState: { scope: "players", q: "", pos: "Any", job: "Any", min_age: "", max_age: "", min_ca: "", listed: false },
    transferState: { mode: "listed", q: "" },
    retireState: { kind: "players", min_age: 35, q: "" },
    sortState: {},        // tableId -> { col, asc }
    chatTurns: [],        // [{ role, text, steps, thinking, tools, error }]
  };

  // ---- HTTP Helpers ----
  async function apiGet(path) {
    const res = await fetch(path, { headers: { "X-Studio-Token": TOKEN } });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
    return res.json();
  }
  async function apiPost(path, body = {}) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Studio-Token": TOKEN },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
    return res.json();
  }

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function initialsOf(name, fallback = "?") {
    return (name || fallback).split(/\s+/).filter(Boolean).map((w) => w[0]).join("").slice(0, 2).toUpperCase() || fallback;
  }

  function avatarUrl(kind, uid) {
    if (!uid || (kind !== "player" && kind !== "staff")) return "";
    return `/api/avatar/${kind}/${encodeURIComponent(uid)}?token=${encodeURIComponent(TOKEN)}`;
  }

  function avatarHTML(kind, uid, initials, extraClass = "") {
    const src = avatarUrl(kind, uid);
    const img = src
      ? `<img src="${src}" alt="" loading="lazy" onload="this.parentElement.classList.add('has-photo')" onerror="this.remove()" />`
      : "";
    return `<div class="avatar ${extraClass}"><span class="avatar-init">${esc(initials)}</span>${img}</div>`;
  }

  function navAvatarHTML(r) {
    if (r.kind === "player" || r.kind === "staff") {
      const init = initialsOf(r.title, "P");
      const src = avatarUrl(r.kind, r.id);
      return `<span class="nav-avatar"><span>${esc(init)}</span><img src="${src}" alt="" loading="lazy" onload="this.parentElement.classList.add('has-photo')" onerror="this.remove()" /></span>`;
    }
    const icons = { club: "◈", nation: "◎" };
    return `<span class="ico">${icons[r.kind] || "•"}</span>`;
  }

  function numClass(val) {
    const n = Number(val) || 0;
    if (n >= 18) return "n-elite";
    if (n >= 15) return "n-high";
    if (n >= 10) return "n-mid";
    return "n-low";
  }

  // ---- Stacked Toast Notifications (Reference #20) ----
  function toast(message, kind = "ok") {
    const stack = document.getElementById("toastStack");
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.innerHTML = `<span class="toast-dot"></span><span>${esc(message)}</span>`;
    stack.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transform = "translateY(8px)";
      el.style.transition = "all .2s";
      setTimeout(() => el.remove(), 220);
    }, 3200);
  }

  // ---- Magnetic Hover Binding ----
  function bindMagnetic(root = document) {
    root.querySelectorAll(".magnetic").forEach((el) => {
      if (el.dataset.magBound) return;
      el.dataset.magBound = "1";
      el.addEventListener("mousemove", (e) => {
        const r = el.getBoundingClientRect();
        const dx = (e.clientX - (r.left + r.width / 2)) * 0.15;
        const dy = (e.clientY - (r.top + r.height / 2)) * 0.18;
        el.style.transform = `translate(${dx.toFixed(1)}px, ${dy.toFixed(1)}px)`;
      });
      el.addEventListener("mouseleave", () => { el.style.transform = "translate(0, 0)"; });
    });
  }

  // ---- Layout Persistence & Resizable 3-Pane Splitters ----
  function initLayout() {
    const rootStyle = document.documentElement.style;
    const savedL = localStorage.getItem("fm27_left_w");
    const savedR = localStorage.getItem("fm27_right_w");
    if (savedL) rootStyle.setProperty("--left-w", savedL + "px");
    if (savedR) rootStyle.setProperty("--right-w", savedR + "px");

    const paneLeft = document.getElementById("paneLeft");
    const paneRight = document.getElementById("paneRight");
    const divLeft = document.getElementById("divLeft");
    const divRight = document.getElementById("divRight");

    function bindDivider(el, side) {
      el.addEventListener("mousedown", (e) => {
        e.preventDefault();
        el.classList.add("dragging");
        const startX = e.clientX;
        const startW = side === "left" ? paneLeft.offsetWidth : paneRight.offsetWidth;
        function onMove(ev) {
          const dx = ev.clientX - startX;
          if (side === "left") {
            const w = Math.max(175, Math.min(310, startW + dx));
            rootStyle.setProperty("--left-w", w + "px");
            localStorage.setItem("fm27_left_w", String(w));
          } else {
            const w = Math.max(300, Math.min(580, startW - dx));
            rootStyle.setProperty("--right-w", w + "px");
            localStorage.setItem("fm27_right_w", String(w));
          }
        }
        function onUp() {
          el.classList.remove("dragging");
          window.removeEventListener("mousemove", onMove);
          window.removeEventListener("mouseup", onUp);
        }
        window.addEventListener("mousemove", onMove);
        window.addEventListener("mouseup", onUp);
      });
    }
    bindDivider(divLeft, "left");
    bindDivider(divRight, "right");

    const btnL = document.getElementById("toggleLeft");
    const btnR = document.getElementById("toggleRight");
    btnL.onclick = () => {
      const c = paneLeft.classList.toggle("collapsed");
      divLeft.classList.toggle("hidden", c);
      btnL.classList.toggle("active", !c);
    };
    const toggleRight = () => {
      const c = paneRight.classList.toggle("collapsed");
      divRight.classList.toggle("hidden", c);
      btnR.classList.toggle("active", !c);
    };
    btnR.onclick = toggleRight;
    document.getElementById("closeRight").onclick = toggleRight;

    const zoomSlider = document.getElementById("zoomSlider");
    const zoomLabel = document.getElementById("zoomLabel");
    zoomSlider.oninput = () => {
      zoomLabel.textContent = zoomSlider.value + "%";
      document.body.style.zoom = zoomSlider.value + "%";
    };
  }

  // ---- Status Bar & Topbar Sync ----
  function renderStatus(st) {
    if (!st) return;
    state.status = st;
    const dot = document.getElementById("liveDot");
    const text = document.getElementById("liveText");
    const bDot = document.getElementById("statusBottomDot");
    const bText = document.getElementById("statusWriteText");
    const attachLbl = document.getElementById("btnAttachLabel");

    if (st.attached) {
      dot.className = st.read_only ? "status-dot warn" : "status-dot active";
      bDot.className = dot.className;
      text.textContent = `FM27 Live · ${st.game_date} · ${st.managed_club}`;
      bText.textContent = st.read_only
        ? "Attached (Read-Only) · Run as Admin to write"
        : "Connected · Edits write directly to FM27";
      attachLbl.textContent = "Refresh Live";
    } else {
      dot.className = "status-dot";
      bDot.className = "status-dot";
      text.textContent = `Snapshot · ${st.game_date} · ${st.managed_club}`;
      bText.textContent = st.dirty
        ? `${st.dirty} staged edit(s) · Press Load Game to flush into FM27`
        : "Snapshot mode · Press Load Game to attach";
      attachLbl.textContent = "Load Game";
    }

    const c = st.counts || {};
    document.getElementById("statusCounts").textContent =
      `${(c.players || 0).toLocaleString()} Players · ${(c.staff || 0).toLocaleString()} Staff · ${(c.clubs || 0).toLocaleString()} Clubs`;
    document.getElementById("navMyClubLabel").textContent = `My Club (${st.managed_club})`;
    document.getElementById("navHistoryBadge").textContent = String(st.journal_count || 0);
    document.getElementById("navBuild").textContent = `Build ${st.fm_build || "—"}`;
    const dockSave = document.getElementById("dockSave");
    if (dockSave) dockSave.classList.toggle("has-dirty", (st.dirty || 0) > 0);
  }

  // ---- Recent & Pinned List (with Pin + Close Tab controls) ----
  function loadRecent() {
    try {
      const saved = JSON.parse(localStorage.getItem("fm27_recent") || "[]");
      if (Array.isArray(saved)) state.recent = saved;
    } catch (_) {}
  }
  function saveRecent() {
    try { localStorage.setItem("fm27_recent", JSON.stringify(state.recent.slice(0, 16))); } catch (_) {}
  }
  function pushRecent(item) {
    const existing = state.recent.find((x) => x.key === item.key);
    const pinned = existing ? existing.pinned : !!item.pinned;
    state.recent = state.recent.filter((x) => x.key !== item.key);
    state.recent.unshift({ ...item, pinned });
    state.recent.sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned));
    if (state.recent.length > 14) {
      const pinnedItems = state.recent.filter((x) => x.pinned);
      const unpinnedItems = state.recent.filter((x) => !x.pinned);
      state.recent = [...pinnedItems, ...unpinnedItems.slice(0, Math.max(2, 14 - pinnedItems.length))];
    }
    saveRecent();
    renderRecent();
  }
  function removeRecent(key) {
    const wasActive = state.activeTab === key;
    state.recent = state.recent.filter((x) => x.key !== key);
    saveRecent();
    const tidx = state.tabs.findIndex((t) => t.key === key);
    if (tidx !== -1) state.tabs.splice(tidx, 1);
    if (wasActive) {
      const fallback = state.recent[0];
      if (fallback) {
        openEntity(fallback.kind, fallback.id, fallback.title);
        return;
      }
      openMyClub();
      return;
    }
    renderRecent();
    renderWorkspaceNavActive();
  }
  function clearUnpinnedRecent() {
    const activeItem = state.recent.find((x) => x.key === state.activeTab);
    const removedActive = activeItem && !activeItem.pinned;
    const keepKeys = new Set(state.recent.filter((x) => x.pinned).map((x) => x.key));
    state.recent = state.recent.filter((x) => x.pinned);
    state.tabs = state.tabs.filter((t) => keepKeys.has(t.key) || !["player", "club", "staff", "nation"].includes(t.kind));
    saveRecent();
    if (removedActive) {
      const first = state.recent[0];
      if (first) { openEntity(first.kind, first.id, first.title); return; }
      openMyClub();
      return;
    }
    renderRecent();
  }
  function renderRecent() {
    const host = document.getElementById("recentNavList");
    const clearBtn = document.getElementById("btnClearRecent");
    if (clearBtn) {
      clearBtn.style.display = state.recent.some((r) => !r.pinned) ? "inline-block" : "none";
    }
    if (!state.recent.length) {
      host.innerHTML = '<div class="empty-note" style="padding:8px 10px;text-align:left;">No recent items</div>';
      return;
    }
    host.innerHTML = state.recent.map((r) => `
      <div class="nav-item magnetic ${state.activeTab === r.key ? "active" : ""}" data-rkey="${esc(r.key)}" title="${esc(r.title)}">
        ${navAvatarHTML(r)}
        <span class="lbl">${esc(r.title)}</span>
        <span class="recent-actions">
          <button class="pin-btn ${r.pinned ? "pinned" : ""}" data-pin="${esc(r.key)}" title="${r.pinned ? "Unpin" : "Pin to top"}">${r.pinned ? "★" : "☆"}</button>
          <button class="close-btn" data-close-recent="${esc(r.key)}" title="Close">×</button>
        </span>
        <span class="nav-pill">${esc(r.tag || r.kind)}</span>
      </div>
    `).join("");
    host.querySelectorAll(".nav-item").forEach((el) => {
      el.onclick = (e) => {
        if (e.target.closest("[data-pin]") || e.target.closest("[data-close-recent]")) return;
        const item = state.recent.find((x) => x.key === el.dataset.rkey);
        if (item) openEntity(item.kind, item.id, item.title);
      };
    });
    host.querySelectorAll("[data-pin]").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        const item = state.recent.find((x) => x.key === btn.dataset.pin);
        if (item) {
          item.pinned = !item.pinned;
          state.recent.sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned));
          saveRecent();
          renderRecent();
        }
      };
    });
    host.querySelectorAll("[data-close-recent]").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        removeRecent(btn.dataset.closeRecent);
      };
    });
    bindMagnetic(host);
  }

  // ---- Tabs & Navigation ----
  function openTab(tab) {
    const existing = state.tabs.find((t) => t.key === tab.key);
    if (existing) {
      Object.assign(existing, tab, { subtab: tab.subtab || existing.subtab });
    } else {
      state.tabs.push(tab);
      if (state.tabs.length > 10) state.tabs.shift();
    }
    state.activeTab = tab.key;
    renderTabs();
    renderWorkspaceNavActive();
    renderRecent();
    renderActiveView();
    updateAiContext();
    updateFloatingDock();
  }

  function closeTab(key) {
    const idx = state.tabs.findIndex((t) => t.key === key);
    if (idx === -1) return;
    state.tabs.splice(idx, 1);
    if (state.activeTab === key) {
      const next = state.tabs[Math.max(0, idx - 1)] || state.tabs[0];
      if (next) {
        state.activeTab = next.key;
      } else {
        openMyClub();
        return;
      }
    }
    renderTabs();
    renderWorkspaceNavActive();
    renderRecent();
    renderActiveView();
    updateAiContext();
    updateFloatingDock();
  }

  function getActiveTab() {
    return state.tabs.find((t) => t.key === state.activeTab) || null;
  }

  function renderTabs() {
    const bar = document.getElementById("tabBar");
    if (!bar) return;
    bar.innerHTML = state.tabs.map((t) => `
      <div class="tab ${t.key === state.activeTab ? "active" : ""}" data-tab="${esc(t.key)}">
        <span>${esc(t.title)}</span>
        <span class="tab-x" data-close="${esc(t.key)}">×</span>
      </div>
    `).join("");
    bar.querySelectorAll(".tab").forEach((el) => {
      el.onclick = (e) => {
        if (e.target.dataset.close) {
          closeTab(e.target.dataset.close);
          return;
        }
        state.activeTab = el.dataset.tab;
        renderTabs();
        renderWorkspaceNavActive();
        renderRecent();
        renderActiveView();
        updateAiContext();
        updateFloatingDock();
      };
    });
  }

  function renderWorkspaceNavActive() {
    const tab = getActiveTab();
    document.querySelectorAll(".pane-left .nav-item[data-nav]").forEach((el) => {
      const nav = el.dataset.nav;
      let on = false;
      if (tab) {
        if (nav === "myclub" && tab.kind === "club" && String(tab.id) === String(state.status?.managed_club_id)) on = true;
        else if (nav === tab.kind) on = true;
      }
      el.classList.toggle("active", on);
    });
  }

  function openEntity(kind, id, title = "") {
    if (kind === "player") {
      openTab({ key: `player:${id}`, kind: "player", id: Number(id), title: title || `Player #${id}`, subtab: "attributes" });
    } else if (kind === "club") {
      openTab({ key: `club:${id}`, kind: "club", id, title: title || `Club`, subtab: "squad" });
    } else if (kind === "staff") {
      openTab({ key: `staff:${id}`, kind: "staff", id: Number(id), title: title || `Staff #${id}` });
    } else if (kind === "nation") {
      openTab({ key: `nation:${id}`, kind: "nation", id: String(id), title: title || String(id), subtab: "players" });
    }
  }

  function openMyClub() {
    const cid = state.status?.managed_club_id || 5598;
    const name = state.status?.managed_club || "AC Milan";
    openEntity("club", cid, name);
  }

  function openSearch(query = "") {
    if (query !== undefined && query !== null) state.searchState.q = query;
    openTab({ key: "search", kind: "search", id: "search", title: "People Search" });
  }

  // ---- Floating Expandable Action Dock (Reference #10) ----
  function setDockOpen(open) {
    state.dockOpen = open;
    document.getElementById("dockPopover").classList.toggle("collapsed", !open);
    document.getElementById("dockPlus").textContent = open ? "×" : "+";
  }

  function updateFloatingDock() {
    const pop = document.getElementById("dockPopover");
    const tab = getActiveTab();
    let actions = [];
    if (tab && tab.kind === "player") {
      actions = [
        { ico: "✦", label: "Heal Player",    sub: "10000 · Fit", fn: () => runPlayerPreset(tab.id, "heal") },
        { ico: "⚡", label: "Inspire",        sub: "Morale 100",  fn: () => runPlayerPreset(tab.id, "inspire") },
        { ico: "★", label: "Make Wonderkid", sub: "PA 200",      fn: () => runPlayerPreset(tab.id, "wonderkid") },
        { ico: "⊘", label: "Destroy",        sub: "Morale 0",    fn: () => runPlayerPreset(tab.id, "destroy") },
      ];
    } else if (tab && tab.kind === "club") {
      actions = [
        { ico: "✦", label: "Heal Full Squad",    sub: "All fit",    fn: () => runClubPreset(tab.id, "heal_squad") },
        { ico: "⚡", label: "Inspire Full Squad", sub: "Morale 100", fn: () => runClubPreset(tab.id, "inspire_squad") },
      ];
    } else {
      actions = [
        { ico: "⌂", label: "Open My Club",  sub: state.status?.managed_club || "Milan", fn: openMyClub },
        { ico: "⌕", label: "People Search", sub: "42k players",                         fn: () => openSearch("") },
        { ico: "⇄", label: "Transfers",     sub: "Market",                              fn: () => openTab({ key: "transfers", kind: "transfers", id: "transfers", title: "Future Transfers" }) },
      ];
    }
    pop.innerHTML = actions.map((a, idx) => `
      <button class="dock-action-pill magnetic" data-dock-idx="${idx}">
        <span>${a.ico}</span><span>${esc(a.label)}</span><span class="sub">${esc(a.sub)}</span>
      </button>
    `).join("");
    pop.querySelectorAll(".dock-action-pill").forEach((btn) => {
      btn.onclick = () => {
        setDockOpen(false);
        actions[Number(btn.dataset.dockIdx)].fn();
      };
    });
    bindMagnetic(pop);
  }

  async function runPlayerPreset(uid, preset) {
    try {
      const res = await apiPost(`/api/player/${uid}/preset`, { preset });
      renderStatus(res.status);
      toast(res.message, res.ok ? "ok" : "error");
      if (res.player && getActiveTab()?.key === `player:${uid}`) {
        renderPlayerPage(res.player, getActiveTab());
      }
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function runClubPreset(clubId, preset) {
    try {
      const res = await apiPost(`/api/club/${encodeURIComponent(clubId)}/preset`, { preset });
      renderStatus(res.status);
      toast(res.message, res.ok ? "ok" : "error");
      if (res.club && getActiveTab()?.key === `club:${clubId}`) {
        renderClubPage(res.club, getActiveTab());
      }
    } catch (err) {
      toast(err.message, "error");
    }
  }

  // ---- Render Active Center View ----
  async function renderActiveView() {
    const tab = getActiveTab();
    const body = document.getElementById("centerBody");
    const meta = document.getElementById("centerMeta");
    const setMeta = (txt) => { if (meta) meta.textContent = txt; };
    if (!tab) return;

    try {
      if (tab.kind === "player") {
        setMeta(state.status?.attached ? "Real-time RAM Sync" : "Snapshot Player");
        const data = await apiGet(`/api/player/${tab.id}`);
        tab.title = data.name;
        tab.meta = { name: data.name, club: data.club_raw || data.club, ca: data.ca };
        pushRecent({ key: tab.key, kind: "player", id: data.unique_id, title: data.name, tag: String(data.ca) });
        renderTabs();
        updateAiContext();
        renderPlayerPage(data, tab);
      } else if (tab.kind === "club") {
        setMeta("Club & Squad");
        const data = await apiGet(`/api/club/${encodeURIComponent(tab.id)}`);
        tab.title = data.name;
        tab.meta = { name: data.name, club: data.raw_name };
        pushRecent({ key: tab.key, kind: "club", id: data.id, title: data.name, tag: "Club" });
        renderTabs();
        updateAiContext();
        renderClubPage(data, tab);
      } else if (tab.kind === "staff") {
        setMeta("Staff Profile");
        const data = await apiGet(`/api/staff/${tab.id}`);
        tab.title = data.name;
        tab.meta = { name: data.name, club: data.club_raw || data.club };
        pushRecent({ key: tab.key, kind: "staff", id: data.unique_id, title: data.name, tag: "Staff" });
        renderTabs();
        updateAiContext();
        renderStaffPage(data);
      } else if (tab.kind === "nation") {
        setMeta("National Pool");
        const data = await apiGet(`/api/nation/${encodeURIComponent(tab.id)}`);
        tab.title = data.name;
        tab.meta = { name: data.name };
        pushRecent({ key: tab.key, kind: "nation", id: data.name, title: data.name, tag: "Nation" });
        renderTabs();
        updateAiContext();
        renderNationPage(data, tab);
      } else if (tab.kind === "search") {
        setMeta("Global Search");
        await renderSearchPage();
      } else if (tab.kind === "transfers") {
        setMeta("Transfer Market");
        await renderTransfersPage();
      } else if (tab.kind === "retirements") {
        setMeta("Retirement Watch");
        await renderRetirementsPage();
      } else if (tab.kind === "history") {
        setMeta("Edit Journal");
        await renderHistoryPage();
      }
    } catch (err) {
      body.innerHTML = `<div class="empty-state">Error: ${esc(err.message)}</div>`;
    }
  }

  // ---- Inline Edit Helper (Reference #23) ----
  function startInlineEdit(el, uid, column, currentVal, isWide = false) {
    if (el.querySelector("input")) return;
    const origHTML = el.innerHTML;
    const input = document.createElement("input");
    input.type = "text";
    input.value = currentVal == null ? "" : String(currentVal);
    input.className = isWide ? "inline-input inline-input-wide" : "inline-input";
    el.innerHTML = "";
    el.appendChild(input);
    input.focus();
    input.select();

    let done = false;
    const finish = async (commit) => {
      if (done) return;
      done = true;
      const next = input.value.trim();
      if (!commit || next === String(currentVal)) {
        el.innerHTML = origHTML;
        return;
      }
      try {
        const res = await apiPost(`/api/player/${uid}/edit`, { column, value: next });
        renderStatus(res.status);
        toast(res.message || res.error || "Updated", res.ok ? "ok" : "error");
        if (res.player && getActiveTab()?.key === `player:${uid}`) {
          renderPlayerPage(res.player, getActiveTab());
        } else {
          el.innerHTML = origHTML;
        }
      } catch (err) {
        toast(err.message, "error");
        el.innerHTML = origHTML;
      }
    };
    input.onkeydown = (e) => {
      if (e.key === "Enter") finish(true);
      if (e.key === "Escape") finish(false);
    };
    input.onblur = () => finish(true);
  }

  // ---- PLAYER VIEW ----
  function renderPlayerPage(p, tab) {
    const body = document.getElementById("centerBody");
    const sub = tab.subtab || "attributes";
    const initials = (p.name || "PL").split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
    const shirt = p.shirt_number ? `${p.shirt_number}. ` : "";

    let subHTML = "";
    if (sub === "attributes") {
      const renderGroup = (title, list) => `
        <div class="card">
          <div class="card-head"><span>${title}</span><span style="color:var(--faint)">Click to edit</span></div>
          <div class="card-body">
            ${list.map((a) => `
              <div class="attr-row">
                <span class="attr-name">${esc(a.label)}</span>
                <span class="num-badge editable ${numClass(a.value)}" data-edit-col="${esc(a.column)}" data-edit-val="${a.value}">${a.value}</span>
              </div>
            `).join("")}
          </div>
        </div>
      `;
      const isGk = (p.pos || "").startsWith("GK");
      const condPct = Math.max(0, Math.min(100, (Number(p.condition || 0) / 100)));
      const sharpPct = Math.max(0, Math.min(100, (Number(p.sharpness || 0) / 100)));
      const moraleVal = Number(p.morale ?? 0);
      const moralePct = Math.max(0, Math.min(100, moraleVal <= 20 && !state.status?.attached ? moraleVal * 5 : moraleVal));
      const jadePct = Math.max(4, Math.min(100, Math.abs(Number(p.jadedness || 0)) / 10));

      subHTML = `
        <div class="grid-4">
          ${renderGroup(isGk ? "Goalkeeping" : "Technical", p.attributes[isGk ? "Goalkeeping" : "Technical"] || [])}
          ${renderGroup("Mental", p.attributes.Mental || [])}
          ${renderGroup("Physical", p.attributes.Physical || [])}
          <div class="card">
            <div class="card-head"><span>Live Status &amp; Hidden</span><span style="color:var(--muted)">RAM</span></div>
            <div class="card-body">
              <div class="meter-row">
                <div class="meter-head"><span>Condition</span><span class="meter-val" data-edit-col="condition" data-edit-val="${p.condition ?? 0}">${Number(p.condition || 0).toLocaleString()}</span></div>
                <div class="meter-track"><div class="meter-fill" style="width:${condPct}%"></div></div>
              </div>
              <div class="meter-row">
                <div class="meter-head"><span>Match Sharpness</span><span class="meter-val" data-edit-col="match_sharpness" data-edit-val="${p.sharpness ?? 0}">${Number(p.sharpness || 0).toLocaleString()}</span></div>
                <div class="meter-track"><div class="meter-fill dark" style="width:${sharpPct}%"></div></div>
              </div>
              <div class="meter-row">
                <div class="meter-head"><span>Morale (0–100)</span><span class="meter-val" data-edit-col="morale" data-edit-val="${p.morale ?? 0}">${p.morale ?? "—"}</span></div>
                <div class="meter-track"><div class="meter-fill" style="width:${moralePct}%"></div></div>
              </div>
              <div class="meter-row" style="margin-bottom:10px;">
                <div class="meter-head"><span>Jadedness</span><span class="meter-val" data-edit-col="jadedness" data-edit-val="${p.jadedness ?? 0}">${p.jadedness ?? 0}</span></div>
                <div class="meter-track"><div class="meter-fill dark" style="width:${jadePct}%"></div></div>
              </div>
              ${(p.attributes.Hidden || []).map((a) => `
                <div class="attr-row">
                  <span class="attr-name">${esc(a.label)}</span>
                  <span class="num-badge editable ${numClass(a.value)}" data-edit-col="${esc(a.column)}" data-edit-val="${a.value}">${a.value}</span>
                </div>
              `).join("")}
            </div>
          </div>
        </div>
        <div class="grid-2" style="margin-top:12px;">
          ${renderGroup("Personality", p.attributes.Personality || [])}
          ${renderGroup(isGk ? "Technical" : "Goalkeeping", p.attributes[isGk ? "Technical" : "Goalkeeping"] || [])}
        </div>
      `;
    } else if (sub === "contract") {
      subHTML = `
        <div class="grid-2">
          <div class="card">
            <div class="card-head"><span>Contract &amp; Financials</span><span style="color:var(--faint)">Click values to edit</span></div>
            <div class="card-body">
              <div class="attr-row"><span class="attr-name">Club</span><span class="link-ink" data-open-club="${esc(p.club_id || p.club_raw)}">${esc(p.club)}</span></div>
              <div class="attr-row"><span class="attr-name">Weekly Wage (£ raw)</span><span class="meter-val" data-edit-col="salary_weekly_raw" data-edit-val="${p.wage_raw}" data-wide="1">${p.wage} (${p.wage_raw.toLocaleString()})</span></div>
              <div class="attr-row"><span class="attr-name">Asking Price (£ raw)</span><span class="meter-val" data-edit-col="asking_price_raw" data-edit-val="${p.value_raw}" data-wide="1">${p.value} (${p.value_raw.toLocaleString()})</span></div>
              <div class="attr-row"><span class="attr-name">Contract Expires (YYYY-MM-DD)</span><span class="meter-val" data-edit-col="contract_end_date" data-edit-val="${esc(p.contract_iso)}" data-wide="1">${esc(p.contract)}</span></div>
              <div class="attr-row"><span class="attr-name">Joined Club (YYYY-MM-DD)</span><span class="meter-val" data-edit-col="joined_club_date" data-edit-val="${esc(p.joined_club_iso)}" data-wide="1">${esc(p.joined_club_date)}</span></div>
            </div>
          </div>
          <div class="card">
            <div class="card-head"><span>Identity &amp; Reputation</span></div>
            <div class="card-body">
              <div class="attr-row"><span class="attr-name">Full Legal Name</span><strong>${esc(p.full_name)}</strong></div>
              <div class="attr-row"><span class="attr-name">Unique ID</span><code>${p.unique_id}</code></div>
              <div class="attr-row"><span class="attr-name">Date of Birth</span><span>${esc(p.dob)}</span></div>
              <div class="attr-row"><span class="attr-name">Height (cm)</span><span class="meter-val" data-edit-col="height_cm" data-edit-val="${p.height_cm || 180}">${p.height_cm || "—"}</span></div>
              <div class="attr-row"><span class="attr-name">Reputation (Cur / Home / World)</span><strong>${p.current_reputation} / ${p.home_reputation} / ${p.world_reputation}</strong></div>
            </div>
          </div>
        </div>
      `;
    } else if (sub === "positions") {
      subHTML = `
        <div class="card">
          <div class="card-head"><span>Positional Competence (1–20)</span><span style="color:var(--faint)">Click any rating to edit</span></div>
          <div class="card-body">
            <div class="grid-4">
              ${p.positions.map((pos) => `
                <div class="attr-row" style="padding:7px 8px;border:1px solid var(--line);border-radius:8px;background:#fafaf8;">
                  <strong>${esc(pos.label)}</strong>
                  <span class="num-badge editable ${numClass(pos.value)}" data-edit-col="${esc(pos.column)}" data-edit-val="${pos.value}">${pos.value}</span>
                </div>
              `).join("")}
            </div>
          </div>
        </div>
      `;
    } else if (sub === "injury") {
      subHTML = `
        <div class="card">
          <div class="card-head">
            <span>Injury &amp; Fitness Status</span>
            <button class="mini-btn mini-btn-dark magnetic" id="btnHealInline">✦ Heal Player Now</button>
          </div>
          <div class="card-body">
            <div class="attr-row"><span class="attr-name">Status</span><span class="etag ${p.injured ? "danger" : "lime"}">${p.injured ? esc(p.injury || "Injured") : "Fit"}</span></div>
            <div class="attr-row"><span class="attr-name">Injury Start</span><span>${esc(p.injury_start_date)}</span></div>
            <div class="attr-row"><span class="attr-name">Expected Return</span><span>${esc(p.injury_expected_return)}</span></div>
            <div class="attr-row"><span class="attr-name">Light / Full Training Days</span><span>${esc(p.injury_light_days || "—")} / ${esc(p.injury_full_days || "—")}</span></div>
          </div>
        </div>
      `;
    } else if (sub === "compare") {
      subHTML = `
        <div class="card card-spaced">
          <div class="card-head"><span>Compare ${esc(p.name)} With Another Player</span></div>
          <div class="card-body">
            <div class="filter-bar">
              <input id="cmpSearchInput" class="filter-input" style="flex:1;" placeholder="Search player name (e.g. Pulisic, Haaland, Yamal)…" />
              <button class="mini-btn mini-btn-dark" id="btnCmpSearch">Find</button>
            </div>
            <div id="cmpPickerResults" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;"></div>
          </div>
        </div>
        <div id="cmpTableHost"><div class="empty-note">Pick a player above to view side-by-side comparison.</div></div>
      `;
    }

    body.innerHTML = `
      <div class="entity-card">
        <div class="entity-top">
          <div class="entity-id">
            ${avatarHTML("player", p.unique_id, initials)}
            <div>
              <div class="entity-name">${esc(shirt + p.name)}</div>
              <div class="entity-tags">
                <span class="etag clickable" data-open-club="${esc(p.club_id || p.club_raw)}">${esc(p.club)}</span>
                <span class="etag clickable" data-open-nation="${esc(p.nationality)}">${esc(p.nationality)}</span>
                <span class="etag">${p.age} Years</span>
                <span class="etag">${esc(p.pos)}</span>
                <span class="etag">Feet L${p.left_foot} / R${p.right_foot}</span>
                ${p.injured ? `<span class="etag danger">Injured: ${esc(p.injury)}</span>` : `<span class="etag lime">● ${state.status?.attached ? "Live Synced" : "Fit"}</span>`}
              </div>
            </div>
          </div>
          <div class="segmented" id="playerSubNav">
            ${[["attributes","Attributes"],["contract","Contract"],["positions","Positions"],["injury","Injury"],["compare","Compare"]].map(([k, lbl]) => `
              <div class="seg-item ${sub === k ? "active" : ""}" data-sub="${k}">${lbl}</div>
            `).join("")}
          </div>
        </div>

        <div class="kpi-row">
          <div class="kpi">
            <span class="kpi-label">Current / Potential Ability (click to edit)</span>
            <div class="kpi-val-row">
              <span class="kpi-val">
                <span class="meter-val" data-edit-col="ca" data-edit-val="${p.ca}">${p.ca}</span> /
                <span class="meter-val" data-edit-col="pa" data-edit-val="${p.pa}">${p.pa}</span>
              </span>
              <span class="kpi-trend">${p.ca >= 170 ? "World Class" : p.pa >= 170 ? "High PA" : "First Team"}</span>
            </div>
          </div>
          <div class="kpi">
            <span class="kpi-label">Asking Price</span>
            <div class="kpi-val-row"><span class="kpi-val">${esc(p.value)}</span><span class="kpi-trend">${p.transfer_listed ? "Listed" : "Valued"}</span></div>
          </div>
          <div class="kpi">
            <span class="kpi-label">Weekly Wage</span>
            <div class="kpi-val-row"><span class="kpi-val">${esc(p.wage)}</span><span style="font-size:10.5px;color:var(--muted);">until ${esc(p.contract)}</span></div>
          </div>
          <div class="kpi">
            <span class="kpi-label">Best Role Rating</span>
            <div class="kpi-val-row"><span class="kpi-val">${p.rating.toFixed(2)}%</span><span class="kpi-trend">${esc(p.best_role)}</span></div>
          </div>
        </div>
      </div>
      ${subHTML}
    `;

    // Wire sub-navigation
    body.querySelectorAll("#playerSubNav .seg-item").forEach((el) => {
      el.onclick = () => { tab.subtab = el.dataset.sub; renderPlayerPage(p, tab); };
    });
    // Wire inline editors
    body.querySelectorAll("[data-edit-col]").forEach((el) => {
      el.onclick = () => startInlineEdit(el, p.unique_id, el.dataset.editCol, el.dataset.editVal, el.dataset.wide === "1");
    });
    // Wire club / nation links
    body.querySelectorAll("[data-open-club]").forEach((el) => {
      el.onclick = () => { if (el.dataset.openClub) openEntity("club", el.dataset.openClub, p.club); };
    });
    body.querySelectorAll("[data-open-nation]").forEach((el) => {
      el.onclick = () => { if (el.dataset.openNation && el.dataset.openNation !== "—") openEntity("nation", el.dataset.openNation, el.dataset.openNation); };
    });
    const healBtn = document.getElementById("btnHealInline");
    if (healBtn) healBtn.onclick = () => runPlayerPreset(p.unique_id, "heal");

    // Wire comparison search
    if (sub === "compare") {
      const doCmpSearch = async () => {
        const q = document.getElementById("cmpSearchInput").value.trim();
        const res = await apiGet(`/api/search?scope=players&q=${encodeURIComponent(q)}&limit=8`);
        const host = document.getElementById("cmpPickerResults");
        host.innerHTML = res.rows.map((r) => `
          <button class="mini-btn" data-pick-uid="${r.unique_id}">${esc(r.name)} (${r.ca})</button>
        `).join("");
        host.querySelectorAll("[data-pick-uid]").forEach((b) => {
          b.onclick = () => loadCompareTable(p.unique_id, Number(b.dataset.pickUid));
        });
      };
      document.getElementById("btnCmpSearch").onclick = doCmpSearch;
      document.getElementById("cmpSearchInput").onkeydown = (e) => { if (e.key === "Enter") doCmpSearch(); };
      if (state.compareOtherUid && state.compareOtherUid !== p.unique_id) {
        loadCompareTable(p.unique_id, state.compareOtherUid);
      }
    }
    bindMagnetic(body);
  }

  function renderRadarSvg(radar, nameA, nameB) {
    const cx = 155, cy = 148, rMax = 96;
    const n = radar.length || 8;
    const pt = (idx, val) => {
      const ang = -Math.PI / 2 + (idx * 2 * Math.PI) / n;
      const ratio = Math.max(0, Math.min(20, Number(val) || 0)) / 20;
      return [cx + rMax * ratio * Math.cos(ang), cy + rMax * ratio * Math.sin(ang)];
    };
    const ringPoly = (lvl) =>
      Array.from({ length: n }, (_, i) => pt(i, lvl).map((v) => v.toFixed(1)).join(",")).join(" ");
    const polyA = radar.map((d, i) => pt(i, d.a).map((v) => v.toFixed(1)).join(",")).join(" ");
    const polyB = radar.map((d, i) => pt(i, d.b).map((v) => v.toFixed(1)).join(",")).join(" ");
    const spokes = radar.map((_, i) => {
      const [x, y] = pt(i, 20);
      return `<line x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" stroke="#e6e6e0" stroke-width="1"/>`;
    }).join("");
    const dotsA = radar.map((d, i) => {
      const [x, y] = pt(i, d.a);
      return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3.3" fill="#111111"><title>${esc(nameA)} · ${esc(d.label)}: ${d.a}</title></circle>`;
    }).join("");
    const dotsB = radar.map((d, i) => {
      const [x, y] = pt(i, d.b);
      return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3.3" fill="#d4ff38" stroke="#4f6b00" stroke-width="1.3"><title>${esc(nameB)} · ${esc(d.label)}: ${d.b}</title></circle>`;
    }).join("");
    const labels = radar.map((d, i) => {
      const ang = -Math.PI / 2 + (i * 2 * Math.PI) / n;
      const lx = cx + (rMax + 26) * Math.cos(ang);
      const ly = cy + (rMax + 22) * Math.sin(ang);
      const anchor = Math.abs(Math.cos(ang)) < 0.25 ? "middle" : (Math.cos(ang) > 0 ? "start" : "end");
      return `
        <text x="${lx.toFixed(1)}" y="${(ly - 2).toFixed(1)}" text-anchor="${anchor}" font-size="10.5" font-weight="700" fill="#111111">${esc(d.label)}</text>
        <text x="${lx.toFixed(1)}" y="${(ly + 10).toFixed(1)}" text-anchor="${anchor}" font-size="9.5" font-weight="700" fill="#6e6e73">${d.a} / ${d.b}</text>
      `;
    }).join("");
    return `
      <svg class="radar-svg" viewBox="0 0 310 296" xmlns="http://www.w3.org/2000/svg">
        ${[5, 10, 15, 20].map((lvl) => `<polygon points="${ringPoly(lvl)}" fill="none" stroke="${lvl === 20 ? "#d6d6ce" : "#ecece6"}" stroke-width="${lvl === 20 ? 1.2 : 1}"/>`).join("")}
        ${spokes}
        <polygon points="${polyB}" fill="rgba(212, 255, 56, 0.42)" stroke="#6d8f00" stroke-width="2" stroke-linejoin="round"/>
        <polygon points="${polyA}" fill="rgba(17, 17, 17, 0.16)" stroke="#111111" stroke-width="2.2" stroke-linejoin="round"/>
        ${dotsB}
        ${dotsA}
        ${labels}
      </svg>
    `;
  }

  function formatMoneyDiff(diff) {
    if (!diff) return "0";
    const sign = diff > 0 ? "+" : "-";
    const abs = Math.abs(diff);
    if (abs >= 1_000_000) return `${sign}£${(abs / 1_000_000).toFixed(1)}M`;
    if (abs >= 1_000) return `${sign}£${(abs / 1_000).toFixed(1)}k`;
    return `${sign}£${abs.toLocaleString()}`;
  }

  function renderCompareView(data) {
    const host = document.getElementById("cmpTableHost");
    if (!host) return;
    const mode = state.compareSub || "overview";
    const a = data.a, b = data.b;
    const ov = data.overview || {};
    const radar = data.radar || [];

    const pct = (v, max) => Math.max(4, Math.min(100, Math.round((Number(v || 0) / Math.max(1, max)) * 100)));
    const maxVal = Math.max(a.value_raw || 1, b.value_raw || 1);
    const maxWage = Math.max(a.wage_raw || 1, b.wage_raw || 1);
    const diffPill = (val, text, invert = false) => {
      const pos = invert ? val < 0 : val > 0;
      const neg = invert ? val > 0 : val < 0;
      const cls = pos ? "n-diff-pos" : neg ? "n-diff-neg" : "n-low";
      return `<span class="num-badge ${cls}">${esc(text)}</span>`;
    };

    let bodyHTML = "";
    if (mode === "overview") {
      bodyHTML = `
        <div class="cmp-overview-grid">
          <div class="card">
            <div class="card-head"><span>Attribute Radar Comparison</span><span style="color:var(--muted)">0–20 Scale</span></div>
            <div class="radar-wrap">
              ${renderRadarSvg(radar, a.name, b.name)}
              <div class="radar-legend">
                <span><span class="radar-dot a"></span>${esc(a.name)}</span>
                <span><span class="radar-dot b"></span>${esc(b.name)}</span>
              </div>
            </div>
          </div>

          <div class="card">
            <div class="card-head"><span>Core Rating, Ability &amp; Valuation</span><span style="color:var(--muted)">A vs B</span></div>
            <div class="card-body">
              <div class="cmp-metric-row">
                <div class="cmp-metric-top">
                  <span>Best Role Rating</span>
                  ${diffPill(ov.rating_diff || 0, `${(ov.rating_diff || 0) > 0 ? "+" : ""}${(ov.rating_diff || 0).toFixed(2)}%`)}
                </div>
                <div class="cmp-metric-vals">
                  <div><div class="cmp-val-a">${a.rating.toFixed(2)}%</div><div class="cmp-sub-label">${esc(a.best_role)}</div></div>
                  <div style="text-align:right;"><div class="cmp-val-b">${b.rating.toFixed(2)}%</div><div class="cmp-sub-label">${esc(b.best_role)}</div></div>
                </div>
                <div class="cmp-dual-bars">
                  <div class="cmp-bar-track left"><div class="cmp-bar-a" style="width:${pct(a.rating, 100)}%"></div></div>
                  <div class="cmp-bar-track"><div class="cmp-bar-b" style="width:${pct(b.rating, 100)}%"></div></div>
                </div>
              </div>

              <div class="cmp-metric-row">
                <div class="cmp-metric-top">
                  <span>Current Ability (CA)</span>
                  ${diffPill(ov.ca_diff || 0, `${(ov.ca_diff || 0) > 0 ? "+" : ""}${ov.ca_diff || 0}`)}
                </div>
                <div class="cmp-metric-vals">
                  <span class="cmp-val-a">${a.ca}</span>
                  <span class="cmp-val-b">${b.ca}</span>
                </div>
                <div class="cmp-dual-bars">
                  <div class="cmp-bar-track left"><div class="cmp-bar-a" style="width:${pct(a.ca, 200)}%"></div></div>
                  <div class="cmp-bar-track"><div class="cmp-bar-b" style="width:${pct(b.ca, 200)}%"></div></div>
                </div>
              </div>

              <div class="cmp-metric-row">
                <div class="cmp-metric-top">
                  <span>Potential Ability (PA)</span>
                  ${diffPill(ov.pa_diff || 0, `${(ov.pa_diff || 0) > 0 ? "+" : ""}${ov.pa_diff || 0}`)}
                </div>
                <div class="cmp-metric-vals">
                  <span class="cmp-val-a">${a.pa}</span>
                  <span class="cmp-val-b">${b.pa}</span>
                </div>
                <div class="cmp-dual-bars">
                  <div class="cmp-bar-track left"><div class="cmp-bar-a" style="width:${pct(a.pa, 200)}%"></div></div>
                  <div class="cmp-bar-track"><div class="cmp-bar-b" style="width:${pct(b.pa, 200)}%"></div></div>
                </div>
              </div>

              <div class="cmp-metric-row">
                <div class="cmp-metric-top">
                  <span>Market Value / Asking Price</span>
                  ${diffPill(ov.value_diff || 0, formatMoneyDiff(ov.value_diff || 0))}
                </div>
                <div class="cmp-metric-vals">
                  <span class="cmp-val-a">${esc(a.value)}</span>
                  <span class="cmp-val-b">${esc(b.value)}</span>
                </div>
                <div class="cmp-dual-bars">
                  <div class="cmp-bar-track left"><div class="cmp-bar-a" style="width:${pct(a.value_raw, maxVal)}%"></div></div>
                  <div class="cmp-bar-track"><div class="cmp-bar-b" style="width:${pct(b.value_raw, maxVal)}%"></div></div>
                </div>
              </div>

              <div class="cmp-metric-row">
                <div class="cmp-metric-top">
                  <span>Weekly Wage &amp; Contract</span>
                  ${diffPill(ov.wage_diff || 0, formatMoneyDiff(ov.wage_diff || 0) + "/wk", true)}
                </div>
                <div class="cmp-metric-vals">
                  <div><div class="cmp-val-a">${esc(a.wage)}</div><div class="cmp-sub-label">until ${esc(a.contract)}</div></div>
                  <div style="text-align:right;"><div class="cmp-val-b">${esc(b.wage)}</div><div class="cmp-sub-label">until ${esc(b.contract)}</div></div>
                </div>
                <div class="cmp-dual-bars">
                  <div class="cmp-bar-track left"><div class="cmp-bar-a" style="width:${pct(a.wage_raw, maxWage)}%"></div></div>
                  <div class="cmp-bar-track"><div class="cmp-bar-b" style="width:${pct(b.wage_raw, maxWage)}%"></div></div>
                </div>
              </div>
            </div>
          </div>
        </div>
      `;
    } else {
      bodyHTML = `
        <div class="card">
          <div class="card-head">
            <span>Detailed Attribute Comparison (${data.rows.length} attributes)</span>
            <span class="link-ink" id="openOtherPlayer">Open ${esc(b.name)} →</span>
          </div>
          <div class="table-wrap">
            <table class="data-table">
              <thead><tr><th>Group</th><th>Attribute</th><th>${esc(a.name)}</th><th>${esc(b.name)}</th><th>Diff</th></tr></thead>
              <tbody>
                ${data.rows.map((r) => `
                  <tr>
                    <td style="color:var(--muted)">${esc(r.group)}</td>
                    <td>${esc(r.label)}</td>
                    <td><span class="num-badge ${numClass(r.a)}">${r.a}</span></td>
                    <td><span class="num-badge ${numClass(r.b)}">${r.b}</span></td>
                    <td><span class="num-badge ${r.diff > 0 ? "n-diff-pos" : r.diff < 0 ? "n-diff-neg" : "n-low"}">${r.diff > 0 ? "+" + r.diff : r.diff}</span></td>
                  </tr>
                `).join("")}
              </tbody>
            </table>
          </div>
        </div>
      `;
    }

    host.innerHTML = `
      <div class="cmp-versus">
        <div class="cmp-side">
          ${avatarHTML("player", a.unique_id, initialsOf(a.name), "avatar-sm")}
          <div style="min-width:0;">
            <div class="cmp-side-name">${esc(a.name)}</div>
            <div class="cmp-side-sub">${esc(a.club)} · ${esc(a.pos)} · ${a.age}y</div>
          </div>
        </div>
        <div class="cmp-vs-pill">VS</div>
        <div class="cmp-side right">
          ${avatarHTML("player", b.unique_id, initialsOf(b.name), "avatar-sm")}
          <div style="min-width:0;">
            <div class="cmp-side-name link-ink" id="cmpOpenB">${esc(b.name)}</div>
            <div class="cmp-side-sub">${esc(b.club)} · ${esc(b.pos)} · ${b.age}y</div>
          </div>
        </div>
      </div>

      <div class="cmp-header-strip">
        <div class="segmented" id="cmpModeNav">
          <div class="seg-item ${mode === "overview" ? "active" : ""}" data-cmode="overview">Overview &amp; Radar</div>
          <div class="seg-item ${mode === "details" ? "active" : ""}" data-cmode="details">Detailed Attributes</div>
        </div>
        <span style="font-size:11px;color:var(--muted);">Black = ${esc(a.name)} · Acid Lime = ${esc(b.name)}</span>
      </div>
      ${bodyHTML}
    `;

    host.querySelectorAll("#cmpModeNav .seg-item").forEach((el) => {
      el.onclick = () => { state.compareSub = el.dataset.cmode; renderCompareView(data); };
    });
    const openB = document.getElementById("cmpOpenB");
    if (openB) openB.onclick = () => openEntity("player", b.unique_id, b.name);
    const openOther = document.getElementById("openOtherPlayer");
    if (openOther) openOther.onclick = () => openEntity("player", b.unique_id, b.name);
  }

  async function loadCompareTable(uid1, uid2) {
    state.compareOtherUid = uid2;
    const host = document.getElementById("cmpTableHost");
    if (!host) return;
    host.innerHTML = '<div class="empty-note">Loading comparison…</div>';
    const data = await apiGet(`/api/player/${uid1}/compare?other=${uid2}`);
    renderCompareView(data);
  }

  // ---- CLUB VIEW ----
  function renderClubPage(c, tab) {
    const body = document.getElementById("centerBody");
    const sub = tab.subtab || "squad";
    const initials = (c.name || "CL").split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();

    let subHTML = "";
    if (sub === "squad") {
      const nextFix = c.next_fixture
        ? `${c.next_fixture.kickoff_date} vs ${c.next_fixture.opponent || c.next_fixture.away_club}`
        : "No upcoming fixture in snapshot";
      subHTML = `
        <div class="grid-2">
          <div class="card">
            <div class="card-head">
              <span>Key Club Staff</span>
              <span class="link-ink" id="jumpClubStaff">All ${c.staff.length} →</span>
            </div>
            <div class="card-body">
              ${c.staff.slice(0, 5).map((s) => `
                <div class="attr-row">
                  <span class="link-ink" data-open-staff="${s.unique_id}">${esc(s.name)} — ${esc(s.job)}</span>
                  <span class="num-badge ${numClass(Math.round(s.ca / 10))}">${s.ca || "—"}</span>
                </div>
              `).join("") || '<div class="empty-note">No staff recorded.</div>'}
            </div>
          </div>
          <div class="card">
            <div class="card-head"><span>Squad Health &amp; Next Match</span><span style="color:var(--muted)">${esc(state.status?.game_date)}</span></div>
            <div class="card-body">
              <div class="attr-row"><span class="attr-name">Next Match</span><strong>${esc(nextFix)}</strong></div>
              <div class="attr-row"><span class="attr-name">First Team Fit</span><span class="num-badge n-elite">${c.fit_count}/${c.squad_count}</span></div>
              <div class="attr-row"><span class="attr-name">Average Morale</span><span class="num-badge n-high">${c.avg_morale}</span></div>
              <div class="attr-row"><span class="attr-name">Weekly Wage Bill</span><strong>${esc(c.weekly_wage_bill)} / wk</strong></div>
            </div>
          </div>
        </div>
        <div class="card">
          <div class="card-head"><span>Squad (${c.squad.length} players)</span><span style="color:var(--muted)">Click header to sort · Click row to open</span></div>
          <div class="table-wrap" id="clubSquadTable"></div>
        </div>
      `;
    } else if (sub === "staff") {
      subHTML = `
        <div class="card">
          <div class="card-head"><span>Club Staff (${c.staff.length})</span><span style="color:var(--muted)">Click any staff member</span></div>
          <div class="table-wrap" id="clubStaffTable"></div>
        </div>
      `;
    } else if (sub === "finances") {
      subHTML = `
        <div class="card">
          <div class="card-head"><span>Finances &amp; Facilities</span></div>
          <div class="card-body">
            <div class="attr-row"><span class="attr-name">Overall Balance</span><strong>${esc(c.balance)}</strong></div>
            <div class="attr-row"><span class="attr-name">Transfer Budget</span><strong>${esc(c.transfer_budget)}</strong></div>
            <div class="attr-row"><span class="attr-name">Weekly Wage Bill</span><strong>${esc(c.weekly_wage_bill)}</strong></div>
            <div class="attr-row"><span class="attr-name">Training Facilities</span><span class="num-badge ${numClass(c.training_facilities)}">${c.training_facilities ?? "—"}</span></div>
            <div class="attr-row"><span class="attr-name">Youth Facilities</span><span class="num-badge ${numClass(c.youth_facilities)}">${c.youth_facilities ?? "—"}</span></div>
            <div class="attr-row"><span class="attr-name">Youth Coaching</span><span class="num-badge ${numClass(c.youth_coaching)}">${c.youth_coaching ?? "—"}</span></div>
            <div class="attr-row"><span class="attr-name">Youth Recruitment</span><span class="num-badge ${numClass(c.youth_recruitment)}">${c.youth_recruitment ?? "—"}</span></div>
          </div>
        </div>
      `;
    } else if (sub === "fixtures") {
      subHTML = `
        <div class="card">
          <div class="card-head"><span>Upcoming Fixtures (${c.fixtures.length})</span></div>
          <div class="table-wrap">
            <table class="data-table">
              <thead><tr><th>Date</th><th>Time</th><th>Competition</th><th>Home</th><th>Away</th></tr></thead>
              <tbody>
                ${c.fixtures.map((f) => `
                  <tr>
                    <td>${esc(f.kickoff_date)}</td><td>${esc(f.kickoff_time || "—")}</td>
                    <td>${esc(f.competition || "—")}</td><td>${esc(f.home_club || "—")}</td><td>${esc(f.away_club || "—")}</td>
                  </tr>
                `).join("") || '<tr><td colspan="5" class="empty-note">No fixtures recorded.</td></tr>'}
              </tbody>
            </table>
          </div>
        </div>
      `;
    }

    body.innerHTML = `
      <div class="entity-card">
        <div class="entity-top">
          <div class="entity-id">
            <div class="avatar"><span class="avatar-init">${esc(initials)}</span></div>
            <div>
              <div class="entity-name">${esc(c.name)}</div>
              <div class="entity-tags">
                <span class="etag">${esc(c.competition)}</span>
                <span class="etag clickable" data-open-nation="${esc(c.nation)}">${esc(c.nation)}</span>
                <span class="etag">${esc(c.stadium)} · ${esc(c.founded)}</span>
                ${c.raw_name === state.status?.managed_club_raw ? `<span class="etag lime">● Your Managed Club</span>` : ""}
              </div>
            </div>
          </div>
          <div class="segmented" id="clubSubNav">
            ${[["squad","Squad"],["staff",`Staff (${c.staff.length})`],["finances","Finances & Facilities"],["fixtures","Fixtures"]].map(([k, lbl]) => `
              <div class="seg-item ${sub === k ? "active" : ""}" data-sub="${k}">${lbl}</div>
            `).join("")}
          </div>
        </div>

        <div class="kpi-row">
          <div class="kpi"><span class="kpi-label">Reputation</span><div class="kpi-val-row"><span class="kpi-val">${c.reputation.toLocaleString()}</span><span class="kpi-trend">${esc(c.competition)}</span></div></div>
          <div class="kpi"><span class="kpi-label">Club Balance</span><div class="kpi-val-row"><span class="kpi-val">${esc(c.balance)}</span></div></div>
          <div class="kpi"><span class="kpi-label">Transfer Budget</span><div class="kpi-val-row"><span class="kpi-val">${esc(c.transfer_budget)}</span></div></div>
          <div class="kpi"><span class="kpi-label">Training / Youth</span><div class="kpi-val-row"><span class="kpi-val">${c.training_facilities ?? "—"} / ${c.youth_facilities ?? "—"}</span><span class="kpi-trend">Facilities</span></div></div>
        </div>
      </div>
      ${subHTML}
    `;

    body.querySelectorAll("#clubSubNav .seg-item").forEach((el) => {
      el.onclick = () => { tab.subtab = el.dataset.sub; renderClubPage(c, tab); };
    });
    const jumpStaff = document.getElementById("jumpClubStaff");
    if (jumpStaff) jumpStaff.onclick = () => { tab.subtab = "staff"; renderClubPage(c, tab); };
    body.querySelectorAll("[data-open-staff]").forEach((el) => {
      el.onclick = () => openEntity("staff", Number(el.dataset.openStaff));
    });
    body.querySelectorAll("[data-open-nation]").forEach((el) => {
      el.onclick = () => { if (el.dataset.openNation && el.dataset.openNation !== "—") openEntity("nation", el.dataset.openNation, el.dataset.openNation); };
    });

    if (sub === "squad") {
      renderSortableTable("clubSquadTable", [
        { key: "name", label: "Player", render: (r) => `<span class="link-ink">${esc(r.name)}</span>` },
        { key: "pos", label: "Pos" },
        { key: "age", label: "Age" },
        { key: "ca", label: "CA", render: (r) => `<strong>${r.ca}</strong>` },
        { key: "pa", label: "PA" },
        { key: "morale", label: "Morale" },
        { key: "condition_pct", label: "Cond", render: (r) => `${r.condition_pct}%` },
        { key: "wage_raw", label: "Wage", render: (r) => esc(r.wage) },
        { key: "contract", label: "Contract" },
      ], c.squad, (r) => openEntity("player", r.unique_id, r.name));
    } else if (sub === "staff") {
      renderSortableTable("clubStaffTable", [
        { key: "name", label: "Name", render: (r) => `<span class="link-ink">${esc(r.name)}</span>` },
        { key: "job", label: "Job" },
        { key: "age", label: "Age" },
        { key: "nationality", label: "Nationality" },
        { key: "ca", label: "CA", render: (r) => `<strong>${r.ca}</strong>` },
        { key: "pa", label: "PA" },
      ], c.staff, (r) => openEntity("staff", r.unique_id, r.name));
    }
  }

  // ---- STAFF VIEW ----
  function renderStaffPage(s) {
    const body = document.getElementById("centerBody");
    const initials = (s.name || "ST").split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
    body.innerHTML = `
      <div class="entity-card">
        <div class="entity-top">
          <div class="entity-id">
            ${avatarHTML("staff", s.unique_id, initials)}
            <div>
              <div class="entity-name">${esc(s.name)}</div>
              <div class="entity-tags">
                <span class="etag">${esc(s.job)}</span>
                <span class="etag clickable" data-open-club="${esc(s.club_id || s.club_raw)}">${esc(s.club)}</span>
                <span class="etag clickable" data-open-nation="${esc(s.nationality)}">${esc(s.nationality)}</span>
                <span class="etag">${s.age || "—"} Years</span>
              </div>
            </div>
          </div>
          <div class="kpi-trend" style="font-size:12px;padding:5px 12px;">CA ${s.ca} / PA ${s.pa}</div>
        </div>
      </div>
      <div class="grid-2">
        <div class="card">
          <div class="card-head"><span>Staff Details</span></div>
          <div class="card-body">
            <div class="attr-row"><span class="attr-name">Unique ID</span><code>${s.unique_id}</code></div>
            <div class="attr-row"><span class="attr-name">Club</span><span class="link-ink" data-open-club="${esc(s.club_id || s.club_raw)}">${esc(s.club)}</span></div>
            <div class="attr-row"><span class="attr-name">Role</span><strong>${esc(s.job)}</strong></div>
            <div class="attr-row"><span class="attr-name">Date of Birth</span><span>${esc(s.dob)}</span></div>
          </div>
        </div>
        <div class="card">
          <div class="card-head"><span>Club Colleagues (${s.colleagues.length})</span></div>
          <div class="card-body">
            ${s.colleagues.slice(0, 10).map((col) => `
              <div class="attr-row">
                <span class="link-ink" data-open-staff="${col.unique_id}">${esc(col.name)} — ${esc(col.job)}</span>
                <span class="num-badge n-mid">${col.ca || "—"}</span>
              </div>
            `).join("") || '<div class="empty-note">No colleagues listed.</div>'}
          </div>
        </div>
      </div>
    `;
    body.querySelectorAll("[data-open-club]").forEach((el) => {
      el.onclick = () => { if (el.dataset.openClub) openEntity("club", el.dataset.openClub, s.club); };
    });
    body.querySelectorAll("[data-open-staff]").forEach((el) => {
      el.onclick = () => openEntity("staff", Number(el.dataset.openStaff));
    });
    body.querySelectorAll("[data-open-nation]").forEach((el) => {
      el.onclick = () => { if (el.dataset.openNation && el.dataset.openNation !== "—") openEntity("nation", el.dataset.openNation, el.dataset.openNation); };
    });
  }

  // ---- NATION VIEW ----
  function renderNationPage(n, tab) {
    const body = document.getElementById("centerBody");
    const sub = tab.subtab || "players";
    body.innerHTML = `
      <div class="entity-card">
        <div class="entity-top">
          <div class="entity-id">
            <div class="avatar">${esc((n.name || "NA").slice(0, 2).toUpperCase())}</div>
            <div>
              <div class="entity-name">${esc(n.name)}</div>
              <div class="entity-tags">
                <span class="etag">${n.counts.players.toLocaleString()} Players</span>
                <span class="etag">${n.counts.clubs.toLocaleString()} Clubs</span>
                <span class="etag">${n.counts.staff.toLocaleString()} Staff</span>
                <span class="etag lime">Top CA ${n.best_ca}</span>
              </div>
            </div>
          </div>
          <div class="segmented" id="nationSubNav">
            ${[["players","Players"],["clubs","Clubs"],["staff","Staff"]].map(([k, lbl]) => `
              <div class="seg-item ${sub === k ? "active" : ""}" data-sub="${k}">${lbl}</div>
            `).join("")}
          </div>
        </div>
      </div>
      <div class="card"><div class="table-wrap" id="nationTableHost"></div></div>
    `;
    body.querySelectorAll("#nationSubNav .seg-item").forEach((el) => {
      el.onclick = () => { tab.subtab = el.dataset.sub; renderNationPage(n, tab); };
    });
    if (sub === "clubs") {
      renderSortableTable("nationTableHost", [
        { key: "name", label: "Club", render: (r) => `<span class="link-ink">${esc(r.name)}</span>` },
        { key: "competition", label: "Competition" },
        { key: "reputation", label: "Reputation" },
        { key: "balance", label: "Balance" },
      ], n.clubs, (r) => openEntity("club", r.id, r.name));
    } else if (sub === "staff") {
      renderSortableTable("nationTableHost", [
        { key: "name", label: "Name", render: (r) => `<span class="link-ink">${esc(r.name)}</span>` },
        { key: "club", label: "Club" },
        { key: "job", label: "Job" },
        { key: "age", label: "Age" },
        { key: "ca", label: "CA" },
      ], n.staff, (r) => openEntity("staff", r.unique_id, r.name));
    } else {
      renderSortableTable("nationTableHost", [
        { key: "name", label: "Player", render: (r) => `<span class="link-ink">${esc(r.name)}</span>` },
        { key: "club", label: "Club" },
        { key: "pos", label: "Pos" },
        { key: "age", label: "Age" },
        { key: "ca", label: "CA", render: (r) => `<strong>${r.ca}</strong>` },
        { key: "pa", label: "PA" },
        { key: "value", label: "Value" },
      ], n.players, (r) => openEntity("player", r.unique_id, r.name));
    }
  }

  // ---- PEOPLE SEARCH ----
  async function renderSearchPage() {
    const body = document.getElementById("centerBody");
    const st = state.searchState;
    const qs = new URLSearchParams({
      scope: st.scope, q: st.q, pos: st.pos, job: st.job,
      min_age: st.min_age, max_age: st.max_age, min_ca: st.min_ca,
      listed: st.listed ? "1" : "", limit: "250",
    });
    const data = await apiGet(`/api/search?${qs.toString()}`);

    const posOptions = ["Any", ...(state.boot?.positions || []).map((p) => p.label)];
    const jobOptions = ["Any", ...(state.boot?.staff_jobs || [])];

    body.innerHTML = `
      <div class="entity-card">
        <div class="entity-top" style="margin-bottom:10px;">
          <div class="segmented" id="searchScopeNav">
            ${[["players","Players"],["staff","Staff"],["clubs","Clubs"],["nations","Nations"]].map(([k, lbl]) => `
              <div class="seg-item ${st.scope === k ? "active" : ""}" data-scope="${k}">${lbl}</div>
            `).join("")}
          </div>
          <span style="font-size:11.5px;color:var(--muted);">${data.count} results</span>
        </div>
        <div class="filter-bar">
          <input id="srchQ" class="filter-input" style="min-width:200px;" placeholder="Name or club…" value="${esc(st.q)}" />
          ${st.scope === "players" ? `
            <select id="srchPos" class="mini-select">${posOptions.map((o) => `<option ${st.pos === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>
            <input id="srchMinAge" class="filter-input filter-sm" placeholder="Min Age" value="${esc(st.min_age)}" />
            <input id="srchMaxAge" class="filter-input filter-sm" placeholder="Max Age" value="${esc(st.max_age)}" />
            <input id="srchMinCa" class="filter-input filter-sm" placeholder="Min CA" value="${esc(st.min_ca)}" />
            <label class="filter-check"><input type="checkbox" id="srchListed" ${st.listed ? "checked" : ""} /> Listed only</label>
          ` : st.scope === "staff" ? `
            <select id="srchJob" class="mini-select">${jobOptions.map((o) => `<option ${st.job === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>
            <input id="srchMinAge" class="filter-input filter-sm" placeholder="Min Age" value="${esc(st.min_age)}" />
            <input id="srchMaxAge" class="filter-input filter-sm" placeholder="Max Age" value="${esc(st.max_age)}" />
            <input id="srchMinCa" class="filter-input filter-sm" placeholder="Min CA" value="${esc(st.min_ca)}" />
          ` : ""}
          <button class="mini-btn mini-btn-dark" id="btnRunSearch">Search</button>
          <button class="mini-btn" id="btnResetSearch">Reset</button>
        </div>
      </div>
      <div class="card"><div class="table-wrap" id="searchTableHost"></div></div>
    `;

    body.querySelectorAll("#searchScopeNav .seg-item").forEach((el) => {
      el.onclick = () => { st.scope = el.dataset.scope; renderSearchPage(); };
    });
    const run = () => {
      st.q = document.getElementById("srchQ").value.trim();
      const posEl = document.getElementById("srchPos");
      const jobEl = document.getElementById("srchJob");
      if (posEl) st.pos = posEl.value;
      if (jobEl) st.job = jobEl.value;
      const mi = document.getElementById("srchMinAge");
      const ma = document.getElementById("srchMaxAge");
      const mc = document.getElementById("srchMinCa");
      const ls = document.getElementById("srchListed");
      if (mi) st.min_age = mi.value.trim();
      if (ma) st.max_age = ma.value.trim();
      if (mc) st.min_ca = mc.value.trim();
      if (ls) st.listed = ls.checked;
      renderSearchPage();
    };
    document.getElementById("btnRunSearch").onclick = run;
    document.getElementById("srchQ").onkeydown = (e) => { if (e.key === "Enter") run(); };
    document.getElementById("btnResetSearch").onclick = () => {
      Object.assign(st, { q: "", pos: "Any", job: "Any", min_age: "", max_age: "", min_ca: "", listed: false });
      renderSearchPage();
    };

    if (st.scope === "staff") {
      renderSortableTable("searchTableHost", [
        { key: "name", label: "Name", render: (r) => `<span class="link-ink">${esc(r.name)}</span>` },
        { key: "club", label: "Club" },
        { key: "job", label: "Job" },
        { key: "age", label: "Age" },
        { key: "nationality", label: "Nationality" },
        { key: "ca", label: "CA", render: (r) => `<strong>${r.ca}</strong>` },
        { key: "pa", label: "PA" },
      ], data.rows, (r) => openEntity("staff", r.unique_id, r.name));
    } else if (st.scope === "clubs") {
      renderSortableTable("searchTableHost", [
        { key: "name", label: "Club", render: (r) => `<span class="link-ink">${esc(r.name)}</span>` },
        { key: "nation", label: "Nation" },
        { key: "competition", label: "Competition" },
        { key: "reputation", label: "Reputation" },
        { key: "training_facilities", label: "Training" },
        { key: "youth_facilities", label: "Youth" },
      ], data.rows, (r) => openEntity("club", r.id, r.name));
    } else if (st.scope === "nations") {
      renderSortableTable("searchTableHost", [
        { key: "name", label: "Nation", render: (r) => `<span class="link-ink">${esc(r.name)}</span>` },
        { key: "players", label: "Players" },
        { key: "clubs", label: "Clubs" },
        { key: "staff", label: "Staff" },
        { key: "best_ca", label: "Top CA" },
      ], data.rows, (r) => openEntity("nation", r.name, r.name));
    } else {
      renderSortableTable("searchTableHost", [
        { key: "name", label: "Player", render: (r) => `<span class="link-ink">${esc(r.name)}</span>` },
        { key: "club", label: "Club" },
        { key: "pos", label: "Pos" },
        { key: "age", label: "Age" },
        { key: "ca", label: "CA", render: (r) => `<strong>${r.ca}</strong>` },
        { key: "pa", label: "PA" },
        { key: "value_raw", label: "Value", render: (r) => esc(r.value) },
        { key: "wage_raw", label: "Wage", render: (r) => esc(r.wage) },
        { key: "contract", label: "Contract" },
      ], data.rows, (r) => openEntity("player", r.unique_id, r.name));
    }
  }

  // ---- FUTURE TRANSFERS ----
  async function renderTransfersPage() {
    const body = document.getElementById("centerBody");
    const st = state.transferState;
    const data = await apiGet(`/api/transfers?mode=${encodeURIComponent(st.mode)}&q=${encodeURIComponent(st.q)}`);
    body.innerHTML = `
      <div class="entity-card">
        <div class="entity-top" style="margin-bottom:10px;">
          <div class="segmented" id="tfModeNav">
            ${[["agreed",`Agreed Future (${data.agreed_count})`],["listed","Transfer Listed"],["loan","Loan Listed"],["expiring","Expiring Contracts"]].map(([k, lbl]) => `
              <div class="seg-item ${st.mode === k ? "active" : ""}" data-mode="${k}">${lbl}</div>
            `).join("")}
          </div>
          <span style="font-size:11.5px;color:var(--muted);">${data.count} players</span>
        </div>
        <div class="filter-bar">
          <input id="tfQuery" class="filter-input" style="min-width:220px;" placeholder="Filter by player or club…" value="${esc(st.q)}" />
          <button class="mini-btn mini-btn-dark" id="btnTfFilter">Filter</button>
        </div>
      </div>
      <div class="card"><div class="table-wrap" id="tfTableHost"></div></div>
    `;
    body.querySelectorAll("#tfModeNav .seg-item").forEach((el) => {
      el.onclick = () => { st.mode = el.dataset.mode; renderTransfersPage(); };
    });
    const run = () => { st.q = document.getElementById("tfQuery").value.trim(); renderTransfersPage(); };
    document.getElementById("btnTfFilter").onclick = run;
    document.getElementById("tfQuery").onkeydown = (e) => { if (e.key === "Enter") run(); };

    renderSortableTable("tfTableHost", [
      { key: "name", label: "Player", render: (r) => `<span class="link-ink">${esc(r.name)}</span>` },
      { key: "club", label: "Club" },
      { key: "pos", label: "Pos" },
      { key: "age", label: "Age" },
      { key: "ca", label: "CA", render: (r) => `<strong>${r.ca}</strong>` },
      { key: "pa", label: "PA" },
      { key: "value_raw", label: "Asking Price", render: (r) => esc(r.value) },
      { key: "contract", label: "Contract" },
    ], data.rows, (r) => openEntity("player", r.unique_id, r.name));
  }

  // ---- RETIREMENTS ----
  async function renderRetirementsPage() {
    const body = document.getElementById("centerBody");
    const st = state.retireState;
    const data = await apiGet(`/api/retirements?kind=${st.kind}&min_age=${st.min_age}&q=${encodeURIComponent(st.q)}`);
    const ageChoices = st.kind === "players" ? [33, 35, 37, 38, 40] : [55, 60, 65, 70, 75];
    body.innerHTML = `
      <div class="entity-card">
        <div class="entity-top" style="margin-bottom:10px;">
          <div class="segmented" id="retKindNav">
            <div class="seg-item ${st.kind === "players" ? "active" : ""}" data-kind="players">Veteran Players</div>
            <div class="seg-item ${st.kind === "staff" ? "active" : ""}" data-kind="staff">Veteran Staff</div>
          </div>
          <span style="font-size:11.5px;color:var(--muted);">${data.count} tracked (age ${data.min_age}+)</span>
        </div>
        <div class="filter-bar">
          <span style="font-size:11.5px;color:var(--muted);">Min Age</span>
          <select id="retAge" class="mini-select">${ageChoices.map((a) => `<option value="${a}" ${Number(st.min_age) === a ? "selected" : ""}>${a}+</option>`).join("")}</select>
          <input id="retQ" class="filter-input" style="min-width:200px;" placeholder="Filter name or club…" value="${esc(st.q)}" />
          <button class="mini-btn mini-btn-dark" id="btnRetRun">Filter</button>
        </div>
      </div>
      <div class="card"><div class="table-wrap" id="retTableHost"></div></div>
    `;
    body.querySelectorAll("#retKindNav .seg-item").forEach((el) => {
      el.onclick = () => {
        st.kind = el.dataset.kind;
        st.min_age = st.kind === "players" ? 35 : 60;
        renderRetirementsPage();
      };
    });
    const run = () => {
      st.min_age = Number(document.getElementById("retAge").value);
      st.q = document.getElementById("retQ").value.trim();
      renderRetirementsPage();
    };
    document.getElementById("btnRetRun").onclick = run;
    document.getElementById("retAge").onchange = run;
    document.getElementById("retQ").onkeydown = (e) => { if (e.key === "Enter") run(); };

    if (st.kind === "staff") {
      renderSortableTable("retTableHost", [
        { key: "name", label: "Name", render: (r) => `<span class="link-ink">${esc(r.name)}</span>` },
        { key: "club", label: "Club" },
        { key: "job", label: "Job" },
        { key: "age", label: "Age", render: (r) => `<strong>${r.age}</strong>` },
        { key: "ca", label: "CA" },
      ], data.rows, (r) => openEntity("staff", r.unique_id, r.name));
    } else {
      renderSortableTable("retTableHost", [
        { key: "name", label: "Player", render: (r) => `<span class="link-ink">${esc(r.name)}</span>` },
        { key: "club", label: "Club" },
        { key: "pos", label: "Pos" },
        { key: "age", label: "Age", render: (r) => `<strong>${r.age}</strong>` },
        { key: "ca", label: "CA" },
        { key: "contract", label: "Contract" },
      ], data.rows, (r) => openEntity("player", r.unique_id, r.name));
    }
  }

  // ---- EDIT HISTORY ----
  async function renderHistoryPage() {
    const body = document.getElementById("centerBody");
    const data = await apiGet("/api/history");
    body.innerHTML = `
      <div class="entity-card">
        <div class="entity-top" style="margin-bottom:0;">
          <div>
            <div class="entity-name" style="font-size:17px;">Session Edit Journal</div>
            <div class="entity-tags">
              <span class="etag">${data.actions} action(s)</span>
              <span class="etag">${data.redo} in redo stack</span>
              ${data.dirty ? `<span class="etag lime">${data.dirty} staged locally</span>` : ""}
            </div>
          </div>
          <div style="display:flex;gap:8px;">
            <button class="mini-btn" id="histUndo">↶ Undo</button>
            <button class="mini-btn" id="histRedo">↷ Redo</button>
            ${data.dirty ? `<button class="mini-btn" id="histDiscard">Discard Staged</button>` : ""}
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-body">
          ${data.entries.map((e) => `
            <div class="attr-row" style="padding:7px 0;">
              <span class="link-ink" data-hkind="${esc(e.target_kind)}" data-hid="${esc(e.target_id)}">${esc(e.target_name)}</span>
              <span style="flex:1;color:var(--ink-2);">${esc(e.label)}</span>
              <strong>${esc(e.old)} → ${esc(e.new)}</strong>
              <span class="etag ${e.in_game ? "lime" : ""}">${e.in_game ? "In Game" : "Staged"}</span>
            </div>
          `).join("") || '<div class="empty-note">No edits yet in this session. Click any attribute or use the floating + toolbar.</div>'}
        </div>
      </div>
    `;
    document.getElementById("histUndo").onclick = doUndo;
    document.getElementById("histRedo").onclick = doRedo;
    const disc = document.getElementById("histDiscard");
    if (disc) disc.onclick = async () => {
      const r = await apiPost("/api/discard");
      renderStatus(r.status);
      toast(r.message, "info");
      renderHistoryPage();
    };
    body.querySelectorAll("[data-hkind]").forEach((el) => {
      el.onclick = () => openEntity(el.dataset.hkind, el.dataset.hid, el.textContent);
    });
  }

  // ---- Generic Sortable Table Renderer ----
  function renderSortableTable(hostId, cols, rows, onClickRow) {
    const host = document.getElementById(hostId);
    if (!host) return;
    const st = state.sortState[hostId] || { col: null, asc: false };
    const sorted = [...rows];
    if (st.col) {
      sorted.sort((a, b) => {
        const va = a[st.col], vb = b[st.col];
        if (typeof va === "number" && typeof vb === "number") return st.asc ? va - vb : vb - va;
        return st.asc ? String(va ?? "").localeCompare(String(vb ?? "")) : String(vb ?? "").localeCompare(String(va ?? ""));
      });
    }
    host.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            ${cols.map((c) => `
              <th data-scol="${esc(c.key)}">${esc(c.label)}${st.col === c.key ? (st.asc ? " ▲" : " ▼") : ""}</th>
            `).join("")}
          </tr>
        </thead>
        <tbody>
          ${sorted.map((r, idx) => `
            <tr data-ridx="${idx}">
              ${cols.map((c) => `<td>${c.render ? c.render(r) : esc(r[c.key] ?? "—")}</td>`).join("")}
            </tr>
          `).join("") || `<tr><td colspan="${cols.length}" class="empty-note">No matching records.</td></tr>`}
        </tbody>
      </table>
    `;
    host.querySelectorAll("th[data-scol]").forEach((th) => {
      th.onclick = () => {
        const col = th.dataset.scol;
        state.sortState[hostId] = { col, asc: st.col === col ? !st.asc : false };
        renderSortableTable(hostId, cols, rows, onClickRow);
      };
    });
    host.querySelectorAll("tbody tr[data-ridx]").forEach((tr) => {
      tr.onclick = () => onClickRow(sorted[Number(tr.dataset.ridx)]);
    });
  }

  // ---- AI ASSISTANT: CONTEXT, CHAT SSE, THINKING, TOOLS & / COMMAND PALETTE ----
  function activeContextPayload() {
    const tab = getActiveTab();
    if (!tab) return { kind: "", id: null, name: "", club: state.status?.managed_club_raw || "" };
    return {
      kind: tab.kind,
      id: tab.id,
      name: tab.meta?.name || tab.title,
      club: tab.meta?.club || state.status?.managed_club_raw || "",
    };
  }

  function updateAiContext() {
    const ctx = activeContextPayload();
    const el = document.getElementById("aiCtxText");
    if (ctx.kind === "player") el.textContent = `${ctx.name} · ${ctx.club}`;
    else if (ctx.kind === "club") el.textContent = `${ctx.name} · Club`;
    else if (ctx.kind === "staff") el.textContent = `${ctx.name} · Staff`;
    else el.textContent = `${state.status?.managed_club || "AC Milan"} · Workspace`;
  }

  function renderSimpleMarkdown(text) {
    const raw = String(text || "").replace(/\r\n/g, "\n");
    if (!raw.trim()) return "<p>(empty)</p>";

    const inline = (s) => {
      // Inline code first so markdown symbols inside code stay literal
      const codes = [];
      let out = esc(s).replace(/`([^`]+)`/g, (_m, inner) => {
        codes.push(`<code>${inner}</code>`);
        return `\u0000CODE${codes.length - 1}\u0000`;
      });
      out = out
        .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
          '<a class="md-link" href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/__(.+?)__/g, "<strong>$1</strong>")
        .replace(/(^|[\s(])\*([^*\n]+?)\*(?=[\s).,!?:;]|$)/g, "$1<em>$2</em>");
      return out.replace(/\u0000CODE(\d+)\u0000/g, (_m, i) => codes[Number(i)]);
    };

    const isTableSep = (ln) => /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(ln);
    const splitRow = (ln) => {
      let s = ln.trim();
      if (s.startsWith("|")) s = s.slice(1);
      if (s.endsWith("|")) s = s.slice(0, -1);
      return s.split("|").map((c) => c.trim());
    };

    const lines = raw.split("\n");
    let html = "";
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];
      const trimmed = line.trim();

      if (!trimmed) { i++; continue; }

      // Fenced code block ```
      if (trimmed.startsWith("```")) {
        const buf = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith("```")) {
          buf.push(lines[i]);
          i++;
        }
        if (i < lines.length) i++;
        html += `<pre class="md-pre"><code>${esc(buf.join("\n"))}</code></pre>`;
        continue;
      }

      // Horizontal rule
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
        html += "<hr />";
        i++;
        continue;
      }

      // Headings (#..####)
      const hm = line.match(/^\s*(#{1,4})\s+(.+?)\s*#*$/);
      if (hm) {
        const lvl = hm[1].length;
        html += `<h${lvl}>${inline(hm[2])}</h${lvl}>`;
        i++;
        continue;
      }

      // GFM Table (header + separator row, tolerating blank lines between rows)
      if (trimmed.includes("|")) {
        let sepIdx = i + 1;
        while (sepIdx < lines.length && !lines[sepIdx].trim()) sepIdx++;
        if (sepIdx < lines.length && isTableSep(lines[sepIdx])) {
          const headers = splitRow(line);
          i = sepIdx + 1;
          const bodyRows = [];
          while (i < lines.length) {
            if (!lines[i].trim()) {
              // Peek ahead: if next non-empty line is another pipe row, skip blank line
              let peek = i + 1;
              while (peek < lines.length && !lines[peek].trim()) peek++;
              if (peek < lines.length && lines[peek].trim().startsWith("|") && lines[peek].includes("|")) {
                i = peek;
                continue;
              }
              break;
            }
            if (!lines[i].includes("|")) break;
            bodyRows.push(splitRow(lines[i]));
            i++;
          }
          html += `
            <div class="md-table-wrap">
              <table class="md-table">
                <thead><tr>${headers.map((h) => `<th>${inline(h)}</th>`).join("")}</tr></thead>
                <tbody>${bodyRows.map((r) => `<tr>${headers.map((_, ci) => `<td>${inline(r[ci] || "")}</td>`).join("")}</tr>`).join("")}</tbody>
              </table>
            </div>
          `;
          continue;
        }
      }

      // Blockquote
      if (/^\s*>\s?/.test(line)) {
        const qbuf = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          qbuf.push(lines[i].replace(/^\s*>\s?/, ""));
          i++;
        }
        html += `<blockquote>${qbuf.map((l) => inline(l)).join("<br />")}</blockquote>`;
        continue;
      }

      // Unordered list
      if (/^\s*[-*•]\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*[-*•]\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*[-*•]\s+/, ""));
          i++;
        }
        html += `<ul>${items.map((it) => `<li>${inline(it)}</li>`).join("")}</ul>`;
        continue;
      }

      // Ordered list
      if (/^\s*\d+\.\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
          i++;
        }
        html += `<ol>${items.map((it) => `<li>${inline(it)}</li>`).join("")}</ol>`;
        continue;
      }

      // Paragraph
      const pbuf = [line];
      i++;
      while (i < lines.length) {
        const nxt = lines[i];
        const nt = nxt.trim();
        if (!nt || nt.startsWith("```") || /^\s*(#{1,4}\s|[-*•]\s|\d+\.\s|>\s?|(-{3,}|\*{3,})$)/.test(nxt)) break;
        if (nt.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) break;
        pbuf.push(nxt);
        i++;
      }
      html += `<p>${pbuf.map((l) => inline(l)).join("<br />")}</p>`;
    }

    return html || "<p>(empty)</p>";
  }

  function renderChatThread() {
    const host = document.getElementById("chatThread");
    if (!state.chatTurns.length) {
      host.innerHTML = `
        <div class="chat-empty">
          <h4>FM27 AI Tactical &amp; Squad Assistant</h4>
          <p>Ask about the player or club open on the left, or type <code>/</code> for instant commands (<code>/analyze</code>, <code>/bestxi</code>, <code>/heal</code>, <code>/inspire</code>).</p>
          <div class="starter-grid">
            <button class="starter-btn" data-starter="/analyze">Analyze my squad depth &amp; injury risks</button>
            <button class="starter-btn" data-starter="/bestxi">Build my optimal Best XI &amp; tactic roles</button>
            <button class="starter-btn" data-starter="/shortlist U23 right-back under £30M">Scout U23 right-back targets under £30M</button>
            <button class="starter-btn" data-starter="/inspire">Inspire active player (Morale 100)</button>
          </div>
        </div>
      `;
      host.querySelectorAll("[data-starter]").forEach((b) => {
        b.onclick = () => sendChat(b.dataset.starter);
      });
      return;
    }

    host.innerHTML = state.chatTurns.map((t, idx) => {
      if (t.role === "user") {
        return `<div class="msg-user">${esc(t.text)}</div>`;
      }
      const totalToolMs = (t.tools || []).reduce((acc, x) => acc + (x.ms || 0), 0);
      const totalSec = t.elapsedMs ? `${(t.elapsedMs / 1000).toFixed(1)}s` : (t.pending ? "Thinking…" : "Done");
      const progressPct = t.pending ? 68 : 100;
      const thinkBlock = (t.pending || (t.steps && t.steps.length) || t.thinking) ? `
        <div class="think-card">
          <div class="think-head" data-toggle-think="${idx}">
            <span>Thinking Process · ${totalSec}</span>
            <span style="color:var(--muted)">${t.thinkOpen ? "▾" : "▸"}</span>
          </div>
          <div class="think-progress"><div class="think-progress-fill" style="width:${progressPct}%"></div></div>
          ${t.steps && t.steps.length ? `
            <div class="think-steps">
              ${t.steps.map((s) => `<span class="tstep"><span class="tstep-dot">✓</span>${esc(s.label)} ${s.ms}ms</span>`).join("")}
            </div>
          ` : ""}
          ${t.thinking ? `<div class="think-detail ${t.thinkOpen ? "" : "collapsed"}">${esc(t.thinking)}</div>` : ""}
        </div>
      ` : "";

      const toolsBlock = (t.tools && t.tools.length) ? `
        <div class="tools-card">
          <div class="tools-head" data-toggle-tools="${idx}">
            <span><span class="tool-ok"></span>${t.tools.length} tool${t.tools.length > 1 ? "s" : ""} called · ${totalToolMs}ms</span>
            <span style="color:var(--muted)">${t.toolsOpen ? "▾" : "▸"}</span>
          </div>
          <div class="tools-list ${t.toolsOpen ? "" : "collapsed"}">
            ${t.tools.map((tl) => `
              <div class="tool-row">
                <span><span class="tool-ok ${tl.ok === false ? "tool-err" : ""}"></span>${esc(tl.name)}(${esc(JSON.stringify(tl.args || {}))})</span>
                <span style="color:var(--muted)">${tl.running ? "…" : (tl.ms + "ms")}</span>
              </div>
            `).join("")}
          </div>
        </div>
      ` : "";

      const answerBlock = t.text ? `
        <div class="msg-ai ${t.error ? "error" : ""}">${t.error ? esc(t.text) : renderSimpleMarkdown(t.text)}</div>
      ` : "";

      return `<div class="msg-turn">${thinkBlock}${toolsBlock}${answerBlock}</div>`;
    }).join("");

    host.querySelectorAll("[data-toggle-think]").forEach((el) => {
      el.onclick = () => {
        const t = state.chatTurns[Number(el.dataset.toggleThink)];
        t.thinkOpen = !t.thinkOpen;
        renderChatThread();
      };
    });
    host.querySelectorAll("[data-toggle-tools]").forEach((el) => {
      el.onclick = () => {
        const t = state.chatTurns[Number(el.dataset.toggleTools)];
        t.toolsOpen = !t.toolsOpen;
        renderChatThread();
      };
    });
    const aiBody = document.getElementById("aiBody");
    aiBody.scrollTop = aiBody.scrollHeight;
  }

  async function sendChat(overrideText = null) {
    if (state.aiBusy) return;
    const input = document.getElementById("chatInput");
    const prompt = (overrideText !== null ? overrideText : input.value).trim();
    if (!prompt) return;
    input.value = "";
    closeCmdPalette();

    state.aiBusy = true;
    document.getElementById("btnSend").disabled = true;
    state.chatTurns.push({ role: "user", text: prompt });
    const turn = { role: "assistant", text: "", steps: [], thinking: "", tools: [], pending: true, thinkOpen: true, toolsOpen: false };
    state.chatTurns.push(turn);
    renderChatThread();

    try {
      const res = await fetch("/api/ai/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Studio-Token": TOKEN },
        body: JSON.stringify({ prompt, context: activeContextPayload() }),
      });
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const chunks = buf.split("\n\n");
        buf = chunks.pop() || "";
        for (const chunk of chunks) {
          const line = chunk.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;
          const ev = JSON.parse(line.slice(6));
          if (ev.type === "step") {
            turn.steps.push(ev);
          } else if (ev.type === "thinking") {
            turn.thinking = (turn.thinking ? turn.thinking + "\n\n" : "") + ev.text;
          } else if (ev.type === "tool_start") {
            turn.tools.push({ id: ev.id, name: ev.name, args: ev.args, running: true, ms: 0, ok: true });
          } else if (ev.type === "tool_done") {
            const existing = turn.tools.find((x) => x.id === ev.id);
            if (existing) Object.assign(existing, { running: false, ms: ev.ms, ok: ev.ok, summary: ev.summary });
            else turn.tools.push({ id: ev.id, name: ev.name, args: ev.args, running: false, ms: ev.ms, ok: ev.ok });
          } else if (ev.type === "done") {
            turn.pending = false;
            turn.thinkOpen = false;
            turn.text = ev.text || "";
            turn.elapsedMs = ev.elapsed_ms || 0;
            if (ev.status) renderStatus(ev.status);
            if (ev.refresh_entity && getActiveTab()?.key === `${ev.refresh_entity.kind}:${ev.refresh_entity.id}`) {
              renderActiveView();
            }
          } else if (ev.type === "error") {
            turn.pending = false;
            turn.error = true;
            turn.text = ev.message + (ev.hint ? ` (${ev.hint})` : "");
          }
          renderChatThread();
        }
      }
    } catch (err) {
      turn.pending = false;
      turn.error = true;
      turn.text = `Connection failed: ${err.message}`;
      renderChatThread();
    } finally {
      state.aiBusy = false;
      document.getElementById("btnSend").disabled = false;
    }
  }

  // ---- Slash Command Palette ----
  function filteredCommands(prefix) {
    const p = (prefix || "").toLowerCase();
    return SLASH_COMMANDS.filter((c) => c.key.startsWith(p));
  }
  function openCmdPalette(filter = "/") {
    const pal = document.getElementById("cmdPalette");
    const list = filteredCommands(filter);
    if (!list.length) { closeCmdPalette(); return; }
    state.cmdIndex = Math.min(state.cmdIndex, list.length - 1);
    document.getElementById("cmdList").innerHTML = list.map((c, idx) => `
      <div class="cmd-item ${idx === state.cmdIndex ? "selected" : ""}" data-cidx="${idx}">
        <span class="cmd-ico">${c.ico}</span>
        <span class="cmd-key">${esc(c.key)}</span>
        <span class="cmd-desc">${esc(c.desc)}</span>
        <span>${idx === state.cmdIndex ? "↵" : "›"}</span>
      </div>
    `).join("");
    document.querySelectorAll("#cmdList .cmd-item").forEach((el) => {
      el.onclick = () => selectCommand(list[Number(el.dataset.cidx)]);
    });
    pal.classList.add("open");
  }
  function closeCmdPalette() {
    document.getElementById("cmdPalette").classList.remove("open");
  }
  function selectCommand(cmd) {
    const input = document.getElementById("chatInput");
    closeCmdPalette();
    if (cmd.instant) {
      sendChat(cmd.insert);
    } else {
      input.value = cmd.insert;
      input.focus();
    }
  }

  // ---- AI Tactics & Training Tabs ----
  async function loadTacticsTab() {
    const host = document.getElementById("tacticsContent");
    const style = document.getElementById("tacticStyleSelect").value;
    const club = activeContextPayload().club || state.status?.managed_club_raw || "Milan";
    host.innerHTML = '<div class="empty-note">Optimizing Best XI…</div>';
    try {
      const data = await apiGet(`/api/plans/tactic?club=${encodeURIComponent(club)}&style=${encodeURIComponent(style)}`);
      if (data.error) { host.innerHTML = `<div class="empty-note">${esc(data.error)}</div>`; return; }
      const slots = data.roles || data.lineup || data.slots || [];
      const rows = [[], [], [], [], []];
      for (const s of slots) {
        const pos = String(s.position || s.slot || "").toUpperCase();
        if (pos === "GK") rows[4].push(s);
        else if (["DL","DC","DR","WBL","WBR"].includes(pos)) rows[3].push(s);
        else if (["DM","MC","ML","MR"].includes(pos)) rows[2].push(s);
        else if (["AML","AMC","AMR"].includes(pos)) rows[1].push(s);
        else rows[0].push(s);
      }
      const instr = data.instructions || data.team_instructions || {};
      const shapeLabel = data.template || data.shape || data.formation || "Best XI";
      host.innerHTML = `
        <div class="pitch">
          ${rows.filter((r) => r.length).map((line) => `
            <div class="pitch-line">
              ${line.map((s) => {
                const uid = s.player_unique_id || s.unique_id || "";
                const fullName = s.player_name || s.player || s.name || s.position || "";
                const shortName = fullName.split(" ").slice(-1)[0];
                const roleName = (s.in_possession && s.in_possession.role) || s.role || s.position || "";
                return `
                  <div class="pnode" data-open-p="${uid}" title="${esc(fullName)} — ${esc(roleName)}">
                    ${esc(shortName)}
                    <span>${esc(s.position || roleName)}</span>
                  </div>
                `;
              }).join("")}
            </div>
          `).join("")}
        </div>
        <div class="card">
          <div class="card-head"><span>${esc(shapeLabel)} · ${esc(style)}</span></div>
          <div class="card-body">
            ${Object.entries(instr).slice(0, 10).map(([k, v]) => `
              <div class="attr-row"><span class="attr-name">${esc(k)}</span><strong>${esc(v)}</strong></div>
            `).join("")}
          </div>
        </div>
      `;
      host.querySelectorAll("[data-open-p]").forEach((el) => {
        if (el.dataset.openP) el.onclick = () => openEntity("player", Number(el.dataset.openP));
      });
    } catch (err) {
      host.innerHTML = `<div class="empty-note">${esc(err.message)}</div>`;
    }
  }

  async function loadOnlineTactics() {
    const host = document.getElementById("onlineTacticsContent");
    if (!host) return;
    const source = document.getElementById("onlineTacticSource")?.value || "fm-arena";
    const q = (document.getElementById("onlineTacticQuery")?.value || "").trim();
    host.innerHTML = `<div class="empty-note">Fetching FM27 tactics from ${esc(source)}…</div>`;
    try {
      const data = await apiGet(`/api/tactics/online?source=${encodeURIComponent(source)}&q=${encodeURIComponent(q)}&limit=25`);
      if (!data.ok) {
        host.innerHTML = `<div class="empty-note">${esc(data.error || "Failed to fetch tactics.")}</div>`;
        return;
      }
      const list = data.tactics || [];
      if (!list.length) {
        host.innerHTML = `<div class="empty-note">No tactics matched "${esc(q)}".</div>`;
        return;
      }
      host.innerHTML = `
        <div class="modal-note" style="padding:2px 2px 4px;">
          ${data.total} tactics · Installs to <code>${esc(data.tactics_dir || "FM27/tactics")}</code>
        </div>
        ${list.map((t, idx) => `
          <div class="online-tactic-card">
            <div class="online-tactic-top">
              <div class="online-tactic-title">#${t.rank} ${t.crown ? "★ " : ""}${esc(t.name)}</div>
              <span class="kpi-trend">${esc(t.pts)}</span>
            </div>
            <div class="online-tactic-meta">
              <span class="etag">${esc(t.formation)}</span>
              <span>by ${esc(t.author)}</span>
              ${t.xgd ? `<span>xGD ${esc(t.xgd)}</span>` : ""}
              ${t.gd ? `<span>GD ${esc(t.gd)}</span>` : ""}
              ${t.downloads ? `<span>${esc(t.downloads)} DLs</span>` : ""}
            </div>
            <div class="online-tactic-foot">
              <a class="dl-pill-link" href="${esc(t.url)}" target="_blank" rel="noopener noreferrer">↗ View Thread</a>
              ${t.direct_download
                ? `<button class="dl-pill-btn" data-dl-tactic="${idx}">↓ Install .fmf</button>`
                : `<span style="font-size:10px;color:var(--muted);">Login required on site</span>`}
            </div>
          </div>
        `).join("")}
      `;
      host.querySelectorAll("[data-dl-tactic]").forEach((btn) => {
        btn.onclick = async () => {
          const item = list[Number(btn.dataset.dlTactic)];
          btn.disabled = true;
          btn.textContent = "Installing…";
          try {
            const res = await apiPost("/api/tactics/download", { url: item.url });
            if (res.ok) {
              btn.textContent = "✓ Installed";
              toast(res.message || `Installed ${res.filename}`, "ok");
            } else {
              btn.disabled = false;
              btn.textContent = "↓ Install .fmf";
              toast(res.error || "Download failed", "error");
            }
          } catch (err) {
            btn.disabled = false;
            btn.textContent = "↓ Install .fmf";
            toast(err.message, "error");
          }
        };
      });
    } catch (err) {
      host.innerHTML = `<div class="empty-note">${esc(err.message)}</div>`;
    }
  }

  async function loadTrainingTab() {
    const host = document.getElementById("trainingContent");
    const focus = document.getElementById("trainingFocusSelect").value;
    const club = activeContextPayload().club || state.status?.managed_club_raw || "Milan";
    host.innerHTML = '<div class="empty-note">Building training week…</div>';
    try {
      const week = await apiGet(`/api/plans/training?focus=${encodeURIComponent(focus)}&club=${encodeURIComponent(club)}`);
      const local = week.local_schedules || {};
      const localList = (local.files || []).slice(0, 8);
      host.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
          <span class="modal-note">S24 evidence-based schedule</span>
          <button class="mini-btn" id="btnCopyTrainingSheet">Copy Sheet</button>
        </div>
        ${(week.days || []).map((d) => `
          <div class="day-card">
            <div class="day-head"><span>${esc(d.day)}</span></div>
            <div class="day-sessions">
              ${(d.sessions || []).map((s, i) => `<div>${i + 1}. ${esc(s.label || s.name || "")}</div>`).join("")}
            </div>
          </div>
        `).join("")}
        ${localList.length ? `
          <div class="card">
            <div class="card-head"><span>Local FM27 Schedules (${local.count})</span></div>
            <div class="card-body">
              ${localList.map((f) => `<div class="attr-row"><span class="attr-name">${esc(f.name)}</span><span style="font-size:10px;color:var(--muted)">.fmf</span></div>`).join("")}
            </div>
          </div>
        ` : ""}
      `;
      const copyBtn = document.getElementById("btnCopyTrainingSheet");
      if (copyBtn && week.sheet) {
        copyBtn.onclick = () => {
          navigator.clipboard?.writeText(week.sheet).then(
            () => toast("Training sheet copied to clipboard", "ok"),
            () => toast("Could not access clipboard", "info")
          );
        };
      }
    } catch (err) {
      host.innerHTML = `<div class="empty-note">${esc(err.message)}</div>`;
    }
  }

  // ---- Save / Undo / Redo / Attach ----
  async function doSave() {
    try {
      const res = await apiPost("/api/save");
      renderStatus(res.status);
      toast(res.message, res.ok ? "ok" : "error");
    } catch (err) {
      toast(err.message, "error");
    }
  }
  async function doUndo() {
    const res = await apiPost("/api/undo");
    renderStatus(res.status);
    toast(res.message, res.ok ? (res.warning ? "info" : "ok") : "info");
    if (res.ok) renderActiveView();
  }
  async function doRedo() {
    const res = await apiPost("/api/redo");
    renderStatus(res.status);
    toast(res.message, res.ok ? (res.warning ? "info" : "ok") : "info");
    if (res.ok) renderActiveView();
  }
  async function doAttach(silent = false) {
    const btn = document.getElementById("btnAttach");
    btn.disabled = true;
    try {
      const res = await apiPost("/api/attach", { refresh_all: false });
      renderStatus(res.status);
      if (res.ok) {
        if (!silent) toast(res.message, "ok");
        renderActiveView();
      } else if (!silent) {
        toast(res.error || "Could not attach to FM27", "error");
      }
    } catch (err) {
      if (!silent) toast(err.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  // ---- Settings Modal ----
  function openSettingsModal() {
    const ai = state.aiSettings;
    if (!ai) return;
    const sel = document.getElementById("setProvider");
    sel.innerHTML = ai.providers.map((p) => `<option value="${esc(p.id)}" ${p.id === ai.provider ? "selected" : ""}>${esc(p.label)}</option>`).join("");
    document.getElementById("setBaseUrl").value = ai.base_url || "";
    document.getElementById("setModel").value = ai.model || "";
    document.getElementById("setReasoning").value = ai.reasoning || "Default";
    document.getElementById("setApiKey").value = "";
    document.getElementById("setKeyStatus").textContent = `Key status: ${ai.key_status}`;
    const gInput = document.getElementById("setGraphicsDir");
    if (gInput) gInput.value = ai.graphics_dir || "";
    const fNote = document.getElementById("setFacesStatus");
    if (fNote) {
      fNote.textContent = ai.faces_available
        ? `Facepack active: ${ai.graphics_dir}`
        : "No facepack folder detected (avatars fall back to initials).";
    }
    const mSel = document.getElementById("setModelSelect");
    const mNote = document.getElementById("setModelsStatus");
    if (mSel) { mSel.style.display = "none"; mSel.innerHTML = ""; }
    if (mNote) { mNote.style.display = "none"; mNote.textContent = ""; }
    sel.onchange = () => {
      const info = ai.providers.find((p) => p.id === sel.value);
      if (info) {
        document.getElementById("setBaseUrl").value = info.default_url || "";
        document.getElementById("setModel").value = info.default_model || "";
      }
      if (mSel) { mSel.style.display = "none"; mSel.innerHTML = ""; }
      if (mNote) { mNote.style.display = "none"; mNote.textContent = ""; }
    };
    document.getElementById("settingsModal").classList.add("open");
  }
  function closeSettingsModal() {
    document.getElementById("settingsModal").classList.remove("open");
  }

  // ---- Global Event Wiring ----
  function wireEvents() {
    document.getElementById("brandHome").onclick = openMyClub;
    document.getElementById("btnAttach").onclick = () => doAttach(false);
    document.getElementById("btnUndo").onclick = doUndo;
    document.getElementById("btnRedo").onclick = doRedo;

    const omni = document.getElementById("globalSearch");
    omni.onkeydown = (e) => {
      if (e.key === "Enter") openSearch(omni.value.trim());
    };

    document.querySelectorAll(".pane-left .nav-item[data-nav]").forEach((el) => {
      el.onclick = () => {
        const nav = el.dataset.nav;
        if (nav === "myclub") openMyClub();
        else if (nav === "search") openSearch("");
        else if (nav === "transfers") openTab({ key: "transfers", kind: "transfers", id: "transfers", title: "Future Transfers" });
        else if (nav === "retirements") openTab({ key: "retirements", kind: "retirements", id: "retirements", title: "Retirements" });
        else if (nav === "history") openTab({ key: "history", kind: "history", id: "history", title: "Edit History" });
      };
    });

    // Bottom Floating Dock & Recent Clear
    document.getElementById("dockMyClub").onclick = openMyClub;
    document.getElementById("dockSearch").onclick = () => openSearch("");
    document.getElementById("dockPlus").onclick = () => setDockOpen(!state.dockOpen);
    document.getElementById("dockSave").onclick = doSave;
    document.getElementById("dockUndo").onclick = doUndo;
    const clearRecentBtn = document.getElementById("btnClearRecent");
    if (clearRecentBtn) clearRecentBtn.onclick = clearUnpinnedRecent;

    // AI Modes
    document.querySelectorAll(".ai-mode[data-aimode]").forEach((el) => {
      el.onclick = () => {
        state.aiMode = el.dataset.aimode;
        document.querySelectorAll(".ai-mode[data-aimode]").forEach((m) => m.classList.toggle("active", m === el));
        document.getElementById("aiViewChat").classList.toggle("active", state.aiMode === "chat");
        document.getElementById("aiViewTactics").classList.toggle("active", state.aiMode === "tactics");
        document.getElementById("aiViewTraining").classList.toggle("active", state.aiMode === "training");
        document.getElementById("aiComposer").style.display = state.aiMode === "chat" ? "block" : "none";
        if (state.aiMode === "tactics") loadTacticsTab();
        if (state.aiMode === "training") loadTrainingTab();
      };
    });
    document.querySelectorAll(".ai-mode[data-tmode]").forEach((el) => {
      el.onclick = () => {
        const mode = el.dataset.tmode;
        document.querySelectorAll(".ai-mode[data-tmode]").forEach((m) => m.classList.toggle("active", m === el));
        document.getElementById("tacticBestXiPane").style.display = mode === "bestxi" ? "flex" : "none";
        document.getElementById("tacticOnlinePane").style.display = mode === "online" ? "flex" : "none";
        if (mode === "online") loadOnlineTactics();
      };
    });
    document.getElementById("btnRebuildTactic").onclick = loadTacticsTab;
    document.getElementById("btnRebuildTraining").onclick = loadTrainingTab;
    const btnOnline = document.getElementById("btnFetchOnlineTactics");
    if (btnOnline) btnOnline.onclick = loadOnlineTactics;
    const onlineQ = document.getElementById("onlineTacticQuery");
    if (onlineQ) onlineQ.onkeydown = (e) => { if (e.key === "Enter") loadOnlineTactics(); };
    const onlineSrc = document.getElementById("onlineTacticSource");
    if (onlineSrc) onlineSrc.onchange = loadOnlineTactics;

    // Chat composer + / Command Palette
    const chatInput = document.getElementById("chatInput");
    document.getElementById("btnSend").onclick = () => sendChat();
    document.getElementById("btnSlashCmd").onclick = () => {
      const pal = document.getElementById("cmdPalette");
      if (pal.classList.contains("open")) closeCmdPalette();
      else { openCmdPalette("/"); chatInput.focus(); }
    };
    document.getElementById("btnNewChat").onclick = async () => {
      await apiPost("/api/ai/clear");
      state.chatTurns = [];
      renderChatThread();
      toast("Started a new chat", "info");
    };
    chatInput.addEventListener("input", () => {
      const v = chatInput.value;
      if (v.startsWith("/") && !v.includes(" ")) openCmdPalette(v);
      else closeCmdPalette();
    });
    chatInput.addEventListener("keydown", (e) => {
      const pal = document.getElementById("cmdPalette");
      if (pal.classList.contains("open")) {
        const list = filteredCommands(chatInput.value.startsWith("/") ? chatInput.value : "/");
        if (e.key === "ArrowDown") { e.preventDefault(); state.cmdIndex = (state.cmdIndex + 1) % list.length; openCmdPalette(chatInput.value || "/"); return; }
        if (e.key === "ArrowUp") { e.preventDefault(); state.cmdIndex = (state.cmdIndex - 1 + list.length) % list.length; openCmdPalette(chatInput.value || "/"); return; }
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); selectCommand(list[state.cmdIndex]); return; }
        if (e.key === "Escape") { e.preventDefault(); closeCmdPalette(); return; }
      }
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
    });

    // Settings Modal
    document.getElementById("btnSettings").onclick = openSettingsModal;
    document.getElementById("btnAiSettings").onclick = openSettingsModal;
    document.getElementById("closeSettings").onclick = closeSettingsModal;
    document.getElementById("btnCancelSettings").onclick = closeSettingsModal;
    document.getElementById("btnClearKey").onclick = async () => {
      const r = await apiPost("/api/ai/settings", { clear_key: true });
      state.aiSettings = r.ai;
      document.getElementById("setKeyStatus").textContent = `Key status: ${r.ai.key_status}`;
      toast("API key cleared", "info");
    };
    const btnFetchModels = document.getElementById("btnFetchModels");
    if (btnFetchModels) {
      btnFetchModels.onclick = async () => {
        const mSel = document.getElementById("setModelSelect");
        const mNote = document.getElementById("setModelsStatus");
        const mInput = document.getElementById("setModel");
        btnFetchModels.disabled = true;
        btnFetchModels.textContent = "Fetching…";
        if (mNote) { mNote.style.display = "block"; mNote.textContent = "Querying provider /models…"; }
        try {
          const r = await apiPost("/api/ai/models", {
            provider: document.getElementById("setProvider").value,
            base_url: document.getElementById("setBaseUrl").value,
            model: mInput.value,
            api_key: document.getElementById("setApiKey").value,
          });
          if (!r.ok) {
            if (mNote) mNote.textContent = r.error + (r.hint ? ` (${r.hint})` : "");
            toast(r.error || "Failed to fetch models", "error");
            return;
          }
          const list = r.models || [];
          if (!list.length) {
            if (mNote) mNote.textContent = "Provider returned an empty model list.";
            return;
          }
          if (mSel) {
            const cur = mInput.value.trim();
            mSel.innerHTML = list.map((m) =>
              `<option value="${esc(m.id)}" ${m.id === cur ? "selected" : ""}>${esc(m.name && m.name !== m.id ? `${m.id} — ${m.name}` : m.id)}</option>`
            ).join("");
            mSel.style.display = "block";
            mSel.onchange = () => { mInput.value = mSel.value; };
            if (!list.some((m) => m.id === cur)) {
              mInput.value = list[0].id;
            }
          }
          if (mNote) mNote.textContent = `Loaded ${list.length} model(s). Pick one above or edit manually.`;
          toast(`Loaded ${list.length} models`, "ok");
        } catch (err) {
          if (mNote) mNote.textContent = err.message;
          toast(err.message, "error");
        } finally {
          btnFetchModels.disabled = false;
          btnFetchModels.textContent = "Fetch Models";
        }
      };
    }
    document.getElementById("btnSaveSettings").onclick = async () => {
      const r = await apiPost("/api/ai/settings", {
        provider: document.getElementById("setProvider").value,
        base_url: document.getElementById("setBaseUrl").value,
        model: document.getElementById("setModel").value,
        reasoning: document.getElementById("setReasoning").value,
        api_key: document.getElementById("setApiKey").value,
        graphics_dir: document.getElementById("setGraphicsDir")?.value ?? "",
      });
      state.aiSettings = r.ai;
      document.getElementById("modelCaption").textContent = `${r.ai.model} · ${r.ai.reasoning}`;
      closeSettingsModal();
      renderRecent();
      renderActiveView();
      toast("Settings saved", "ok");
    };

    // Keyboard shortcuts (Ctrl+K search, Ctrl+S save, Ctrl+Z undo, Ctrl+Y / Ctrl+Shift+Z redo)
    window.addEventListener("keydown", (e) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      const k = e.key.toLowerCase();
      if (k === "k") {
        e.preventDefault();
        omni.focus();
        omni.select();
      } else if (k === "s") {
        e.preventDefault();
        doSave();
      } else if (k === "z" && !e.shiftKey && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA") {
        e.preventDefault();
        doUndo();
      } else if ((k === "y" || (k === "z" && e.shiftKey)) && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA") {
        e.preventDefault();
        doRedo();
      }
    });
  }

  // ---- Bootstrap ----
  async function init() {
    initLayout();
    loadRecent();
    wireEvents();
    bindMagnetic(document);
    renderChatThread();

    try {
      const boot = await apiGet("/api/bootstrap");
      state.boot = boot;
      state.aiSettings = boot.ai;
      renderStatus(boot.status);
      document.getElementById("modelCaption").textContent = `${boot.ai.model} · ${boot.ai.reasoning}`;

      if (!state.recent.length && boot.default_players) {
        for (const p of boot.default_players.slice(0, 3)) {
          state.recent.push({ key: `player:${p.unique_id}`, kind: "player", id: p.unique_id, title: p.name, tag: String(p.ca), pinned: false });
        }
        state.recent.push({ key: `club:${boot.status.managed_club_id || 5598}`, kind: "club", id: boot.status.managed_club_id || 5598, title: boot.status.managed_club, tag: "Club", pinned: true });
        renderRecent();
      }

      // Open managed club + top player tabs on start (or deep-link via ?player=&compare=)
      const initPlayer = Number(params.get("player") || 0);
      const initCompare = Number(params.get("compare") || 0);
      if (initPlayer) {
        if (initCompare) {
          state.compareOtherUid = initCompare;
          openTab({ key: `player:${initPlayer}`, kind: "player", id: initPlayer, title: `Player #${initPlayer}`, subtab: "compare" });
        } else {
          openEntity("player", initPlayer);
        }
      } else {
        if (boot.default_players && boot.default_players[0]) {
          const first = boot.default_players[0];
          openEntity("player", first.unique_id, first.name);
        }
        openMyClub();
      }

      // Auto-attach silently if FM27 is running (takes ~35ms on build 2505117)
      if (!boot.status.attached) {
        doAttach(true);
      }
    } catch (err) {
      document.getElementById("centerBody").innerHTML = `<div class="empty-state">Failed to connect to FM27 Studio backend: ${esc(err.message)}</div>`;
    }
  }

  init();
})();
