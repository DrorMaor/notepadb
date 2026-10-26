/* notepadb.com: theme, OS-aware install, tabs, palette, and live version/release info. */
(() => {
  "use strict";

  const root = document.documentElement;
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const OS = root.dataset.os || "other";

  /* ---------- theme ---------- */

  const THEME_KEY = "notepadb-theme";
  const systemDark = matchMedia("(prefers-color-scheme: dark)");
  const toggleBtn = document.getElementById("theme-toggle");
  const themeMetas = [...document.querySelectorAll('meta[name="theme-color"]')];

  const isDark = () => {
    const t = root.getAttribute("data-theme");
    return t ? t === "dark" : systemDark.matches;
  };

  function syncTheme() {
    const dark = isDark();
    if (toggleBtn) {
      toggleBtn.dataset.dark = String(dark);
      toggleBtn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
    }
    if (root.hasAttribute("data-theme")) {
      for (const m of themeMetas) m.setAttribute("content", dark ? "#0d1016" : "#ffffff");
    }
  }

  function applyTheme(next) {
    root.setAttribute("data-theme", next);
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch (_) {
      /* private mode: the choice lasts for this page view */
    }
    syncTheme();
  }

  // The new theme grows out of the toggle button as a circle.
  function toggleTheme() {
    const next = isDark() ? "light" : "dark";
    if (reduceMotion || !document.startViewTransition) return applyTheme(next);
    const r = toggleBtn ? toggleBtn.getBoundingClientRect() : null;
    const x = r && r.width ? r.left + r.width / 2 : innerWidth / 2;
    const y = r && r.width ? r.top + r.height / 2 : 0;
    root.style.setProperty("--vt-x", `${x}px`);
    root.style.setProperty("--vt-y", `${y}px`);
    root.style.setProperty("--vt-r", `${Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y))}px`);
    document.startViewTransition(() => applyTheme(next));
  }

  if (toggleBtn) toggleBtn.addEventListener("click", toggleTheme);
  if (systemDark.addEventListener) systemDark.addEventListener("change", syncTheme);
  window.notepadbTheme = { isDark, toggle: toggleTheme };
  syncTheme();

  /* ---------- install command for the visitor's OS ---------- */

  // The hero picked the command before first paint (inline script); repeat
  // it for the closing band.
  const cta = window.notepadbCta || { cmd: "pipx install notepadb", note: "" };
  for (const n of document.querySelectorAll("[data-cta-cmd]")) n.textContent = cta.cmd;
  for (const n of document.querySelectorAll("[data-cta-copy]")) n.setAttribute("data-copy", cta.cmd);

  /* ---------- copy buttons ---------- */

  const live = document.createElement("div");
  live.className = "sr-only";
  live.setAttribute("aria-live", "polite");
  document.body.appendChild(live);

  function legacyCopy(text) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.className = "sr-only";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (_) {
      ok = false;
    }
    ta.remove();
    return ok;
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      return legacyCopy(text);
    }
  }

  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-copy]");
    if (!btn) return;
    const ok = await copyText(btn.getAttribute("data-copy"));
    const label = btn.querySelector("span");
    if (label) {
      if (!btn.dataset.label) btn.dataset.label = label.textContent;
      label.textContent = ok ? "Copied" : "Press Ctrl+C";
      btn.dataset.copied = String(ok);
      clearTimeout(btn.copyTimer);
      btn.copyTimer = setTimeout(() => {
        label.textContent = btn.dataset.label;
        btn.dataset.copied = "false";
      }, 1800);
    }
    live.textContent = ok ? "Copied to clipboard" : "Couldn't copy. Select the text and press Ctrl+C.";
  });

  /* ---------- tabs ---------- */

  for (const group of document.querySelectorAll("[data-tabs]")) {
    const list = group.querySelector('[role="tablist"]');
    const tabs = [...list.querySelectorAll(':scope > [role="tab"]')];
    const select = (tab, focus) => {
      for (const t of tabs) {
        const on = t === tab;
        t.setAttribute("aria-selected", String(on));
        t.tabIndex = on ? 0 : -1;
        const panel = document.getElementById(t.getAttribute("aria-controls"));
        if (panel) panel.hidden = !on;
      }
      if (focus) tab.focus();
      document.dispatchEvent(new CustomEvent("tabs:change", { detail: { tab } }));
    };
    group.selectTab = select;
    for (const t of tabs) t.addEventListener("click", () => select(t, false));
    list.addEventListener("keydown", (e) => {
      const i = tabs.indexOf(document.activeElement);
      if (i < 0) return;
      const n = tabs.length;
      const j = { ArrowRight: (i + 1) % n, ArrowLeft: (i - 1 + n) % n, Home: 0, End: n - 1 }[e.key];
      if (j === undefined) return;
      e.preventDefault();
      select(tabs[j], true);
    });
  }

  // Open the install steps for the visitor's OS.
  const osTab = document.querySelector(`[data-os-tab="${OS === "ios" ? "mac" : OS === "android" ? "linux" : OS}"]`);
  if (osTab) osTab.closest("[data-tabs]").selectTab(osTab, false);

  /* ---------- 404 page ---------- */

  for (const node of document.querySelectorAll("[data-404-path]")) node.textContent = location.pathname;
  for (const node of document.querySelectorAll("[data-now]")) node.textContent = new Date().toTimeString().slice(0, 8);

  /* ---------- feature vignettes: move the highlight while on screen ---------- */

  for (const el of document.querySelectorAll(".step, .feature, .shot, .stat")) el.classList.add("reveal");

  if (!reduceMotion && "IntersectionObserver" in window) {
    const advance = (vig) => {
      const lists = [...vig.querySelectorAll("[data-cycle]")];
      let next = null;
      for (const list of lists) {
        const items = [...list.children].filter((c) => c.tagName !== "HR");
        const i = items.findIndex((c) => c.classList.contains("on"));
        if (next === null || !vig.hasAttribute("data-cycle-group")) next = (i + 1) % items.length;
        items.forEach((c, k) => c.classList.toggle("on", k === next));
      }
    };
    const timers = new Map();
    const io = new IntersectionObserver((entries) => {
      for (const { target, isIntersecting } of entries) {
        if (isIntersecting && !timers.has(target)) timers.set(target, setInterval(() => advance(target), 1400));
        if (!isIntersecting && timers.has(target)) {
          clearInterval(timers.get(target));
          timers.delete(target);
        }
      }
    }, { threshold: 0.6 });
    for (const vig of document.querySelectorAll(".vig")) if (vig.querySelector("[data-cycle]")) io.observe(vig);
  }

  /* ---------- command palette ---------- */

  const palette = document.getElementById("palette");
  const pInput = document.getElementById("palette-input");
  const pList = document.getElementById("palette-list");
  const pOpen = document.getElementById("palette-open");

  if (palette && pInput && pList && typeof palette.showModal === "function") {
    const demo = () => window.notepadbDemo;
    const go = (hash) => {
      const target = document.querySelector(hash);
      if (target) target.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
      history.replaceState(null, "", hash);
    };
    const open = (url) => window.open(url, "_blank", "noopener");
    const ITEMS = [
      ["Jump to", "Live demo", "", () => go("#demo")],
      ["Jump to", "How it works", "", () => go("#how")],
      ["Jump to", "Engines", "", () => go("#engines")],
      ["Jump to", "Features", "", () => go("#features")],
      ["Jump to", "Coming from SSMS", "", () => go("#ssms")],
      ["Jump to", "The real app on Linux", "", () => go("#linux")],
      ["Jump to", "Keyboard shortcuts", "", () => go("#keyboard")],
      ["Jump to", "Install", "", () => go("#install")],
      ["Jump to", "Source code", "", () => go("#source")],
      ["Jump to", "Questions", "", () => go("#faq")],
      ["Do", "Copy the install command", () => cta.cmd, async () => {
        const ok = await copyText(cta.cmd);
        live.textContent = ok ? `Copied ${cta.cmd}` : "Couldn't copy";
      }],
      ["Do", () => (isDark() ? "Switch to the light theme" : "Switch to the dark theme"), "", () => toggleTheme()],
      ["Do", "Replay the demo", "", () => { go("#demo"); if (demo()) demo().replay(); }],
      ["Do", "Run SELECT * FROM games in the demo", "", () => { go("#demo"); if (demo()) demo().run("SELECT * FROM games;"); }],
      ["Do", "Show the demo window as Windows", "", () => { go("#demo"); if (demo()) demo().setOS("windows"); }],
      ["Do", "Show the demo window as macOS", "", () => { go("#demo"); if (demo()) demo().setOS("mac"); }],
      ["Do", "Show the demo window as Linux", "", () => { go("#demo"); if (demo()) demo().setOS("linux"); }],
      ["Open", "GitHub repository", "github.com", () => open("https://github.com/DrorMaor/notepadb")],
      ["Open", "PyPI page", "pypi.org", () => open("https://pypi.org/project/notepadb/")],
      ["Open", "Report an issue", "github.com", () => open("https://github.com/DrorMaor/notepadb/issues")],
      ["Open", "Buy me a coffee", "ko-fi.com", () => open("https://ko-fi.com/drormaor")],
    ];
    const text = (v) => (typeof v === "function" ? v() : v);
    let shown = [];
    let active = 0;
    let opener = null;

    function render() {
      const q = pInput.value.trim().toLowerCase();
      shown = ITEMS.filter(([g, label]) => {
        const hay = `${g} ${text(label)}`.toLowerCase();
        return q.split(/\s+/).every((w) => hay.includes(w));
      });
      active = Math.min(active, Math.max(0, shown.length - 1));
      pList.replaceChildren();
      if (!shown.length) {
        const li = document.createElement("li");
        li.className = "empty";
        li.textContent = "Nothing matches. Try “install” or “demo”.";
        pList.appendChild(li);
        pInput.removeAttribute("aria-activedescendant");
        return;
      }
      let group = null;
      shown.forEach(([g, label, hint], i) => {
        if (g !== group) {
          group = g;
          const h = document.createElement("li");
          h.className = "grp";
          h.setAttribute("role", "presentation");
          h.textContent = g;
          pList.appendChild(h);
        }
        const li = document.createElement("li");
        li.id = `pal-${i}`;
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", String(i === active));
        const l = document.createElement("span");
        l.textContent = text(label);
        li.appendChild(l);
        const h = text(hint);
        if (h) {
          const s = document.createElement("span");
          s.className = "hint";
          s.textContent = h;
          li.appendChild(s);
        }
        li.addEventListener("mousemove", () => {
          if (active !== i) {
            active = i;
            mark();
          }
        });
        li.addEventListener("click", () => run(i));
        pList.appendChild(li);
      });
      mark();
    }

    function mark() {
      for (const li of pList.querySelectorAll('[role="option"]')) {
        const on = li.id === `pal-${active}`;
        li.setAttribute("aria-selected", String(on));
        if (on) li.scrollIntoView({ block: "nearest" });
      }
      pInput.setAttribute("aria-activedescendant", `pal-${active}`);
    }

    function run(i) {
      const item = shown[i];
      if (!item) return;
      opener = null;
      palette.close();
      item[3]();
    }

    function openPalette() {
      if (palette.open) return;
      opener = document.activeElement;
      pInput.value = "";
      active = 0;
      render();
      palette.showModal();
      pInput.focus();
    }

    palette.addEventListener("close", () => {
      if (opener && opener.focus && opener.isConnected) opener.focus({ preventScroll: true });
      opener = null;
    });
    palette.addEventListener("click", (e) => {
      if (e.target === palette) palette.close();
    });
    pInput.addEventListener("input", () => {
      active = 0;
      render();
    });
    pInput.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!shown.length) return;
        active = (active + (e.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length;
        mark();
      } else if (e.key === "Enter") {
        e.preventDefault();
        run(active);
      }
    });
    if (pOpen) pOpen.addEventListener("click", openPalette);

    const typing = (el) => el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
    document.addEventListener("keydown", (e) => {
      if (palette.open || e.defaultPrevented) return;
      const inDemo = e.target.closest && e.target.closest("#np-shell");
      if (e.key === "/" && !e.ctrlKey && !e.metaKey && !e.altKey && !typing(e.target)) {
        e.preventDefault();
        openPalette();
      } else if ((e.ctrlKey || e.metaKey) && !e.shiftKey && (e.key === "k" || e.key === "K") && !inDemo) {
        e.preventDefault();
        openPalette();
      }
    });
  }

  /* ---------- live version and Windows release ---------- */

  const HOUR = 3600 * 1000;

  // GitHub allows 60 unauthenticated API calls an hour per IP address, so
  // answers are kept for a while in localStorage.
  function cached(key, maxAge) {
    try {
      const v = JSON.parse(localStorage.getItem(key));
      if (v && Date.now() - v.t < maxAge) return { hit: true, data: v.d };
    } catch (_) {
      /* storage unavailable */
    }
    return { hit: false };
  }

  function remember(key, data) {
    try {
      localStorage.setItem(key, JSON.stringify({ t: Date.now(), d: data }));
    } catch (_) {
      /* storage unavailable */
    }
  }

  async function pypiVersion() {
    const c = cached("notepadb-pypi", HOUR);
    if (c.hit) return c.data;
    const r = await fetch("https://pypi.org/pypi/notepadb/json");
    if (!r.ok) return null;
    const v = (await r.json()).info.version;
    remember("notepadb-pypi", v);
    return v;
  }

  // The list endpoint answers 200 with [] before the first release, where
  // /releases/latest would answer 404 and log a console error.
  async function latestRelease() {
    const c = cached("notepadb-release", 12 * HOUR);
    if (c.hit) return c.data;
    const r = await fetch("https://api.github.com/repos/DrorMaor/notepadb/releases?per_page=10", {
      headers: { Accept: "application/vnd.github+json" },
    });
    if (!r.ok) return null;
    let data = null;
    for (const rel of await r.json()) {
      if (rel.draft || rel.prerelease) continue;
      const exe = (rel.assets || []).find((a) => /\.exe$/i.test(a.name));
      if (exe) {
        data = { tag: rel.tag_name, name: exe.name, url: exe.browser_download_url, size: exe.size };
        break;
      }
    }
    remember("notepadb-release", data);
    return data;
  }

  function showVersion(v) {
    if (!v || !/^[0-9A-Za-z.+-]{1,32}$/.test(v)) return;
    for (const node of document.querySelectorAll("[data-pypi-version]")) node.textContent = v;
  }

  function showRelease(rel) {
    if (!rel || !/^https:\/\/github\.com\//.test(rel.url)) return;
    const size = `${(rel.size / 1048576).toFixed(1)} MB`;
    const hero = document.querySelector("[data-win-download]");
    if (hero && (OS === "windows" || OS === "other")) {
      hero.href = rel.url;
      hero.title = `${rel.name}, ${rel.tag}, ${size}`;
      hero.hidden = false;
    }
    for (const none of document.querySelectorAll("[data-release-none]")) none.hidden = true;
    const link = document.querySelector("[data-release-link]");
    const meta = document.querySelector("[data-release-meta]");
    if (link) {
      link.href = rel.url;
      link.querySelector("[data-release-label]").textContent = `Download ${rel.name}`;
      link.hidden = false;
    }
    if (meta) {
      meta.textContent = `${rel.tag} · ${size}`;
      meta.hidden = false;
    }
  }

  // The .exe only matters on Windows: check for it when the visitor is on
  // Windows, or opens the Windows install steps.
  let releaseChecked = false;
  const checkRelease = () => {
    if (releaseChecked) return;
    releaseChecked = true;
    latestRelease().then(showRelease, () => {});
  };
  const lookUp = () => {
    pypiVersion().then(showVersion, () => {});
    if (OS === "windows" || OS === "other") checkRelease();
  };
  document.addEventListener("tabs:change", (e) => {
    if (e.detail && e.detail.tab && e.detail.tab.dataset.osTab === "windows") checkRelease();
  });
  if (document.querySelector("[data-pypi-version], [data-win-download]")) {
    if ("requestIdleCallback" in window) requestIdleCallback(lookUp, { timeout: 3000 });
    else setTimeout(lookUp, 1200);
  }
})();
