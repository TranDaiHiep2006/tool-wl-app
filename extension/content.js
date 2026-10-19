// Content script for tool-wl-app DOM bridge.
// PURELY READ-ONLY: extracts DOM text and structure. No clicks, no typing, no
// focus changes, no form submission. The active tab's content is extracted on
// demand by the background script via chrome.runtime.sendMessage.
//
// Two extractors:
//   - getPageText()    : boilerplate-stripped readable text (max ~8000 chars).
//   - getPageStructure(): pruned interactive structure (headings/links/buttons/inputs),
//                        max ~200 elements.
//
// All selectors are tolerant: pages with non-standard markup just yield less data.

(() => {
  "use strict";

  // ---------- helpers ------------------------------------------------------

  // Tags whose visible text is generally NOT part of the article body.
  const BOILERPLATE_TAGS = new Set([
    "SCRIPT", "STYLE", "NOSCRIPT", "IFRAME", "SVG", "CANVAS", "VIDEO",
    "AUDIO", "PICTURE", "SOURCE", "TRACK", "EMBED", "OBJECT", "META",
    "LINK", "TEMPLATE", "FORM",
  ]);

  const SKIP_CLASS_HINTS = [
    "nav", "navbar", "navigation", "menu", "sidebar", "side-bar",
    "footer", "header", "banner", "cookie", "consent", "ads", "ad-",
    "advert", "promo", "promotion", "modal", "popup", "newsletter",
    "subscribe", "social", "share", "breadcrumb", "breadcrumbs",
    "pagination", "pager", "toolbar", "skip", "sr-only",
  ];

  function isBoilerplateElement(el) {
    if (!el || el.nodeType !== 1) return false;
    const tag = el.tagName;
    if (BOILERPLATE_TAGS.has(tag)) return true;
    // role-based heuristic
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (role === "navigation" || role === "banner" || role === "contentinfo" ||
        role === "dialog" || role === "search" || role === "menu") {
      return true;
    }
    const id = (el.id || "").toLowerCase();
    const cls = (el.className && typeof el.className === "string")
      ? el.className.toLowerCase() : "";
    const blob = id + " " + cls;
    for (const hint of SKIP_CLASS_HINTS) {
      // word-boundary-ish match (avoid matching "navigation" inside "innavigation")
      if (blob.indexOf(hint) !== -1) {
        // Allow list items inside main, but skip wrapper containers.
        if (el.tagName === "LI" || el.tagName === "A") continue;
        return true;
      }
    }
    // aria-hidden
    if (el.getAttribute("aria-hidden") === "true") return true;
    return false;
  }

  // ---------- text extraction ----------------------------------------------

  // Walks the DOM, collects visible text nodes, skips boilerplate, collapses
  // whitespace. Truncates at `maxChars`.
  function getPageText(maxChars) {
    maxChars = Math.max(100, Math.min(8000, maxChars | 0 || 8000));
    const root = document.body || document.documentElement;
    if (!root) return { text: "", truncated: false, chars: 0 };

    const parts = [];
    let total = 0;

    function visit(el) {
      if (!el) return;
      if (isBoilerplateElement(el)) return;
      // Walk children; collect text nodes.
      const childNodes = el.childNodes;
      for (let i = 0; i < childNodes.length; i++) {
        const n = childNodes[i];
        if (!n) continue;
        if (n.nodeType === 3 /* text */) {
          const t = n.nodeValue;
          if (!t) continue;
          const cleaned = t.replace(/\s+/g, " ");
          if (!cleaned.trim()) continue;
          parts.push(cleaned);
          total += cleaned.length + 1;
          if (total >= maxChars) return;
        } else if (n.nodeType === 1 /* element */) {
          if (n.tagName === "BR") { parts.push("\n"); continue; }
          if (isBoilerplateElement(n)) continue;
          // Check inline display
          visit(n);
          if (total >= maxChars) return;
        }
      }
    }

    // Inject newlines around block-level tags for readability.
    function walkBlocks(el) {
      if (!el) return;
      if (isBoilerplateElement(el)) return;
      const tag = el.tagName;
      const BLOCKS = new Set([
        "P", "DIV", "SECTION", "ARTICLE", "MAIN", "HEADER", "FOOTER", "NAV",
        "ASIDE", "H1", "H2", "H3", "H4", "H5", "H6", "LI", "UL", "OL",
        "BLOCKQUOTE", "PRE", "HR", "TABLE", "TR", "BR",
      ]);
      if (tag === "BR") {
        parts.push("\n"); total += 1;
        return;
      }
      // Leaf-like inline content
      if (tag === "P" || tag === "LI" || /^H[1-6]$/.test(tag) ||
          tag === "BLOCKQUOTE" || tag === "PRE") {
        // Open block
        parts.push("\n"); total += 1;
      }
      const kids = el.childNodes;
      for (let i = 0; i < kids.length; i++) {
        const n = kids[i];
        if (!n) continue;
        if (n.nodeType === 3) {
          const t = n.nodeValue;
          if (t) {
            const cleaned = t.replace(/[\t\n\r]+/g, " ");
            parts.push(cleaned); total += cleaned.length + 1;
          }
        } else if (n.nodeType === 1) {
          if (n.tagName === "BR") { parts.push("\n"); total += 1; continue; }
          walkBlocks(n);
        }
        if (total >= maxChars) return;
      }
      if (BLOCKS.has(tag)) {
        parts.push("\n"); total += 1;
      }
    }

    walkBlocks(root);

    let text = parts.join("");
    // Collapse 3+ blank lines into one blank line
    text = text.replace(/\n{3,}/g, "\n\n");
    // Trim each line
    text = text.split("\n").map((s) => s.replace(/^\s+|\s+$/g, "")).join("\n");
    text = text.replace(/^\n+|\n+$/g, "");
    if (text.length > maxChars) {
      text = text.slice(0, maxChars) + "…";
      return { text, truncated: true, chars: text.length };
    }
    return { text, truncated: false, chars: text.length };
  }

  // ---------- structure extraction -----------------------------------------

  function getTrim(s, n) {
    s = (s == null ? "" : String(s));
    s = s.replace(/\s+/g, " ").trim();
    if (s.length > n) s = s.slice(0, n - 1) + "…";
    return s;
  }

  function safeHref(href) {
    if (!href) return "";
    // Keep absolute URLs only; drop mailto:/javascript:/data:/
    try {
      const u = new URL(href, location.href);
      if (u.protocol === "javascript:" || u.protocol === "data:" ||
          u.protocol === "blob:" || u.protocol === "file:") return "";
      return u.href;
    } catch (e) { return ""; }
  }

  function findLabel(el) {
    // Find a label for an input/textarea/select.
    if (!el) return "";
    if (el.id) {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab) return getTrim(lab.textContent, 80);
    }
    // Wrapping label
    let p = el.parentElement;
    while (p) {
      if (p.tagName === "LABEL") return getTrim(p.textContent, 80);
      p = p.parentElement;
    }
    if (el.getAttribute("aria-label")) return getTrim(el.getAttribute("aria-label"), 80);
    if (el.getAttribute("placeholder")) return getTrim(el.getAttribute("placeholder"), 80);
    if (el.getAttribute("title")) return getTrim(el.getAttribute("title"), 80);
    return "";
  }

  function getPageStructure(maxItems) {
    maxItems = Math.max(10, Math.min(500, maxItems | 0 || 200));
    const items = [];
    const root = document.body || document.documentElement;
    if (!root) return { items, truncated: false, count: 0 };

    // 1) Headings — most useful for structure.
    const heads = root.querySelectorAll("h1, h2, h3, h4, h5, h6, [role='heading']");
    for (let i = 0; i < heads.length && items.length < maxItems; i++) {
      const el = heads[i];
      const level = parseInt((el.tagName.match(/^H(\d)$/) || [])[1] || el.getAttribute("aria-level") || "0", 10);
      items.push({ kind: "heading", level: level || 0, text: getTrim(el.textContent, 200) });
    }

    // 2) Links — text + href (skip pure #fragment and javascript:).
    const links = root.querySelectorAll("a[href]");
    for (let i = 0; i < links.length && items.length < maxItems; i++) {
      const el = links[i];
      const href = safeHref(el.getAttribute("href"));
      if (!href) continue;
      items.push({ kind: "link", text: getTrim(el.textContent, 120), href: href });
    }

    // 3) Buttons — including [role=button] and input[type=submit|button].
    const buttons = root.querySelectorAll(
      "button, input[type='button'], input[type='submit'], input[type='reset'], [role='button']"
    );
    for (let i = 0; i < buttons.length && items.length < maxItems; i++) {
      const el = buttons[i];
      let text = "";
      if (el.tagName === "INPUT") {
        text = el.value || el.getAttribute("aria-label") || el.name || "";
      } else {
        text = el.textContent || el.getAttribute("aria-label") || el.title || "";
      }
      items.push({ kind: "button", text: getTrim(text, 120) });
    }

    // 4) Inputs (text-like) — text, search, email, tel, url, password, textarea, select.
    const inputs = root.querySelectorAll(
      "input, textarea, select"
    );
    for (let i = 0; i < inputs.length && items.length < maxItems; i++) {
      const el = inputs[i];
      const type = (el.type || el.tagName).toLowerCase();
      // Skip pure submit/reset/hidden (already covered by buttons)
      if (type === "submit" || type === "reset" || type === "hidden" || type === "button") continue;
      items.push({
        kind: "input",
        input_type: type,
        name: el.name || "",
        id: el.id || "",
        label: findLabel(el),
        placeholder: getTrim(el.getAttribute("placeholder") || "", 80),
        value: el.value != null ? getTrim(String(el.value), 80) : "",
      });
    }

    return { items, truncated: items.length >= maxItems, count: items.length };
  }

  // ---------- message bridge -----------------------------------------------

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || !msg.op) {
      sendResponse({ ok: false, error: "missing op" });
      return false;
    }
    try {
      if (msg.op === "get_page_text") {
        const out = getPageText(msg.maxChars);
        sendResponse({ ok: true, data: out });
        return true;
      }
      if (msg.op === "get_page_structure") {
        const out = getPageStructure(msg.maxItems);
        sendResponse({ ok: true, data: out });
        return true;
      }
      if (msg.op === "ping") {
        sendResponse({ ok: true, data: { url: location.href, title: document.title } });
        return true;
      }
      sendResponse({ ok: false, error: "unknown op: " + msg.op });
      return false;
    } catch (e) {
      sendResponse({ ok: false, error: String(e && e.message || e) });
      return false;
    }
  });
})();