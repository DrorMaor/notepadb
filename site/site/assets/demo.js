/*
  notepaDB demo: a browser rebuild of the desktop app's main window.

  Statement splitting and autocomplete are ported from notepadb/app.py
  (split_statements) and notepadb/autocomplete.py. Queries run on SQLite
  compiled to WebAssembly (sql.js), seeded with the repo's baseball sample.
  Each tab is its own session, like QueryTab in the app.
*/
(() => {
  "use strict";

  const shell = document.getElementById("np-shell");
  const np = document.getElementById("np");
  if (!np || !shell) return;

  const q = (sel) => np.querySelector(sel);
  const input = q("#np-input");
  const hl = q("#np-hl");
  const gutter = q("#np-gutter");
  const acList = q("#np-ac");
  const codeBox = q(".np-code");
  const treeEl = q("#np-tree");
  const filterEl = q("#np-filter");
  const rtabsEl = q("#np-rtabs");
  const gridEl = q("#np-gridwrap");
  const logEl = q("#np-log");
  const menuEl = q("#np-menu");
  const modalEl = q("#np-modal");
  const menubar = document.getElementById("np-menubar"); // on macOS it lives in the global menu bar, outside #np
  const toolbar = q(".np-toolbar");
  const tabsEl = q("#np-tabs");
  const connEl = q("#np-conn");
  const runBtn = q('[data-act="run"]');
  const globalbar = document.getElementById("np-globalbar");
  const osSwitch = document.getElementById("np-os");
  const statusEl = document.getElementById("demo-status");
  const replayBtn = document.getElementById("demo-replay");

  const VENDOR = "/assets/vendor/sqljs-1.14.2/";
  const DB_FILE = "baseball.db";
  const HISTORY_LIMIT = 300;
  const GRID_ROW_LIMIT = 2000;
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

  const ENGINES = [
    ["sqlserver", "SQL Server"],
    ["postgres", "PostgreSQL"],
    ["mysql", "MySQL / MariaDB"],
    ["oracle", "Oracle"],
    ["sqlite", "SQLite"],
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
  const ms = (x) => Math.round(x).toString();
  const isFocused = () => document.activeElement === input;

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

  function iconSvg(name, cls) {
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
  // The app ends a FROM/JOIN clause only at another keyword or the very end
  // of the text. Ending it at a line break, ";" or a join keyword as well
  // lets "FROM teams t" on its own line register the alias t.
  const CLAUSE_RE = /(FROM|JOIN)\s+([^\n;]+?)(?=\b(?:FROM|JOIN|WHERE|GROUP|ORDER|ON|LEFT|RIGHT|INNER|FULL|OUTER|CROSS|HAVING|LIMIT|UNION)\b|\n|;|$)/gi;
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

    // The app tests the text including the partial word, so table names only
    // came up right after "FROM ". Testing the text before the word keeps
    // them coming while you type "FROM te".
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

  /* ---------- app.py: split_statements, ported ---------- */

  const STRING_RE = /'(?:[^'\\]|\\.)*'/y;

  function splitStatements(sql) {
    const statements = [];
    let buf = "";
    let i = 0;
    const n = sql.length;
    while (i < n) {
      const ch = sql[i];
      if (ch === "'") {
        STRING_RE.lastIndex = i;
        const m = STRING_RE.exec(sql);
        if (m) {
          buf += m[0];
          i += m[0].length;
          continue;
        }
      }
      if (sql.startsWith("--", i)) {
        let end = sql.indexOf("\n", i);
        if (end === -1) end = n;
        buf += sql.slice(i, end);
        i = end;
        continue;
      }
      if (sql.startsWith("/*", i)) {
        let end = sql.indexOf("*/", i);
        end = end === -1 ? n : end + 2;
        buf += sql.slice(i, end);
        i = end;
        continue;
      }
      if (ch === ";") {
        const stmt = buf.trim();
        if (stmt) statements.push(stmt);
        buf = "";
        i += 1;
        continue;
      }
      buf += ch;
      i += 1;
    }
    const tail = buf.trim();
    if (tail) statements.push(tail);
    return statements;
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

  // _find_bracket_match: a depth count that, like the app, ignores strings.
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

  /* ---------- sessions: one per tab, like QueryTab ---------- */

  let tabCounter = 0;
  const sessions = [];
  let cur = null;

  function newSession() {
    tabCounter += 1;
    return {
      index: tabCounter,
      title: `Query ${tabCounter}`,
      autoTitled: false,
      connected: false,
      text: "",
      selA: 0,
      selB: 0,
      scrollTop: 0,
      scrollLeft: 0,
      sets: [],
      activeSet: -1,
      log: "",
      dirty: false,
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
    cur.log = logEl.textContent;
  }

  function loadCurrent() {
    input.value = cur.text;
    input.setSelectionRange(cur.selA, cur.selB);
    logEl.textContent = cur.log;
    logEl.scrollTop = logEl.scrollHeight;
    hideSuggestions();
    renderConn();
    renderTree();
    renderResults();
    render();
    input.scrollTop = cur.scrollTop;
    input.scrollLeft = cur.scrollLeft;
    syncScroll();
  }

  function renderTabs() {
    tabsEl.replaceChildren(...sessions.map((s) => {
      const b = el("button", {
        class: "np-tab",
        type: "button",
        role: "tab",
        "aria-selected": s === cur ? "true" : "false",
        tabindex: s === cur ? "0" : "-1",
      }, s.title + (s.dirty ? "*" : ""));
      b.addEventListener("click", () => switchTo(s));
      b.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        openMenuAt([["Close Tab", "", () => closeTab(s)]], e.clientX, e.clientY, false, b);
      });
      return b;
    }));
  }

  function switchTo(s) {
    if (s === cur) return;
    saveCurrent();
    cur = s;
    loadCurrent();
    renderTabs();
  }

  function addTab() {
    const s = newSession();
    s.log = `[${clock()}] Ready.`;
    sessions.push(s);
    switchTo(s);
    return s;
  }

  // NotepaDBApp.close_tab: the last tab is reset instead of closed.
  function closeTab(s) {
    if (sessions.length === 1) {
      s.connected = false;
      s.text = "";
      s.selA = s.selB = 0;
      s.sets = [];
      s.activeSet = -1;
      s.title = `Query ${s.index}`;
      s.autoTitled = false;
      s.dirty = false;
      if (s === cur) {
        cur.log = logEl.textContent;
        loadCurrent();
      }
      log("Tab reset.");
      renderTabs();
      return;
    }
    const i = sessions.indexOf(s);
    sessions.splice(i, 1);
    if (s === cur) {
      cur = null;
      cur = sessions[Math.min(i, sessions.length - 1)];
      loadCurrent();
    }
    renderTabs();
  }

  // QueryTab._auto_title: name the tab after the database, like SQLQuery1.
  function autoTitle(s, base) {
    const taken = new Set(sessions.filter((t) => t !== s).map((t) => t.title));
    let n = 1;
    while (taken.has(`${base}${n}`)) n += 1;
    s.title = `${base}${n}`;
    s.autoTitled = true;
  }

  function markDirty() {
    if (cur && !cur.dirty) {
      cur.dirty = true;
      renderTabs();
    }
  }

  function renderConn() {
    const on = cur && cur.connected;
    connEl.textContent = on ? `Connected: ${DB_FILE} (SQLite)` : "Not connected";
    connEl.classList.toggle("is-off", !on);
  }

  /* ---------- editor ---------- */

  let fake = false; // the autoplay owns the caret, so draw caret and selection ourselves
  let suppressSuggest = false;
  let composing = false;
  let escArmed = false;
  let charW = 7.6;
  let lastLineCount = 0;

  function caretPos() {
    return input.selectionDirection === "backward" ? input.selectionStart : input.selectionEnd;
  }

  function render() {
    const text = input.value;
    const cls = tokenClasses(text);
    const caret = caretPos();
    const pair = fake || isFocused() ? bracketPair(text, caret) : null;
    const selA = fake ? input.selectionStart : 0;
    const selB = fake ? input.selectionEnd : 0;
    const caretAt = fake && selA === selB ? caret : -1;

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
    // the inner block carries the overlay's extra scroll room (see .hl-in)
    hl.innerHTML = `<span class="hl-in">${html}\n</span>`;

    const lines = text.split("\n").length;
    if (lines !== lastLineCount) {
      lastLineCount = lines;
      let g = "";
      for (let i = 1; i <= lines; i++) g += i + "\n";
      gutter.textContent = g;
    }
    syncScroll();
  }

  function syncScroll() {
    hl.scrollTop = input.scrollTop;
    hl.scrollLeft = input.scrollLeft;
    gutter.scrollTop = input.scrollTop;
  }

  function measure() {
    const probe = el("span", { class: "np-probe", "aria-hidden": "true" }, "0".repeat(80));
    probe.style.font = getComputedStyle(input).font;
    codeBox.appendChild(probe);
    const w = probe.getBoundingClientRect().width / 80;
    probe.remove();
    if (w > 0) charW = w;
  }

  function caretXY(pos) {
    const before = input.value.slice(0, pos);
    const lines = before.split("\n");
    let col = 0;
    for (const ch of lines[lines.length - 1]) col = ch === "\t" ? col + 4 - (col % 4) : col + 1;
    const cs = getComputedStyle(input);
    const lh = parseFloat(cs.lineHeight) || 20;
    return {
      x: parseFloat(cs.paddingLeft) + col * charW - input.scrollLeft,
      top: parseFloat(cs.paddingTop) + (lines.length - 1) * lh - input.scrollTop,
      lh,
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
  function replaceRange(start, end, s) {
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
    suppressSuggest = false;
    markDirty();
    render();
    ensureVisible(caretPos());
  }

  function currentIndent() {
    const v = input.value;
    const lineStart = v.lastIndexOf("\n", input.selectionStart - 1) + 1;
    return v.slice(lineStart).match(/^[ \t]*/)[0];
  }

  function selectedOrAll() {
    const a = input.selectionStart, b = input.selectionEnd;
    return a !== b ? input.value.slice(a, b) : input.value;
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

  /* ---------- autocomplete popup ---------- */

  let ac = { items: [], index: 0, start: 0 };

  const acVisible = () => !acList.hidden;

  function hideSuggestions() {
    if (acList.hidden) return;
    acList.hidden = true;
    input.removeAttribute("aria-activedescendant");
  }

  function updateSuggestions() {
    const sch = sessionSchema();
    if (!sch || input.selectionStart !== input.selectionEnd) return hideSuggestions();
    const pos = input.selectionEnd;
    const [start, , items] = getSuggestions(input.value, pos, sch);
    if (!items.length) return hideSuggestions();
    ac = { items, index: 0, start };
    acList.replaceChildren(...items.map((s, i) => el("li", { id: `np-ac-${i}`, role: "option", "aria-selected": i === 0 ? "true" : "false" }, s)));
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
    acList.style.left = `${Math.max(2, Math.min(x - 2, maxLeft))}px`;
    acList.style.top = `${top + lh + 2}px`;
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

  /* ---------- editor events ---------- */

  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-controls", "np-ac");

  input.addEventListener("keydown", (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (e.key === "F5" || (mod && e.key === "Enter")) {
      e.preventDefault();
      hideSuggestions();
      runQuery();
      return;
    }
    if (mod && !e.shiftKey && (e.key === "l" || e.key === "L")) {
      e.preventDefault();
      explainQuery();
      return;
    }
    if (mod && !e.shiftKey && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      openConnect();
      return;
    }
    if (mod && e.shiftKey && (e.key === "h" || e.key === "H")) {
      e.preventDefault();
      openHistory();
      return;
    }
    if (acVisible()) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        moveSuggestion(e.key === "ArrowDown" ? 1 : -1);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
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
    if (e.key === "Escape") {
      escArmed = true;
      return;
    }
    if (e.key === "Tab" && !mod && !e.altKey) {
      // Esc, then Tab, leaves the editor; a plain Tab indents like the app.
      if (escArmed || e.shiftKey) {
        escArmed = false;
        return;
      }
      e.preventDefault();
      replaceRange(input.selectionStart, input.selectionEnd, "    ");
      return;
    }
    if (e.key === "Enter" && !mod && !e.shiftKey && !e.altKey && !e.isComposing) {
      e.preventDefault();
      replaceRange(input.selectionStart, input.selectionEnd, "\n" + currentIndent());
      return;
    }
    escArmed = false;
  });

  input.addEventListener("input", () => {
    markDirty();
    render();
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
  input.addEventListener("scroll", () => {
    syncScroll();
    hideSuggestions();
  });
  input.addEventListener("focus", () => render());
  input.addEventListener("blur", () => {
    hideSuggestions();
    render();
  });

  /* ---------- schema browser ---------- */

  const openTables = new Set(["teams"]);
  let dbOpen = true;
  let selectedKey = null;

  function treeRow(depth, kind, label, key, opts = {}) {
    const row = el("div", {
      class: `np-row d${depth}` + (key === selectedKey ? " is-selected" : ""),
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
      title: opts.title || label,
    });
    const tw = el("span", { class: "tw" });
    if (opts.expanded !== undefined) tw.appendChild(iconSvg("i-chev"));
    row.appendChild(tw);
    row.appendChild(document.createTextNode(label));
    if (opts.type) {
      row.appendChild(document.createTextNode("  "));
      row.appendChild(el("span", { class: "ty" }, opts.type));
    }
    return row;
  }

  function renderTree() {
    const sch = sessionSchema();
    if (!sch) {
      treeEl.replaceChildren();
      return;
    }
    const filt = filterEl.value.trim().toLowerCase();
    const rootLi = el("li", { role: "none" });
    rootLi.appendChild(treeRow(0, "database", `${DB_FILE}  (active)`, "db", { expanded: dbOpen, label: DB_FILE }));
    if (dbOpen) {
      const group = el("ul", { role: "group" });
      for (const table of Object.keys(sch).sort(byCode)) {
        const cols = sch[table];
        if (filt && !table.toLowerCase().includes(filt) && !cols.some(([name]) => name.toLowerCase().includes(filt))) continue;
        const open = filt ? true : openTables.has(table);
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
    if (row.dataset.kind === "database") dbOpen = !dbOpen;
    else if (row.dataset.kind === "table") {
      const t = row.dataset.name;
      if (openTables.has(t)) openTables.delete(t);
      else openTables.add(t);
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
  treeEl.addEventListener("dblclick", (e) => {
    const row = e.target.closest(".np-row");
    if (row) toggleRow(row);
  });
  treeEl.addEventListener("dragstart", (e) => {
    const row = e.target.closest(".np-row");
    if (!row || !row.dataset.name) return;
    e.dataTransfer.setData("text/plain", row.dataset.name);
    e.dataTransfer.effectAllowed = "copy";
  });

  // Type-to-jump, like QueryTab._on_tree_keypress: letters typed within
  // 0.8 s build a prefix; table names match without their schema part.
  let searchBuf = "";
  let searchAt = 0;
  treeEl.addEventListener("keydown", (e) => {
    const rows = visibleRows();
    const row = e.target.closest(".np-row");
    if (!row) return;
    const i = rows.indexOf(row);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))];
      selectRow(next, true);
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
      items = [[`Use '${DB_FILE}'`, "", () => {}]];
    } else if (row.dataset.kind === "table") {
      const t = row.dataset.name;
      items = [
        ["Select Top 1000 Rows", "", () => selectTopRows(t)],
        ["Script as CREATE TABLE", "", () => scriptTable(t)],
        ["Copy Column List", "", () => copyColumns(t)],
        "-",
        ["Export Table to CSV...", "", () => exportTable(t)],
      ];
    }
    if (items) openMenuAt(items, x, y, viaKeyboard, row);
  }

  /* ---------- table actions ---------- */

  const quoteIdent = (name) => `"${name}"`;

  // The app opens these in a new tab; the demo stays in the current one.
  function selectTopRows(table) {
    appendAndSelect(`SELECT * FROM ${quoteIdent(table)} LIMIT 1000`);
    runQuery();
  }

  function scriptTable(table) {
    const cols = schema[table];
    if (!cols) return;
    const lines = cols.map(([name, type]) => `    ${quoteIdent(name)} ${type}`).join(",\n");
    appendAndSelect(`CREATE TABLE ${quoteIdent(table)} (\n${lines}\n);`);
  }

  async function copyColumns(table) {
    const cols = schema[table];
    if (!cols) return;
    const text = cols.map((c) => c[0]).join(", ");
    try {
      await navigator.clipboard.writeText(text);
      log(`Copied ${cols.length} column name(s) to clipboard.`);
    } catch (_) {
      log("Copy failed: this browser blocked clipboard access.");
    }
  }

  async function exportTable(table) {
    log(`Exporting ${table}...`);
    let database;
    try {
      database = await loadEngine();
    } catch (err) {
      log("Export failed.");
      engineFailed(err);
      return;
    }
    try {
      const [columns, rows] = execute(database, `SELECT * FROM ${quoteIdent(table)} LIMIT 100000`);
      // Raw values, as csv.writer writes them: NULL becomes an empty field.
      download(`${table}.csv`, toCsv([columns, ...rows]));
      log(`Exported ${rows.length} row(s) to ${table}.csv`);
    } catch (err) {
      log("Export failed.");
      messageBox("Export failed", err.message, { icon: "i-x" });
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

  // SqliteBackend.execute: (columns, rows) for a result set, else (None, rowcount).
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

  function log(text) {
    logEl.textContent += (logEl.textContent ? "\n" : "") + `[${clock()}] ${text}`;
    logEl.scrollTop = logEl.scrollHeight;
  }

  function setRunning(on) {
    running = on;
    runBtn.disabled = on;
  }

  function engineFailed(err) {
    log(`Error: ${err.message}`);
    messageBox("Query error", "This browser couldn't start the in-page SQLite engine, so the demo can't run queries here. The desktop app doesn't depend on it.", { icon: "i-x" });
  }

  function requireConnection() {
    if (cur.connected) return true;
    messageBox("Not connected", "Connect to a database first.", { icon: "i-i" });
    return false;
  }

  function runQuery() {
    if (!requireConnection()) return Promise.resolve();
    return executeSqlText(selectedOrAll(), false);
  }

  function explainQuery() {
    if (!requireConnection()) return;
    const statements = splitStatements(selectedOrAll());
    if (!statements.length) {
      log("Nothing to explain.");
      return;
    }
    if (statements.length > 1) {
      messageBox("Explain Plan", "Select (or write) just one statement to explain.", { icon: "i-i" });
      return;
    }
    executeSqlText(`EXPLAIN QUERY PLAN ${statements[0]}`, true);
  }

  async function executeSqlText(sqlText, isExplain) {
    if (running) {
      log("A query is already running.");
      return;
    }
    const statements = splitStatements(sqlText);
    if (!statements.length) {
      log("Nothing to run.");
      return;
    }
    const owner = cur;
    log("Running query...");
    setRunning(true);
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
    if (owner !== cur) switchTo(owner);
    showBatchResults(results, elapsed, !isExplain);
  }

  // QueryTab._show_batch_results
  function showBatchResults(results, elapsed, record) {
    clearResults();
    let resultCount = 0;
    let totalRows = 0;
    let hadError = false;
    for (const [stmt, columns, rows, err] of results) {
      if (err) {
        hadError = true;
        log(`Error: ${err.message} — ${snippet(stmt)}`);
        continue;
      }
      if (columns === null) {
        log(`OK. ${rows} row(s) affected. — ${snippet(stmt)}`);
        if (record) recordHistory(stmt, elapsed, rows);
        continue;
      }
      resultCount += 1;
      totalRows += rows.length;
      cur.sets.push({ columns, rows });
      if (record) recordHistory(stmt, elapsed, rows.length);
    }
    cur.activeSet = resultCount ? 0 : -1;
    renderResults();

    if (hadError && results.length === 1) {
      messageBox("Query error", results[0][3].message, { icon: "i-x" });
    }
    if (hadError) log(`Completed with error(s) after ${ms(elapsed)} ms.`);
    else if (resultCount) log(`${totalRows} row(s) total across ${resultCount} result set(s) (${ms(elapsed)} ms).`);
    else log(`OK (${ms(elapsed)} ms).`);
  }

  function recordHistory(sql, elapsed, rows) {
    history.unshift({ time: stamp(), sql, elapsed_ms: Math.round(elapsed), rows, connection: DB_FILE });
    history.length = Math.min(history.length, HISTORY_LIMIT);
  }

  /* ---------- results grid ---------- */

  function clearResults() {
    cur.sets = [];
    cur.activeSet = -1;
    rtabsEl.replaceChildren();
    gridEl.replaceChildren();
  }

  function renderResults() {
    if (!cur.sets.length) {
      rtabsEl.replaceChildren(el("button", { class: "np-rtab", type: "button", role: "tab", "aria-selected": "true" }, "Results"));
      gridEl.replaceChildren(el("div", { class: "np-empty" }, "(no results yet)"));
      return;
    }
    rtabsEl.replaceChildren(...cur.sets.map((set, i) => {
      const shown = Math.min(set.rows.length, GRID_ROW_LIMIT);
      const suffix = set.rows.length > GRID_ROW_LIMIT ? `${shown}+` : String(shown);
      const btn = el("button", { class: "np-rtab", type: "button", role: "tab", "aria-selected": "false" }, `Result ${i + 1}  (${suffix} rows)`);
      btn.addEventListener("click", () => showResult(i));
      return btn;
    }));
    showResult(Math.max(0, cur.activeSet));
  }

  function display(v) {
    if (v instanceof Uint8Array) return `<${v.length} bytes>`;
    return String(v);
  }

  function showResult(index) {
    cur.activeSet = index;
    [...rtabsEl.children].forEach((b, i) => b.setAttribute("aria-selected", i === index ? "true" : "false"));
    const { columns, rows } = cur.sets[index];
    const table = el("table", { class: "np-grid" });
    const head = table.createTHead().insertRow();
    const corner = el("th", { class: "ix", scope: "col" });
    corner.appendChild(el("span", { class: "sr-only" }, "Row"));
    head.appendChild(corner);
    for (const c of columns) head.appendChild(el("th", { scope: "col" }, c));
    const body = table.createTBody();
    rows.slice(0, GRID_ROW_LIMIT).forEach((row, r) => {
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

  gridEl.addEventListener("click", (e) => {
    const td = e.target.closest("td");
    if (!td) return;
    const prev = gridEl.querySelector("td.cur");
    if (prev) prev.classList.remove("cur");
    td.classList.add("cur");
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

  function exportResults() {
    const set = cur.sets[cur.activeSet];
    if (!set) {
      messageBox("Nothing to export", "Run a query that returns rows first.", { icon: "i-i" });
      return;
    }
    // The grid's own text, as the app exports it: NULL cells as "NULL".
    const rows = set.rows.slice(0, GRID_ROW_LIMIT).map((r) => r.map((v) => (v === null ? "NULL" : display(v))));
    download(`${cur.title}.csv`, toCsv([set.columns, ...rows]));
    log(`Exported ${rows.length} row(s) to ${cur.title}.csv`);
  }

  /* ---------- connection ---------- */

  async function refreshSchema() {
    if (!requireConnection()) return;
    log("Refreshing schema...");
    let database;
    try {
      database = await loadEngine();
    } catch (err) {
      log("Schema refresh failed.");
      engineFailed(err);
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
    log("Schema refreshed. 1 database(s).");
  }

  function disconnect() {
    if (!cur.connected) return;
    cur.connected = false;
    hideSuggestions();
    renderConn();
    renderTree();
    log("Disconnected.");
  }

  // ConnectDialog: the same fields as the app, which hides the network
  // fields for SQLite and swaps "Server" for a file path.
  function buildConnectForm(engine, values, interactive) {
    const form = el("div", { class: "np-connect", "data-engine": engine });
    const grid = el("div", { class: "np-cgrid" });
    const field = (labelText, control, cls) => {
      const lab = el("label", { class: "np-clabel" + (cls ? ` ${cls}` : "") }, labelText);
      const box = el("div", { class: "np-cfield" + (cls ? ` ${cls}` : "") });
      box.appendChild(control);
      if (control.id) lab.setAttribute("for", control.id);
      grid.append(lab, box);
      return box;
    };
    // The page's copy of the dialog sits in an inert container, so it can
    // look live; only the demo's own dialog takes input.
    const uid = interactive ? "np-c" : `np-m-${engine}`;
    const off = null;

    const engineSel = el("select", { id: `${uid}-engine`, disabled: off });
    for (const [key, name] of ENGINES) {
      const o = el("option", { value: key }, name);
      if (key === engine) o.selected = true;
      engineSel.appendChild(o);
    }
    field("Engine:", engineSel);

    const savedRow = el("div", { class: "np-crow" });
    savedRow.append(
      el("select", { id: `${uid}-saved`, disabled: interactive ? "" : null }),
      el("button", { class: "np-btn", type: "button", disabled: interactive ? "" : null }, "Save As..."),
      el("button", { class: "np-btn", type: "button", disabled: interactive ? "" : null }, "Delete"),
    );
    field("Saved Connection:", savedRow);

    const isLite = engine === "sqlite";
    const serverRow = el("div", { class: "np-crow" });
    const server = el("input", { id: `${uid}-server`, type: "text", value: values.server || "", disabled: off, spellcheck: "false", autocomplete: "off" });
    serverRow.appendChild(server);
    if (isLite) serverRow.appendChild(el("button", { class: "np-btn", type: "button", disabled: interactive ? "" : null, title: "Opens a file picker in the desktop app" }, "Browse..."));
    field(isLite ? "File path:" : "Server:", serverRow);

    if (!isLite) {
      field("Port (optional):", el("input", { id: `${uid}-port`, type: "text", value: values.port || "", disabled: off, autocomplete: "off" }));
      field("Database (optional):", el("input", { id: `${uid}-db`, type: "text", value: values.database || "", disabled: off, autocomplete: "off" }));
      const auth = values.auth || "windows";
      const radios = el("div", { class: "np-crow", role: "radiogroup", "aria-label": "Authentication" });
      for (const [val, text] of [["windows", "Windows"], ["sql", "SQL Login"]]) {
        const lab = el("label", { class: "np-check" });
        lab.append(el("input", { type: "radio", name: `${uid}-auth`, value: val, checked: auth === val ? "" : null, disabled: off }), document.createTextNode(text));
        radios.appendChild(lab);
      }
      field("Authentication:", radios);
      const sqlAuth = auth === "sql";
      field("Username:", el("input", { id: `${uid}-user`, type: "text", value: values.user || "", disabled: sqlAuth ? null : "", autocomplete: "off" }));
      field("Password:", el("input", { id: `${uid}-pass`, type: "password", value: values.password || "", disabled: sqlAuth ? null : "", autocomplete: "off" }));
      const opts = el("div", { class: "np-crow" });
      for (const text of ["Encrypt", "Trust server certificate"]) {
        const lab = el("label", { class: "np-check" });
        lab.append(el("input", { type: "checkbox", checked: "", disabled: off }), document.createTextNode(text));
        opts.appendChild(lab);
      }
      field("", opts);
    }
    form.appendChild(grid);
    form.appendChild(el("div", { class: "np-cstatus", role: "status" }));
    return form;
  }

  function connectValues(engine) {
    if (engine === "sqlite") return { server: DB_FILE };
    return { server: "localhost", auth: "windows" };
  }

  function openConnect() {
    let engine = "sqlite";
    const wrap = el("div", { class: "np-connect-host" });
    const mount = () => {
      const form = buildConnectForm(engine, connectValues(engine), true);
      wrap.replaceChildren(form);
      const sel = form.querySelector("select");
      sel.addEventListener("change", () => {
        engine = sel.value;
        mount();
        wrap.querySelector("select").focus();
      });
      form.querySelectorAll('input[type="radio"]').forEach((r) => r.addEventListener("change", () => {
        const sqlAuth = form.querySelector('input[type="radio"]:checked').value === "sql";
        for (const id of ["user", "pass"]) form.querySelector(`#np-c-${id}`).disabled = !sqlAuth;
      }));
    };
    mount();
    messageBox("Connect to database", "", { icon: null, body: wrap, buttons: ["Connect", "Cancel"], wide: true, focusSelector: "select" })
      .then((choice) => {
        if (choice !== "Connect") return;
        const server = (wrap.querySelector("#np-c-server").value || "").trim();
        connectWith(engine, server);
      });
  }

  async function connectWith(engine, server) {
    const name = ENGINES.find((e) => e[0] === engine)[1];
    if (!server) {
      messageBox("Connection failed", engine === "sqlite" ? "File path is required." : "Server is required.", { icon: "i-x" });
      return;
    }
    log(`Connecting to ${server}...`);
    if (engine !== "sqlite" || server.split(/[\\/]/).pop() !== DB_FILE) {
      log("Connection failed.");
      messageBox("Connection failed", engine === "sqlite"
        ? `This demo has one database file, ${DB_FILE}. In the desktop app, Browse... opens any SQLite file.`
        : `The browser demo can only open its own SQLite file. In the desktop app, this connects to your ${name} server.`, { icon: "i-x" });
      return;
    }
    try {
      await loadEngine();
    } catch (err) {
      log("Connection failed.");
      engineFailed(err);
      return;
    }
    cur.connected = true;
    autoTitle(cur, DB_FILE);
    renderTabs();
    renderConn();
    renderTree();
    log(`Connected. ${Object.keys(schema).length} tables loaded.`);
  }

  // A read-only copy of the dialog for the Engines section of the page.
  const engineHost = document.getElementById("engine-dialog");
  const MOCK_VALUES = {
    sqlserver: { server: "prod-sql01", database: "sales", auth: "windows" },
    postgres: { server: "pg01.internal", port: "5432", database: "league", auth: "sql", user: "analyst", password: "secret-pass" },
    mysql: { server: "mysql01", port: "3306", database: "shop", auth: "sql", user: "analyst", password: "secret-pass" },
    oracle: { server: "ora01", port: "1521", database: "ORCLPDB1", auth: "sql", user: "SALES", password: "secret-pass" },
    sqlite: { server: "/home/me/data/app.db" },
  };
  function showEngineMock(engine) {
    if (!engineHost || !MOCK_VALUES[engine]) return;
    const form = buildConnectForm(engine, MOCK_VALUES[engine], false);
    const actions = el("div", { class: "np-dialog-actions" });
    actions.append(el("span", { class: "np-btn is-default" }, "Connect"), el("span", { class: "np-btn" }, "Cancel"));
    engineHost.replaceChildren(form, actions);
  }
  if (engineHost) {
    const tabs = document.querySelectorAll("[data-engine]");
    for (const t of tabs) t.addEventListener("click", () => showEngineMock(t.dataset.engine));
    const selected = document.querySelector('[data-engine][aria-selected="true"]');
    showEngineMock(selected ? selected.dataset.engine : "sqlserver");
    document.addEventListener("tabs:change", (e) => {
      const engine = e.detail && e.detail.tab && e.detail.tab.dataset.engine;
      if (engine) showEngineMock(engine);
    });
  }

  /* ---------- window chrome per OS ---------- */

  const OS_NAMES = { windows: "Windows", mac: "macOS", linux: "Linux" };

  // On macOS, Tk puts the menu bar at the top of the screen, not in the window.
  function setOS(os) {
    if (!OS_NAMES[os]) os = "windows";
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
    if (osSwitch) {
      for (const b of osSwitch.querySelectorAll("[data-os]")) b.setAttribute("aria-pressed", b.dataset.os === os ? "true" : "false");
    }
    closeMenu(false);
  }

  if (osSwitch) {
    osSwitch.addEventListener("click", (e) => {
      const b = e.target.closest("[data-os]");
      if (b) setOS(b.dataset.os);
    });
  }

  /* ---------- menus ---------- */

  const theme = () => window.notepadbTheme;
  const MENUS = {
    File: [["New Tab", "Ctrl+T", () => addTab()], ["Open...", "Ctrl+O"], ["Save", "Ctrl+S"], ["Save As..."], "-", ["Close Tab", "Ctrl+W", () => closeTab(cur)], "-", ["Exit"]],
    Connection: [["Connect...", "Ctrl+K", () => openConnect()], ["Disconnect", "", () => disconnect()], ["Refresh Schema", "", () => refreshSchema()]],
    Find: [["Find...", "Ctrl+F"], ["Find Next", "F3"], ["Find Previous", "Shift+F3"], "-", ["Replace...", "Ctrl+H"], ["Replace All"]],
    Query: [
      ["Run", "F5", () => runQuery()], ["Cancel", "Ctrl+Break"], ["Explain Plan", "Ctrl+L", () => explainQuery()], "-",
      ["Format SQL", "Ctrl+Shift+F"], ["History...", "Ctrl+Shift+H", () => openHistory()], "-",
      ["Export Results to CSV...", "", () => exportResults()],
    ],
    Options: [
      ["Dark Mode", "", () => theme() && theme().toggle(), () => !!(theme() && theme().isDark())],
      ["Show Line Numbers", "", () => { np.classList.toggle("no-lines"); }, () => !np.classList.contains("no-lines")],
      ["Word Wrap", "", null, false],
      ["Restore Tabs on Startup", "", null, false],
    ],
    Help: [["About", "", () => showAbout()]],
  };

  let menuOwner = null; // the menubar button, tab or tree row the open menu belongs to

  function fillMenu(items) {
    menuEl.replaceChildren();
    let disabled = 0;
    for (const item of items) {
      if (item === "-") {
        menuEl.appendChild(el("hr"));
        continue;
      }
      const [label, accel, action, checked] = item;
      const isCheck = checked !== undefined;
      const btn = el("button", {
        type: "button",
        role: isCheck ? "menuitemcheckbox" : "menuitem",
        "aria-checked": isCheck ? String(typeof checked === "function" ? checked() : !!checked) : null,
        "aria-disabled": action ? null : "true",
        tabindex: "-1",
      });
      btn.appendChild(el("span", null, label));
      if (accel) btn.appendChild(el("span", { class: "acc" }, accel));
      if (action) {
        btn.addEventListener("click", (ev) => {
          // Keyboard users go back to where they were; pointer users keep focus
          // where it is, which avoids popping up a phone keyboard.
          closeMenu(ev.detail === 0);
          action();
        });
      } else {
        disabled += 1;
      }
      menuEl.appendChild(btn);
    }
    if (disabled) menuEl.appendChild(el("div", { class: "note" }, "Grayed-out commands work in the desktop app."));
  }

  function placeMenu(x, y) {
    const box = np.getBoundingClientRect();
    menuEl.hidden = false;
    const w = menuEl.offsetWidth, h = menuEl.offsetHeight;
    const left = Math.max(4, Math.min(x - box.left, box.width - w - 4));
    const top = Math.max(4, Math.min(y - box.top, box.height - h - 4));
    menuEl.style.left = `${left}px`;
    menuEl.style.top = `${top}px`;
  }

  function enabledItems() {
    return [...menuEl.querySelectorAll("button:not([aria-disabled])")];
  }

  function openMenu(name, focusFirst) {
    const btn = menubar.querySelector(`[data-menu="${name}"]`);
    closeMenu(false);
    fillMenu(MENUS[name]);
    const r = btn.getBoundingClientRect();
    placeMenu(r.left, r.bottom + 1);
    btn.setAttribute("aria-expanded", "true");
    menuOwner = btn;
    menuEl.setAttribute("aria-label", name);
    if (focusFirst) {
      const first = enabledItems()[0];
      if (first) first.focus();
    }
  }

  function openMenuAt(items, x, y, focusFirst, owner) {
    closeMenu(false);
    fillMenu(items);
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
      if (!items.length) return;
      items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
    } else if (e.key === "Escape") {
      e.preventDefault();
      const owner = menuOwner;
      closeMenu(false);
      if (owner && owner.focus) owner.focus();
    } else if ((e.key === "ArrowRight" || e.key === "ArrowLeft") && ownedByMenubar()) {
      e.preventDefault();
      const btns = [...menubar.querySelectorAll("[data-menu]")];
      const j = btns.indexOf(menuOwner);
      const next = btns[(j + (e.key === "ArrowRight" ? 1 : -1) + btns.length) % btns.length];
      openMenu(next.dataset.menu, true);
    } else if (e.key === "Tab") {
      closeMenu(false);
    }
  });

  document.addEventListener("pointerdown", (e) => {
    if (menuEl.hidden) return;
    if (menuEl.contains(e.target) || (ownedByMenubar() && menubar.contains(e.target))) return;
    closeMenu(false);
  });

  /* ---------- dialogs ---------- */

  function messageBox(title, text, { icon = "i-i", buttons = ["OK"], body = null, wide = false, focusSelector = null } = {}) {
    closeMenu(false);
    return new Promise((resolve) => {
      const prevFocus = document.activeElement;
      const dialog = el("div", { class: "np-dialog" + (wide ? " is-wide" : ""), role: "dialog", "aria-modal": "true", "aria-labelledby": "np-dlg-title" });
      const titleEl = el("div", { class: "np-dialog-title", id: "np-dlg-title" }, title);
      const bodyEl = el("div", { class: "np-dialog-body" });
      if (icon) bodyEl.appendChild(iconSvg(icon, "ico"));
      bodyEl.appendChild(body || el("div", null, text));
      const actions = el("div", { class: "np-dialog-actions" });
      const close = (value) => {
        document.removeEventListener("keydown", onKey, true);
        modalEl.hidden = true;
        modalEl.replaceChildren();
        const back = prevFocus && np.contains(prevFocus) && prevFocus !== document.body ? prevFocus : null;
        if (back) back.focus({ preventScroll: true });
        resolve(value);
      };
      buttons.forEach((label, i) => {
        const b = el("button", { class: "np-btn" + (i === 0 ? " is-default" : ""), type: "button" }, label);
        b.addEventListener("click", () => close(label));
        actions.appendChild(b);
      });
      const onKey = (e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          close(null);
        } else if (e.key === "Enter" && e.target.tagName !== "BUTTON" && e.target.tagName !== "A" && dialog.contains(e.target)) {
          // the app binds Return to the default button
          e.preventDefault();
          close(buttons[0]);
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
      dialog.append(titleEl, bodyEl, actions);
      modalEl.replaceChildren(dialog);
      modalEl.hidden = false;
      document.addEventListener("keydown", onKey, true);
      const first = (focusSelector && dialog.querySelector(focusSelector)) || actions.firstChild;
      first.focus({ preventScroll: true });
    });
  }

  function showAbout() {
    const verEl = document.querySelector("[data-pypi-version]");
    const version = verEl ? verEl.textContent.trim() : "0.1.1";
    const body = el("div");
    body.textContent =
      "notepaDB — a notepad, but only for databases.\n\n" +
      "Type SQL, autocomplete real table/column names, run it.\n\n" +
      "Each tab is an independent session with its own connection.\n\n" +
      "Supported engines: SQL Server, PostgreSQL, MySQL/MariaDB, Oracle, SQLite.\n\n" +
      `Version ${version} — GPL-2.0-only\n\n`;
    body.appendChild(el("a", { href: "https://ko-fi.com/drormaor" }, "Buy me a coffee"));
    messageBox("About notepaDB", "", { icon: null, buttons: ["Close"], body });
  }

  async function openHistory() {
    if (!history.length) {
      messageBox("Query History", "Nothing here yet. Statements you run in this demo show up here, newest first.", { icon: "i-i" });
      return;
    }
    let picked = 0;
    const wrap = el("div", { class: "np-gridwrap" });
    const table = el("table", { class: "np-grid" });
    const head = table.createTHead().insertRow();
    for (const h of ["Time", "ms", "Rows", "Connection", "SQL"]) head.appendChild(el("th", { scope: "col" }, h));
    const body = table.createTBody();
    history.forEach((h, i) => {
      const tr = body.insertRow();
      tr.tabIndex = 0;
      for (const v of [h.time, String(h.elapsed_ms), String(h.rows), h.connection, snippet(h.sql, 120)]) tr.insertCell().textContent = v;
      tr.addEventListener("click", () => {
        picked = i;
        body.querySelectorAll("td.cur").forEach((c) => c.classList.remove("cur"));
        [...tr.cells].forEach((c) => c.classList.add("cur"));
      });
    });
    [...body.rows[0].cells].forEach((c) => c.classList.add("cur"));
    wrap.appendChild(table);
    const choice = await messageBox("Query History", "", { icon: null, wide: true, body: wrap, buttons: ["Insert", "Insert & Run", "Close"] });
    if (choice === "Insert" || choice === "Insert & Run") {
      appendAndSelect(history[picked].sql);
      if (choice === "Insert & Run") runQuery();
    }
  }

  /* ---------- window-level keys and buttons ---------- */

  shell.addEventListener("keydown", (e) => {
    if (e.target === input || modalEl.contains(e.target)) return;
    const mod = e.ctrlKey || e.metaKey;
    if (e.key === "F5" || (mod && e.key === "Enter")) {
      e.preventDefault();
      runQuery();
    } else if (mod && !e.shiftKey && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      openConnect();
    }
  });

  runBtn.addEventListener("click", () => {
    hideSuggestions();
    runQuery();
  });

  /* ---------- autoplay ---------- */

  const QUERY_A = "SELECT * FROM teams;";
  const QUERY_B = "SELECT t.team_name, p.last_name, p.jersey_number\nFROM teams t\nLEFT JOIN players p ON p.team_id = t.team_id;";
  const FINAL_TEXT = `${QUERY_A}\n\n${QUERY_B}`;

  // Each popup below is one the autocomplete would really show at that point.
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
    ["wait", 620],
    ["selectB"], ["wait", 700],
    ["run"],
  ];

  let auto = null;
  let interacted = false;

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

  function pressRun() {
    runBtn.classList.add("is-pressed");
    setTimeout(() => runBtn.classList.remove("is-pressed"), 220);
  }

  async function runAutoplay() {
    if (auto) return;
    const token = { cancelled: false };
    auto = token;
    replayBtn.hidden = true;
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
        render();
      } else if (op === "selectB") {
        hideSuggestions();
        input.setSelectionRange(QUERY_A.length + 2, input.value.length);
        input.scrollLeft = 0; // show the selected query from the start of its lines
        render();
      } else if (op === "run") {
        pressRun();
        np.classList.remove("is-typing");
        await runQuery();
      }
    }
    if (token.cancelled) return;
    auto = null;
    fake = false;
    np.classList.remove("is-typing");
    render();
    yourTurn();
  }

  // The visitor touched the demo: stop typing, finish the query at once.
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
    const treeShown = getComputedStyle(q(".np-schema")).display !== "none";
    statusEl.innerHTML =
      '<strong class="your-turn">Your turn.</strong> ' +
      (touch
        ? "Edit the query and tap Run. "
        : "Edit the query and press <kbd>F5</kbd> or <kbd>Ctrl</kbd>&#8239;<kbd>Enter</kbd>. ") +
      (treeShown ? "Right-click a table, or open a new tab from the File menu. " : "Open a new tab from the File menu. ") +
      "The database is SQLite, running in this page.";
    if (replayBtn) replayBtn.hidden = false;
  }

  function firstSession() {
    const s = newSession();
    s.connected = true;
    autoTitle(s, DB_FILE);
    s.dirty = true;
    s.text = QUERY_A;
    s.selA = s.selB = QUERY_A.length;
    s.sets = [INITIAL_RESULT];
    s.activeSet = 0;
    const now = Date.now();
    const at = (sec) => clock(new Date(now - sec * 1000));
    s.log = [
      `[${at(4)}] Ready.`,
      `[${at(3)}] Connecting to ${DB_FILE}...`,
      `[${at(3)}] Connected. ${Object.keys(schema).length} tables loaded.`,
      `[${at(1)}] Running query...`,
      `[${at(1)}] 5 row(s) total across 1 result set(s) (1 ms).`,
    ].join("\n");
    return s;
  }

  function resetDemo() {
    if (auto) {
      auto.cancelled = true;
      auto = null;
    }
    hideSuggestions();
    closeMenu(false);
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

  if (replayBtn) {
    replayBtn.addEventListener("click", () => {
      resetDemo();
      runAutoplay();
    });
  }

  /* ---------- start ---------- */

  function start() {
    np.classList.add("is-live");
    const os = document.documentElement.dataset.os;
    setOS(os === "linux" || os === "android" ? "linux" : os === "mac" || os === "ios" ? "mac" : "windows");
    resetDemo();
    measure();
    render();
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { measure(); render(); });
    window.addEventListener("resize", () => {
      measure();
      hideSuggestions();
      syncScroll();
    });

    // The engine is 659 KB of WebAssembly, so fetch it only once the demo is
    // about to be used: when the autoplay starts, or when the visitor points
    // at or focuses the window. A phone that never scrolls here never pays.
    const warm = () => loadEngine().catch(() => {});
    shell.addEventListener("pointerenter", warm, { once: true });
    shell.addEventListener("focusin", warm, { once: true });
    shell.addEventListener("touchstart", warm, { once: true, passive: true });

    if (!motionOK) {
      // Reduced motion: no autoplay. Offer it as a button instead.
      if (replayBtn) {
        replayBtn.lastChild.textContent = "Play demo";
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
    // Start once the editor itself is in view, so the typing isn't missed.
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        io.disconnect();
        begin();
      }
    }, { threshold: 0.4 });
    io.observe(q(".np-editor"));
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
  };

  start();
})();
