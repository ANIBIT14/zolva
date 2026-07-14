import "./styles.css";

/* Chrome skips the initial anchor jump when scroll-behavior is smooth;
   jump instantly on load so /docs/#section reloads land correctly. */
if (location.hash) {
  document.getElementById(location.hash.slice(1))?.scrollIntoView({ behavior: "instant" });
}

/* ---- Mobile nav ---- */
const toggle = document.querySelector(".nav-toggle");
const links = document.querySelector(".nav-links");
if (toggle && links) {
  toggle.addEventListener("click", () => {
    const open = links.classList.toggle("open");
    toggle.setAttribute("aria-expanded", String(open));
  });
}

/* ---- Toasts (sonner-style, vanilla: bottom-right, stacked, auto-dismiss) ---- */
function toast(message, ok = true) {
  let region = document.querySelector(".toasts");
  if (!region) {
    region = document.createElement("div");
    region.className = "toasts";
    region.setAttribute("role", "status");
    region.setAttribute("aria-live", "polite");
    document.body.appendChild(region);
  }
  const el = document.createElement("div");
  el.className = ok ? "toast" : "toast err";
  el.innerHTML =
    '<svg class="tick" width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">' +
    (ok
      ? '<path d="M2.5 8.5l3.5 3.5 7.5-8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'
      : '<path d="M3 3l10 10M13 3L3 13" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>') +
    "</svg><span></span>";
  el.querySelector("span").textContent = message;
  region.appendChild(el);
  requestAnimationFrame(() => el.classList.add("in"));
  setTimeout(() => {
    el.classList.remove("in");
    el.addEventListener("transitionend", () => el.remove(), { once: true });
    setTimeout(() => el.remove(), 400); // fallback when transitions are disabled
  }, 2200);
}

/* ---- Copy buttons: any [data-copy] copies its target's text ---- */
for (const btn of document.querySelectorAll("[data-copy]")) {
  btn.addEventListener("click", async () => {
    const target = document.querySelector(btn.getAttribute("data-copy"));
    if (!target) return;
    try {
      await navigator.clipboard.writeText(target.innerText.replace(/^\$\s/gm, ""));
      toast("Copied to clipboard");
    } catch {
      toast("Copy failed: clipboard is blocked", false);
    }
  });
}

/* ---- Scroll reveal (once, transform/opacity only) ---- */
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
if (!reduced && "IntersectionObserver" in window) {
  const io = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (e.isIntersecting) {
          e.target.classList.add("in");
          io.unobserve(e.target);
        }
      }
    },
    { rootMargin: "0px 0px -8% 0px", threshold: 0.1 }
  );
  document.querySelectorAll(".reveal").forEach((el) => io.observe(el));
} else {
  document.querySelectorAll(".reveal").forEach((el) => el.classList.add("in"));
}

/* ---- Minimal syntax highlighter for python / yaml / bash snippets.
   Single-pass tokenizer: gaps and matches are escaped separately, so
   inserted markup is never re-scanned. Alternation order = priority
   (strings before comments, so a # inside a string stays a string).
   ponytail: good enough for our own snippets; swap for shiki if the
   docs ever outgrow it. ---- */
const GRAMMARS = {
  python: {
    re: /("""[\s\S]*?"""|"[^"\n]*"|'[^'\n]*')|(#[^\n]*)|(@[\w.]+)|\b(from|import|def|class|return|async|await|assert|if|elif|else|for|in|not|and|or|None|True|False|with|as|raise|pass)\b|\b(\d+(?:\.\d+)?)\b/g,
    classes: ["tok-s", "tok-c", "tok-f", "tok-k", "tok-n"],
  },
  yaml: {
    re: /("[^"\n]*"|'[^'\n]*')|(#[^\n]*)|(^[ \t]*-?[ \t]*[\w-]+(?=[ \t]*:))|\b(\d+(?:\.\d+)?)\b/gm,
    classes: ["tok-s", "tok-c", "tok-y", "tok-n"],
  },
  bash: {
    re: /("[^"\n]*"|'[^'\n]*')|(#[^\n]*)|(^\$(?=\s))|(\s--?[\w-]+)/gm,
    classes: ["tok-s", "tok-c", "tok-c", "tok-y"],
  },
};

const escapeHtml = (s) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

function highlight(code, lang) {
  const grammar = GRAMMARS[lang];
  if (!grammar) return escapeHtml(code);
  let out = "";
  let last = 0;
  let m;
  grammar.re.lastIndex = 0;
  while ((m = grammar.re.exec(code))) {
    out += escapeHtml(code.slice(last, m.index));
    const group = m.slice(1).findIndex((g) => g !== undefined);
    out += `<span class="${grammar.classes[group]}">${escapeHtml(m[0])}</span>`;
    last = m.index + m[0].length;
  }
  return out + escapeHtml(code.slice(last));
}

for (const block of document.querySelectorAll("pre code[data-lang]")) {
  block.innerHTML = highlight(block.textContent, block.getAttribute("data-lang"));
}

/* ---- Docs scrollspy: highlight the sidebar link for the section in view ---- */
const sidebar = document.querySelector(".docs-sidebar");
if (sidebar) {
  const sections = [...document.querySelectorAll(".docs-main section[id]")];
  const byId = new Map(
    [...sidebar.querySelectorAll("a[href^='#']")].map((a) => [a.getAttribute("href").slice(1), a])
  );
  let current = null;
  const spy = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (e.isIntersecting) {
          current?.classList.remove("active");
          current = byId.get(e.target.id) ?? null;
          current?.classList.add("active");
        }
      }
    },
    { rootMargin: "-15% 0px -70% 0px" }
  );
  sections.forEach((s) => spy.observe(s));
}
