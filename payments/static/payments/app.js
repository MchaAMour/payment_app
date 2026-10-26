// Shared helpers: auth (JWT), cart, theme, toast. Pages add their own scripts after this one.
// JWT is kept in sessionStorage (demo only; a production SPA would prefer httpOnly cookies).
const Auth = {
  get access() { return sessionStorage.getItem("access"); },
  get refresh() { return sessionStorage.getItem("refresh"); },
  set(t) { sessionStorage.setItem("access", t.access); if (t.refresh) sessionStorage.setItem("refresh", t.refresh); },
  clear() { sessionStorage.removeItem("access"); sessionStorage.removeItem("refresh"); },
};
function esc(v) { return String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function money(m) { return "$" + ((m || 0) / 100).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}); }
const sleep = ms => new Promise(r => setTimeout(r, ms));
const $ = (s, root = document) => root.querySelector(s);

// Only same-site paths are accepted as a post-login destination (no open redirect).
function safeNext(n) { return typeof n === "string" && /^\/(?![\/\\])/.test(n) ? n : "/"; }

async function refreshToken() {
  if (!Auth.refresh) return false;
  const r = await fetch("/auth/token/refresh", {method: "POST", headers: {"Content-Type": "application/json"},
                                                body: JSON.stringify({refresh: Auth.refresh})});
  if (!r.ok) return false;
  Auth.set(await r.json()); return true;
}
function toLogin() { location.href = "/login/?next=" + encodeURIComponent(location.pathname); }
async function api(path, opts = {}, retried = false) {
  const r = await fetch(path, {...opts, headers: {"Content-Type": "application/json",
    "Authorization": "Bearer " + Auth.access, ...(opts.headers || {})}});
  if (r.status === 401 && !retried && await refreshToken()) return api(path, opts, true);
  if (r.status === 401) { Auth.clear(); toLogin(); throw new Error("unauthenticated"); }
  return r;
}
// Fills the header user chip. With `required`, sends anonymous visitors to the login page.
async function whoami(required) {
  const show = name => {
    $("#who").textContent = name ? name : "";
    $("#login-link").hidden = !!name; $("#logout").hidden = !name;
  };
  if (!Auth.access) { show(null); if (required) toLogin(); return null; }
  try {
    const me = await (await api("/v1/me")).json();
    show(me.username); return me;
  } catch (e) { show(null); return null; }
}
$("#logout").onclick = () => { Auth.clear(); location.href = "/"; };

// ---- cart: {productId: qty} in localStorage; prices always come from the server -----------
const Cart = {
  get() { try { return JSON.parse(localStorage.getItem("cart") || "{}") || {}; } catch (e) { return {}; } },
  save(c) { try { localStorage.setItem("cart", JSON.stringify(c)); } catch (e) {} this.render(); },
  qty(id) { return this.get()[id] || 0; },
  set(id, q, max) {
    const c = this.get(); q = Math.max(0, Math.min(max || 10, q));
    if (q) c[id] = q; else delete c[id];
    this.save(c);
  },
  clear() { this.save({}); },
  count() { return Object.values(this.get()).reduce((a, b) => a + b, 0); },
  items() { return Object.entries(this.get()).map(([id, qty]) => ({id, qty})); },
  render() {
    const n = this.count(), b = $("#cart-badge");
    b.hidden = !n; b.textContent = n;
    b.classList.remove("bump"); void b.offsetWidth; if (n) b.classList.add("bump");
    document.dispatchEvent(new CustomEvent("cart:change"));
  },
};
Cart.render();

let toastTimer;
function toast(text) {
  const t = $("#toast"); t.textContent = text; t.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), 1800);
}

// ---- theme --------------------------------------------------------------------------------
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("theme", t); } catch (e) {}
  $("#theme use").setAttribute("href", t === "dark" ? "#i-sun" : "#i-moon");
}
applyTheme(document.documentElement.dataset.theme || "dark");
$("#theme").onclick = () => {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  document.dispatchEvent(new CustomEvent("theme:change"));
};
document.querySelectorAll("[data-nav]").forEach(a => {
  if (a.dataset.nav === document.body.dataset.page) a.setAttribute("aria-current", "page");
});
