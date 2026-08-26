/* 讀 data/events.json，做多軸篩選（時間／地區／積分／主題／主辦／來源）與排序。
   全部在瀏覽器端跑，沒有後端。資料量幾百筆等級，直接全量過濾就夠快。 */
(function () {
  "use strict";

  var DATA_URL = "data/events.json";
  var REGION_ORDER = ["北部", "中部", "南部", "東部", "離島", "線上", "其他"];
  var DOW = ["日", "一", "二", "三", "四", "五", "六"];

  var ORG_HOSPITAL = "醫院／院所";
  var ORG_NONE = "未標示";

  var state = {
    events: [],
    // 「地區 → 訂閱檔名」對照表，由 build.py 寫進 events.json。
    // 前端刻意不自己維護一份地區清單 —— 抄一份的話，後端加了新地區而這裡沒跟上，
    // 使用者會拿到 404 的訂閱網址，而且不會有任何錯誤訊息。
    feeds: null,
    // 訂閱範圍。刻意跟 state.region（地區**篩選**）分成兩個獨立的值 ——
    // 訂閱範圍只能由使用者在訂閱區塊裡明確選，不跟著瀏覽時的篩選動作改變。
    // null＝全部。
    subscribeRegion: null,
    q: "",
    time: "upcoming",
    region: null,
    credit: 0,
    category: null,
    source: null,
    organizer: null,
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
    toggle: document.getElementById("toggle"),
    subscribe: document.getElementById("subscribe"),
    subscribeLink: document.getElementById("subscribe-link"),
    subscribeCopy: document.getElementById("subscribe-copy"),
    subscribeNote: document.getElementById("subscribe-note")
  };

  // ---------- 工具 ----------
  /** 這個站的「今天」一律是台灣的今天，不看使用者裝置的時區。
   *  她人在國外或手機時區設錯時，「即將舉行」不該跟著跑掉。
   *  台灣沒有日光節約，固定 +08:00 即可。 */
  function todayISO() {
    var now = new Date();
    var taipei = new Date(now.getTime() + (now.getTimezoneOffset() + 480) * 60000);
    return [
      taipei.getFullYear(),
      String(taipei.getMonth() + 1).padStart(2, "0"),
      String(taipei.getDate()).padStart(2, "0")
    ].join("-");
  }

  /** 一場活動的最後一天。判斷「過去了沒」一律用它，不要用 e.date。
   *
   *  目前的三個來源只給單日活動（Event 沒有 end_date 欄位），所以它現在等於 e.date。
   *  刻意還是包成函式：哪天多日活動進來了（姊妹站的年會就是 8/22→8/23 這種），
   *  「用開始日判過期」會讓還在進行中的兩天課在第二天早上就被劃掉，
   *  而且症狀只在跨日活動上出現、平常測不到。改這裡一處即可，不必去追每個呼叫點。 */
  function lastDay(e) {
    return e.end_date || e.date;
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

  /** 主辦單位正規化成可篩選的鍵。
   *
   *  原始 organizer 有 40 種寫法，直接列成篩選器沒人看得完，而且同一個學會
   *  會因為「社團法人」「中華民國」「臺／台」這些前綴變成好幾個不同項目。
   *  所以：學會／公會／協會 收斂成學會名，醫院與院所全部併成一項
   *  （她要挑的是「哪個學會辦的」，不是「哪家醫院」）。
   *  一筆活動可能有多個主辦單位（用、分隔），任一命中就算，所以回傳陣列。 */
  function normalizeOrg(name) {
    return String(name == null ? "" : name)
      // 來源網站打字時會夾雜空白（實例：「台灣兒童 青少年發展障礙學會」
      // 與「新竹市臨床 心理師公會」），不清掉同一個學會會被拆成兩個篩選項目
      .replace(/\s+/g, "")
      .replace(/臺/g, "台")
      // 用 + 量詞讓字首可疊加剝除：「財團法人中華民國OO學會」要一路剝到 OO學會，
      // 只剝一次會讓同一個學會因為原始字串有沒有疊字首而落到兩個不同篩選鍵
      .replace(/^(?:社團法人|財團法人|中華民國|中華|台灣)+/, "");
  }

  function organizerKeys(e) {
    var raw = String(e.organizer == null ? "" : e.organizer).trim();
    if (!raw) return [ORG_NONE];

    var keys = [];
    // 只切實際觀察到的分隔符。斜線刻意不切 —— 機構名本身可能含「/」（如「A/B 中心」），
    // 切下去會生出兩個不存在的篩選鍵。
    raw.split(/[、,，]+/).forEach(function (part) {
      var name = normalizeOrg(part);
      if (!name) return;
      var society = name.match(/^.*?(學會|公會|協會)/);
      // 基金會只認「整串就是一個基金會」的情況（連倚南教授復健醫學教育基金會）。
      // 不能用 /基金會/ 隨便比對：「醫療財團法人徐元智先生藥基金會亞東紀念醫院
      // 兒童發展中心」的基金會在字串中間，它實際上是醫院，該留在「醫院／院所」。
      var key = society ? society[0] : /基金會$/.test(name) ? name : ORG_HOSPITAL;
      if (keys.indexOf(key) === -1) keys.push(key);
    });
    return keys.length ? keys : [ORG_NONE];
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
    { key: "90", label: "近 3 個月", test: function (e, t) { return e.date >= t && e.date <= addDays(t, 90); } }
    // 沒有「含已結束」選項 —— 資料源頭就不收過期活動（KEEP_PAST_DAYS=0），
    // 留一個永遠等同「即將舉行」的選項只會讓人以為點了沒反應。
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

  /** exceptAxis：計算篩選器上的數字時，要略過該軸自己的條件
   *  （否則點了「北部」之後，北部以外的地區數字全部變成 0）。
   *  渲染清單時不傳，就是全部條件都套。 */
  function applyFilters(exceptAxis) {
    var today = todayISO();
    var timeFilter = TIME_FILTERS.filter(function (f) { return f.key === state.time; })[0];
    var q = state.q.trim().toLowerCase();

    var rows = state.events.filter(function (e) {
      if (exceptAxis !== "time" && timeFilter && !timeFilter.test(e, today)) return false;
      if (exceptAxis !== "region" && state.region && e.region !== state.region) return false;
      if (exceptAxis !== "source" && state.source && e.source !== state.source) return false;
      if (exceptAxis !== "organizer" && state.organizer &&
          organizerKeys(e).indexOf(state.organizer) === -1) return false;
      if (exceptAxis !== "credit" && state.credit > 0 && !(e.credits >= state.credit)) return false;
      if (exceptAxis !== "category" && state.category &&
          (e.categories || []).indexOf(state.category) === -1) return false;
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
      /* 過期判定只算這一次，整張卡的灰化就吃這個值 —— 算兩次的話兩套判準遲早會漂掉。
         比的是 lastDay（結束日）不是 date（開始日），理由見 lastDay() 的註解。

         🔴 正常情況下這個站**不會有**過期活動：`KEEP_PAST_DAYS = 0`，資料源頭就把
         過期的濾掉了。灰化是給「資料停止更新」那條路徑用的安全網 ——
         來源掛掉時 build.py 會沿用舊的 events.json（頂著總比整站空掉好），
         但瀏覽器的「今天」照樣往前走，那些課就會變成過去式。
         那正是最需要一眼看出來的時候，光靠頂端一行錯誤提示不夠。 */
      var isPast = lastDay(e) < today;

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
        '<article class="event' + (isPast ? " is-past" : "") + '">' +
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

  /** 篩選器上的數字＝「按下去會看到幾筆」。
   *  所以要拿套過其他軸條件的結果來數，不能數全部資料 ——
   *  否則預設的「即將舉行」會讓數字比實際點下去多（含已結束的場次）。 */
  function countBy(pick, axis) {
    var counts = {};
    applyFilters(axis).forEach(function (e) {
      var values = pick(e);
      (Array.isArray(values) ? values : [values]).forEach(function (v) {
        if (v == null || v === "") return;
        counts[v] = (counts[v] || 0) + 1;
      });
    });
    return counts;
  }

  /** 目前選中的項目就算在其他條件下歸零也要留著，否則 chip 消失就沒得取消，
   *  使用者會卡在一個看不到出口的空清單（例：選了東部又選只在北部辦的學會）。 */
  function keepSelected(counts, selected) {
    if (selected != null && counts[selected] == null) counts[selected] = 0;
    return counts;
  }

  function renderFilters() {
    renderChips("f-time", TIME_FILTERS, function (i) { return state.time === i.key; },
      function (i) { state.time = i.key; });

    var regionCounts = keepSelected(countBy(function (e) { return e.region; }, "region"), state.region);
    var regions = REGION_ORDER.filter(function (r) { return regionCounts[r] != null; })
      .map(function (r) { return { key: r, label: r, count: regionCounts[r] }; });
    renderChips("f-region", [{ key: null, label: "全部" }].concat(regions),
      function (i) { return state.region === i.key; },
      function (i) { state.region = i.key; });

    renderChips("f-credit", CREDIT_FILTERS, function (i) { return state.credit === i.key; },
      function (i) { state.credit = i.key; });

    var catCounts = keepSelected(countBy(function (e) { return e.categories || []; }, "category"), state.category);
    var cats = Object.keys(catCounts).sort(function (a, b) { return catCounts[b] - catCounts[a]; })
      .map(function (c) { return { key: c, label: c, count: catCounts[c] }; });
    renderChips("f-category", [{ key: null, label: "全部" }].concat(cats),
      function (i) { return state.category === i.key; },
      function (i) { state.category = i.key; });

    var srcCounts = keepSelected(countBy(function (e) { return e.source; }, "source"), state.source);
    var srcs = Object.keys(srcCounts).sort()
      .map(function (s) { return { key: s, label: s, count: srcCounts[s] }; });
    renderChips("f-source", [{ key: null, label: "全部" }].concat(srcs),
      function (i) { return state.source === i.key; },
      function (i) { state.source = i.key; });

    var orgCounts = keepSelected(countBy(organizerKeys, "organizer"), state.organizer);
    var orgs = Object.keys(orgCounts)
      .sort(function (a, b) {
        // 「醫院／院所」跟「未標示」是收納桶不是學會，固定壓在最後
        var ra = a === ORG_HOSPITAL || a === ORG_NONE ? 1 : 0;
        var rb = b === ORG_HOSPITAL || b === ORG_NONE ? 1 : 0;
        if (ra !== rb) return ra - rb;
        if (orgCounts[b] !== orgCounts[a]) return orgCounts[b] - orgCounts[a];
        return a < b ? -1 : a > b ? 1 : 0;
      })
      .map(function (o) { return { key: o, label: o, count: orgCounts[o] }; });
    renderChips("f-organizer", [{ key: null, label: "全部" }].concat(orgs),
      function (i) { return state.organizer === i.key; },
      function (i) { state.organizer = i.key; });

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
    if (state.organizer) n++;
    if (state.sort !== "date-asc") n++;
    return n;
  }

  function renderToggle() {
    var n = activeFilterCount();
    el.toggle.innerHTML = "篩選" + (n ? '<span class="badge">' + n + "</span>" : "");
  }

  /* 訂閱按鈕。每個地區各對應一份 build 事先產好的 .ics（data/region-*.ics），
   * 「全部」是 data/all.ics。
   *
   * 🔴 **訂閱範圍是使用者在這一區明確選的一份，完全不跟著上面的篩選器走。**
   *    純靜態站沒有後端，不可能為任意的篩選組合即時產生 ics，只能事先產好幾份固定
   *    的檔案。既然如此，範圍就必須是「明確選的」而不是「跟著瀏覽動作偷偷變的」——
   *    使用者為了找課而點了幾個篩選，回頭按訂閱卻拿到跟他以為的不一樣的東西，
   *    比沒有這個功能更糟。所以 state.subscribeRegion 跟 state.region 是兩個獨立的值。
   *
   * 🔴 **範圍一定要寫在畫面上**：說明文字會寫出這份訂閱檔的實際筆數，並明講
   *    「不受下面的篩選條件影響」。筆數是從資料算出來的，不是寫死的字串。
   *
   * 為什麼切「地區」而不是主題：地區是唯一「一筆活動恰好落在一個值」的軸，
   * 切成檔案不會讓同一場活動出現在兩份訂閱裡（主題可複標，切了就會重複，
   * 而且 UID 相同但分屬不同行事曆時日曆 App 不會幫忙合併）。地區同時也是
   * 真正決定「去不去得成」的條件 —— 值班的人到不了外縣市，那正是要濾掉的雜訊。
   *
   * webcal:// 是行事曆訂閱的慣例 scheme，Google／Apple／Outlook 都認得，
   * 會接成「訂閱」而不是「匯入一次」—— 匯入一次的話之後新增的課就再也不會進來了。
   * 另外附一顆「複製網址」，因為部分桌機環境沒有處理 webcal:// 的程式。 */
  function renderSubscribe() {
    if (!el.subscribe) return;

    // 舊的 events.json（還沒有 feeds 欄位）就整區藏起來，不要給出猜出來的網址。
    var feeds = state.feeds;
    if (!feeds || !feeds[""]) {
      el.subscribe.hidden = true;
      return;
    }
    el.subscribe.hidden = false;

    // 選過的地區可能因為資料更新而不再有活動（該份訂閱檔會被 build 刪掉）→ 退回「全部」。
    if (state.subscribeRegion && !feeds[state.subscribeRegion]) {
      state.subscribeRegion = null;
    }
    var scope = state.subscribeRegion;

    /* 這一排的數字是「訂閱下去會拿到幾場」，所以要數全部資料，
       **不能**用 applyFilters() —— 那是篩選器上的數字，跟訂閱到的內容是兩回事。 */
    var countOf = function (region) {
      return state.events.filter(function (e) {
        return !region || e.region === region;
      }).length;
    };
    /* 選項一律由 feeds 的實際 key 產生，**不能**拿 REGION_ORDER 來篩 ——
       後端加了新地區、這裡的清單沒跟上的話，那份訂閱檔會存在卻沒有任何入口，
       而且不會有錯誤訊息。REGION_ORDER 只拿來決定「排序偏好」，
       不在清單裡的地區排到最後，但一定看得到。 */
    var options = [{ key: null, label: "全部", count: countOf(null) }];
    Object.keys(feeds)
      .filter(function (r) { return r !== ""; })
      .sort(function (a, b) {
        var ia = REGION_ORDER.indexOf(a), ib = REGION_ORDER.indexOf(b);
        if (ia === -1) ia = REGION_ORDER.length;
        if (ib === -1) ib = REGION_ORDER.length;
        if (ia !== ib) return ia - ib;
        return a < b ? -1 : a > b ? 1 : 0;
      })
      .forEach(function (r) { options.push({ key: r, label: r, count: countOf(r) }); });
    renderChips("f-subscribe", options,
      function (i) { return scope === i.key; },
      function (i) { state.subscribeRegion = i.key; });

    var file = scope ? feeds[scope] : feeds[""];
    var httpsURL = new URL("data/" + file, location.href).href;
    el.subscribeLink.href = httpsURL.replace(/^https?:/, "webcal:");
    el.subscribeLink.textContent = scope
      ? "訂閱「" + scope + "」到行事曆"
      : "訂閱「全部」到行事曆";
    el.subscribeCopy.setAttribute("data-url", httpsURL);

    el.subscribeNote.textContent =
      "訂閱的是「" + (scope || "全部") + "」整份的 " + countOf(scope) + " 場，" +
      "不受下面的篩選條件影響（地區／時間／積分／主題／主辦／來源與關鍵字搜尋都不影響）。" +
      "資料每天更新，訂閱後行事曆會自動跟著更新。";
  }

  function render() {
    renderFilters();
    renderToggle();
    renderSubscribe();
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

  /* 複製訂閱網址。webcal:// 在部分桌機環境沒有對應程式會開不起來，
   * 這顆是那時候的退路：貼到日曆 App 的「訂閱行事曆」欄位一樣能用。
   * clipboard API 在非 HTTPS 或使用者拒絕權限時會失敗，所以有 fallback，
   * 兩條路都要給回饋 —— 按了沒反應會讓人以為壞了。 */
  if (el.subscribeCopy) {
    el.subscribeCopy.addEventListener("click", function () {
      var url = el.subscribeCopy.getAttribute("data-url") || "";
      var done = function (ok) {
        el.subscribeCopy.textContent = ok ? "已複製 ✓" : "請手動複製";
        if (!ok) window.prompt("訂閱網址：", url);
        setTimeout(function () {
          el.subscribeCopy.textContent = "複製訂閱網址";
        }, 2000);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(
          function () { done(true); },
          function () { done(false); }
        );
      } else {
        done(false);
      }
    });
  }

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
    state.organizer = null;
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
      state.feeds = data.feeds || null;
      renderHeader(data);
      render();
    })
    .catch(function (err) {
      el.list.innerHTML = "";
      el.empty.hidden = false;
      el.empty.textContent = "資料載入失敗（" + err.message + "）。請稍後重新整理。";
    });
})();
