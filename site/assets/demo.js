/*
  notepaDB demo: a browser copy of the desktop app's main window.

  Statement splitting, statement_at, autocomplete and the command palette's
  fuzzy matching are ported from notepadb/app.py, autocomplete.py and ui.py.
  Status, log and dialog texts are the app's own. Queries run on SQLite
  compiled to WebAssembly (sql.js), seeded with the repo's baseball sample.
  Each tab is its own session, like QueryTab in the app.
*/
(() => {
  "use strict";

  const shell = document.getElementById("np-shell");
  const np = document.getElementById("np");
  if (!np || !shell) return;

  const $ = (sel, root = np) => root.querySelector(sel);
  const input = $("#np-input");
  const hl = $("#np-hl");
  const gutter = $("#np-gutter");
  const hint = $("#np-hint");
  const acList = $("#np-ac");
  const codeBox = $(".np-code");
  const treeEl = $("#np-tree");
  const sideEmpty = $("#np-side-empty");
  const filterEl = $("#np-filter");
  const rtabsEl = $("#np-rtabs");
  const rbody = $("#np-rbody");
  const gridEl = $("#np-gridwrap");
  const logEl = $("#np-log");
  const rfootLabel = $("#np-rfoot-label");
  const exportBtn = $('[data-act="export"]');
  const menuEl = $("#np-menu");
  const modalEl = $("#np-modal");
  const paletteEl = $("#np-palette");
  const menubar = document.getElementById("np-menubar"); // on macOS it moves to the global menu bar
  const toolbar = $(".np-toolbar");
  const tabsEl = $("#np-tabs");
  const titleEl = $("#np-title");
  const connectLabel = $("#np-connect-label");
  const runBtn = $('.np-toolbar [data-act="run"]');
  const stopBtn = $('.np-toolbar [data-act="stop"]');
  const stMsg = $("#np-st-msg");
  const stText = $("#np-st-text");
  const stPos = $("#np-st-pos");
  const stTime = $("#np-st-time");
  const stConn = $("#np-st-conn");
  const stConnText = $("#np-st-conn-text");
  const findBar = $("#np-find");
  const findInput = $("#np-find-input");
  const replaceRow = $("#np-replace-row");
  const replaceInput = $("#np-replace-input");
  const findCount = $("#np-find-count");
  const caseBtn = $('[data-find="case"]');
  const globalbar = document.getElementById("np-globalbar");
  const osSwitch = document.getElementById("np-os");
  const statusEl = document.getElementById("demo-status");
  const replayBtn = document.getElementById("demo-replay");

  const VENDOR = "/assets/vendor/sqljs-1.14.2/";
  const DB_FILE = "baseball.db";
  const HISTORY_LIMIT = 300;
  const RESULT_ROW_CAP = 2000;
  const APP_NAME = "notepaDB";
  const motionOK = !matchMedia("(prefers-reduced-motion: reduce)").matches;

  const SEED_SQL = `
PRAGMA foreign_keys = ON;
CREATE TABLE teams (
    team_id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name TEXT NOT NULL,
    city TEXT NOT NULL,
    home_stadium TEXT NOT NULL,
    founded_year INTEGER
);
CREATE TABLE players (
    player_id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    position TEXT NOT NULL,
    jersey_number INTEGER,
    team_id INTEGER,
    FOREIGN KEY (team_id) REFERENCES teams(team_id) ON DELETE SET NULL
);
CREATE TABLE games (
    game_id INTEGER PRIMARY KEY AUTOINCREMENT,
    home_team_id INTEGER,
    away_team_id INTEGER,
    game_date TEXT NOT NULL,
    home_score INTEGER DEFAULT 0,
    away_score INTEGER DEFAULT 0,
    FOREIGN KEY (home_team_id) REFERENCES teams(team_id) ON DELETE CASCADE,
    FOREIGN KEY (away_team_id) REFERENCES teams(team_id) ON DELETE CASCADE
);
INSERT INTO teams (team_name, city, home_stadium, founded_year) VALUES
('Astros', 'Houston', 'Minute Maid Park', 1962),
('Yankees', 'New York', 'Yankee Stadium', 1903),
('Dodgers', 'Los Angeles', 'Dodger Stadium', 1883),
('Cubs', 'Chicago', 'Wrigley Field', 1876),
('Red Sox', 'Boston', 'Fenway Park', 1901);
INSERT INTO players (first_name, last_name, position, jersey_number, team_id) VALUES
('José', 'Altuve', '2B', 27, 1),
('Yordan', 'Alvarez', 'LF', 44, 1),
('Aaron', 'Judge', 'CF', 99, 2),
('Juan', 'Soto', 'RF', 22, 2),
('Mookie', 'Betts', 'RF', 50, 3),
('Shohei', 'Ohtani', 'DH', 17, 3);
INSERT INTO games (home_team_id, away_team_id, game_date, home_score, away_score) VALUES
(1, 2, '2026-06-12', 5, 3),
(2, 3, '2026-06-15', 4, 6),
(3, 1, '2026-06-18', 2, 1),
(4, 5, '2026-06-20', 7, 4),
(5, 2, '2026-06-22', 6, 2);`;

  // What SqliteBackend.list_schema() returns for the seed: {table: [[column, type], ...]}
  let schema = {
    games: [["game_id", "integer"], ["home_team_id", "integer"], ["away_team_id", "integer"], ["game_date", "text"], ["home_score", "integer"], ["away_score", "integer"]],
    players: [["player_id", "integer"], ["first_name", "text"], ["last_name", "text"], ["position", "text"], ["jersey_number", "integer"], ["team_id", "integer"]],
    teams: [["team_id", "integer"], ["team_name", "text"], ["city", "text"], ["home_stadium", "text"], ["founded_year", "integer"]],
  };

  const INITIAL_RESULT = {
    columns: ["team_id", "team_name", "city", "home_stadium", "founded_year"],
    rows: [
      [1, "Astros", "Houston", "Minute Maid Park", 1962],
      [2, "Yankees", "New York", "Yankee Stadium", 1903],
      [3, "Dodgers", "Los Angeles", "Dodger Stadium", 1883],
      [4, "Cubs", "Chicago", "Wrigley Field", 1876],
      [5, "Red Sox", "Boston", "Fenway Park", 1901],
    ],
  };

  const ENGINES = [["sqlserver", "SQL Server"], ["postgres", "PostgreSQL"], ["mysql", "MySQL / MariaDB"], ["oracle", "Oracle"], ["sqlite", "SQLite"]];
  // app.py ENGINE_AUTH: (label for "windows", label for "sql", choice offered)
  const ENGINE_AUTH = {
    sqlserver: ["Windows authentication", "SQL Server login", true],
    postgres: ["No password (peer / trust)", "Username and password", true],
    mysql: ["No password", "Username and password", true],
    oracle: ["Username and password", "Username and password", false],
    sqlite: ["", "", false],
  };

  // app.py SHORTCUT_GROUPS
  const SHORTCUT_GROUPS = [
    ["Query", [["Run selection, or everything", "F5"], ["Run the statement at the caret", "Ctrl+Enter"], ["Run everything", "Ctrl+Shift+Enter"], ["Stop the running query", "Ctrl+Break"], ["Explain plan", "Ctrl+L"], ["Query history", "Ctrl+Shift+H"]]],
    ["Connection", [["Connect…", "Ctrl+K"], ["Refresh schema", "Ctrl+Shift+R"], ["Show / hide schema sidebar", "Ctrl+B"]]],
    ["Editor", [["Find", "Ctrl+F"], ["Find next / previous", "F3 / Shift+F3"], ["Replace", "Ctrl+H"], ["Toggle line comment", "Ctrl+/"], ["Format SQL", "Ctrl+Shift+F"], ["Go to line", "Ctrl+G"], ["Move line up / down", "Alt+↑ / Alt+↓"], ["Indent / outdent selection", "Tab / Shift+Tab"], ["Accept completion", "Tab or Enter"], ["Zoom in / out / reset", "Ctrl+= / Ctrl+- / Ctrl+0"]]],
    ["Tabs & files", [["New tab", "Ctrl+T"], ["Close tab", "Ctrl+W"], ["Next / previous tab", "Ctrl+Tab / Ctrl+Shift+Tab"], ["Open file", "Ctrl+O"], ["Save / Save as", "Ctrl+S / Ctrl+Shift+S"], ["Command palette", "Ctrl+Shift+P"], ["Full screen", "F11"], ["Keyboard shortcuts", "F1"], ["Quit", "Ctrl+Q"]]],
  ];

  /* ---------- small helpers ---------- */

  const pad2 = (n) => String(n).padStart(2, "0");
  const clock = (d = new Date()) => `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  const stamp = (d = new Date()) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${clock(d)}`;
  const uniq = (arr) => [...new Set(arr)];
  const byLower = (a, b) => {
    const x = a.toLowerCase(), y = b.toLowerCase();
    return x < y ? -1 : x > y ? 1 : 0;
  };
  const byCode = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
  const esc = (s) => s.replace(/[&<>]/g, (c) => (c === "&" ? "&amp;" : c === "<" ? "&lt;" : "&gt;"));
  const isFocused = () => document.activeElement === input;
  const fmtInt = (n) => (typeof n === "number" && Number.isFinite(n) ? Math.round(n).toLocaleString("en-US") : String(n));
  // app.py _fmt_elapsed and _plural
  function fmtElapsed(ms) {
    if (ms === null || ms === undefined) return "";
    if (ms < 1000) return `${Math.round(ms)} ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(2)} s`;
    return `${Math.floor(ms / 60000)} min ${Math.round((ms % 60000) / 1000)} s`;
  }
  const PLURALS = { entry: "entries", query: "queries" };
  const plural = (n, word) => `${fmtInt(n)} ${n === 1 ? word : PLURALS[word] || word + "s"}`;
  function snippet(sql, limit = 80) {
    const s = sql.split(/\s+/).filter(Boolean).join(" ");
    return s.length <= limit ? s : s.slice(0, limit - 1) + "…";
  }
  function el(tag, attrs, text) {
    const node = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else node.setAttribute(k, v === true ? "" : String(v));
    }
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function icon(name, cls) {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    if (cls) svg.setAttribute("class", cls);
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(ns, "use");
    use.setAttribute("href", `#${name}`);
    svg.appendChild(use);
    return svg;
  }

  /* ---------- autocomplete.py, ported ---------- */

  const SQL_KEYWORDS = [
    "SELECT", "FROM", "WHERE", "JOIN", "INNER JOIN", "LEFT JOIN", "RIGHT JOIN",
    "FULL JOIN", "ON", "GROUP BY", "ORDER BY", "HAVING", "INSERT INTO",
    "VALUES", "UPDATE", "SET", "DELETE", "CREATE TABLE", "ALTER TABLE",
    "DROP TABLE", "DISTINCT", "TOP", "AS", "AND", "OR", "NOT", "NULL",
    "IS NULL", "IS NOT NULL", "IN", "EXISTS", "BETWEEN", "LIKE", "UNION",
    "UNION ALL", "CASE", "WHEN", "THEN", "ELSE", "END", "COUNT", "SUM",
    "AVG", "MIN", "MAX", "OVER", "PARTITION BY", "WITH", "DESC", "ASC",
  ];
  const IDENT_RE = /[A-Za-z0-9_.[\]]*$/;
  const TABLE_CONTEXT_RE = /(FROM|JOIN|INTO|UPDATE|TABLE)\s+$/i;
  const CLAUSE_RE = /(FROM|JOIN)\s+([^\n;]+?)(?=\b(?:FROM|JOIN|WHERE|GROUP|ORDER|ON|LEFT|RIGHT|INNER|FULL|OUTER|CROSS|HAVING|LIMIT|UNION)\b|[\n;]|$)/gi;
  const ITEM_RE = /^([A-Za-z0-9_.[\]]+)\s*(?:(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*))?$/i;
  const stripBrackets = (name) => name.replace(/^[[\]]+|[[\]]+$/g, "");

  function buildAliasMap(fullText, sch) {
    const aliasMap = {};
    const keysLower = {};
    const bare = {};
    for (const key of Object.keys(sch)) {
      keysLower[key.toLowerCase()] = key;
      const b = key.split(".").pop().toLowerCase();
      (bare[b] = bare[b] || []).push(key);
    }
    CLAUSE_RE.lastIndex = 0;
    let m;
    while ((m = CLAUSE_RE.exec(fullText))) {
      for (const raw of m[2].split(",")) {
        const im = raw.trim().match(ITEM_RE);
        if (!im) continue;
        const name = stripBrackets(im[1]);
        let key = keysLower[name.toLowerCase()];
        if (key === undefined) {
          const candidates = bare[name.split(".").pop().toLowerCase()];
          if (candidates && candidates.length === 1) key = candidates[0];
        }
        if (key === undefined) continue;
        aliasMap[name.toLowerCase()] = key;
        if (im[2]) aliasMap[im[2].toLowerCase()] = key;
      }
    }
    return aliasMap;
  }

  function rank(pool, prefix) {
    if (!prefix) return [];
    const p = prefix.toLowerCase();
    const starts = uniq(pool.filter((x) => x.toLowerCase().startsWith(p))).sort(byLower);
    const contains = uniq(pool.filter((x) => {
      const l = x.toLowerCase();
      return l.includes(p) && !l.startsWith(p);
    })).sort(byLower);
    return starts.concat(contains).slice(0, 50);
  }

  function getSuggestions(fullText, cursor, sch) {
    const before = fullText.slice(0, cursor);
    const word = (before.match(IDENT_RE) || [""])[0];
    const prefixStart = cursor - word.length;
    if (word.includes(".")) {
      const dot = word.lastIndexOf(".");
      const aliasPart = stripBrackets(word.slice(0, dot)).toLowerCase();
      const colPrefix = word.slice(dot + 1);
      let key = buildAliasMap(fullText, sch)[aliasPart];
      if (key === undefined) key = Object.keys(sch).find((k) => k.split(".").pop().toLowerCase() === aliasPart);
      const cols = key ? sch[key].map((c) => c[0]) : [];
      const suggestions = colPrefix ? rank(cols, colPrefix) : cols.slice().sort(byLower);
      return [cursor - colPrefix.length, colPrefix, suggestions];
    }
    if (TABLE_CONTEXT_RE.test(before.slice(0, prefixStart))) {
      const keys = Object.keys(sch);
      const pool = keys.slice().sort(byCode).concat(uniq(keys.map((k) => k.split(".").pop())).sort(byCode));
      const suggestions = word ? rank(pool, word) : uniq(pool).sort(byLower).slice(0, 50);
      return [prefixStart, word, suggestions];
    }
    const allColumns = uniq(Object.values(sch).flatMap((cols) => cols.map((c) => c[0]))).sort(byCode);
    const pool = SQL_KEYWORDS.concat(Object.keys(sch).sort(byCode), allColumns);
    return [prefixStart, word, rank(pool, word)];
  }

  /* ---------- app.py: statement_spans, split_statements, statement_at ---------- */

  const STRING_RE = /'(?:[^'\\]|\\.)*'/y;

  function statementSpans(sql) {
    const spans = [];
    const n = sql.length;
    let i = 0;
    let start = 0;
    while (i < n) {
      const ch = sql[i];
      if (ch === "'") {
        STRING_RE.lastIndex = i;
        const m = STRING_RE.exec(sql);
        if (m) {
          i += m[0].length;
          continue;
        }
      }
      if (sql.startsWith("--", i)) {
        const end = sql.indexOf("\n", i);
        i = end === -1 ? n : end;
        continue;
      }
      if (sql.startsWith("/*", i)) {
        const end = sql.indexOf("*/", i);
        i = end === -1 ? n : end + 2;
        continue;
      }
      if (ch === ";") {
        spans.push([start, i]);
        start = i + 1;
      }
      i += 1;
    }
    spans.push([start, n]);
    const trimmed = [];
    for (const [s, e] of spans) {
      const chunk = sql.slice(s, e);
      if (!chunk.trim()) continue;
      const lead = chunk.length - chunk.trimStart().length;
      const trail = chunk.length - chunk.trimEnd().length;
      trimmed.push([s + lead, e - trail]);
    }
    return trimmed;
  }
  const splitStatements = (sql) => statementSpans(sql).map(([s, e]) => sql.slice(s, e));

  function statementAt(sql, offset) {
    const spans = statementSpans(sql);
    if (!spans.length) return null;
    for (const [s, e] of spans) if (s <= offset && offset <= e) return sql.slice(s, e);
    const before = spans.filter(([, e]) => e < offset);
    if (before.length) {
      const [s, e] = before[before.length - 1];
      if ((sql.slice(e, offset).match(/\n/g) || []).length <= 1) return sql.slice(s, e);
    }
    const after = spans.filter(([s]) => s > offset);
    if (after.length) return sql.slice(after[0][0], after[0][1]);
    const [s, e] = before[before.length - 1];
    return sql.slice(s, e);
  }

  /* ---------- syntax highlighting (the app's keyword set) ---------- */

  const KW_WORDS = uniq(SQL_KEYWORDS.map((k) => k.split(" ")[0])).sort((a, b) => b.length - a.length);
  const TOKEN_RE = new RegExp(
    String.raw`(/\*[\s\S]*?\*/)|(--[^\n]*)|('(?:[^'\\]|\\.)*')|\b(\d+(?:\.\d+)?)\b|\b(` + KW_WORDS.join("|") + String.raw`)\b`,
    "gi",
  );
  const OPEN = { "(": ")", "[": "]", "{": "}" };
  const CLOSE = { ")": "(", "]": "[", "}": "{" };

  function tokenClasses(text) {
    const cls = new Array(text.length).fill("");
    TOKEN_RE.lastIndex = 0;
    let m;
    while ((m = TOKEN_RE.exec(text))) {
      const c = m[1] || m[2] ? "c" : m[3] ? "s" : m[4] ? "n" : "k";
      cls.fill(c, m.index, m.index + m[0].length);
    }
    return cls;
  }
  function highlightHtml(text) {
    const cls = tokenClasses(text);
    let html = "";
    let run = "";
    let key = "";
    for (let i = 0; i <= text.length; i++) {
      const c = i < text.length ? cls[i] : null;
      if (c !== key || i === text.length) {
        if (run) html += key ? `<span class="${key}">${esc(run)}</span>` : esc(run);
        run = "";
        key = c;
      }
      if (i < text.length) run += text[i];
    }
    return html;
  }

  function findBracket(text, idx, open, close, forward) {
    let depth = 0;
    if (forward) {
      for (let i = idx; i < text.length; i++) {
        if (text[i] === open) depth++;
        else if (text[i] === close && --depth === 0) return i;
      }
    } else {
      for (let i = idx; i >= 0; i--) {
        if (text[i] === close) depth++;
        else if (text[i] === open && --depth === 0) return i;
      }
    }
    return null;
  }
  function bracketPair(text, pos) {
    for (const p of [pos, pos - 1]) {
      if (p < 0 || p >= text.length) continue;
      const ch = text[p];
      let match;
      if (OPEN[ch]) match = findBracket(text, p, ch, OPEN[ch], true);
      else if (CLOSE[ch]) match = findBracket(text, p, CLOSE[ch], ch, false);
      else continue;
      if (match !== null) return [p, match];
    }
    return null;
  }

  /* ---------- ui.py fuzzy_score, for the command palette ---------- */

  function fuzzyScore(query, text) {
    const q = query.toLowerCase();
    const t = text.toLowerCase();
    if (!q) return 0;
    const pos = t.indexOf(q);
    if (pos === 0) return 1000 - t.length;
    if (pos > 0) return 700 + (/[a-z0-9]/i.test(t[pos - 1]) ? 0 : 60) - pos - Math.floor(t.length / 4);
    let score = 0;
    let ti = 0;
    let prev = -2;
    for (const ch of q) {
      const j = t.indexOf(ch, ti);
      if (j < 0) return null;
      if (j === 0 || !/[a-z0-9]/i.test(t[j - 1])) score += 30;
      if (j === prev + 1) score += 12;
      score -= j - ti;
      prev = j;
      ti = j + 1;
    }
    return 300 + score - Math.floor(t.length / 6);
  }

  /* ---------- sessions: one per tab, like QueryTab ---------- */

  let tabCounter = 0;
  const sessions = [];
  let cur = null;
  let sidebarShown = true;
  let lineNumbers = true;
  let editorPt = 11;

  function newSession() {
    tabCounter += 1;
    return {
      index: tabCounter,
      title: `Query ${tabCounter}`,
      connected: false,
      text: "",
      selA: 0,
      selB: 0,
      scrollTop: 0,
      scrollLeft: 0,
      sets: [],
      view: "placeholder",
      log: [],
      dirty: false,
      status: ["Ready.", "info"],
      timing: "",
      openTables: new Set(),
      dbOpen: true,
    };
  }

  const sessionSchema = () => (cur && cur.connected ? schema : null);

  function saveCurrent() {
    if (!cur) return;
    cur.text = input.value;
    cur.selA = input.selectionStart;
    cur.selB = input.selectionEnd;
    cur.scrollTop = input.scrollTop;
    cur.scrollLeft = input.scrollLeft;
  }

  function loadCurrent() {
    input.value = cur.text;
    input.setSelectionRange(cur.selA, cur.selB);
    hideSuggestions();
    if (!findBar.hidden) refreshFind(false);
    renderTree();
    renderResults();
    renderLog();
    syncChrome();
    render();
    input.scrollTop = cur.scrollTop;
    input.scrollLeft = cur.scrollLeft;
    syncScroll();
  }

  function renderTabs() {
    tabsEl.replaceChildren(...sessions.map((s) => {
      const wrap = el("div", { class: "np-tab" + (s === cur ? " is-current" : ""), role: "presentation" });
      const b = el("button", { type: "button", role: "tab", "aria-selected": s === cur ? "true" : "false", tabindex: s === cur ? "0" : "-1" }, s.title + (s.dirty ? " •" : ""));
      b.addEventListener("click", () => switchTo(s));
      const x = el("span", { class: "np-tabx", "aria-hidden": "true", title: "Close tab (Ctrl+W)" });
      x.appendChild(icon("ni-close"));
      x.addEventListener("click", (e) => {
        e.stopPropagation();
        closeTab(s);
      });
      wrap.addEventListener("auxclick", (e) => {
        if (e.button === 1) {
          e.preventDefault();
          closeTab(s);
        }
      });
      wrap.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        openMenuAt([
          ["Close tab", "Ctrl+W", () => closeTab(s)],
          ["Close other tabs", "", sessions.length > 1 ? () => closeOthers(s) : null],
          ["Close all tabs", "", () => closeAll()],
          "-",
          ["New tab", "Ctrl+T", () => addTab()],
        ], e.clientX, e.clientY, false, b, true);
      });
      wrap.append(b, x);
      return wrap;
    }));
  }
  // NotepaDBApp._on_tab_double_click: double-click the empty tab strip for a new tab
  tabsEl.addEventListener("dblclick", (e) => {
    if (e.target === tabsEl) addTab();
  });
  tabsEl.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const i = sessions.indexOf(cur);
    const next = sessions[(i + (e.key === "ArrowRight" ? 1 : -1) + sessions.length) % sessions.length];
    switchTo(next);
    const btn = tabsEl.querySelector('[aria-selected="true"]');
    if (btn) btn.focus();
  });

  function switchTo(s) {
    if (s === cur) return;
    saveCurrent();
    cur = s;
    loadCurrent();
    renderTabs();
  }

  function addTab() {
    const s = newSession();
    s.log.push([clock(), "Ready.", "info"]);
    sessions.push(s);
    switchTo(s);
    if (!fake) input.focus({ preventScroll: true });
    return s;
  }

  // NotepaDBApp.close_tab: the last tab is reset instead of closed.
  function closeTab(s) {
    s = s || cur;
    if (sessions.length === 1) {
      Object.assign(s, { connected: false, text: "", selA: 0, selB: 0, sets: [], view: "placeholder", title: `Query ${s.index}`, dirty: false, timing: "", status: ["Ready.", "info"] });
      if (s === cur) loadCurrent();
      renderTabs();
      return;
    }
    const i = sessions.indexOf(s);
    sessions.splice(i, 1);
    if (s === cur) {
      cur = sessions[Math.min(i, sessions.length - 1)];
      loadCurrent();
    }
    renderTabs();
  }
  function closeOthers(keep) {
    for (const s of sessions.slice()) if (s !== keep) closeTab(s);
    if (cur !== keep) switchTo(keep);
  }
  function closeAll() {
    for (const s of sessions.slice()) closeTab(s);
  }
  function nextTab(delta) {
    if (sessions.length < 2) return;
    switchTo(sessions[(sessions.indexOf(cur) + delta + sessions.length) % sessions.length]);
  }

  // QueryTab._auto_title: name the tab after the database; SQLite drops the extension.
  function autoTitle(s) {
    const base = DB_FILE.replace(/\.[^.]+$/, "");
    const taken = new Set(sessions.filter((t) => t !== s).map((t) => t.title));
    let n = 1;
    while (taken.has(`${base}${n}`)) n += 1;
    s.title = `${base}${n}`;
  }

  function markDirty() {
    if (cur && !cur.dirty) {
      cur.dirty = true;
      renderTabs();
      renderTitle();
    }
  }

  /* ---------- status bar, log and window chrome ---------- */

  const STATUS_ICON = { info: "ni-info", muted: "ni-info", ok: "ni-success", error: "ni-error", warning: "ni-warning", busy: "ni-history" };

  function logMessage(s, text, kind = "info") {
    s.log.push([clock(), text, kind]);
    if (s === cur) renderLog();
  }
  // QueryTab.set_status: the status bar message, optionally also logged in Messages
  function setStatus(text, kind = "info", log = true, s = cur) {
    s.status = [text, kind];
    if (log) logMessage(s, text, kind);
    if (s === cur) renderStatus();
  }
  function renderLog() {
    const cls = { error: "err", warning: "err", ok: "ok", muted: "mut" };
    logEl.innerHTML = cur.log.map(([t, text, kind]) => `<span class="t">${t}  </span>${cls[kind] ? `<span class="${cls[kind]}">${esc(text)}</span>` : esc(text)}`).join("\n") + "\n";
    logEl.scrollTop = logEl.scrollHeight;
  }
  function renderStatus() {
    const [text, kind] = cur.status;
    stMsg.className = "np-st-msg" + ({ ok: " is-ok", error: " is-err", warning: " is-warn", busy: " is-busy" }[kind] || "");
    stMsg.querySelector("use").setAttribute("href", `#${STATUS_ICON[kind] || "ni-info"}`);
    stText.textContent = text;
    stTime.textContent = cur.timing || "";
  }
  function renderTitle() {
    titleEl.textContent = `${cur.title}${cur.dirty ? " •" : ""} — ${APP_NAME}`;
  }
  function renderPosition() {
    const before = input.value.slice(0, caretPos());
    const line = before.split("\n").length;
    const col = before.length - before.lastIndexOf("\n");
    stPos.textContent = `Ln ${line}, Col ${col}`;
  }
  function syncChrome() {
    const on = cur.connected;
    stConn.classList.toggle("is-on", on);
    stConnText.textContent = on ? `${DB_FILE}  ·  SQLite` : "Not connected";
    connectLabel.textContent = on ? DB_FILE : "Connect";
    runBtn.disabled = running;
    stopBtn.disabled = !running;
    renderStatus();
    renderTitle();
    renderPosition();
  }

  /* ---------- editor ---------- */

  let fake = false; // the autoplay owns the caret, so draw caret and selection ourselves
  let suppressSuggest = false;
  let composing = false;
  let charW = 8.2;
  let lineH = 21;
  let lastGutter = "";
  const curlineEl = el("div", { class: "np-curline", "aria-hidden": "true" });
  codeBox.insertBefore(curlineEl, hl);

  function caretPos() {
    return input.selectionDirection === "backward" ? input.selectionStart : input.selectionEnd;
  }

  let findMatches = [];

  function render() {
    const text = input.value;
    const cls = tokenClasses(text);
    const caret = caretPos();
    const live = fake || isFocused();
    const pair = live ? bracketPair(text, caret) : null;
    const selA = fake ? input.selectionStart : 0;
    const selB = fake ? input.selectionEnd : 0;
    const caretAt = fake && selA === selB ? caret : -1;
    const findMask = findMatches.length ? new Uint8Array(text.length) : null;
    if (findMask) for (const [a, b] of findMatches) findMask.fill(1, a, b);

    let html = "";
    let run = "";
    let runKey = "";
    const flush = () => {
      if (!run) return;
      html += runKey ? `<span class="${runKey}">${esc(run)}</span>` : esc(run);
      run = "";
    };
    for (let i = 0; i <= text.length; i++) {
      if (i === caretAt) {
        flush();
        html += '<span class="caret"></span>';
      }
      if (i === text.length) break;
      const parts = [];
      if (cls[i]) parts.push(cls[i]);
      if (findMask && findMask[i]) parts.push("f");
      if (pair && (i === pair[0] || i === pair[1])) parts.push("b");
      if (i >= selA && i < selB) parts.push("sel");
      const key = parts.join(" ");
      if (key !== runKey) {
        flush();
        runKey = key;
      }
      run += text[i];
    }
    flush();
    // the inner block carries the overlay's extra scroll room
    hl.innerHTML = `<span class="hl-in">${html}\n</span>`;

    const lines = text.split("\n").length;
    const curLine = text.slice(0, caret).split("\n").length;
    const key = `${lines}:${curLine}:${live}`;
    if (key !== lastGutter) {
      lastGutter = key;
      let g = "";
      for (let i = 1; i <= lines; i++) g += (i === curLine && live ? `<span class="cur">${i}</span>` : String(i)) + "\n";
      gutter.innerHTML = g;
      gutter.style.minWidth = `${Math.max(3, String(lines).length) + 1}ch`;
    }
    hint.hidden = text.length > 0;
    placeCurline(curLine, live);
    syncScroll();
    renderPosition();
  }

  function placeCurline(line, live) {
    curlineEl.hidden = !live;
    curlineEl.style.transform = `translateY(${8 + (line - 1) * lineH - input.scrollTop}px)`;
    curlineEl.style.height = `${lineH}px`;
    curlineEl.dataset.line = String(line);
  }

  function syncScroll() {
    hl.scrollTop = input.scrollTop;
    hl.scrollLeft = input.scrollLeft;
    gutter.scrollTop = input.scrollTop;
    if (curlineEl.dataset.line) curlineEl.style.transform = `translateY(${8 + (Number(curlineEl.dataset.line) - 1) * lineH - input.scrollTop}px)`;
  }

  function measure() {
    const probe = el("span", { class: "np-probe", "aria-hidden": "true" }, "0".repeat(80));
    const cs = getComputedStyle(input);
    probe.style.font = cs.font;
    codeBox.appendChild(probe);
    const w = probe.getBoundingClientRect().width / 80;
    probe.remove();
    if (w > 0) charW = w;
    lineH = parseFloat(cs.lineHeight) || lineH;
  }

  function caretXY(pos) {
    const before = input.value.slice(0, pos);
    const lines = before.split("\n");
    let col = 0;
    for (const ch of lines[lines.length - 1]) col = ch === "\t" ? col + 4 - (col % 4) : col + 1;
    const cs = getComputedStyle(input);
    return {
      x: parseFloat(cs.paddingLeft) + col * charW - input.scrollLeft,
      top: parseFloat(cs.paddingTop) + (lines.length - 1) * lineH - input.scrollTop,
      lh: lineH,
    };
  }

  function ensureVisible(pos) {
    const { x, top, lh } = caretXY(pos);
    if (top < 0) input.scrollTop += top - 4;
    else if (top + lh > input.clientHeight - 4) input.scrollTop += top + lh - input.clientHeight + 8;
    if (x < 24) input.scrollLeft = Math.max(0, input.scrollLeft + x - 48);
    else if (x > input.clientWidth - 24) input.scrollLeft += x - input.clientWidth + 64;
    syncScroll();
  }

  // Replace [start, end) with s. When the user is typing, go through
  // execCommand so the change lands in the textarea's undo history.
  function replaceRange(start, end, s, selectInserted = false) {
    suppressSuggest = true;
    let done = false;
    if (isFocused() && !fake) {
      input.setSelectionRange(start, end);
      try {
        done = document.execCommand("insertText", false, s);
      } catch (_) {
        done = false;
      }
    }
    if (!done) input.setRangeText(s, start, end, "end");
    if (selectInserted) input.setSelectionRange(start, start + s.length);
    suppressSuggest = false;
    markDirty();
    render();
    ensureVisible(caretPos());
  }

  function lineBounds(a, b) {
    const v = input.value;
    const start = v.lastIndexOf("\n", a - 1) + 1;
    let endIdx = b > a && v[b - 1] === "\n" ? b - 1 : b;
    let end = v.indexOf("\n", endIdx);
    if (end === -1) end = v.length;
    return [start, end];
  }

  function selectedSql() {
    const a = input.selectionStart, b = input.selectionEnd;
    return a !== b ? input.value.slice(a, b) : null;
  }

  // QueryTab._insert_text: append on its own line and select it, so Run acts
  // on just this statement.
  function appendAndSelect(sql) {
    let v = input.value;
    if (v && !v.endsWith("\n")) v += "\n";
    const start = v.length;
    input.value = v + sql;
    input.setSelectionRange(start, input.value.length);
    markDirty();
    render();
    ensureVisible(input.value.length);
  }

  // toggle_comment: "-- " on every non-blank line, or off again
  function toggleComment() {
    const [a, b] = lineBounds(input.selectionStart, input.selectionEnd);
    const hadSel = input.selectionStart !== input.selectionEnd;
    const lines = input.value.slice(a, b).split("\n");
    const nonBlank = lines.filter((l) => l.trim());
    const all = nonBlank.length > 0 && nonBlank.every((l) => l.trimStart().startsWith("--"));
    const out = lines.map((l) => (all ? l.replace(/^(\s*)--\s?/, "$1") : l.trim() ? "-- " + l : l)).join("\n");
    const caret = input.selectionEnd;
    replaceRange(a, b, out, hadSel);
    if (!hadSel) {
      const p = Math.max(a, Math.min(a + out.length, caret + (out.length - (b - a))));
      input.setSelectionRange(p, p);
      render();
    }
  }

  // _indent_selection: four spaces in, or up to four (or a tab) out
  function indentLines(outdent) {
    const [a, b] = lineBounds(input.selectionStart, input.selectionEnd);
    const lines = input.value.slice(a, b).split("\n");
    const out = lines.map((l) => {
      if (!outdent) return "    " + l;
      const spaces = l.length - l.replace(/^ +/, "").length;
      const remove = spaces ? Math.min(4, spaces) : l.startsWith("\t") ? 1 : 0;
      return l.slice(remove);
    }).join("\n");
    replaceRange(a, b, out, true);
  }

  // move_line: Alt+Up / Alt+Down
  function moveLine(delta) {
    const v = input.value;
    const [a, b] = lineBounds(input.selectionStart, input.selectionEnd);
    const caret = caretPos();
    const hadSel = input.selectionStart !== input.selectionEnd;
    if (delta < 0) {
      if (a === 0) return;
      const pa = v.lastIndexOf("\n", a - 2) + 1;
      const other = v.slice(pa, a - 1);
      const block = v.slice(a, b);
      replaceRange(pa, b, block + "\n" + other);
      const shift = -(other.length + 1);
      if (hadSel) input.setSelectionRange(pa, pa + block.length);
      else input.setSelectionRange(caret + shift, caret + shift);
    } else {
      if (b >= v.length) return;
      let nb = v.indexOf("\n", b + 1);
      if (nb === -1) nb = v.length;
      const other = v.slice(b + 1, nb);
      const block = v.slice(a, b);
      replaceRange(a, nb, other + "\n" + block);
      const shift = other.length + 1;
      if (hadSel) input.setSelectionRange(a + shift, a + shift + block.length);
      else input.setSelectionRange(caret + shift, caret + shift);
    }
    render();
    ensureVisible(caretPos());
  }

  function goToLine() {
    const total = input.value.split("\n").length;
    const current = input.value.slice(0, caretPos()).split("\n").length;
    promptDialog("Go to line", `Line number (1–${total})`, String(current), "Go", (value) => {
      const v = value.trim();
      return /^\d+$/.test(v) && Number(v) >= 1 && Number(v) <= total ? null : `Enter a line number between 1 and ${total}.`;
    }).then((value) => {
      if (value === null) return;
      const n = Number(value.trim());
      const lines = input.value.split("\n");
      let pos = 0;
      for (let i = 0; i < n - 1; i++) pos += lines[i].length + 1;
      input.focus({ preventScroll: true });
      input.setSelectionRange(pos, pos);
      render();
      ensureVisible(pos);
    });
  }

  function setEditorSize(pt) {
    editorPt = Math.max(7, Math.min(32, pt));
    if (editorPt === 11) {
      np.style.removeProperty("--np-code-size");
      np.style.removeProperty("--np-lh");
    } else {
      np.style.setProperty("--np-code-size", `${editorPt}pt`);
      np.style.setProperty("--np-lh", `${Math.round(editorPt * (4 / 3) * 1.25 + 2.5)}px`);
    }
    measure();
    lastGutter = "";
    render();
  }
  function zoom(delta) {
    setEditorSize(editorPt + delta);
    setStatus(`Editor font size ${editorPt} pt`, "muted", false);
  }
  function zoomReset() {
    setEditorSize(11);
  }

  /* ---------- autocomplete popup ---------- */

  let ac = { items: [], index: 0, start: 0 };
  const acVisible = () => !acList.hidden;

  function hideSuggestions() {
    if (acList.hidden) return;
    acList.hidden = true;
    input.removeAttribute("aria-activedescendant");
  }

  function kindOf(value) {
    const low = value.toLowerCase();
    for (const [table, cols] of Object.entries(schema)) {
      if (table.toLowerCase() === low) return "table";
      if (cols.some((c) => c[0].toLowerCase() === low)) return "column";
    }
    return "keyword";
  }

  function updateSuggestions() {
    const sch = sessionSchema();
    if (!sch || input.selectionStart !== input.selectionEnd) return hideSuggestions();
    const pos = input.selectionEnd;
    const [start, , items] = getSuggestions(input.value, pos, sch);
    if (!items.length) return hideSuggestions();
    ac = { items, index: 0, start };
    const width = Math.max(...items.map((v) => v.length));
    acList.replaceChildren(...items.map((v, i) => {
      const kind = kindOf(v);
      const li = el("li", { id: `np-ac-${i}`, role: "option", class: `is-${kind}`, "aria-selected": i === 0 ? "true" : "false" });
      li.append(el("span", { class: "val" }, v.padEnd(width)), el("span", { class: "kind" }, kind));
      return li;
    }));
    acList.hidden = false;
    acList.scrollTop = 0;
    input.setAttribute("aria-activedescendant", "np-ac-0");
    placeSuggestions(pos);
  }

  function placeSuggestions(pos) {
    const { x, top, lh } = caretXY(pos);
    acList.style.left = "0px";
    acList.style.top = "0px";
    const maxLeft = codeBox.clientWidth - acList.offsetWidth - 4;
    acList.style.left = `${Math.max(2, Math.min(x, maxLeft))}px`;
    let y = top + lh + 4;
    if (y + acList.offsetHeight > codeBox.clientHeight && top - acList.offsetHeight - 4 > 0) y = top - acList.offsetHeight - 4;
    acList.style.top = `${y}px`;
  }

  function moveSuggestion(delta) {
    const n = ac.items.length;
    if (!n) return;
    const prev = acList.children[ac.index];
    if (prev) prev.setAttribute("aria-selected", "false");
    ac.index = (ac.index + delta + n) % n;
    const li = acList.children[ac.index];
    li.setAttribute("aria-selected", "true");
    li.scrollIntoView({ block: "nearest" });
    input.setAttribute("aria-activedescendant", li.id);
  }

  function acceptSuggestion() {
    if (!acVisible()) return false;
    const value = ac.items[ac.index];
    const start = ac.start;
    const end = input.selectionEnd;
    hideSuggestions();
    replaceRange(start, end, value);
    return true;
  }

  acList.addEventListener("mousedown", (e) => e.preventDefault());
  acList.addEventListener("click", (e) => {
    const li = e.target.closest("li");
    if (!li) return;
    moveSuggestion([...acList.children].indexOf(li) - ac.index);
    acceptSuggestion();
    input.focus();
  });

  /* ---------- keyboard: editor and window ---------- */

  const isKey = (e, key) => e.key.toLowerCase() === key;

  // Window-level shortcuts, as bound in NotepaDBApp._bind_shortcuts.
  // Returns true when the event was handled.
  function windowShortcut(e) {
    const mod = e.ctrlKey || e.metaKey;
    const sh = e.shiftKey;
    const run = (fn) => {
      e.preventDefault();
      hideSuggestions();
      fn();
      return true;
    };
    if (e.key === "F5") return run(runQuery);
    if (mod && e.key === "Enter") return run(sh ? runAll : runStatement);
    if (mod && (e.key === "Pause" || e.key === "Cancel")) return run(cancelQuery);
    if (e.key === "F1") return run(showShortcuts);
    if (e.key === "F3") return run(() => (sh ? findPrev() : findNext()));
    if (!mod) return false;
    if (sh && isKey(e, "p")) return run(openPalette);
    if (sh && isKey(e, "h")) return run(openHistory);
    if (sh && isKey(e, "r")) return run(refreshSchema);
    if (sh && isKey(e, "f")) return run(() => desktopOnly("Format SQL"));
    if (sh && isKey(e, "s")) return run(() => desktopOnly("Save as"));
    if (sh) return false;
    if (isKey(e, "l")) return run(explainQuery);
    if (isKey(e, "k")) return run(openConnect);
    if (isKey(e, "t") || isKey(e, "n")) return run(addTab);
    if (isKey(e, "w")) return run(() => closeTab(cur));
    if (isKey(e, "f")) return run(() => openFind(false));
    if (isKey(e, "h")) return run(() => openFind(true));
    if (isKey(e, "b")) return run(toggleSidebar);
    if (isKey(e, "g")) return run(goToLine);
    if (isKey(e, "p")) return run(openPalette);
    if (isKey(e, "e")) return run(runStatement);
    if (e.key === "/") return run(toggleComment);
    if (e.key === "=" || e.key === "+") return run(() => zoom(1));
    if (e.key === "-") return run(() => zoom(-1));
    if (e.key === "0") return run(zoomReset);
    if (isKey(e, "o")) return run(() => desktopOnly("Open"));
    if (isKey(e, "s")) return run(() => desktopOnly("Save"));
    if (e.key === "Tab") return run(() => nextTab(e.shiftKey ? -1 : 1));
    return false;
  }

  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-controls", "np-ac");

  let escArmed = false;
  input.addEventListener("keydown", (e) => {
    if (acVisible()) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        moveSuggestion(e.key === "ArrowDown" ? 1 : -1);
        return;
      }
      if (e.key === "Enter" && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        acceptSuggestion();
        return;
      }
      if (e.key === "Tab" && !e.shiftKey) {
        e.preventDefault();
        acceptSuggestion();
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        hideSuggestions();
        escArmed = true;
        return;
      }
    }
    if (windowShortcut(e)) return;
    if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
      e.preventDefault();
      moveLine(e.key === "ArrowUp" ? -1 : 1);
      return;
    }
    if (e.key === "Escape") {
      if (!findBar.hidden) {
        e.preventDefault();
        closeFind();
        return;
      }
      escArmed = true;
      return;
    }
    if (e.key === "Tab" && !e.ctrlKey && !e.metaKey && !e.altKey) {
      // Esc, then Tab, leaves the editor; otherwise Tab indents like the app.
      if (escArmed) {
        escArmed = false;
        return;
      }
      e.preventDefault();
      const a = input.selectionStart, b = input.selectionEnd;
      const multi = input.value.slice(a, b).includes("\n");
      if (e.shiftKey || multi) indentLines(e.shiftKey);
      else replaceRange(a, b, "    ");
      return;
    }
    if (e.key === "Enter" && !e.shiftKey && !e.altKey && !e.isComposing) {
      e.preventDefault();
      const v = input.value;
      const ls = v.lastIndexOf("\n", input.selectionStart - 1) + 1;
      const indent = v.slice(ls).match(/^[ \t]*/)[0];
      replaceRange(input.selectionStart, input.selectionEnd, "\n" + indent);
      return;
    }
    escArmed = false;
  });

  input.addEventListener("input", () => {
    markDirty();
    render();
    if (!findBar.hidden) refreshFind(false);
    if (!suppressSuggest && !composing) updateSuggestions();
  });
  input.addEventListener("compositionstart", () => { composing = true; });
  input.addEventListener("compositionend", () => {
    composing = false;
    updateSuggestions();
  });
  input.addEventListener("keyup", (e) => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) {
      render();
      updateSuggestions();
    } else if (["ArrowUp", "ArrowDown", "PageUp", "PageDown"].includes(e.key) && !acVisible()) {
      render();
    }
  });
  input.addEventListener("mousedown", () => hideSuggestions());
  input.addEventListener("mouseup", () => render());
  input.addEventListener("select", () => renderPosition());
  input.addEventListener("scroll", () => {
    syncScroll();
    hideSuggestions();
  });
  input.addEventListener("focus", () => render());
  input.addEventListener("blur", () => {
    hideSuggestions();
    render();
  });
  input.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    const hasSel = input.selectionStart !== input.selectionEnd;
    openMenuAt([
      ["Undo", "Ctrl+Z", () => { input.focus(); document.execCommand("undo"); }],
      ["Redo", "Ctrl+Y", () => { input.focus(); document.execCommand("redo"); }],
      "-",
      ["Cut", "Ctrl+X", hasSel ? () => { input.focus(); document.execCommand("cut"); } : null],
      ["Copy", "Ctrl+C", hasSel ? () => { input.focus(); document.execCommand("copy"); } : null],
      ["Paste", "Ctrl+V", null],
      ["Select all", "Ctrl+A", () => { input.focus(); input.select(); render(); }],
      "-",
      [hasSel ? "Run selection" : "Run statement", "Ctrl+Enter", () => runStatement()],
      ["Explain plan", "Ctrl+L", () => explainQuery()],
      "-",
      ["Toggle comment", "Ctrl+/", () => { input.focus(); toggleComment(); }],
      ["Format SQL", "Ctrl+Shift+F", null],
    ], e.clientX, e.clientY, false, input);
  });

  // Shortcuts that work anywhere in the window, not only in the editor.
  np.addEventListener("keydown", (e) => {
    if (e.target === input || e.defaultPrevented) return;
    if (modalEl.contains(e.target) || paletteEl.contains(e.target)) return;
    if (e.target === findInput || e.target === replaceInput) return;
    windowShortcut(e);
  });

  /* ---------- inline find / replace ---------- */

  let findCurrent = null;

  function findAll(query) {
    if (!query) return [];
    const caseOn = caseBtn.getAttribute("aria-pressed") === "true";
    const hay = caseOn ? input.value : input.value.toLowerCase();
    const needle = caseOn ? query : query.toLowerCase();
    const out = [];
    let i = 0;
    while (out.length < 5000) {
      const j = hay.indexOf(needle, i);
      if (j < 0) break;
      out.push([j, j + needle.length]);
      i = j + needle.length;
    }
    return out;
  }

  function setFindCount() {
    const q = findInput.value;
    if (!findMatches.length) findCount.textContent = q ? "No matches" : "";
    else if (findCurrent === null) findCount.textContent = `${fmtInt(findMatches.length)} match${findMatches.length !== 1 ? "es" : ""}`;
    else findCount.textContent = `${findCurrent + 1} of ${findMatches.length}`;
  }

  function refreshFind(reposition) {
    findMatches = findBar.hidden ? [] : findAll(findInput.value);
    findCurrent = null;
    const a = input.selectionStart, b = input.selectionEnd;
    const idx = findMatches.findIndex(([s, e]) => s === a && e === b);
    if (idx >= 0) findCurrent = idx;
    if (reposition && findMatches.length && findCurrent === null) {
      let k = findMatches.findIndex(([s]) => s >= a);
      if (k < 0) k = 0;
      selectMatch(k, false);
      return;
    }
    setFindCount();
    render();
  }

  function selectMatch(k, focusEditor) {
    const [s, e] = findMatches[k];
    findCurrent = k;
    input.setSelectionRange(s, e);
    if (focusEditor) input.focus({ preventScroll: true });
    ensureVisible(s);
    setFindCount();
    render();
  }

  function openFind(withReplace) {
    findBar.hidden = false;
    if (withReplace && replaceRow.hidden) toggleReplace();
    const sel = selectedSql();
    if (sel && !sel.includes("\n")) findInput.value = sel;
    findInput.focus();
    findInput.select();
    refreshFind(false);
  }
  function closeFind() {
    findBar.hidden = true;
    findMatches = [];
    findCurrent = null;
    setFindCount();
    render();
    input.focus({ preventScroll: true });
  }
  function toggleReplace() {
    replaceRow.hidden = !replaceRow.hidden;
    $('[data-find="toggle"]').setAttribute("aria-expanded", String(!replaceRow.hidden));
    (replaceRow.hidden ? findInput : replaceInput).focus();
  }
  function findNext() {
    if (findBar.hidden && !findInput.value) return openFind(false);
    if (findBar.hidden) {
      findBar.hidden = false;
      refreshFind(false);
    }
    if (!findMatches.length) return;
    const pos = input.selectionEnd;
    let k = findCurrent !== null ? findCurrent + 1 : findMatches.findIndex(([s]) => s >= pos);
    if (k < 0 || k >= findMatches.length) k = 0;
    selectMatch(k, false);
  }
  function findPrev() {
    if (findBar.hidden && !findInput.value) return openFind(false);
    if (findBar.hidden) {
      findBar.hidden = false;
      refreshFind(false);
    }
    if (!findMatches.length) return;
    const pos = input.selectionStart;
    let k = findCurrent !== null ? findCurrent - 1 : findMatches.map(([s]) => s < pos).lastIndexOf(true);
    if (k < 0) k = findMatches.length - 1;
    selectMatch(k, false);
  }
  function replaceCurrent() {
    if (findCurrent === null) return findNext();
    const [s, e] = findMatches[findCurrent];
    replaceRange(s, e, replaceInput.value);
    refreshFind(false);
    findNext();
  }
  function replaceAll() {
    const q = findInput.value;
    if (!q) return;
    const matches = findAll(q);
    if (!matches.length) return;
    let out = "";
    let last = 0;
    for (const [s, e] of matches) {
      out += input.value.slice(last, s) + replaceInput.value;
      last = e;
    }
    out += input.value.slice(last);
    replaceRange(0, input.value.length, out);
    setStatus(`Replaced ${plural(matches.length, "occurrence")}.`, "ok", false);
    refreshFind(false);
  }

  findInput.addEventListener("input", () => refreshFind(true));
  findInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      e.shiftKey ? findPrev() : findNext();
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeFind();
    } else if (windowShortcut(e)) {
      /* handled */
    }
  });
  replaceInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      replaceCurrent();
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeFind();
    }
  });
  findBar.addEventListener("click", (e) => {
    const b = e.target.closest("[data-find]");
    if (!b) return;
    const act = b.dataset.find;
    if (act === "prev") findPrev();
    else if (act === "next") findNext();
    else if (act === "close") closeFind();
    else if (act === "toggle") toggleReplace();
    else if (act === "replace") replaceCurrent();
    else if (act === "all") replaceAll();
    else if (act === "case") {
      b.setAttribute("aria-pressed", String(b.getAttribute("aria-pressed") !== "true"));
      refreshFind(false);
    }
  });

  /* ---------- schema sidebar ---------- */

  let selectedKey = null;

  function treeRow(depth, kind, label, key, opts = {}) {
    const row = el("div", {
      class: `np-row d${depth}` + (key === selectedKey ? " is-selected" : "") + (opts.active ? " is-active" : ""),
      role: "treeitem",
      "aria-level": depth + 1,
      "aria-selected": key === selectedKey ? "true" : "false",
      "aria-expanded": opts.expanded === undefined ? null : String(opts.expanded),
      "data-kind": kind,
      "data-key": key,
      "data-name": opts.name,
      "data-label": opts.label || label,
      draggable: opts.name ? "true" : null,
      tabindex: "-1",
      title: opts.title || null,
    });
    const tw = el("span", { class: "tw" });
    if (opts.expanded !== undefined) tw.appendChild(icon(opts.expanded ? "ni-chev_down" : "ni-chev_right"));
    row.appendChild(tw);
    row.appendChild(icon(kind === "database" ? "ni-database" : kind === "table" ? "ni-table" : "ni-column", `ti${kind === "database" ? " ti-db" : kind === "column" ? " ti-col" : ""}`));
    row.appendChild(document.createTextNode(label));
    if (opts.type) row.appendChild(el("span", { class: "ty" }, opts.type));
    return row;
  }

  function renderTree() {
    const sch = sessionSchema();
    sideEmpty.hidden = !!sch;
    treeEl.hidden = !sch;
    filterEl.disabled = !sch;
    if (!sch) {
      treeEl.replaceChildren();
      return;
    }
    const filt = filterEl.value.trim().toLowerCase();
    const rootLi = el("li", { role: "none" });
    rootLi.appendChild(treeRow(0, "database", `${DB_FILE}  (active)`, "db", { expanded: cur.dbOpen, label: DB_FILE, active: true }));
    if (cur.dbOpen) {
      const group = el("ul", { role: "group" });
      for (const table of Object.keys(sch).sort(byCode)) {
        const cols = sch[table];
        if (filt && !table.toLowerCase().includes(filt) && !cols.some(([name]) => name.toLowerCase().includes(filt))) continue;
        const open = filt ? true : cur.openTables.has(table);
        const li = el("li", { role: "none" });
        li.appendChild(treeRow(1, "table", table, `t:${table}`, { expanded: open, name: table }));
        if (open) {
          const sub = el("ul", { role: "group" });
          for (const [name, type] of cols) {
            if (filt && !table.toLowerCase().includes(filt) && !name.toLowerCase().includes(filt)) continue;
            const cli = el("li", { role: "none" });
            cli.appendChild(treeRow(2, "column", name, `c:${table}.${name}`, { name, type, title: `${name}  ${type}` }));
            sub.appendChild(cli);
          }
          li.appendChild(sub);
        }
        group.appendChild(li);
      }
      rootLi.appendChild(group);
    }
    treeEl.replaceChildren(rootLi);
    const sel = treeEl.querySelector(".is-selected") || treeEl.querySelector(".np-row");
    if (sel) sel.tabIndex = 0;
  }

  const visibleRows = () => [...treeEl.querySelectorAll(".np-row")];

  function selectRow(row, focus) {
    selectedKey = row.dataset.key;
    for (const r of visibleRows()) {
      const on = r === row;
      r.classList.toggle("is-selected", on);
      r.setAttribute("aria-selected", on ? "true" : "false");
      r.tabIndex = on ? 0 : -1;
    }
    if (focus) row.focus({ preventScroll: false });
  }

  function toggleRow(row) {
    if (row.dataset.kind === "database") cur.dbOpen = !cur.dbOpen;
    else if (row.dataset.kind === "table") {
      const t = row.dataset.name;
      if (cur.openTables.has(t)) cur.openTables.delete(t);
      else cur.openTables.add(t);
    } else return;
    const key = row.dataset.key;
    renderTree();
    const again = treeEl.querySelector(`[data-key="${CSS.escape(key)}"]`);
    if (again) selectRow(again, document.activeElement && treeEl.contains(document.activeElement));
  }

  treeEl.addEventListener("click", (e) => {
    const row = e.target.closest(".np-row");
    if (!row) return;
    selectRow(row, true);
    if (e.target.closest(".tw")) toggleRow(row);
  });
  // _on_tree_double_click: a table opens its top 1000 rows; a database becomes active
  treeEl.addEventListener("dblclick", (e) => {
    const row = e.target.closest(".np-row");
    if (!row) return;
    if (row.dataset.kind === "table") selectTopRows(row.dataset.name);
    else if (row.dataset.kind === "database") toggleRow(row);
  });
  treeEl.addEventListener("dragstart", (e) => {
    const row = e.target.closest(".np-row");
    if (!row || !row.dataset.name) return;
    e.dataTransfer.setData("text/plain", row.dataset.name);
    e.dataTransfer.effectAllowed = "copy";
  });

  // Type-to-jump, like QueryTab._on_tree_keypress
  let searchBuf = "";
  let searchAt = 0;
  treeEl.addEventListener("keydown", (e) => {
    const rows = visibleRows();
    const row = e.target.closest(".np-row");
    if (!row) return;
    const i = rows.indexOf(row);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      selectRow(rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))], true);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      if (row.getAttribute("aria-expanded") === "false") toggleRow(row);
      else if (rows[i + 1]) selectRow(rows[i + 1], true);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      if (row.getAttribute("aria-expanded") === "true") toggleRow(row);
      else {
        const parent = row.parentElement.parentElement.closest("li");
        const prow = parent && parent.querySelector(".np-row");
        if (prow) selectRow(prow, true);
      }
    } else if (e.key === "Enter") {
      e.preventDefault();
      toggleRow(row);
    } else if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) {
      e.preventDefault();
      const r = row.getBoundingClientRect();
      openTreeMenu(row, r.left + 24, r.bottom, true);
    } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey && e.key.trim()) {
      e.preventDefault();
      const now = performance.now();
      if (now - searchAt > 800) searchBuf = "";
      searchAt = now;
      searchBuf += e.key.toLowerCase();
      const keyOf = (r) => {
        const label = r.dataset.label || "";
        return label.includes(".") && r.dataset.kind !== "database" ? label.split(".").pop() : label;
      };
      const find = (p) => rows.find((r) => keyOf(r).toLowerCase().startsWith(p));
      let match = find(searchBuf);
      if (!match && searchBuf.length > 1) {
        searchBuf = e.key.toLowerCase();
        match = find(searchBuf);
      }
      if (match) selectRow(match, true);
    }
  });

  filterEl.addEventListener("input", renderTree);
  filterEl.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      filterEl.value = "";
      renderTree();
      input.focus();
    } else if (e.key === "Enter") {
      e.preventDefault();
      const first = treeEl.querySelector(".np-row");
      if (first) selectRow(first, true);
    }
  });

  treeEl.addEventListener("contextmenu", (e) => {
    const row = e.target.closest(".np-row");
    if (!row) return;
    e.preventDefault();
    selectRow(row, false);
    openTreeMenu(row, e.clientX, e.clientY, false);
  });

  function openTreeMenu(row, x, y, viaKeyboard) {
    let items = null;
    if (row.dataset.kind === "database") {
      items = [[`Use “${DB_FILE}”`, "", null], ["Refresh schema", "", () => refreshSchema()]];
    } else if (row.dataset.kind === "table") {
      const t = row.dataset.name;
      items = [
        ["Select top 1000 rows", "", () => selectTopRows(t)],
        ["Script as CREATE TABLE", "", () => scriptTable(t)],
        ["Copy column list", "", () => copyText(schema[t].map((c) => c[0]).join(", "), plural(schema[t].length, "column name"))],
        ["Copy table name", "", () => copyText(t, "table name")],
        "-",
        ["Export table to CSV…", "", () => exportTable(t)],
      ];
    } else if (row.dataset.kind === "column") {
      const c = row.dataset.name;
      items = [
        ["Insert column name", "", () => { input.focus({ preventScroll: true }); replaceRange(input.selectionStart, input.selectionEnd, c); }],
        ["Copy column name", "", () => copyText(c, "column name")],
      ];
    }
    if (items) openMenuAt(items, x, y, viaKeyboard, row, true);
  }

  /* ---------- table actions ---------- */

  const quoteIdent = (name) => `"${name}"`;

  // _select_top_rows: a new tab on the same connection, the query inserted and run
  function selectTopRows(table) {
    if (!cur.connected) return;
    const s = newSession();
    s.connected = true;
    s.openTables = new Set(cur.openTables);
    sessions.push(s);
    autoTitle(s);
    switchTo(s);
    appendAndSelect(`SELECT * FROM ${quoteIdent(table)} LIMIT 1000`);
    runQuery();
  }

  // _script_table_ddl: a new, unconnected tab titled "<table> DDL"
  function scriptTable(table) {
    const cols = schema[table];
    if (!cols) return;
    const lines = cols.map(([name, type]) => `    ${quoteIdent(name)} ${type}`).join(",\n");
    const s = newSession();
    s.title = `${table} DDL`;
    s.text = `CREATE TABLE ${quoteIdent(table)} (\n${lines}\n);`;
    s.log.push([clock(), "Ready.", "info"]);
    sessions.push(s);
    switchTo(s);
  }

  async function copyText(text, what) {
    try {
      await navigator.clipboard.writeText(text);
      setStatus(`Copied ${what} to clipboard.`, "muted", false);
    } catch (_) {
      setStatus("Copy failed: this browser blocked clipboard access.", "warning", false);
    }
  }

  async function exportTable(table) {
    setStatus(`Exporting ${table}…`, "busy", false);
    let database;
    try {
      database = await loadEngine();
    } catch (err) {
      engineFailed(err);
      return;
    }
    try {
      const [columns, rows] = execute(database, `SELECT * FROM ${quoteIdent(table)} LIMIT 100000`);
      download(`${table}.csv`, toCsv([columns, ...rows]));
      setStatus(`Exported ${plural(rows.length, "row")} to ${table}.csv`, "ok");
    } catch (err) {
      setStatus(`Export failed: ${err.message}`, "error");
    }
  }

  /* ---------- running SQL ---------- */

  let SQL = null;
  let db = null;
  let enginePromise = null;
  let running = false;
  const history = [];

  function loadEngine() {
    if (!enginePromise) {
      enginePromise = new Promise((resolve, reject) => {
        if (window.initSqlJs) return resolve();
        const s = document.createElement("script");
        s.src = VENDOR + "sql-wasm.js";
        s.async = true;
        s.onload = () => resolve();
        s.onerror = () => reject(new Error("The SQLite engine didn't download."));
        document.head.appendChild(s);
      })
        .then(() => window.initSqlJs({ locateFile: (file) => VENDOR + file }))
        .then((mod) => {
          SQL = mod;
          return freshDb();
        })
        .catch((err) => {
          enginePromise = null;
          throw err;
        });
    }
    return enginePromise;
  }

  function freshDb() {
    if (db) db.close();
    db = new SQL.Database();
    db.exec(SEED_SQL);
    return db;
  }

  const DML_RE = /^\s*(?:(?:--[^\n]*\n)|(?:\/\*[\s\S]*?\*\/)|\s)*(INSERT|UPDATE|DELETE|REPLACE)\b/i;

  // SqliteBackend.execute: (columns, rows) for a result set, else (None, rowcount)
  function execute(database, sql) {
    let stmt;
    try {
      stmt = database.prepare(sql);
    } catch (err) {
      if (/nothing to prepare/i.test(err.message)) return [null, -1];
      throw err;
    }
    try {
      const columns = stmt.getColumnNames();
      if (!columns.length) {
        stmt.step();
        return [null, DML_RE.test(sql) ? database.getRowsModified() : -1];
      }
      const rows = [];
      while (stmt.step()) rows.push(stmt.get());
      return [columns, rows];
    } finally {
      stmt.free();
    }
  }

  function setRunning(on) {
    running = on;
    runBtn.disabled = on;
    stopBtn.disabled = !on;
  }

  function engineFailed(err) {
    setStatus(`Error: ${err.message}`, "error");
    messageBox({ title: "Query error", message: "This browser couldn't start the in-page SQLite engine, so the demo can't run queries here. The desktop app doesn't depend on it.", kind: "error" });
  }

  function requireConnection() {
    if (cur.connected) return true;
    setStatus("Connect to a database first (Ctrl+K).", "warning", false);
    openConnect();
    return false;
  }

  function runQuery() {
    if (!requireConnection()) return Promise.resolve();
    return executeSqlText(selectedSql() ?? input.value, false);
  }
  function runStatement() {
    if (!requireConnection()) return Promise.resolve();
    const sql = selectedSql() ?? (statementAt(input.value, caretPos()) || "");
    return executeSqlText(sql, false);
  }
  function runAll() {
    if (!requireConnection()) return Promise.resolve();
    return executeSqlText(input.value, false);
  }
  function cancelQuery() {
    if (!running) return;
    setStatus("Sending cancel request…", "busy", false);
  }

  function explainQuery() {
    if (!requireConnection()) return;
    const sql = selectedSql() ?? (statementAt(input.value, caretPos()) || "");
    const statements = splitStatements(sql);
    if (!statements.length) return setStatus("Nothing to explain.", "muted", false);
    if (statements.length > 1) return setStatus("Select just one statement to explain.", "warning", false);
    executeSqlText(`EXPLAIN QUERY PLAN ${statements[0]}`, true);
  }

  async function executeSqlText(sqlText, isExplain) {
    if (running) {
      setStatus("A query is already running — stop it first (Ctrl+Break).", "warning", false);
      return;
    }
    const statements = splitStatements(sqlText);
    if (!statements.length) {
      setStatus("Nothing to run.", "muted", false);
      return;
    }
    const owner = cur;
    setRunning(true);
    setStatus("Running…", "busy", false);
    let database;
    try {
      database = await loadEngine();
    } catch (err) {
      setRunning(false);
      engineFailed(err);
      return;
    }
    const started = performance.now();
    const results = [];
    for (const stmt of statements) {
      try {
        const [columns, rows] = execute(database, stmt);
        results.push([stmt, columns, rows, null]);
      } catch (err) {
        results.push([stmt, null, null, err]);
        break;
      }
    }
    const elapsed = performance.now() - started;
    setRunning(false);
    owner.timing = fmtElapsed(elapsed);
    showBatchResults(owner, results, elapsed, !isExplain);
  }

  // QueryTab._show_batch_results
  function showBatchResults(s, results, elapsed, record) {
    s.sets = [];
    let totalRows = 0;
    let hadError = false;
    for (const [stmt, columns, rows, err] of results) {
      if (err) {
        hadError = true;
        logMessage(s, err.message, "error");
        logMessage(s, `    in: ${snippet(stmt, 160)}`, "muted");
        continue;
      }
      if (columns === null) {
        const affected = rows;
        logMessage(s, `OK · ${affected !== null && affected >= 0 ? plural(affected, "row") : "rows"} affected · ${snippet(stmt)}`, "ok");
        if (record) recordHistory(stmt, elapsed, affected);
        continue;
      }
      s.sets.push({ columns, rows });
      totalRows += rows.length;
      if (record) recordHistory(stmt, elapsed, rows.length);
    }
    const count = s.sets.length;
    const e = fmtElapsed(elapsed);
    if (hadError) {
      const first = results.find((r) => r[3]);
      s.view = "messages";
      setStatus(`${snippet(first[3].message, 140)}  ·  ${e}`, "error", false, s);
    } else if (count) {
      s.view = "set:0";
      setStatus(`${plural(totalRows, "row")}${count > 1 ? ` across ${plural(count, "result set")}` : ""}  ·  ${e}`, "ok", true, s);
    } else {
      s.view = "placeholder";
      setStatus(`Done  ·  ${e}`, "ok", false, s);
    }
    if (s === cur) {
      renderResults();
      renderStatus();
    }
  }

  function recordHistory(sql, elapsed, rows) {
    history.unshift({ time: stamp(), sql, elapsed_ms: Math.round(elapsed), rows, connection: DB_FILE });
    history.length = Math.min(history.length, HISTORY_LIMIT);
  }

  /* ---------- results ---------- */

  function renderResults() {
    const s = cur;
    const tabs = [];
    const mk = (label, view) => {
      const b = el("button", { class: "np-rtab", type: "button", role: "tab", "aria-selected": s.view === view ? "true" : "false", tabindex: s.view === view ? "0" : "-1" }, label);
      b.addEventListener("click", () => {
        s.view = view;
        renderResults();
      });
      tabs.push(b);
    };
    if (!s.sets.length) mk("Results", "placeholder");
    s.sets.forEach((set, i) => mk(`Result${s.sets.length > 1 ? ` ${i + 1}` : ""}  ·  ${fmtInt(set.rows.length)}`, `set:${i}`));
    mk("Messages", "messages");
    rtabsEl.replaceChildren(...tabs);

    const isSet = s.view.startsWith("set:");
    logEl.hidden = s.view !== "messages";
    gridEl.hidden = !isSet;
    let empty = rbody.querySelector(".np-rempty");
    if (s.view === "placeholder") {
      if (!empty) {
        empty = el("div", { class: "np-empty np-rempty" });
        empty.append(icon("ni-table"), el("b", null, "No results yet"), el("span", null, "Run a query to see its rows here. F5 runs the selection or everything, Ctrl+Enter the statement at the caret."));
        rbody.appendChild(empty);
      }
    } else if (empty) {
      empty.remove();
    }
    if (isSet) showResult(Number(s.view.slice(4)));
    else if (s.view === "messages") renderLog();
    renderFooter();
  }

  function display(v) {
    if (v instanceof Uint8Array) return `<${v.length} bytes>`;
    return String(v);
  }

  function showResult(index) {
    const { columns, rows } = cur.sets[index];
    const table = el("table", { class: "np-grid" });
    const head = table.createTHead().insertRow();
    const corner = el("th", { class: "ix", scope: "col" });
    corner.appendChild(el("span", { class: "sr-only" }, "Row"));
    head.appendChild(corner);
    for (const c of columns) head.appendChild(el("th", { scope: "col" }, c));
    const body = table.createTBody();
    rows.slice(0, RESULT_ROW_CAP).forEach((row, r) => {
      const tr = body.insertRow();
      tr.appendChild(el("th", { class: "ix", scope: "row" }, String(r + 1)));
      for (const v of row) {
        const td = tr.insertCell();
        if (v === null) {
          td.className = "null";
          td.textContent = "NULL";
        } else {
          td.textContent = display(v);
        }
      }
    });
    gridEl.replaceChildren(table);
    gridEl.scrollTop = 0;
    gridEl.scrollLeft = 0;
  }

  function renderFooter() {
    const s = cur;
    if (!s.view.startsWith("set:")) {
      rfootLabel.textContent = "";
      exportBtn.disabled = true;
      return;
    }
    const set = s.sets[Number(s.view.slice(4))];
    const parts = [plural(set.rows.length, "row"), plural(set.columns.length, "column")];
    if (set.rows.length > RESULT_ROW_CAP) parts.push(`showing the first ${fmtInt(RESULT_ROW_CAP)}`);
    if (s.timing) parts.push(s.timing);
    rfootLabel.textContent = parts.join("  ·  ");
    exportBtn.disabled = false;
  }

  gridEl.addEventListener("click", (e) => {
    const td = e.target.closest("td");
    if (!td) return;
    const prev = gridEl.querySelector("td.cur");
    if (prev) prev.classList.remove("cur");
    td.classList.add("cur");
  });
  gridEl.addEventListener("contextmenu", (e) => {
    const td = e.target.closest("td");
    if (!td) return;
    e.preventDefault();
    const prev = gridEl.querySelector("td.cur");
    if (prev) prev.classList.remove("cur");
    td.classList.add("cur");
    const tr = td.parentElement;
    const col = [...tr.children].indexOf(td);
    const header = gridEl.querySelectorAll("thead th")[col];
    openMenuAt([
      ["Copy", "Ctrl+C", () => copyText(td.textContent, "1 cell")],
      ["Copy cells with headers", "", () => copyText(`${header.textContent}\n${td.textContent}`, "1 row with headers")],
    ], e.clientX, e.clientY, false, gridEl, true);
  });

  function csvField(v) {
    if (v === null || v === undefined) return "";
    const s = v instanceof Uint8Array ? display(v) : String(v);
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }
  const toCsv = (rows) => rows.map((r) => r.map(csvField).join(",")).join("\r\n") + "\r\n";

  function download(name, text) {
    // utf-8-sig, like the app, so Excel reads accented names correctly
    const url = URL.createObjectURL(new Blob(["﻿" + text], { type: "text/csv;charset=utf-8" }));
    const a = el("a", { href: url, download: name });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  // QueryTab.export_csv: the grid's own text, so NULL cells export as "NULL"
  function exportResults() {
    const set = cur.view.startsWith("set:") ? cur.sets[Number(cur.view.slice(4))] : null;
    if (!set) {
      setStatus("Run a query that returns rows first.", "warning", false);
      return;
    }
    const rows = set.rows.slice(0, RESULT_ROW_CAP).map((r) => r.map((v) => (v === null ? "NULL" : display(v))));
    download(`${cur.title}.csv`, toCsv([set.columns, ...rows]));
    setStatus(`Exported ${plural(rows.length, "row")} to ${cur.title}.csv`, "ok");
  }

  /* ---------- connection ---------- */

  async function refreshSchema() {
    if (!cur.connected) {
      setStatus("Connect to a database first (Ctrl+K).", "warning", false);
      return;
    }
    setStatus("Refreshing schema…", "busy", false);
    let database;
    try {
      database = await loadEngine();
    } catch (err) {
      setStatus(`Schema refresh failed: ${err.message}`, "error");
      return;
    }
    const next = {};
    const tables = database.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name");
    for (const [name] of tables.length ? tables[0].values : []) {
      const info = database.exec(`PRAGMA table_info(${quoteIdent(String(name).replace(/"/g, '""'))})`);
      next[name] = info.length ? info[0].values.map((r) => [r[1], String(r[2] || "").toLowerCase()]) : [];
    }
    schema = next;
    renderTree();
    setStatus("Schema refreshed · 1 database.", "ok");
  }

  function disconnect() {
    if (!cur.connected) return;
    cur.connected = false;
    hideSuggestions();
    renderTree();
    syncChrome();
    setStatus("Disconnected.", "muted");
  }

  const savedProfiles = [
    { name: "baseball (SQLite)", engine: "sqlite", server: DB_FILE },
    { name: "reporting (PostgreSQL)", engine: "postgres", server: "db.internal", port: "5432", database: "reporting", auth: "sql", user: "analyst" },
    { name: "sales (SQL Server)", engine: "sqlserver", server: "sql01", database: "Sales", auth: "windows" },
  ];

  // ConnectDialog: saved connections on the left, the form on the right
  function buildConnect(values, interactive, uid) {
    const engine = values.engine;
    const [labelA, labelB, offered] = ENGINE_AUTH[engine];
    const isLite = engine === "sqlite";
    const root = el("div", { class: "np-connect" });

    const left = el("div", { class: "np-saved" });
    left.appendChild(el("b", null, "Saved connections"));
    const list = el("ul", { role: "listbox", "aria-label": "Saved connections", tabindex: interactive ? "0" : "-1", id: `${uid}-saved` });
    savedProfiles.forEach((p, i) => list.appendChild(el("li", { role: "option", "aria-selected": p.name === values.profile ? "true" : "false", "data-i": i }, p.name)));
    if (!savedProfiles.length) list.appendChild(el("li", { class: "np-saved-empty" }, "Nothing saved yet. Fill in the form, then click “Save as…”."));
    left.appendChild(list);
    const sa = el("div", { class: "np-saved-actions" });
    const saveBtn = el("button", { class: "np-b np-tool", type: "button", "data-c": "save" });
    saveBtn.append(icon("ni-plus"), document.createTextNode("Save as…"));
    sa.append(saveBtn, el("button", { class: "np-b np-tool", type: "button", "data-c": "delete", disabled: values.profile ? null : "" }, "Delete"));
    left.appendChild(sa);

    const form = el("form", { class: "np-form", novalidate: true, autocomplete: "off" });
    form.addEventListener("submit", (e) => e.preventDefault());
    const field = (labelText, control, id) => {
      const lab = id ? el("label", { for: id }, labelText) : el("span", { class: "np-flabel" }, labelText);
      const box = el("div", { class: "np-field" });
      if (control) box.append(...[].concat(control));
      form.append(lab, box);
      return box;
    };
    const engineSel = el("select", { id: `${uid}-engine` });
    for (const [key, name] of ENGINES) {
      const o = el("option", { value: key }, name);
      if (key === engine) o.selected = true;
      engineSel.appendChild(o);
    }
    field("Engine", engineSel, `${uid}-engine`);
    if (isLite) {
      field("Database file", [
        el("input", { id: `${uid}-server`, type: "text", value: values.server || "", spellcheck: "false", autocomplete: "off" }),
        el("button", { class: "np-btn", type: "button", disabled: "", title: "Opens the file chooser in the desktop app" }, "Browse…"),
      ], `${uid}-server`);
    } else {
      field("Server", el("input", { id: `${uid}-server`, type: "text", value: values.server || "", spellcheck: "false", autocomplete: "off" }), `${uid}-server`);
      field("Port", [el("input", { id: `${uid}-port`, class: "np-port", type: "text", value: values.port || "", autocomplete: "off" }), el("span", { class: "np-hintx" }, "Leave empty for the default")], `${uid}-port`);
      field(engine === "oracle" ? "Service name" : "Database", el("input", { id: `${uid}-db`, type: "text", value: values.database || "", autocomplete: "off" }), `${uid}-db`);
      const auth = offered ? values.auth || "windows" : "sql";
      if (offered) {
        const radios = el("div", { class: "np-checks", role: "radiogroup", "aria-label": "Sign in" });
        for (const [val, text] of [["windows", labelA], ["sql", labelB]]) {
          const lab = el("label", { class: "np-check" });
          lab.append(el("input", { type: "radio", name: `${uid}-auth`, value: val, checked: auth === val ? "" : null }), document.createTextNode(text));
          radios.appendChild(lab);
        }
        field("Sign in", radios);
      }
      const needCreds = !offered || auth === "sql";
      const userOff = !needCreds && !(engine === "postgres" || engine === "mysql");
      field("Username", el("input", { id: `${uid}-user`, type: "text", value: values.user || "", disabled: userOff ? "" : null, autocomplete: "off" }), `${uid}-user`);
      field("Password", el("input", { id: `${uid}-pass`, type: "password", value: values.password || "", disabled: needCreds ? null : "", autocomplete: "off" }), `${uid}-pass`);
      const opts = el("div", { class: "np-checks" });
      for (const text of ["Encrypt connection (TLS)", "Trust server certificate"]) {
        const lab = el("label", { class: "np-check" });
        lab.append(el("input", { type: "checkbox", checked: "" }), document.createTextNode(text));
        opts.appendChild(lab);
      }
      field("", opts);
    }
    form.appendChild(el("div", { class: "np-form-status", role: "status" }, values.status || ""));
    root.append(left, form);
    return root;
  }

  function readForm(host, uid) {
    const v = (id) => {
      const n = host.querySelector(`#${uid}-${id}`);
      return n ? n.value.trim() : "";
    };
    const radio = host.querySelector(`input[name="${uid}-auth"]:checked`);
    return { engine: v("engine"), server: v("server"), port: v("port"), database: v("db"), auth: radio ? radio.value : "sql", user: v("user"), password: host.querySelector(`#${uid}-pass`) ? host.querySelector(`#${uid}-pass`).value : "" };
  }

  function openConnect() {
    let values = { engine: "sqlite", server: DB_FILE, profile: null, status: "" };
    const uid = "np-c";
    const host = el("div");
    const status = (text, kind) => {
      const st = host.querySelector(".np-form-status");
      st.textContent = text;
      st.className = "np-form-status" + (kind === "error" ? " is-err" : kind === "ok" ? " is-ok" : "");
      values.status = "";
    };
    const mount = (focusId) => {
      host.replaceChildren(buildConnect(values, true, uid));
      host.querySelector(`#${uid}-engine`).addEventListener("change", (e) => {
        values = { ...readForm(host, uid), engine: e.target.value, profile: null, server: e.target.value === "sqlite" ? DB_FILE : "" };
        mount(`${uid}-engine`);
      });
      host.querySelectorAll(`input[name="${uid}-auth"]`).forEach((r) => r.addEventListener("change", () => {
        values = { ...readForm(host, uid), profile: values.profile };
        mount();
      }));
      const list = host.querySelector(`#${uid}-saved`);
      const pick = (i) => {
        const p = savedProfiles[i];
        if (!p) return;
        values = { engine: p.engine, server: p.server, port: p.port || "", database: p.database || "", auth: p.auth || "windows", user: p.user || "", profile: p.name };
        const needPass = p.engine !== "sqlite" && (p.auth === "sql" || p.engine === "oracle");
        values.status = `Loaded “${p.name}”.${needPass ? " Enter the password to connect." : ""}`;
        mount();
      };
      list.addEventListener("click", (e) => {
        const li = e.target.closest("li[data-i]");
        if (li) pick(Number(li.dataset.i));
      });
      list.addEventListener("dblclick", (e) => {
        if (e.target.closest("li[data-i]")) finish("connect");
      });
      list.addEventListener("keydown", (e) => {
        if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
        e.preventDefault();
        const i = savedProfiles.findIndex((p) => p.name === values.profile);
        pick(Math.max(0, Math.min(savedProfiles.length - 1, i + (e.key === "ArrowDown" ? 1 : -1))));
        host.querySelector(`#${uid}-saved`).focus();
      });
      host.querySelector('[data-c="save"]').addEventListener("click", () => {
        const f = readForm(host, uid);
        const suggestion = values.profile || (f.engine === "sqlite" ? f.server.replace(/\.[^.]+$/, "") : f.database ? `${f.server}/${f.database}` : f.server);
        promptDialog("Save connection", "Name for this connection", suggestion, "Save", (v) => (v.trim() ? null : "Enter a name.")).then((name) => {
          if (name === null) return;
          name = name.trim();
          const existing = savedProfiles.findIndex((p) => p.name === name);
          const prof = { name, engine: f.engine, server: f.server, port: f.port, database: f.database, auth: f.auth, user: f.user };
          if (existing >= 0) savedProfiles[existing] = prof;
          else savedProfiles.push(prof);
          savedProfiles.sort((a, b) => byLower(a.name, b.name));
          values = { ...f, profile: name, status: `Saved “${name}”.` };
          mount();
        });
      });
      host.querySelector('[data-c="delete"]').addEventListener("click", () => {
        const name = values.profile;
        if (!name) return;
        messageBox({ title: "Delete saved connection?", message: `“${name}” will be removed from your saved connections.`, kind: "question", buttons: [{ label: "Cancel", value: false }, { label: "Delete", value: true, kind: "danger" }], defaultValue: false }).then((yes) => {
          if (!yes) return;
          const i = savedProfiles.findIndex((p) => p.name === name);
          if (i >= 0) savedProfiles.splice(i, 1);
          values = { ...readForm(host, uid), profile: null, status: `Deleted “${name}”.` };
          mount();
        });
      });
      const target = focusId ? host.querySelector(`#${focusId}`) : null;
      if (target) target.focus();
    };
    mount();
    let finish = () => {};
    const dialog = openDialog({
      title: "Connect to a database",
      body: host,
      cls: "np-connect-dialog",
      flush: true,
      actions: [
        { label: "Test connection", value: "test", left: true, keepOpen: true },
        { label: "Cancel", value: null },
        { label: "Connect", value: "connect", kind: "accent", isDefault: true, keepOpen: true },
      ],
      focus: () => host.querySelector(`#${uid}-server`),
      onAction: async (value, close) => {
        const f = readForm(host, uid);
        if (value === "test") {
          const started = performance.now();
          status("Testing connection…", "muted");
          const err = await tryOpen(f);
          if (err) status(err, "error");
          else status(`Connected in ${fmtElapsed(performance.now() - started)} · ${plural(1, "database")} visible.`, "ok");
          return;
        }
        if (value === "connect") {
          if (!f.server) {
            status(f.engine === "sqlite" ? "Choose the database file to open." : "Server is required.", "error");
            return;
          }
          close("connect");
          connectWith(f);
        }
      },
    });
    finish = dialog.trigger;
  }

  async function tryOpen(f) {
    const name = ENGINES.find((e) => e[0] === f.engine)[1];
    if (!f.server) return f.engine === "sqlite" ? "Choose the database file to open." : "Server is required.";
    if (f.engine !== "sqlite") return `This browser demo can only open its own SQLite file. In the desktop app, this connects to your ${name} server.`;
    if (f.server.split(/[\\/]/).pop() !== DB_FILE) return `The demo has one database file, ${DB_FILE}. In the desktop app, Browse… opens any SQLite file.`;
    try {
      await loadEngine();
    } catch (err) {
      return err.message;
    }
    return null;
  }

  async function connectWith(f) {
    const target = f.engine === "sqlite" ? f.server.split(/[\\/]/).pop() : f.server;
    const s = cur;
    setStatus(`Connecting to ${target}…`, "busy");
    const err = await tryOpen(f);
    if (err) {
      setStatus(`Connection failed: ${err}`, "error", true, s);
      messageBox({ title: "Couldn't connect", message: err, kind: "error" });
      return;
    }
    s.connected = true;
    s.dbOpen = true;
    autoTitle(s);
    renderTabs();
    if (s === cur) {
      renderTree();
      syncChrome();
    }
    setStatus(`Connected to ${DB_FILE} · ${plural(Object.keys(schema).length, "table")} loaded.`, "ok", true, s);
    if (s === cur) input.focus({ preventScroll: true });
  }

  // A read-only copy of the dialog for the Engines section of the page
  const engineHost = document.getElementById("engine-dialog");
  const MOCK_VALUES = {
    sqlserver: { engine: "sqlserver", server: "sql01", database: "Sales", auth: "windows", profile: "sales (SQL Server)" },
    postgres: { engine: "postgres", server: "db.internal", port: "5432", database: "reporting", auth: "sql", user: "analyst", password: "", profile: "reporting (PostgreSQL)", status: "Loaded “reporting (PostgreSQL)”. Enter the password to connect." },
    mysql: { engine: "mysql", server: "mysql01", port: "3306", database: "shop", auth: "sql", user: "analyst", password: "secret-pass" },
    oracle: { engine: "oracle", server: "ora01", port: "1521", database: "ORCLPDB1", auth: "sql", user: "SALES", password: "secret-pass" },
    sqlite: { engine: "sqlite", server: "/home/me/data/app.db", profile: null },
  };
  function showEngineMock(engine) {
    if (!engineHost || !MOCK_VALUES[engine]) return;
    const title = el("div", { class: "np-dlg-title" }, "Connect to a database");
    const x = el("span", { class: "np-dlg-x" });
    x.appendChild(icon("ni-close"));
    title.appendChild(x);
    const actions = el("div", { class: "np-connect-actions" });
    actions.append(el("span", { class: "np-btn np-left" }, "Test connection"), el("span", { class: "np-btn" }, "Cancel"), el("span", { class: "np-btn is-accent" }, "Connect"));
    engineHost.replaceChildren(title, buildConnect(MOCK_VALUES[engine], false, `np-m-${engine}`), actions);
  }
  if (engineHost) {
    const selected = document.querySelector('[data-engine][aria-selected="true"]');
    showEngineMock(selected ? selected.dataset.engine : "sqlserver");
    for (const t of document.querySelectorAll("[data-engine]")) t.addEventListener("click", () => showEngineMock(t.dataset.engine));
    document.addEventListener("tabs:change", (e) => {
      const engine = e.detail && e.detail.tab && e.detail.tab.dataset.engine;
      if (engine) showEngineMock(engine);
    });
  }

  /* ---------- window chrome per desktop ---------- */

  const OS_NAMES = { linux: "GNOME", windows: "Windows", mac: "macOS" };

  // On macOS, Tk puts the menu bar at the top of the screen, not in the window.
  function setOS(os) {
    if (!OS_NAMES[os]) os = "linux";
    np.dataset.os = os;
    shell.dataset.os = os;
    if (globalbar) {
      if (os === "mac") {
        globalbar.hidden = false;
        globalbar.appendChild(menubar);
      } else {
        globalbar.hidden = true;
        np.insertBefore(menubar, toolbar);
      }
    }
    if (osSwitch) for (const b of osSwitch.querySelectorAll("[data-os]")) b.setAttribute("aria-pressed", b.dataset.os === os ? "true" : "false");
    closeMenu(false);
    requestAnimationFrame(() => {
      measure();
      render();
    });
  }
  if (osSwitch) osSwitch.addEventListener("click", (e) => {
    const b = e.target.closest("[data-os]");
    if (b) setOS(b.dataset.os);
  });

  /* ---------- menus ---------- */

  const theme = () => window.notepadbTheme;
  const themeMode = () => (theme() ? theme().mode() : "system");

  function toggleSidebar() {
    sidebarShown = !sidebarShown;
    np.classList.toggle("no-side", !sidebarShown);
    requestAnimationFrame(() => {
      measure();
      render();
    });
  }
  function toggleLineNumbers() {
    lineNumbers = !lineNumbers;
    gutter.hidden = !lineNumbers;
  }
  function desktopOnly(what) {
    setStatus(`${what} works in the desktop app.`, "info", false);
  }

  const MENUS = {
    File: () => [
      ["New tab", "Ctrl+T", () => addTab()], ["Open…", "Ctrl+O", null], "-",
      ["Save", "Ctrl+S", null], ["Save as…", "Ctrl+Shift+S", null], "-",
      ["Close tab", "Ctrl+W", () => closeTab(cur)], ["Close other tabs", "", sessions.length > 1 ? () => closeOthers(cur) : null], "-",
      ["Restore tabs on startup", "", null, false], "-", ["Quit", "Ctrl+Q", null],
    ],
    Edit: () => [
      ["Undo", "Ctrl+Z", () => { input.focus(); document.execCommand("undo"); }], ["Redo", "Ctrl+Y", () => { input.focus(); document.execCommand("redo"); }], "-",
      ["Cut", "Ctrl+X", null], ["Copy", "Ctrl+C", null], ["Paste", "Ctrl+V", null], ["Select all", "Ctrl+A", () => { input.focus(); input.select(); render(); }], "-",
      ["Find…", "Ctrl+F", () => openFind(false)], ["Find next", "F3", () => findNext()], ["Find previous", "Shift+F3", () => findPrev()], ["Replace…", "Ctrl+H", () => openFind(true)], "-",
      ["Toggle comment", "Ctrl+/", () => { input.focus(); toggleComment(); }], ["Format SQL", "Ctrl+Shift+F", null], ["Go to line…", "Ctrl+G", () => goToLine()],
    ],
    Connection: () => [
      ["Connect…", "Ctrl+K", () => openConnect()], ["Disconnect", "", cur.connected ? () => disconnect() : null], "-",
      ["Refresh schema", "Ctrl+Shift+R", () => refreshSchema()],
    ],
    Query: () => [
      ["Run selection or all", "F5", () => runQuery()], ["Run statement at caret", "Ctrl+Enter", () => runStatement()],
      ["Run everything", "Ctrl+Shift+Enter", () => runAll()], ["Stop", "Ctrl+Break", running ? () => cancelQuery() : null], "-",
      ["Explain plan", "Ctrl+L", () => explainQuery()], ["History…", "Ctrl+Shift+H", () => openHistory()], "-",
      ["Export results to CSV…", "", () => exportResults()],
    ],
    View: () => [
      ["Schema sidebar", "Ctrl+B", () => toggleSidebar(), sidebarShown], ["Line numbers", "", () => toggleLineNumbers(), lineNumbers], ["Word wrap", "", null, false], "-",
      ["Theme: follow system", "", () => theme() && theme().set("system"), themeMode() === "system", "radio"],
      ["Theme: light", "", () => theme() && theme().set("light"), themeMode() === "light", "radio"],
      ["Theme: dark", "", () => theme() && theme().set("dark"), themeMode() === "dark", "radio"], "-",
      ["Zoom in", "Ctrl+=", () => zoom(1)], ["Zoom out", "Ctrl+-", () => zoom(-1)], ["Reset zoom", "Ctrl+0", () => zoomReset()], "-",
      ["Full screen", "F11", null], ["Command palette…", "Ctrl+Shift+P", () => openPalette()],
    ],
    Help: () => [
      ["Keyboard shortcuts", "F1", () => showShortcuts()], ["Website", "", () => setStatus("You're on it: notepadb.com", "info", false)],
      ["Report an issue", "", () => window.open("https://github.com/DrorMaor/notepadb/issues", "_blank", "noopener")], "-",
      [`About ${APP_NAME}`, "", () => showAbout()],
    ],
  };

  let menuOwner = null;
  const finePointer = matchMedia("(pointer: fine)");

  function fillMenu(items, withNote = true) {
    menuEl.replaceChildren();
    let disabled = 0;
    for (const item of items) {
      if (item === "-") {
        menuEl.appendChild(el("hr"));
        continue;
      }
      const [label, accel, action, checked, kind] = item;
      const isCheck = checked !== undefined;
      const btn = el("button", {
        type: "button",
        role: isCheck ? (kind === "radio" ? "menuitemradio" : "menuitemcheckbox") : "menuitem",
        "aria-checked": isCheck ? String(!!checked) : null,
        "aria-disabled": action ? null : "true",
        tabindex: "-1",
      });
      btn.appendChild(el("span", null, label));
      if (accel) btn.appendChild(el("span", { class: "acc" }, accel));
      if (action) {
        btn.addEventListener("click", (ev) => {
          closeMenu(ev.detail === 0);
          action();
          // Tk keeps the focus in the editor; do the same, except on touch
          // screens where focusing the editor would pop up the keyboard.
          const lost = !document.activeElement || document.activeElement === document.body;
          if (lost && ev.detail !== 0 && finePointer.matches && modalEl.hidden && paletteEl.hidden) input.focus({ preventScroll: true });
        });
      } else {
        disabled += 1;
      }
      menuEl.appendChild(btn);
    }
    if (disabled && withNote) menuEl.appendChild(el("div", { class: "note" }, "Grayed-out commands work in the desktop app."));
  }

  function placeMenu(x, y) {
    const box = np.getBoundingClientRect();
    menuEl.hidden = false;
    const w = menuEl.offsetWidth, h = menuEl.offsetHeight;
    menuEl.style.left = `${Math.max(4, Math.min(x - box.left, box.width - w - 4))}px`;
    menuEl.style.top = `${Math.max(4, Math.min(y - box.top, box.height - h - 4))}px`;
  }
  const enabledItems = () => [...menuEl.querySelectorAll("button:not([aria-disabled])")];

  function openMenu(name, focusFirst) {
    const btn = menubar.querySelector(`[data-menu="${name}"]`);
    closeMenu(false);
    fillMenu(MENUS[name]());
    const r = btn.getBoundingClientRect();
    placeMenu(r.left, r.bottom);
    btn.setAttribute("aria-expanded", "true");
    menuOwner = btn;
    menuEl.setAttribute("aria-label", name);
    if (focusFirst) {
      const first = enabledItems()[0];
      if (first) first.focus();
    }
  }

  function openMenuAt(items, x, y, focusFirst, owner, noNote) {
    closeMenu(false);
    fillMenu(items, !noNote);
    placeMenu(x, y);
    menuOwner = owner;
    menuEl.setAttribute("aria-label", "Context menu");
    if (focusFirst) {
      const first = enabledItems()[0];
      if (first) first.focus();
    }
  }

  function closeMenu(restoreFocus) {
    if (menuEl.hidden) return;
    menuEl.hidden = true;
    menuEl.replaceChildren();
    for (const b of menubar.querySelectorAll('[aria-expanded="true"]')) b.setAttribute("aria-expanded", "false");
    const owner = menuOwner;
    menuOwner = null;
    if (restoreFocus && owner && owner.focus && owner.isConnected) owner.focus({ preventScroll: true });
  }
  const ownedByMenubar = () => menuOwner && menuOwner.closest && menuOwner.closest("#np-menubar");

  menubar.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-menu]");
    if (!btn) return;
    if (btn.getAttribute("aria-expanded") === "true") closeMenu(false);
    else openMenu(btn.dataset.menu, e.detail === 0);
  });
  menubar.addEventListener("mouseover", (e) => {
    const btn = e.target.closest("[data-menu]");
    if (btn && !menuEl.hidden && ownedByMenubar() && menuOwner !== btn) openMenu(btn.dataset.menu, false);
  });
  menubar.addEventListener("keydown", (e) => {
    const btns = [...menubar.querySelectorAll("[data-menu]")];
    const i = btns.indexOf(e.target);
    if (i < 0) return;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      btns[(i + (e.key === "ArrowRight" ? 1 : -1) + btns.length) % btns.length].focus();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      openMenu(e.target.dataset.menu, true);
    }
  });
  menuEl.addEventListener("keydown", (e) => {
    const items = enabledItems();
    const i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (items.length) items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
    } else if (e.key === "Escape") {
      e.preventDefault();
      const owner = menuOwner;
      closeMenu(false);
      if (owner && owner.focus) owner.focus();
    } else if ((e.key === "ArrowRight" || e.key === "ArrowLeft") && ownedByMenubar()) {
      e.preventDefault();
      const btns = [...menubar.querySelectorAll("[data-menu]")];
      const j = btns.indexOf(menuOwner);
      openMenu(btns[(j + (e.key === "ArrowRight" ? 1 : -1) + btns.length) % btns.length].dataset.menu, true);
    } else if (e.key === "Tab") {
      closeMenu(false);
    }
  });
  document.addEventListener("pointerdown", (e) => {
    if (menuEl.hidden) return;
    if (menuEl.contains(e.target) || (ownedByMenubar() && menubar.contains(e.target))) return;
    closeMenu(false);
  });

  /* ---------- dialogs (ui.Dialog, ui.MessageDialog) ---------- */

  const KIND_ICON = { info: "ni-info", question: "ni-question", warning: "ni-warning", error: "ni-error", success: "ni-success" };

  // A themed dialog: title bar, body, right-aligned buttons; Esc cancels and
  // Enter triggers the default button, as in ui.Dialog.
  function openDialog({ title, body, actions, cls = "", flush = false, focus = null, onAction = null }) {
    closeMenu(false);
    closePalette();
    hideSuggestions();
    const prevFocus = document.activeElement;
    let resolveFn;
    const done = new Promise((resolve) => { resolveFn = resolve; });
    const dialog = el("div", { class: `np-dialog ${cls}`.trim(), role: "dialog", "aria-modal": "true", "aria-labelledby": "np-dlg-title" });
    const titleEl = el("div", { class: "np-dlg-title", id: "np-dlg-title" }, title);
    const x = el("button", { class: "np-dlg-x", type: "button", "aria-label": "Close" });
    x.appendChild(icon("ni-close"));
    titleEl.appendChild(x);
    const bodyEl = el("div", { class: flush ? "" : "np-dlg-body" });
    bodyEl.appendChild(body);
    const actionsEl = el("div", { class: flush ? "np-connect-actions" : "np-dlg-actions" });
    const close = (value) => {
      document.removeEventListener("keydown", onKey, true);
      modalEl.hidden = true;
      modalEl.replaceChildren();
      const back = prevFocus && np.contains(prevFocus) && prevFocus !== document.body ? prevFocus : null;
      if (back) back.focus({ preventScroll: true });
      resolveFn(value);
    };
    let defaultBtn = null;
    const trigger = (value) => {
      const a = actions.find((x2) => x2.value === value);
      if (a && a.keepOpen && onAction) onAction(value, close);
      else close(value);
    };
    for (const a of actions) {
      const b = el("button", { class: `np-btn${a.kind === "accent" ? " is-accent" : a.kind === "danger" ? " is-danger" : ""}${a.left ? " np-left" : ""}`, type: "button" }, a.label);
      b.addEventListener("click", () => trigger(a.value));
      if (a.isDefault) defaultBtn = b;
      actionsEl.appendChild(b);
    }
    x.addEventListener("click", () => close(null));
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        close(null);
      } else if (e.key === "Enter" && dialog.contains(e.target) && e.target.tagName !== "BUTTON" && e.target.tagName !== "A" && e.target.tagName !== "TEXTAREA") {
        if (defaultBtn) {
          e.preventDefault();
          defaultBtn.click();
        }
      } else if (e.key === "Tab") {
        const f = [...dialog.querySelectorAll("button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), [tabindex='0']")];
        if (!f.length) return;
        const i = f.indexOf(document.activeElement);
        if (e.shiftKey && i <= 0) {
          e.preventDefault();
          f[f.length - 1].focus();
        } else if (!e.shiftKey && i === f.length - 1) {
          e.preventDefault();
          f[0].focus();
        }
      }
    };
    dialog.append(titleEl, bodyEl, actionsEl);
    modalEl.replaceChildren(dialog);
    modalEl.hidden = false;
    document.addEventListener("keydown", onKey, true);
    const first = (focus && focus()) || defaultBtn || actionsEl.lastChild;
    if (first) first.focus({ preventScroll: true });
    return { done, close, trigger };
  }

  function messageBox({ title, message, kind = "info", buttons = null, defaultValue = "ok" }) {
    const body = el("div", { class: `np-msg is-${kind}` });
    body.appendChild(icon(KIND_ICON[kind] || "ni-info"));
    const col = el("div");
    col.append(el("h2", { class: "np-h" }, title), el("p", null, message));
    body.appendChild(col);
    const acts = (buttons || [{ label: "OK", value: "ok", kind: "accent" }]).map((b) => ({ ...b, isDefault: b.value === defaultValue }));
    return openDialog({ title, body, actions: acts }).done;
  }

  function promptDialog(title, prompt, initial, ok, validate) {
    const body = el("div", { class: "np-prompt" });
    const lab = el("label", { for: "np-prompt-in" }, prompt);
    const field = el("div", { class: "np-field" });
    const inp = el("input", { id: "np-prompt-in", type: "text", value: initial || "", autocomplete: "off", spellcheck: "false" });
    field.appendChild(inp);
    const err = el("div", { class: "np-form-status is-err", role: "status" });
    body.append(lab, field, err);
    field.style.margin = "8px 0 4px";
    field.style.minWidth = "320px";
    return openDialog({
      title,
      body,
      actions: [{ label: "Cancel", value: null }, { label: ok, value: "ok", kind: "accent", isDefault: true, keepOpen: true }],
      focus: () => {
        setTimeout(() => inp.select(), 0);
        return inp;
      },
      onAction: (value, close) => {
        const problem = validate ? validate(inp.value) : null;
        if (problem) {
          err.textContent = problem;
          inp.setAttribute("aria-invalid", "true");
          return;
        }
        close(inp.value);
      },
    }).done.then((v) => (v === null || v === undefined ? null : v));
  }

  function showShortcuts() {
    const body = el("div", { class: "np-keys" });
    for (const [title, items] of SHORTCUT_GROUPS) {
      const g = el("div");
      g.appendChild(el("h2", { class: "np-h" }, title));
      const dl = el("dl");
      for (const [action, keys] of items) {
        const row = el("div");
        row.append(el("dt", null, action), el("dd", null, keys));
        dl.appendChild(row);
      }
      g.appendChild(dl);
      body.appendChild(g);
    }
    openDialog({ title: "Keyboard shortcuts", body, actions: [{ label: "Close", value: null, kind: "accent", isDefault: true }] });
  }

  function showAbout() {
    const verEl = document.querySelector("[data-pypi-version]");
    const version = verEl ? verEl.textContent.trim() : "0.2.0";
    const body = el("div", { class: "np-about" });
    body.appendChild(icon("ni-logo"));
    const col = el("div");
    col.append(
      el("h2", { class: "np-h" }, APP_NAME),
      el("p", { class: "mut" }, "A notepad, but only for databases."),
      el("p", { style: "margin-top:12px" }, "Type SQL, autocomplete the real table and column names on your server, press F5. Every tab is its own session with its own connection, schema browser and results."),
      el("p", { class: "mut", style: "margin-top:10px" }, "SQL Server · PostgreSQL · MySQL / MariaDB · Oracle · SQLite"),
      el("p", { class: "mut" }, `Version ${version} · Free software (GPL-2.0)`),
    );
    const links = el("div", { class: "links" });
    links.append(
      el("a", { href: "https://notepadb.com/" }, "Website"), el("span", { class: "mut" }, "·"),
      el("a", { href: "https://github.com/DrorMaor/notepadb" }, "Source code"), el("span", { class: "mut" }, "·"),
      el("a", { href: "https://ko-fi.com/drormaor" }, "Buy me a coffee"),
    );
    col.appendChild(links);
    body.appendChild(col);
    openDialog({ title: `About ${APP_NAME}`, body, actions: [{ label: "Close", value: null, kind: "accent", isDefault: true }] });
  }

  // HistoryDialog: filter, newest first, a highlighted preview, Insert / Insert & run
  function openHistory() {
    const body = el("div");
    const top = el("div", { class: "np-hist-filter" });
    const filt = el("input", { type: "text", placeholder: "Filter by SQL text or connection", "aria-label": "Filter by SQL text or connection", autocomplete: "off", spellcheck: "false" });
    const count = el("span");
    top.append(icon("ni-search"), filt, count);
    const tableBox = el("div", { class: "np-hist-table", tabindex: "0", role: "grid", "aria-label": "Query history" });
    const prev = el("pre", { class: "np-hist-prev", "aria-label": "Preview" });
    body.append(top, tableBox, prev);
    body.style.display = "grid";
    body.style.gap = "10px";
    let visible = [];
    let picked = 0;
    const fill = () => {
      const q = filt.value.trim().toLowerCase();
      visible = history.filter((h) => !q || `${h.sql} ${h.connection}`.toLowerCase().includes(q));
      count.textContent = q ? `${fmtInt(visible.length)} of ${fmtInt(history.length)}` : plural(history.length, "entry");
      const t = el("table");
      const head = t.createTHead().insertRow();
      for (const [h, r] of [["Time", ""], ["Duration", "r"], ["Rows", "r"], ["Connection", ""], ["SQL", ""]]) head.appendChild(el("th", { class: r || null, scope: "col" }, h));
      const tb = t.createTBody();
      visible.forEach((h, i) => {
        const tr = tb.insertRow();
        tr.setAttribute("aria-selected", i === picked ? "true" : "false");
        for (const [v, r] of [[h.time, ""], [fmtElapsed(h.elapsed_ms), "r"], [fmtInt(h.rows), "r"], [h.connection, ""], [snippet(h.sql, 140), ""]]) {
          const td = tr.insertCell();
          td.textContent = v;
          if (r) td.className = r;
        }
        tr.addEventListener("click", () => {
          picked = i;
          fill();
        });
        tr.addEventListener("dblclick", () => dlg.trigger("insert"));
      });
      if (!visible.length) {
        const tr = tb.insertRow();
        const td = tr.insertCell();
        td.colSpan = 5;
        td.textContent = history.length ? "No matches." : "Nothing here yet. Statements you run in this demo show up here, newest first.";
      }
      tableBox.replaceChildren(t);
      const h = visible[picked];
      prev.innerHTML = h ? highlightHtml(h.sql) : "";
    };
    filt.addEventListener("input", () => {
      picked = 0;
      fill();
    });
    tableBox.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      e.preventDefault();
      picked = Math.max(0, Math.min(visible.length - 1, picked + (e.key === "ArrowDown" ? 1 : -1)));
      fill();
    });
    fill();
    const dlg = openDialog({
      title: "Query history",
      body,
      cls: "np-hist",
      actions: [
        { label: "Clear history", value: "clear", left: true, keepOpen: true },
        { label: "Close", value: null },
        { label: "Insert", value: "insert" },
        { label: "Insert & run", value: "run", kind: "accent", isDefault: true },
      ],
      focus: () => filt,
      onAction: (value) => {
        if (value === "clear") {
          history.length = 0;
          picked = 0;
          fill();
        }
      },
    });
    dlg.done.then((choice) => {
      const h = visible[picked];
      if (!h || (choice !== "insert" && choice !== "run")) return;
      appendAndSelect(h.sql);
      if (choice === "run") runQuery();
    });
  }

  /* ---------- command palette (ui.CommandPalette) ---------- */

  let palItems = [];
  let palVisible = [];
  let palIndex = 0;

  // NotepaDBApp._palette_items, in the same order; desktop-only commands say so
  function paletteItems() {
    const items = [
      ["Run selection or everything", "F5", runQuery, "execute query"],
      ["Run statement at caret", "Ctrl+Enter", runStatement, "execute"],
      ["Run everything", "Ctrl+Shift+Enter", runAll, "execute all"],
      ["Stop query", "Ctrl+Break", cancelQuery, "cancel"],
      ["Explain plan", "Ctrl+L", explainQuery, "query plan"],
      ["Connect to a database…", "Ctrl+K", openConnect, "connection server"],
      ["Disconnect", "", disconnect, "connection"],
      ["Refresh schema", "Ctrl+Shift+R", refreshSchema, "reload tables"],
      ["New tab", "Ctrl+T", addTab, ""],
      ["Close tab", "Ctrl+W", () => closeTab(cur), ""],
      ["Open SQL file…", "Ctrl+O", () => desktopOnly("Open"), "load"],
      ["Save", "Ctrl+S", () => desktopOnly("Save"), "write file"],
      ["Save as…", "Ctrl+Shift+S", () => desktopOnly("Save as"), "write file"],
      ["Find", "Ctrl+F", () => openFind(false), "search"],
      ["Replace", "Ctrl+H", () => openFind(true), "search substitute"],
      ["Go to line…", "Ctrl+G", goToLine, "jump"],
      ["Toggle comment", "Ctrl+/", () => { input.focus(); toggleComment(); }, "comment lines"],
      ["Format SQL", "Ctrl+Shift+F", () => desktopOnly("Format SQL"), "pretty print beautify"],
      ["Query history…", "Ctrl+Shift+H", openHistory, "recent queries"],
      ["Export results to CSV…", "", exportResults, "save grid"],
      ["Toggle schema sidebar", "Ctrl+B", toggleSidebar, "explorer panel"],
      ["Toggle line numbers", "", toggleLineNumbers, "gutter"],
      ["Toggle word wrap", "", () => desktopOnly("Word wrap"), "editor"],
      ["Theme: follow system", "", () => theme() && theme().set("system"), "appearance dark light"],
      ["Theme: light", "", () => theme() && theme().set("light"), "appearance"],
      ["Theme: dark", "", () => theme() && theme().set("dark"), "appearance"],
      ["Zoom in", "Ctrl+=", () => zoom(1), "font size bigger"],
      ["Zoom out", "Ctrl+-", () => zoom(-1), "font size smaller"],
      ["Reset zoom", "Ctrl+0", zoomReset, "font size"],
      ["Full screen", "F11", () => desktopOnly("Full screen"), "window"],
      ["Keyboard shortcuts", "F1", showShortcuts, "help keys"],
      [`About ${APP_NAME}`, "", showAbout, "version"],
    ];
    if (cur.connected) for (const t of Object.keys(schema).sort(byLower)) items.push([`Table: ${t}`, "select top 1000 rows", () => selectTopRows(t), "open browse"]);
    return items;
  }

  function openPalette() {
    closeMenu(false);
    hideSuggestions();
    palItems = paletteItems();
    paletteEl.replaceChildren();
    const row = el("div", { class: "np-pal-in" });
    const inp = el("input", { type: "text", placeholder: "Type a command or a table name…", "aria-label": "Command", role: "combobox", "aria-expanded": "true", "aria-controls": "np-pal-list", "aria-autocomplete": "list", autocomplete: "off", spellcheck: "false" });
    row.append(icon("ni-terminal"), inp);
    const list = el("ul", { class: "np-pal-list", id: "np-pal-list", role: "listbox", "aria-label": "Commands" });
    paletteEl.append(row, list, el("p", { class: "np-pal-hint" }, "↑↓ to navigate · Enter to run · Esc to close"));
    paletteEl.hidden = false;
    const refilter = () => {
      const q = inp.value.trim();
      const scored = [];
      for (const item of palItems) {
        let s = fuzzyScore(q, item[0]);
        if (s === null && item[3]) {
          const k = fuzzyScore(q, item[3]);
          s = k === null ? null : k - 50;
        }
        if (s !== null) scored.push([s, item[0].toLowerCase(), item]);
      }
      if (q) scored.sort((a, b) => b[0] - a[0] || (a[1] < b[1] ? -1 : 1));
      palVisible = scored.slice(0, 60).map((x) => x[2]);
      palIndex = 0;
      list.replaceChildren(...palVisible.map((item, i) => {
        const li = el("li", { role: "option", id: `np-pal-${i}`, "aria-selected": i === 0 ? "true" : "false" });
        li.append(el("span", null, item[0]), el("em", null, item[1]));
        li.addEventListener("mousedown", (e) => e.preventDefault());
        li.addEventListener("click", () => runPal(i));
        return li;
      }));
      inp.setAttribute("aria-activedescendant", palVisible.length ? "np-pal-0" : "");
    };
    const move = (d) => {
      if (!palVisible.length) return;
      const items = list.children;
      items[palIndex].setAttribute("aria-selected", "false");
      palIndex = Math.max(0, Math.min(palVisible.length - 1, palIndex + d));
      items[palIndex].setAttribute("aria-selected", "true");
      items[palIndex].scrollIntoView({ block: "nearest" });
      inp.setAttribute("aria-activedescendant", items[palIndex].id);
    };
    inp.addEventListener("input", refilter);
    inp.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        move(e.key === "ArrowDown" ? 1 : -1);
      } else if (e.key === "PageDown" || e.key === "PageUp") {
        e.preventDefault();
        move(e.key === "PageDown" ? 8 : -8);
      } else if (e.key === "Enter") {
        e.preventDefault();
        runPal(palIndex);
      } else if (e.key === "Escape") {
        e.preventDefault();
        closePalette(true);
      }
    });
    inp.addEventListener("blur", () => setTimeout(() => {
      if (!paletteEl.contains(document.activeElement)) closePalette(false);
    }, 120));
    refilter();
    inp.focus({ preventScroll: true });
  }
  function runPal(i) {
    const item = palVisible[i];
    closePalette(true);
    if (item) item[2]();
  }
  function closePalette(focusEditor) {
    if (paletteEl.hidden) return;
    paletteEl.hidden = true;
    paletteEl.replaceChildren();
    if (focusEditor) input.focus({ preventScroll: true });
  }

  /* ---------- toolbar, status bar, sidebar buttons ---------- */

  np.addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    if (!b || b.disabled || !np.contains(b)) return;
    const act = b.dataset.act;
    hideSuggestions();
    if (act === "run") runQuery();
    else if (act === "stop") cancelQuery();
    else if (act === "connect") openConnect();
    else if (act === "refresh") refreshSchema();
    else if (act === "newtab") addTab();
    else if (act === "open") desktopOnly("Open");
    else if (act === "save") desktopOnly("Save");
    else if (act === "format") desktopOnly("Format SQL");
    else if (act === "history") openHistory();
    else if (act === "sidebar") toggleSidebar();
    else if (act === "palette") openPalette();
    else if (act === "theme" && theme()) theme().toggle(b);
    else if (act === "export") exportResults();
  });

  /* ---------- autoplay ---------- */

  const QUERY_A = "SELECT * FROM teams;";
  const QUERY_B = "SELECT t.team_name, p.last_name, p.jersey_number\nFROM teams t\nLEFT JOIN players p ON p.team_id = t.team_id;";
  const FINAL_TEXT = `${QUERY_A}\n\n${QUERY_B}`;

  // Each popup below is one the autocomplete really shows at that point.
  const SCRIPT = [
    ["type", "\n\nSELECT *\nFROM "], ["suggest"], ["wait", 720],
    ["down"], ["wait", 240], ["down"], ["wait", 420], ["accept"],
    ["type", " t\nLEFT JOIN "], ["suggest"], ["wait", 520],
    ["type", "pl", true], ["wait", 360], ["accept"],
    ["type", " p ON p."], ["suggest"], ["wait", 640],
    ["type", "te", true], ["wait", 320], ["accept"],
    ["type", " = t."], ["suggest"], ["wait", 560],
    ["type", "team_i", true], ["wait", 300], ["accept"],
    ["type", ";"], ["wait", 560],
    ["selectStar"], ["wait", 560],
    ["type", "t."], ["suggest"], ["wait", 560],
    ["type", "team_n", true], ["wait", 300], ["accept"],
    ["type", ", p."], ["suggest"], ["wait", 520],
    ["type", "la", true], ["wait", 320], ["accept"],
    ["type", ", p.j", true], ["wait", 380], ["accept"],
    ["wait", 700],
    ["runStatement"],
  ];

  let auto = null;
  let interacted = false;
  const keyHud = el("div", { class: "np-keyhud", "aria-hidden": "true" });
  shell.appendChild(keyHud);

  const pause = (token, msec) => new Promise((resolve) => {
    if (token.cancelled) return resolve();
    setTimeout(resolve, msec);
  });
  function typingDelay(ch) {
    if (ch === "\n") return 150;
    if (ch === " ") return 70 + Math.random() * 40;
    return 38 + Math.random() * 44;
  }
  async function typeText(token, text, suggestEach) {
    for (const ch of text) {
      if (token.cancelled) return;
      input.setRangeText(ch, input.selectionStart, input.selectionEnd, "end");
      markDirty();
      render();
      ensureVisible(input.selectionEnd);
      if (suggestEach) updateSuggestions();
      else hideSuggestions();
      await pause(token, typingDelay(ch));
    }
  }
  function showKeys(keys) {
    keyHud.replaceChildren(...keys.map((k) => el("kbd", null, k)));
    keyHud.classList.add("is-on");
    setTimeout(() => keyHud.classList.remove("is-on"), 1300);
  }

  async function runAutoplay() {
    if (auto) return;
    const token = { cancelled: false };
    auto = token;
    if (replayBtn) replayBtn.hidden = true;
    fake = true;
    np.classList.add("is-typing");
    input.setSelectionRange(input.value.length, input.value.length);
    render();
    loadEngine().catch(() => {});
    await pause(token, 500);
    for (const [op, arg, flag] of SCRIPT) {
      if (token.cancelled) return;
      if (op === "type") await typeText(token, arg, flag);
      else if (op === "suggest") updateSuggestions();
      else if (op === "wait") await pause(token, arg);
      else if (op === "down") moveSuggestion(1);
      else if (op === "accept") acceptSuggestion();
      else if (op === "selectStar") {
        const star = input.value.indexOf("SELECT *\nFROM teams t") + 7;
        hideSuggestions();
        input.setSelectionRange(star, star + 1);
        input.scrollLeft = 0;
        render();
      } else if (op === "runStatement") {
        hideSuggestions();
        showKeys(["Ctrl", "Enter"]);
        np.classList.remove("is-typing");
        await runStatement();
      }
    }
    if (token.cancelled) return;
    auto = null;
    fake = false;
    np.classList.remove("is-typing");
    render();
    yourTurn();
  }

  // The visitor touched the demo: stop typing and finish the query at once.
  function stopAutoplay() {
    interacted = true;
    if (!auto) return;
    auto.cancelled = true;
    auto = null;
    fake = false;
    np.classList.remove("is-typing");
    hideSuggestions();
    if (cur === sessions[0] && input.value !== QUERY_A && input.value !== FINAL_TEXT) {
      input.value = FINAL_TEXT;
      input.setSelectionRange(FINAL_TEXT.length, FINAL_TEXT.length);
      input.scrollLeft = 0;
    }
    render();
    yourTurn();
  }
  shell.addEventListener("pointerdown", stopAutoplay, true);
  shell.addEventListener("keydown", stopAutoplay, true);
  shell.addEventListener("focusin", stopAutoplay, true);

  function yourTurn() {
    if (!statusEl) return;
    const touch = matchMedia("(pointer: coarse)").matches;
    statusEl.innerHTML =
      '<strong class="your-turn">Your turn.</strong> ' +
      (touch
        ? "Edit the query and tap Run. Tap the plug icon to reconnect or the tab icon for a new tab. "
        : "Edit the query and press <kbd>F5</kbd>, or <kbd>Ctrl</kbd>+<kbd>Enter</kbd> for just the statement under the caret. Try <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>P</kbd>, <kbd>F1</kbd>, or right-click a table. ") +
      "The database is SQLite, running in this page.";
    if (replayBtn) replayBtn.hidden = false;
  }

  function firstSession() {
    const s = newSession();
    s.connected = true;
    s.title = "baseball1";
    s.dirty = true;
    s.text = QUERY_A;
    s.selA = s.selB = QUERY_A.length;
    s.sets = [INITIAL_RESULT];
    s.view = "set:0";
    s.openTables = new Set(["teams"]);
    s.timing = "1 ms";
    s.status = ["5 rows  ·  1 ms", "ok"];
    const now = Date.now();
    const at = (sec) => clock(new Date(now - sec * 1000));
    s.log = [
      [at(4), "Ready.", "info"],
      [at(3), `Connecting to ${DB_FILE}…`, "busy"],
      [at(3), `Connected to ${DB_FILE} · 3 tables loaded.`, "ok"],
      [at(1), "5 rows  ·  1 ms", "ok"],
    ];
    return s;
  }

  function resetDemo() {
    if (auto) {
      auto.cancelled = true;
      auto = null;
    }
    fake = false;
    hideSuggestions();
    closeMenu(false);
    closePalette(false);
    if (!modalEl.hidden) {
      modalEl.hidden = true;
      modalEl.replaceChildren();
    }
    sessions.length = 0;
    tabCounter = 0;
    cur = null;
    const s = firstSession();
    sessions.push(s);
    cur = s;
    loadCurrent();
    renderTabs();
    input.scrollTop = 0;
    input.scrollLeft = 0;
    if (SQL) freshDb();
  }

  if (replayBtn) replayBtn.addEventListener("click", () => {
    resetDemo();
    runAutoplay();
  });

  /* ---------- start ---------- */

  function start() {
    np.classList.add("is-live");
    const os = document.documentElement.dataset.os;
    setOS(os === "windows" ? "windows" : os === "mac" || os === "ios" ? "mac" : "linux");
    resetDemo();
    measure();
    render();
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { measure(); render(); });
    window.addEventListener("resize", () => {
      measure();
      hideSuggestions();
      render();
    });

    // The engine is 659 KB of WebAssembly, so fetch it only once the demo is
    // about to be used: when the autoplay starts, or when the visitor points
    // at or focuses the window. A phone that never scrolls here never pays.
    const warm = () => loadEngine().catch(() => {});
    shell.addEventListener("pointerenter", warm, { once: true });
    shell.addEventListener("focusin", warm, { once: true });
    shell.addEventListener("touchstart", warm, { once: true, passive: true });

    if (!motionOK) {
      if (replayBtn) {
        replayBtn.querySelector("span").textContent = "Play the demo";
        replayBtn.hidden = false;
      }
      return;
    }
    const begin = () => {
      if (interacted || auto) return;
      if (document.hidden) {
        document.addEventListener("visibilitychange", begin, { once: true });
        return;
      }
      setTimeout(() => {
        if (!interacted) runAutoplay();
      }, 600);
    };
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        io.disconnect();
        begin();
      }
    }, { threshold: 0.4 });
    io.observe($(".np-editor"));
  }

  window.notepadbDemo = {
    setOS,
    replay: () => {
      resetDemo();
      runAutoplay();
    },
    run: (sql) => {
      stopAutoplay();
      appendAndSelect(sql);
      runQuery();
    },
    openPalette,
    showShortcuts,
  };

  start();
})();
