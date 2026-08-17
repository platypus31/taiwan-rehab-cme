/* 讀 data/events.json，做四軸篩選（時間／地區／積分／主題＋來源）與排序。
   全部在瀏覽器端跑，沒有後端。資料量幾百筆等級，直接全量過濾就夠快。 */
(function () {
  "use strict";

  var DATA_URL = "data/events.json";
  var REGION_ORDER = ["北部", "中部", "南部", "東部", "離島", "線上", "其他"];
  var DOW = ["日", "一", "二", "三", "四", "五", "六"];

  var state = {
    events: [],
    q: "",
    time: "upcoming",
    region: null,
    credit: 0,
    category: null,
    source: null,
    sort: "date-asc"
  };

  var el = {
    list: document.getElementById("list"),
    empty: document.getElementById("empty"),
    q: document.getElementById("q"),
    reset: document.getElementById("reset"),
    resultCount: document.getElementById("result-count"),
    notice: document.getElementById("notice"),
    filters: document.querySelector(".filters"),
    toggle: document.getElementById("toggle")
  };

  // ---------- 工具 ----------
  function todayISO() {
    var d = new Date();
    return [
      d.getFullYear(),
      String(d.getMonth() + 1).padStart(2, "0"),
      String(d.getDate()).padStart(2, "0")
    ].join("-");
  }

  function addDays(iso, n) {
    var d = new Date(iso + "T00:00:00");
    d.setDate(d.getDate() + n);
    return [
      d.getFullYear(),
      String(d.getMonth() + 1).padStart(2, "0"),
      String(d.getDate()).padStart(2, "0")
    ].join("-");
  }

  // updated_at 帶時區偏移（本機跑是 +08:00，GitHub Actions 跑是 +00:00）。
  // 直接切字串會讓 CI 產出的時間看起來早 8 小時、像是資料很舊，所以一律換算成台北時間。
  function formatUpdatedAt(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso).slice(0, 16).replace("T", " ");
    try {
      // 用 formatToParts 逐欄取值自己拼，不對 format() 的字串做正則替換 ——
      // 各瀏覽器 ICU 對 zh-TW 的輸出格式不一致（可能是 2026/08/17，也可能是 2026年08月17日），
      // 依賴字串長相會在某些裝置上顯示成沒被轉換的樣子。
      var parts = new Intl.DateTimeFormat("zh-TW", {
        timeZone: "Asia/Taipei",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23"
      }).formatToParts(d);
      var v = {};
      for (var i = 0; i < parts.length; i++) v[parts[i].type] = parts[i].value;
      if (!v.year || !v.month || !v.day || !v.hour || !v.minute) throw new Error("parts");
      var pad = function (s) {
        return String(s).padStart(2, "0");
      };
      // hourCycle h23 在少數舊 ICU 仍可能吐 24，正規化回 00
      var hour = pad(v.hour === "24" ? "0" : v.hour);
      return v.year + "-" + pad(v.month) + "-" + pad(v.day) + " " + hour + ":" + pad(v.minute);
    } catch (err) {
      return String(iso).slice(0, 16).replace("T", " ");
    }
  }

  function escapeHTML(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---------- 篩選 ----------
  var TIME_FILTERS = [
    { key: "upcoming", label: "即將舉行", test: function (e, t) { return e.date >= t; } },
    { key: "7", label: "近 7 天", test: function (e, t) { return e.date >= t && e.date <= addDays(t, 7); } },
    { key: "30", label: "近 30 天", test: function (e, t) { return e.date >= t && e.date <= addDays(t, 30); } },
    { key: "90", label: "近 3 個月", test: function (e, t) { return e.date >= t && e.date <= addDays(t, 90); } },
    { key: "all", label: "全部（含已結束）", test: function () { return true; } }
  ];

  var CREDIT_FILTERS = [
    { key: 0, label: "不限" },
    { key: 1, label: "1 點以上" },
    { key: 2, label: "2 點以上" },
    { key: 3, label: "3 點以上" }
  ];

  var SORTS = [
    { key: "date-asc", label: "日期近→遠" },
    { key: "date-desc", label: "日期遠→近" },
    { key: "credit-desc", label: "積分高→低" }
  ];

  function applyFilters() {
    var today = todayISO();
    var timeFilter = TIME_FILTERS.filter(function (f) { return f.key === state.time; })[0];
    var q = state.q.trim().toLowerCase();

    var rows = state.events.filter(function (e) {
      if (timeFilter && !timeFilter.test(e, today)) return false;
      if (state.region && e.region !== state.region) return false;
      if (state.source && e.source !== state.source) return false;
      if (state.credit > 0 && !(e.credits >= state.credit)) return false;
      if (state.category && (e.categories || []).indexOf(state.category) === -1) return false;
      if (q) {
        var blob = [e.title, e.organizer, e.location, e.source].join(" ").toLowerCase();
        if (blob.indexOf(q) === -1) return false;
      }
      return true;
    });

    rows.sort(function (a, b) {
      if (state.sort === "credit-desc") {
        var ca = a.credits == null ? -1 : a.credits;
        var cb = b.credits == null ? -1 : b.credits;
        if (cb !== ca) return cb - ca;
        return a.date < b.date ? -1 : a.date > b.date ? 1 : 0;
      }
      if (state.sort === "date-desc") return a.date < b.date ? 1 : a.date > b.date ? -1 : 0;
      return a.date < b.date ? -1 : a.date > b.date ? 1 : 0;
    });

    return rows;
  }

  /** 產生 Google 日曆的「新增活動」連結。
   *  來源網站只公告日期不公告起訖時間，所以一律建成整天事件
   *  （dates 用 開始日/隔天，這是 Google 全天事件的格式）。 */
  function calendarURL(e) {
    var start = e.date.replace(/-/g, "");
    var end = addDays(e.date, 1).replace(/-/g, "");
    var details = [
      e.organizer ? "主辦：" + e.organizer : "",
      e.credits != null ? "積分：" + e.credits + " 點" : "",
      e.url ? "簡章與報名：" + e.url : "",
      "",
      "（時間為整天，實際起訖請看主辦單位公告）"
    ].filter(Boolean).join("\n");

    return "https://calendar.google.com/calendar/render?action=TEMPLATE" +
      "&text=" + encodeURIComponent(e.title) +
      "&dates=" + start + "/" + end +
      "&location=" + encodeURIComponent(e.location || "") +
      "&details=" + encodeURIComponent(details);
  }

  // ---------- 渲染 ----------
  function renderList(rows) {
    var today = todayISO();
    var soonLimit = addDays(today, 7);

    el.list.innerHTML = rows.map(function (e) {
      var parts = e.date.split("-");
      var dow = DOW[new Date(e.date + "T00:00:00").getDay()];
      var soon = e.date >= today && e.date <= soonLimit ? " soon" : "";

      var meta = [];
      if (e.organizer) meta.push("<span>主辦：" + escapeHTML(e.organizer) + "</span>");
      if (e.location) meta.push("<span>地點：" + escapeHTML(e.location) + "</span>");

      var tags = [];
      if (e.credits != null) tags.push('<span class="tag credit">' + e.credits + " 點</span>");
      else tags.push('<span class="tag">積分未標示</span>');
      tags.push('<span class="tag region">' + escapeHTML(e.region) + "</span>");
      (e.categories || []).forEach(function (c) {
        tags.push('<span class="tag">' + escapeHTML(c) + "</span>");
      });
      tags.push('<span class="tag source">' + escapeHTML(e.source) + "</span>");

      var title = e.url
        ? '<a href="' + escapeHTML(e.url) + '" target="_blank" rel="noopener">' + escapeHTML(e.title) + "</a>"
        : escapeHTML(e.title);

      return (
        '<article class="event">' +
          '<div class="date-badge' + soon + '">' +
            '<div class="md">' + parts[1] + "/" + parts[2] + "</div>" +
            '<div class="dow">週' + dow + "</div>" +
            '<div class="yr">' + parts[0] + "</div>" +
          "</div>" +
          '<div class="event-body">' +
            '<h2 class="event-title">' + title + "</h2>" +
            (meta.length ? '<div class="event-meta">' + meta.join("") + "</div>" : "") +
            '<div class="tags">' + tags.join("") + "</div>" +
            '<div class="actions">' +
              '<a class="cal" href="' + escapeHTML(calendarURL(e)) + '" target="_blank" rel="noopener">' +
                "加入 Google 日曆</a>" +
            "</div>" +
          "</div>" +
        "</article>"
      );
    }).join("");

    el.empty.hidden = rows.length > 0;
    el.resultCount.textContent = "顯示 " + rows.length + " 場";
  }

  function chipButton(label, active, count) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip";
    btn.setAttribute("aria-pressed", active ? "true" : "false");
    btn.innerHTML = escapeHTML(label) + (count == null ? "" : ' <span class="n">' + count + "</span>");
    return btn;
  }

  function renderChips(containerId, items, isActive, onPick) {
    var box = document.getElementById(containerId);
    box.innerHTML = "";
    items.forEach(function (item) {
      var btn = chipButton(item.label, isActive(item), item.count);
      btn.addEventListener("click", function () {
        onPick(item);
        render();
      });
      box.appendChild(btn);
    });
  }

  function countBy(pick) {
    var counts = {};
    state.events.forEach(function (e) {
      var values = pick(e);
      (Array.isArray(values) ? values : [values]).forEach(function (v) {
        if (v == null || v === "") return;
        counts[v] = (counts[v] || 0) + 1;
      });
    });
    return counts;
  }

  function renderFilters() {
    renderChips("f-time", TIME_FILTERS, function (i) { return state.time === i.key; },
      function (i) { state.time = i.key; });

    var regionCounts = countBy(function (e) { return e.region; });
    var regions = REGION_ORDER.filter(function (r) { return regionCounts[r]; })
      .map(function (r) { return { key: r, label: r, count: regionCounts[r] }; });
    renderChips("f-region", [{ key: null, label: "全部" }].concat(regions),
      function (i) { return state.region === i.key; },
      function (i) { state.region = i.key; });

    renderChips("f-credit", CREDIT_FILTERS, function (i) { return state.credit === i.key; },
      function (i) { state.credit = i.key; });

    var catCounts = countBy(function (e) { return e.categories || []; });
    var cats = Object.keys(catCounts).sort(function (a, b) { return catCounts[b] - catCounts[a]; })
      .map(function (c) { return { key: c, label: c, count: catCounts[c] }; });
    renderChips("f-category", [{ key: null, label: "全部" }].concat(cats),
      function (i) { return state.category === i.key; },
      function (i) { state.category = i.key; });

    var srcCounts = countBy(function (e) { return e.source; });
    var srcs = Object.keys(srcCounts).sort()
      .map(function (s) { return { key: s, label: s, count: srcCounts[s] }; });
    renderChips("f-source", [{ key: null, label: "全部" }].concat(srcs),
      function (i) { return state.source === i.key; },
      function (i) { state.source = i.key; });

    renderChips("f-sort", SORTS, function (i) { return state.sort === i.key; },
      function (i) { state.sort = i.key; });
  }

  /** 收合狀態下也要看得出有沒有在篩 —— 按鈕上掛一個數字。 */
  function activeFilterCount() {
    var n = 0;
    if (state.time !== "upcoming") n++;
    if (state.region) n++;
    if (state.credit > 0) n++;
    if (state.category) n++;
    if (state.source) n++;
    if (state.sort !== "date-asc") n++;
    return n;
  }

  function renderToggle() {
    var n = activeFilterCount();
    el.toggle.innerHTML = "篩選" + (n ? '<span class="badge">' + n + "</span>" : "");
  }

  function render() {
    renderFilters();
    renderToggle();
    renderList(applyFilters());
  }

  function renderHeader(data) {
    var upcoming = data.events.filter(function (e) { return e.date >= todayISO(); });
    document.getElementById("stat-count").textContent = upcoming.length;

    if (upcoming.length) {
      var first = upcoming[0].date.slice(5).replace("-", "/");
      var last = upcoming[upcoming.length - 1].date.slice(5).replace("-", "/");
      document.getElementById("stat-range").textContent = first + " – " + last;
    } else {
      document.getElementById("stat-range").textContent = "—";
    }

    document.getElementById("stat-sources").textContent = Object.keys(data.sources || {}).length;

    if (data.updated_at) {
      document.getElementById("stat-updated").textContent =
        "更新於 " + formatUpdatedAt(data.updated_at);
    }

    if (data.errors && data.errors.length) {
      el.notice.hidden = false;
      el.notice.textContent = "部分來源這次沒抓到資料：" + data.errors.join("；");
    }
  }

  // ---------- 啟動 ----------
  el.q.addEventListener("input", function () {
    state.q = el.q.value;
    renderList(applyFilters());
  });

  el.toggle.addEventListener("click", function () {
    var collapsed = el.filters.classList.toggle("collapsed");
    el.toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  });

  // 手機預設收合，讓活動清單出現在第一屏；桌面維持全部攤開
  if (window.matchMedia("(max-width: 560px)").matches) {
    el.filters.classList.add("collapsed");
  }

  el.reset.addEventListener("click", function () {
    state.q = "";
    el.q.value = "";
    state.time = "upcoming";
    state.region = null;
    state.credit = 0;
    state.category = null;
    state.source = null;
    state.sort = "date-asc";
    render();
  });

  fetch(DATA_URL, { cache: "no-cache" })
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      state.events = data.events || [];
      renderHeader(data);
      render();
    })
    .catch(function (err) {
      el.list.innerHTML = "";
      el.empty.hidden = false;
      el.empty.textContent = "資料載入失敗（" + err.message + "）。請稍後重新整理。";
    });
})();
