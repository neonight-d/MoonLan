/* MoonLan web UI, v0.4 */

const els = {
  network: document.getElementById("network"),
  switchList: document.getElementById("switch-list"),
  namedSection: document.getElementById("named-section"),
  namedList: document.getElementById("named-list"),
  namedCount: document.getElementById("named-count"),
  hostList: document.getElementById("host-list"),
  unlocatedSection: document.getElementById("unlocated-section"),
  unlocatedList: document.getElementById("unlocated-list"),
  unlocatedCount: document.getElementById("unlocated-count"),
  switchCount: document.getElementById("switch-count"),
  hostCount: document.getElementById("host-count"),
  search: document.getElementById("search"),
  sidebar: document.getElementById("sidebar"),
  scanStatus: document.getElementById("scan-status"),
  details: document.getElementById("details"),
  detailsBody: document.getElementById("details-body"),
  detailsTitle: document.getElementById("details-title"),
  detailsClose: document.getElementById("details-close"),
  journal: document.getElementById("journal"),
  journalBtn: document.getElementById("journal-btn"),
  journalList: document.getElementById("journal-list"),
  journalClose: document.getElementById("journal-close"),
  notesPanel: document.getElementById("notes-panel"),
  printSheet: document.getElementById("print-sheet"),
  exportDialog: document.getElementById("export-dialog"),
  about: document.getElementById("about"),
  aboutBody: document.getElementById("about-body"),
  aboutClose: document.getElementById("about-close"),
  exportTitle: document.getElementById("export-title"),
  exportBody: document.getElementById("export-body"),
  exportClose: document.getElementById("export-close"),
  printPage: document.getElementById("print-page"),
  notesTitle: document.getElementById("notes-title"),
  notesBody: document.getElementById("notes-body"),
  notesClose: document.getElementById("notes-close"),
  ports: document.getElementById("ports"),
  portsTitle: document.getElementById("ports-title"),
  portsBody: document.getElementById("ports-body"),
  portsClose: document.getElementById("ports-close"),
  alarms: document.getElementById("alarms"),
  alarmsBtn: document.getElementById("alarms-btn"),
  alarmsBadge: document.getElementById("alarms-badge"),
  alarmsBody: document.getElementById("alarms-body"),
  alarmsClose: document.getElementById("alarms-close"),
  stp: document.getElementById("stp"),
  stpBtn: document.getElementById("stp-btn"),
  stpBody: document.getElementById("stp-body"),
  stpClose: document.getElementById("stp-close"),
  emptyState: document.getElementById("empty-state"),
  freezeBtn: document.getElementById("freeze-btn"),
  arrangeBtn: document.getElementById("arrange-btn"),
  portLabelsBtn: document.getElementById("port-labels-btn"),
  portLabelsMenu: document.getElementById("port-labels-menu"),
  nodeMenu: document.getElementById("node-menu"),
  actions: document.getElementById("actions"),
  actionsTitle: document.getElementById("actions-title"),
  actionsBody: document.getElementById("actions-body"),
  actionsClose: document.getElementById("actions-close"),
  moreBtn: document.getElementById("more-btn"),
  main: document.querySelector("main"),
  sidebarToggle: document.getElementById("sidebar-toggle"),
  moreMenu: document.getElementById("more-menu"),
  userMenu: document.getElementById("user-menu"),
  notice: document.getElementById("notice"),
  userChip: document.getElementById("user-chip"),
  gate: document.getElementById("gate"),
  gateTitle: document.getElementById("gate-title"),
  gateBody: document.getElementById("gate-body"),
  account: document.getElementById("account"),
  accountBody: document.getElementById("account-body"),
  accountClose: document.getElementById("account-close"),
  users: document.getElementById("users"),
  usersBody: document.getElementById("users-body"),
  usersClose: document.getElementById("users-close"),
  langSwitch: document.getElementById("lang-switch"),
  langRu: document.getElementById("lang-ru"),
  langEn: document.getElementById("lang-en"),
};

let network = null;
let nodesDs = null;
let edgesDs = null;
let topology = { switches: [], links: [], hosts: [], pseudo_switches: [] };
let isScanning = false;
let shownDetails = null; // {type: "node"|"link", id} — to re-render on language switch
let lastEvents = null; // cached journal events for re-render
let activeAlarms = []; // refreshed with the topology — badge and red borders
let lastAlarms = null; // {active, cleared} cached for the alarms panel
let lastStp = null; // cached STP report for re-render on language switch
let flapWindowHours = 2; // from the API, for the FLAP tooltip
let portsIp = null; // switch whose ports panel is open
let portsTimer = null; // its 30 s auto-refresh
let lastPorts = null; // cached ports payload for re-render
let portsSort = null; // {key, dir} chosen by clicking a column header
let portsHighlight = null; // port name to mark, e.g. the one an alarm is on
let selectedNodeId = null; // its caption gets a backdrop and brighter text
let hoveredNodeId = null; // the same, weaker, while the mouse is over it

const REFRESH_MS = 30000;

const colors = {
  moon: "#e8e4d5",
  warn: "#d9a86b",
  link: "#7fb4d9",
  text: "#d7dee9",
  dim: "#8593a8",
  ok: "#7fc98f",
  panel: "#141c2c",
  alarm: "#d96b6b",
  line: "#26324a",
};

/* ---------- localization ---------- */

const LANG_KEY = "moonlan-lang";
let lang = localStorage.getItem(LANG_KEY);
if (lang !== "ru" && lang !== "en") {
  lang = (navigator.language || "").toLowerCase().startsWith("ru") ? "ru" : "en";
}

function t(key) {
  return (I18N[lang] && I18N[lang][key]) || I18N.en[key] || key;
}

/* t() with {placeholder} substitution */
/* A translation with its {places} filled (v0.8.3: every one of them,
   and as they are — a replacement given as a string to replace() reads
   $& and $1 in it, and a device's name or a port's description may
   have them) */
function fmt(key, params = {}) {
  let s = t(key);
  for (const [name, value] of Object.entries(params)) {
    s = s.replaceAll("{" + name + "}", () => (value == null ? "" : String(value)));
  }
  return s;
}

function locale() {
  return lang === "ru" ? "ru-RU" : "en-US";
}

/* Fill in all static texts for the current language */
function applyStatic() {
  document.documentElement.lang = lang;
  document.title = t("title");
  for (const el of document.querySelectorAll("[data-i18n]")) {
    el.textContent = t(el.dataset.i18n);
  }
  for (const el of document.querySelectorAll("[data-i18n-html]")) {
    el.innerHTML = t(el.dataset.i18nHtml);
  }
  els.search.placeholder = t("searchPlaceholder");
  // names for a screen reader (v0.8.2): the placeholder is not one, and
  // "×" says nothing — every close button is marked data-close
  els.search.setAttribute("aria-label", t("searchLabel"));
  els.langSwitch.setAttribute("aria-label", t("langSwitch"));
  for (const button of document.querySelectorAll("[data-close]")) {
    button.setAttribute("aria-label", t("close"));
    button.title = t("close");
  }
  labelIcon(els.journalBtn, t("journalBtn"));
  labelIcon(els.moreBtn, t("moreBtn"));
  renderBadge();
  // what the columns are: the full name where a phone shows a short
  // one, and what the two counters actually mean (v0.5.4)
  for (const th of document.querySelectorAll("#ports th[data-sort]")) th.title = columnTitle(th);
  els.langRu.classList.toggle("active", lang === "ru");
  els.langEn.classList.toggle("active", lang === "en");
  applyFreeze();
  applySidebarCollapsed();
  applyPortLabelMode();
}

function setLang(newLang) {
  if (newLang === lang) return;
  lang = newLang;
  localStorage.setItem(LANG_KEY, lang);
  applyStatic();
  applyRole();
  applyArrangeMode();
  updateScanStatus();
  renderSidebar();
  renderGraph();
  // every panel and dialog that is open, in the new language
  for (const [panel, redraw] of panelRedraws) {
    if (!panel.classList.contains("hidden")) redraw();
  }
}

/* What redraws each panel and dialog in a newly chosen language
   (v0.8.2). The list in setLang named seven panels, and four more were
   added since without it: their headings changed language and what
   was under them did not. Each registers here, next to the code that
   draws it; tests/test_panels.py holds the registrations to the
   .panel and dialog elements of index.html, so the next one cannot be
   forgotten either. */
const panelRedraws = new Map();

function redrawOnLanguage(panel, redraw) {
  panelRedraws.set(panel, redraw);
}

/* ---------- who is looking ----------

   Sign-in arrives with the first administrator created on the server
   (python -m moonlan.users). Until then the map is open to everybody,
   which the header says for as long as it lasts, with the command that
   ends it.

   After that every request carries the session cookie. A 401 anywhere
   brings the sign-in form up over the map, and the request that met it
   waits and goes on once somebody has signed in: a session that ended
   while the page was open costs a password, not the place on the map.
   A 403 "step_required" does the same with what has to come first — a
   password of one's own, an administrator's second factor. */

let me = null; // /api/auth/me: {sign_in: false, ...}, or who this browser is
let everSignedIn = false; // …so a later 401 reads "the session ended"
const ROLE_RANK = { viewer: 1, user: 2, admin: 3 };

/* null when this browser may do what needs `role`, or why not. While
   sign-in is off the map is what it always was: everything is allowed.
   Before /api/auth/me has answered nothing is refused here either —
   the server refuses whatever it has to. */
function needRole(role) {
  if (!me || !me.sign_in || !me.role) return null;
  if (ROLE_RANK[me.role] >= ROLE_RANK[role]) return null;
  return fmt("reasonNeedRole", { role: t("role_" + role) });
}

/* Accounts are never open: not before sign-in exists, and not below
   an administrator. */
function accountsReason() {
  if (!me || !me.sign_in) return t("reasonSignInOff");
  return needRole("admin");
}

async function loadMe() {
  let response;
  try {
    response = await fetch("/api/auth/me");
  } catch (e) {
    return;
  }
  const answer = await response.json();
  me = response.status === 401
    ? { sign_in: true, https_url: answer.https_url, passkeys: answer.passkeys }
    : answer;
  if (me.name) everSignedIn = true;
  applyRole();
  // the sign-in form may have come up before this answer, on the map's
  // first 401: what it says about HTTPS and keys is brought up to date
  // in place, so nothing typed meanwhile is lost
  if (gateUp() && gateView === "signIn" && signInState) signInState();
}

/* Every call to the service goes through here. Network errors are
   thrown as before — the callers know what "the service is gone"
   means for them. */
async function api(path, options) {
  for (;;) {
    if (gateWait) await gateWait;
    const response = await fetch(path, options);
    if (response.status === 401) {
      await throughGate("signIn");
      continue;
    }
    if (response.status === 403) {
      let body = null;
      try {
        body = await response.clone().json();
      } catch (e) {
        body = null;
      }
      if (body && body.error === "step_required") {
        await throughGate(body.step);
        continue;
      }
      if (body && body.error === "forbidden") {
        // the role changed while the page was open
        showToast(fmt("reasonNeedRole", { role: t("role_" + body.need) }));
        loadMe();
      }
    }
    return response;
  }
}

function jsonPost(path, body) {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  };
}

/* A small element builder: the forms below are all fields, labels and
   buttons, and innerHTML would put whatever a name contains into the
   page as markup. */
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children) {
    if (child !== null && child !== undefined && child !== false) el.append(child);
  }
  return el;
}

function field(labelKey, props) {
  const input = h("input", props);
  return { label: h("label", {}, t(labelKey), input), input: input };
}

function errorText(answer) {
  const code = (answer && answer.error) || "other";
  if (code === "locked") {
    return fmt("err_locked", {
      minutes: Math.max(1, Math.ceil((answer.retry_after || 60) / 60)),
    });
  }
  if (code === "too_many_attempts") {
    return fmt("err_too_many_attempts", { seconds: answer.retry_after || 1 });
  }
  const key = "err_" + code;
  return t(key) !== key ? t(key) : fmt("err_other", { error: code });
}

/* POST to /api/auth/*, from inside the gate: not through api(), which
   would wait for the very gate this is part of. */
async function authPost(path, body) {
  try {
    const response = await fetch(path, jsonPost(path, body));
    let answer = {};
    try {
      answer = await response.json();
    } catch (e) {
      answer = {};
    }
    return { ok: response.ok, status: response.status, answer: answer };
  } catch (e) {
    return { ok: false, status: 0, answer: { error: "no_service" } };
  }
}

function plainHttp() {
  return location.protocol !== "https:";
}

/* A translated line with a link in place of {link}. */
function withLink(key, url) {
  return fmt(key, { link: "\u0000" }).split("\u0000").flatMap(
    (text, i) => (i ? [h("a", { href: url, text: url }), text] : [text])
  );
}

/* ---------- the gate ---------- */

let gateWait = null; // what requests wait on while the gate is up
let gateDone = null; // …and what lets them go

function gateUp() {
  return !els.gate.classList.contains("hidden");
}

/* Puts the gate up (unless it is up already) and resolves once
   whoever is at the keyboard has got through it. */
function throughGate(kind) {
  if (!gateWait) {
    gateWait = new Promise((resolve) => {
      gateDone = resolve;
    });
    showGate(kind);
  }
  return gateWait;
}

function showGate(kind) {
  closeNodeMenu();
  closeMoreMenu(false);
  if (kind === "signIn" && (!me || !me.sign_in || me.name)) {
    // whatever the page believed — sign-in off, or signed in as
    // somebody — the server has just said otherwise; what it said about
    // the address and keys still holds
    me = { sign_in: true, https_url: me && me.https_url,
           passkeys: me && me.passkeys };
    applyRole();
  }
  els.gate.classList.remove("hidden");
  document.body.classList.add("gated");
  fitHeader();
  if (kind !== "signIn" && !(me && me.name)) {
    // a step met on the first request of the page, before
    // /api/auth/me has answered: its forms need the name
    els.gateBody.replaceChildren();
    loadMe().then(() => showGate(me && me.name ? kind : "signIn"));
    return;
  }
  if (kind === "signIn") renderSignIn();
  else if (kind === "password") renderNewPassword();
  else if (kind === "totp") renderBindTotp(true);
  else if (kind === "bind") renderBindTotp(false);
}

/* The gate with a form drawn by `render`, from outside a sign-in. */
function openGate(render) {
  els.gate.classList.remove("hidden");
  document.body.classList.add("gated");
  fitHeader();
  render();
}

function closeGate() {
  els.gate.classList.add("hidden");
  document.body.classList.remove("gated");
  fitHeader();
  els.gateBody.replaceChildren();
  const done = gateDone;
  gateWait = null;
  gateDone = null;
  if (done) done();
}

function gateForm(titleKey, parts, onSubmit) {
  els.gateTitle.textContent = t(titleKey);
  const form = h("form", { class: "form", novalidate: true }, ...parts);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    onSubmit(form);
  });
  els.gateBody.replaceChildren(form);
  const first = form.querySelector("input:not([type=hidden]):not([readonly])");
  if (first) first.focus();
  return form;
}

/* After a password or a code: who is this, and is there a step left. */
async function signedIn() {
  await loadMe();
  if (me && me.step) {
    showGate(me.step);
    return;
  }
  closeGate();
}

async function signOut() {
  await authPost("/api/auth/logout", {});
  // a fresh page: nothing of the previous person's map stays behind
  // the form for the next one
  location.reload();
}

let gateView = null; // which form the gate shows, for the language switch
let signInState = null; // redraws what the sign-in form says about HTTPS and keys

/* The sign-in form's own language switch: the language is needed
   before anybody has signed in. */
function gateLangSwitch() {
  const choose = (code) => h("button", {
    type: "button", text: code.toUpperCase(),
    class: lang === code ? "active" : null,
    "aria-pressed": lang === code ? "true" : "false",
    onclick: () => setLang(code),
  });
  return h("span", { class: "lang-switch gate-lang", role: "group",
                     "aria-label": t("langSwitch") }, choose("ru"), choose("en"));
}

function renderSignIn(message, typedName) {
  gateView = "signIn";
  const name = field("fieldName", {
    name: "username", autocomplete: "username", autocapitalize: "none",
    spellcheck: "false", required: true, value: typedName || null,
  });
  const password = field("fieldPassword", {
    type: "password", name: "password", autocomplete: "current-password",
    required: true,
  });
  const error = h("p", { class: "error", role: "alert", text: message || "" });
  const button = h("button", { type: "submit", text: t("signInBtn") });
  // the key road: greyed out with the reason when this page cannot take it
  const withKey = h("button", { type: "button", class: "key-btn", text: t("signInKeyBtn") });
  const note = h("div");
  const keyWhy = h("div");
  signInState = () => {
    // plain HTTP: said before the password is typed — and where to go
    // instead, when the map is served over HTTPS elsewhere
    note.replaceChildren(...(me && me.https_url
      ? [h("p", { class: "http-note" }, ...withLink("httpsSignIn", me.https_url))]
      : plainHttp() ? [h("p", { class: "http-note", text: t("httpSignIn") })] : []));
    const why = keyUnavailable();
    gateButton(withKey, why ? keyWhyText(why) : null);
    keyWhy.replaceChildren(...(why ? [keyWhyLine(why)] : []));
  };
  signInState();
  withKey.addEventListener("click", () => {
    if (!allowed(withKey)) return;
    error.textContent = "";
    signInWithKey("", error, withKey);
  });
  gateForm("signInTitle", [
    gateLangSwitch(),
    everSignedIn ? h("p", { text: t("signInAgain") }) : null,
    note,
    name.label, password.label, error,
    h("div", { class: "buttons" }, button, withKey),
    keyWhy,
  ], async () => {
    button.disabled = true;
    const result = await authPost("/api/auth/login", {
      name: name.input.value.trim(), password: password.input.value,
    });
    button.disabled = false;
    if (result.ok && result.answer.status === "code_required") {
      renderCode(result.answer.ticket, result.answer.methods);
    } else if (result.ok) {
      await signedIn();
    } else if (result.answer.error === "sign_in_off") {
      // nobody to sign in as after all: the map is open
      await loadMe();
      closeGate();
    } else if (result.answer.error === "https_required" && result.answer.url) {
      error.replaceChildren(...withLink("httpsSignIn", result.answer.url));
    } else {
      error.textContent = errorText(result.answer);
      password.input.select();
    }
  });
}

function renderCode(ticket, methods) {
  gateView = "code";
  methods = methods || ["totp", "recovery"];
  const code = field("fieldCode", {
    name: "code", autocomplete: "one-time-code", autocapitalize: "none",
    spellcheck: "false", required: true,
  });
  const error = h("p", { class: "error", role: "alert" });
  const button = h("button", { type: "submit", text: t("confirmBtn") });
  const byKey = methods.includes("passkey");
  const why = byKey ? keyUnavailable() : null;
  const useKey = byKey
    ? h("button", { type: "button", class: "key-btn", text: t("useKeyBtn") }) : null;
  if (useKey) {
    gateButton(useKey, why ? keyWhyText(why) : null);
    useKey.addEventListener("click", () => {
      if (!allowed(useKey)) return;
      error.textContent = "";
      signInWithKey(ticket, error, useKey);
    });
  }
  const hint = methods.includes("totp") ? "codeHint"
    : byKey ? "codeHintKey" : "codeHintKeyOff";
  gateForm("codeTitle", [
    h("p", { text: t(hint) }),
    code.label, error,
    h("div", { class: "buttons" }, useKey, button,
      h("button", { type: "button", text: t("backBtn"), onclick: () => renderSignIn() })),
    why ? keyWhyLine(why) : null,
  ], async () => {
    button.disabled = true;
    const result = await authPost("/api/auth/totp", {
      ticket: ticket, code: code.input.value.trim(),
    });
    button.disabled = false;
    if (result.ok) {
      await signedIn();
    } else if (result.answer.error === "ticket_expired" || result.answer.error === "locked") {
      renderSignIn(errorText(result.answer));
    } else {
      error.textContent = errorText(result.answer);
      code.input.select();
    }
  });
}

/* The fields of a password change; the username goes along hidden so a
   password manager files the new password under the right name. */
function passwordFields() {
  return {
    user: h("input", {
      type: "text", name: "username", autocomplete: "username",
      value: (me && me.name) || "", readonly: true, hidden: true,
    }),
    current: field("fieldCurrentPassword", {
      type: "password", autocomplete: "current-password", required: true,
    }),
    fresh: field("fieldNewPassword", {
      type: "password", autocomplete: "new-password", required: true,
    }),
    again: field("fieldRepeatPassword", {
      type: "password", autocomplete: "new-password", required: true,
    }),
  };
}

async function changePassword(fields, error) {
  if (fields.fresh.input.value !== fields.again.input.value) {
    error.textContent = t("err_mismatch");
    return null;
  }
  const result = await authPost("/api/auth/password", {
    current: fields.current.input.value, new: fields.fresh.input.value,
  });
  if (!result.ok) {
    error.textContent = errorText(result.answer);
    return null;
  }
  return result.answer;
}

function renderNewPassword() {
  gateView = "password";
  const fields = passwordFields();
  const error = h("p", { class: "error", role: "alert" });
  const button = h("button", { type: "submit", text: t("saveBtn") });
  gateForm("newPasswordTitle", [
    h("p", { text: t("newPasswordHint") }),
    h("p", { class: "hint-line", text: t("passwordRules") }),
    fields.user, fields.current.label, fields.fresh.label, fields.again.label,
    error,
    h("div", { class: "buttons" }, button,
      h("button", { type: "button", text: t("logoutBtn"), onclick: signOut })),
  ], async () => {
    button.disabled = true;
    const done = await changePassword(fields, error);
    button.disabled = false;
    if (done) await signedIn();
  });
}

/* ---------- binding a second factor ----------

   The secret is shown, nothing is bound: it waits on the server until a
   code made from it comes back with the password, so a mistake while
   scanning locks nobody out. Over plain HTTP the page says the secret
   is crossing the network in the clear, and how to avoid that. */

const QR_LIBRARY = "https://cdnjs.cloudflare.com/ajax/libs/qrious/4.0.2/qrious.min.js";
let qrLoading = null;

/* Loaded when a QR code is needed and not before: a wall monitor has
   no use for it. Resolves false if cdnjs cannot be reached — the
   secret is on the screen as text anyway. */
function loadQrLibrary() {
  if (window.QRious) return Promise.resolve(true);
  if (!qrLoading) {
    qrLoading = new Promise((resolve) => {
      const script = document.createElement("script");
      script.src = QR_LIBRARY;
      script.onload = () => resolve(!!window.QRious);
      script.onerror = () => {
        qrLoading = null;
        resolve(false);
      };
      document.head.append(script);
    });
  }
  return qrLoading;
}

function copyButton(text) {
  return h("button", {
    type: "button", class: "panel-btn", text: t("copyBtn"),
    onclick: () => copyText(text),
  });
}

function renderBindTotp(required) {
  gateView = "totp";
  const command = "python -m moonlan.users totp " + ((me && me.name) || "<name>");
  const show = h("button", { type: "button", text: t("showSecretBtn") });
  const leave = required
    ? h("button", { type: "button", text: t("logoutBtn"), onclick: signOut })
    : h("button", { type: "button", text: t("backBtn"), onclick: closeGate });
  const area = h("div");
  gateForm("totpTitle", [
    required ? h("p", { text: t("totpRequired") }) : null,
    plainHttp()
      ? h("p", { class: "http-note" },
          ...fmt("totpHttp", { command: "\u0000" }).split("\u0000").flatMap(
            (text, i) => (i ? [h("code", { text: command }), text] : [text])
          ))
      : null,
    required ? h("p", { class: "hint-line", text: t("totpBoundOnServer") }) : null,
    area,
    h("div", { class: "buttons" }, show, leave),
    required ? keyInsteadOfTotp() : null,
  ], () => {});
  show.addEventListener("click", async () => {
    show.disabled = true;
    const result = await authPost("/api/auth/totp/setup", {});
    show.disabled = false;
    if (!result.ok) {
      area.replaceChildren(h("p", { class: "error", text: errorText(result.answer) }));
      return;
    }
    show.remove();
    renderSecret(area, result.answer, required);
  });
}

/* Under the TOTP binding an administrator is forced to: a key does as
   well. Greyed out with the reason when this page cannot use one. */
function keyInsteadOfTotp() {
  const why = keyUnavailable();
  const button = h("button", { type: "button", text: t("addKeyBtn") });
  gateButton(button, why ? keyWhyText(why) : null);
  button.addEventListener("click", () => {
    if (allowed(button)) renderGateKeyAdd();
  });
  return h("div", { class: "key-instead" },
    h("p", { class: "hint-line", text: t("totpOrKey") }),
    h("div", { class: "buttons" }, button),
    why ? keyWhyLine(why) : null);
}

function renderSecret(area, secret, required) {
  const grouped = secret.secret.replace(/(.{4})(?=.)/g, "$1 ");
  const qr = h("div");
  const code = field("fieldCode", {
    autocomplete: "one-time-code", autocapitalize: "none", spellcheck: "false",
    inputmode: "numeric", required: true,
  });
  const user = h("input", {
    type: "text", name: "username", autocomplete: "username",
    value: (me && me.name) || "", readonly: true, hidden: true,
  });
  const password = field("fieldPassword", {
    type: "password", autocomplete: "current-password", required: true,
  });
  const error = h("p", { class: "error", role: "alert" });
  const bind = h("button", { type: "submit", text: t("bindBtn") });
  area.replaceChildren(
    h("p", { class: "hint-line", text: t("totpScanHint") }),
    h("div", { class: "totp-secret" },
      qr,
      h("div", { class: "hint-line", text: t("totpSecretLabel") }),
      h("div", { class: "secret", text: grouped }),
      copyButton(secret.secret),
      h("div", { class: "hint-line", text: t("totpUriLabel") }),
      h("div", { class: "uri", text: secret.uri }),
      copyButton(secret.uri)),
    h("p", { text: t("totpConfirmHint") }),
    user, code.label, password.label, error,
    h("div", { class: "buttons" }, bind)
  );
  code.input.focus();
  loadQrLibrary().then((ok) => {
    if (!ok) {
      qr.replaceChildren(h("p", { class: "hint-line", text: t("qrUnavailable") }));
      return;
    }
    const canvas = h("canvas");
    // eslint-disable-next-line no-undef
    new QRious({ element: canvas, value: secret.uri, size: 200, level: "M" });
    qr.replaceChildren(canvas);
  });
  const form = area.closest("form");
  form.onsubmit = async (event) => {
    event.preventDefault();
    bind.disabled = true;
    const result = await authPost("/api/auth/totp/confirm", {
      code: code.input.value.trim(), password: password.input.value,
    });
    bind.disabled = false;
    if (!result.ok) {
      error.textContent = errorText(result.answer);
      return;
    }
    renderRecovery(result.answer.recovery, required);
  };
}

/* Shown once, and the gate stays until the person says they have them. */
function renderRecovery(codes, required) {
  gateForm("recoveryTitle", [
    h("p", { text: t("recoveryHint") }),
    h("ul", { class: "recovery-codes" }, ...codes.map((c) => h("li", { text: c }))),
    h("div", { class: "buttons" },
      h("button", { type: "submit", text: t("recoveryDoneBtn") }),
      copyButton(codes.join("\n"))),
  ], async () => {
    if (required) {
      await signedIn();
    } else {
      closeGate();
      await loadMe();
      if (!els.account.classList.contains("hidden")) renderAccount();
    }
  });
}

/* ---------- keys (WebAuthn) ----------

   The browser talks to the key; the server checks what comes back. The
   options arrive as JSON with the bytes in base64url and go back the
   same way — converted here by hand rather than with
   PublicKeyCredential.parseCreationOptionsFromJSON and toJSON, which
   the older browsers still found in schools do not have. */

function fromB64url(text) {
  const plain = text.replace(/-/g, "+").replace(/_/g, "/");
  const bytes = atob(plain + "===".slice((plain.length + 3) % 4));
  return Uint8Array.from(bytes, (c) => c.charCodeAt(0));
}

function toB64url(buffer) {
  let text = "";
  for (const byte of new Uint8Array(buffer)) text += String.fromCharCode(byte);
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function withIds(list) {
  return (list || []).map((c) => Object.assign({}, c, { id: fromB64url(c.id) }));
}

function credentialJson(credential) {
  const r = credential.response;
  const json = {
    id: credential.id,
    rawId: toB64url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment || null,
    clientExtensionResults: credential.getClientExtensionResults
      ? credential.getClientExtensionResults() : {},
    response: { clientDataJSON: toB64url(r.clientDataJSON) },
  };
  if (r.attestationObject) {
    json.response.attestationObject = toB64url(r.attestationObject);
    json.response.transports = r.getTransports ? r.getTransports() : [];
  } else {
    json.response.authenticatorData = toB64url(r.authenticatorData);
    json.response.signature = toB64url(r.signature);
    json.response.userHandle = r.userHandle ? toB64url(r.userHandle) : null;
  }
  return json;
}

async function createKey(options) {
  const publicKey = Object.assign({}, options, {
    challenge: fromB64url(options.challenge),
    user: Object.assign({}, options.user, { id: fromB64url(options.user.id) }),
    excludeCredentials: withIds(options.excludeCredentials),
  });
  return credentialJson(await navigator.credentials.create({ publicKey: publicKey }));
}

async function getKey(options) {
  const publicKey = Object.assign({}, options, {
    challenge: fromB64url(options.challenge),
    allowCredentials: withIds(options.allowCredentials),
  });
  return credentialJson(await navigator.credentials.get({ publicKey: publicKey }));
}

/* Why this page cannot use a key right now — the first reason that
   applies, in the order that tells a person what to fix — or null.
   The server's own reasons come with /api/auth/me. */
function keyUnavailable() {
  const info = (me && me.passkeys) || {};
  const addressProblem = info.why && info.why !== "no_fido2";
  // where keys would work, when that is not here
  const there = info.origin && !addressProblem && location.origin !== info.origin
    ? info.origin + location.pathname : null;
  if (!window.isSecureContext) return { key: "passkeyWhy_insecure", link: there };
  if (there) return { key: "passkeyWhy_elsewhere", link: there };
  if (info.why) return { key: "passkeyWhy_" + info.why };
  if (!window.PublicKeyCredential || !navigator.credentials) {
    return { key: "passkeyWhy_no_webauthn" };
  }
  return null;
}

function keyWhyText(why) {
  return why.key === "passkeyWhy_elsewhere"
    ? fmt(why.key, { link: why.link }) : t(why.key);
}

/* The reason as a line under the button, with the address as a link. */
function keyWhyLine(why) {
  const line = h("p", { class: "hint-line key-why" });
  if (why.key === "passkeyWhy_elsewhere") line.append(...withLink(why.key, why.link));
  else {
    line.append(t(why.key));
    if (why.link) line.append(" ", ...withLink("passkeyWhy_elsewhere", why.link));
  }
  return line;
}

/* What the browser threw, in words — by its cause where the browser
   gives it away. `context`: "add", "addPasswordless", "signIn" (a key
   alone) or "second" (after the password). */
function keyErrorText(e, context) {
  const name = e && e.name;
  // Chrome turns WebAuthn off on a page with a certificate error and
  // answers NotAllowedError at once — "WebAuthn is not supported on
  // sites with TLS certificate errors". It is the certificate, not the
  // key, and "the key did not answer" sent people to the wrong place.
  if (/certificate/i.test((e && e.message) || "")) return t("keyErr_certificate");
  if (context === "signIn" && name === "NotAllowedError") {
    // with nothing to look up, a key that keeps no sign-in for this
    // site looks exactly like a cancel to the page
    return t("keyErr_signIn");
  }
  if (context === "addPasswordless") {
    // Asked to keep the sign-in and ask for a PIN, a key that cannot
    // does not say so: the browser answers as if it were cancelled
    // (NotAllowedError), or, rarely, that it cannot (ConstraintError,
    // NotSupportedError). Either way, say what to do about it.
    if (name === "NotAllowedError") return t("keyErr_addPasswordless");
    if (name === "ConstraintError" || name === "NotSupportedError") {
      return t("err_cannot_passwordless");
    }
  }
  const known = ["NotAllowedError", "InvalidStateError", "SecurityError",
    "NotSupportedError", "AbortError", "ConstraintError"];
  if (known.includes(name)) return t("keyErr_" + name);
  return fmt("keyErr_other", { error: name || (e && e.message) || "?" });
}

/* Adding a key: the password (as for TOTP), whether the key may sign in
   on its own, and a name to tell it from the others. */
function keyAddForm(onAdded, onBack) {
  const user = h("input", {
    type: "text", name: "username", autocomplete: "username",
    value: (me && me.name) || "", readonly: true, hidden: true,
  });
  const password = field("fieldPassword", {
    type: "password", autocomplete: "current-password", required: true,
  });
  const label = field("fieldKeyLabel", {
    maxlength: 64, autocomplete: "off", placeholder: t("keyLabelPlaceholder"),
  });
  const alone = h("input", { type: "checkbox", checked: true });
  const error = h("p", { class: "error", role: "alert" });
  const add = h("button", { type: "submit", text: t("addKeyBtn") });
  const form = h("form", { class: "form key-add", novalidate: true },
    h("p", { class: "hint-line", text: t("keyAddHint") }),
    user, password.label, label.label,
    h("label", { class: "check" }, alone, t("keyPasswordless")),
    h("p", { class: "hint-line", text: t("keyPasswordlessHint") }),
    error,
    h("div", { class: "buttons" }, add,
      onBack ? h("button", { type: "button", text: t("backBtn"), onclick: onBack }) : null));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    add.disabled = true;
    try {
      const begun = await authPost("/api/auth/passkeys/begin", {
        password: password.input.value, passwordless: alone.checked,
      });
      if (!begun.ok) {
        error.textContent = errorText(begun.answer);
        return;
      }
      let credential;
      try {
        credential = await createKey(begun.answer.options);
      } catch (e) {
        error.textContent = keyErrorText(e, alone.checked ? "addPasswordless" : "add");
        return;
      }
      const done = await authPost("/api/auth/passkeys/finish", {
        request: begun.answer.request, credential: credential,
        label: label.input.value.trim(),
      });
      if (!done.ok) {
        error.textContent = errorText(done.answer);
        return;
      }
      done.answer.label = label.input.value.trim();
      onAdded(done.answer);
    } finally {
      add.disabled = false;
    }
  });
  return { form: form, focus: () => password.input.focus() };
}

/* Signing in with a key: alone (no ticket) or as the second step after
   the password (the password step's ticket). */
async function signInWithKey(ticket, error, button) {
  button.disabled = true;
  try {
    const begun = await authPost("/api/auth/passkey/begin", { ticket: ticket });
    if (!begun.ok) {
      if (begun.answer.error === "ticket_expired") renderSignIn(errorText(begun.answer));
      else error.textContent = errorText(begun.answer);
      return;
    }
    let credential;
    try {
      credential = await getKey(begun.answer.options);
    } catch (e) {
      error.textContent = keyErrorText(e, ticket ? "second" : "signIn");
      return;
    }
    const done = await authPost("/api/auth/passkey/finish", {
      request: begun.answer.request, credential: credential,
    });
    if (done.ok) {
      await signedIn();
    } else if (ticket && (done.answer.error === "ticket_expired" ||
                          done.answer.error === "locked")) {
      renderSignIn(errorText(done.answer));
    } else {
      error.textContent = errorText(done.answer);
    }
  } finally {
    button.disabled = false;
  }
}

/* What was added, in a sentence. */
function keyAddedText(answer) {
  return fmt("keyAdded", { label: answer.label || "#" + answer.number });
}

/* The forced step of an administrator with no second factor, taking
   the key road instead of TOTP. */
function renderGateKeyAdd() {
  gateView = "keyAdd";
  const adding = keyAddForm((answer) => {
    showToast(keyAddedText(answer));
    if (answer.recovery) renderRecovery(answer.recovery, true);
    else signedIn();
  }, () => renderBindTotp(true));
  gateForm("keyAddTitle", [adding.form], () => {});
  adding.focus();
}

/* ---------- the header: who, and what this role may do ---------- */

function renderNotice() {
  const parts = [];
  if (me && me.sign_in === false) {
    const text = document.createElement("span");
    text.textContent = t("noticeOpen") + " " + t("noticeOpenHint") + " ";
    const command = document.createElement("code");
    command.textContent = me.create_admin;
    parts.push(text, command);
  } else if (me && me.name && plainHttp()) {
    // Signing in over plain HTTP guards against a stranger at the
    // keyboard and a guessed password, not against somebody reading
    // the traffic. The page says so for as long as it is true — calmly,
    // as a fact about this installation, not as an alarm.
    parts.push(h("span", { text: t("noticeHttp") }));
    if (me.https_url) {
      parts.push(document.createTextNode(" "),
                 h("span", {}, ...withLink("noticeHttpsAt", me.https_url)));
    }
  }
  els.notice.replaceChildren(...parts);
  els.notice.classList.toggle("hidden", !parts.length);
}

/* Not for this role: greyed out with the reason, never removed. */
function gateButton(button, reason) {
  button.classList.toggle("off", !!reason);
  button.setAttribute("aria-disabled", reason ? "true" : "false");
  button.dataset.why = reason || "";
  button.title = reason || "";
}

/* For a click handler: go ahead, or say why not. */
function allowed(button) {
  if (!button.dataset.why) return true;
  showToast(button.dataset.why);
  return false;
}

function applyRole() {
  renderNotice();
  const signedInAs = me && me.sign_in && me.name;
  els.userChip.classList.toggle("hidden", !signedInAs);
  if (signedInAs) {
    els.userChip.replaceChildren(
      me.name, h("span", { class: "role", text: t("role_" + me.role) })
    );
    els.userChip.title = t("userChipHint");
  }
  gateButton(els.arrangeBtn, needRole("user"));
  // what the Actions menu offers is decided when it opens; one open now
  // was drawn for another role
  closeMoreMenu(false);
  if (arrangeMode && needRole("user")) arrangeMode = false;
  applyArrangeMode();
  // what an open panel offers depends on the role
  if (!els.details.classList.contains("hidden") && shownDetails) {
    if (shownDetails.type === "link") showLinkDetails(shownDetails.id);
    else showDetails(shownDetails.id);
  }
  if (!els.alarms.classList.contains("hidden") && lastAlarms) renderAlarms();
  if (!els.account.classList.contains("hidden")) renderAccount();
  if (!els.users.classList.contains("hidden") && accountsReason()) {
    els.users.classList.add("hidden");
  }
  // the name in the header may be longer or shorter than before
  fitHeader();
}

/* ---------- your own account ---------- */

// …and About: the panels the header's menus open
function hideAccountPanels() {
  els.account.classList.add("hidden");
  els.users.classList.add("hidden");
  els.about.classList.add("hidden");
  panelAsked = null;
}

/* The panel asked for last (v0.8.3). The ports, alarms, STP and the
   journal open once their answer is in, and an answer late enough
   opened its panel over whatever had been opened since — or after it
   was closed, its 30 s timer included. Showing any other panel or
   closing the ports drops the ticket; an answer whose ticket is gone
   opens nothing. */
let panelAsked = null;

function askPanel() {
  const ticket = {};
  panelAsked = ticket;
  return () => panelAsked === ticket;
}

function hidePanelsFor(panel) {
  panelAsked = null;
  for (const other of [els.details, els.journal, els.alarms, els.stp,
    els.account, els.users, els.notesPanel, els.about]) {
    if (other !== panel) other.classList.add("hidden");
  }
  if (panel !== els.details) shownDetails = null;
  closePorts();
}

function toggleAccount() {
  if (!els.account.classList.contains("hidden")) {
    els.account.classList.add("hidden");
    return;
  }
  hidePanelsFor(els.account);
  els.account.classList.remove("hidden");
  renderAccount();
  loadMe();
}

function renderAccount() {
  if (!me || !me.name) return;
  const totp = me.totp
    ? t("totpOn") + " · " + fmt("recoveryLeft", { n: me.recovery_left })
    : t("totpOff");
  const fields = passwordFields();
  const error = h("p", { class: "error", role: "alert" });
  const done = h("p", { class: "done" });
  const save = h("button", { type: "submit", text: t("saveBtn") });
  const form = h("form", { class: "form hidden", novalidate: true },
    h("p", { class: "hint-line", text: t("passwordRules") }),
    fields.user, fields.current.label, fields.fresh.label, fields.again.label,
    error, h("div", { class: "buttons" }, save));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    save.disabled = true;
    const answer = await changePassword(fields, error);
    save.disabled = false;
    if (answer) {
      form.classList.add("hidden");
      done.textContent = fmt("passwordChanged", { n: answer.sessions_closed });
    }
  });
  const keys = me.keys || [];
  const why = keyUnavailable();
  const adding = keyAddForm(async (answer) => {
    await loadMe();
    renderAccount();
    const line = els.accountBody.querySelector(".done");
    if (line) line.textContent = keyAddedText(answer);
    // the first key of an account without recovery codes brings them,
    // shown this once — over the map, like after binding TOTP
    if (answer.recovery) openGate(() => renderRecovery(answer.recovery, false));
  }, () => adding.form.classList.add("hidden"));
  adding.form.classList.add("hidden");
  const addKey = h("button", { class: "panel-btn", text: t("addKeyBtn") });
  gateButton(addKey, why ? keyWhyText(why) : null);
  addKey.addEventListener("click", () => {
    if (!allowed(addKey)) return;
    form.classList.add("hidden");
    adding.form.classList.toggle("hidden");
    done.textContent = "";
    if (!adding.form.classList.contains("hidden")) adding.focus();
  });
  // replaceChildren would write a null out as the word "null"
  els.accountBody.replaceChildren(...[
    h("dl", {},
      h("dt", { text: t("fieldName") }), h("dd", { text: me.name }),
      h("dt", { text: t("accountRole") }), h("dd", { text: t("role_" + me.role) }),
      h("dt", { text: t("accountTotp") }), h("dd", { text: totp }),
      h("dt", { text: t("accountKeys") }),
      h("dd", {}, keys.length ? keyList(keys, removeOwnKey) : t("keysNone"))),
    h("button", {
      class: "panel-btn", text: t("changePasswordBtn"),
      onclick: () => {
        adding.form.classList.add("hidden");
        form.classList.toggle("hidden");
        done.textContent = "";
        if (!form.classList.contains("hidden")) fields.current.input.focus();
      },
    }),
    h("button", {
      class: "panel-btn", text: t(me.totp ? "rebindTotpBtn" : "bindTotpBtn"),
      title: me.totp ? t("rebindHint") : null,
      onclick: () => showGate("bind"),
    }),
    addKey,
    h("button", { class: "panel-btn", text: t("logoutBtn"), onclick: signOut }),
    why ? keyWhyLine(why) : null,
    done, form, adding.form,
  ].filter(Boolean));
}

/* Keys: the name, what it may do, when added and last used — and a
   button to remove one when `remove` is given. */
function keyList(keys, remove) {
  return h("ul", { class: "key-list" }, ...keys.map((key) => h("li", {},
    h("div", { class: "key-head" },
      h("strong", { text: key.label || "#" + key.number }),
      remove ? h("button", {
        type: "button", class: "key-remove", text: t("actRemoveKey"),
        onclick: () => remove(key),
      }) : null),
    h("div", { class: "hint-line" },
      // "without a password" that the key never confirmed (added before
      // v0.7.7): said so, with what to do — nothing is rewritten quietly
      key.unconfirmed
        ? h("span", { class: "bad", text: t("keyUnconfirmed") })
        : t(key.passwordless ? "keyPasswordlessShort" : "keySecondShort"),
      " · " + [
        fmt("keyAddedOn", { date: fmtDate(key.created_at) }),
        key.last_used ? fmt("keyUsedOn", { date: fmtTime(key.last_used) }) : t("keyNeverUsed"),
      ].join(" · ")))));
}

async function removeOwnKey(key) {
  return busy("passkey:" + key.id, async () => {
    const label = key.label || "#" + key.number;
    if (!window.confirm(fmt("confirmRemoveOwnKey", { label: label }))) return;
    let response;
    try {
      response = await api("/api/auth/passkeys/" + key.id, { method: "DELETE" });
    } catch (e) {
      serviceLost("removing a key");
      return;
    }
    const answer = await response.json().catch(() => ({}));
    if (!response.ok) {
      // the last second factor of an administrator, most likely
      showToast(errorText(answer));
      return;
    }
    await loadMe();
    renderAccount();
    const line = els.accountBody.querySelector(".done");
    if (line) line.textContent = fmt("keyRemoved", { n: answer.sessions_closed });
  }, []);
}

/* ---------- accounts, for an administrator ---------- */

let usersNotice = null; // a temporary password, shown once

async function toggleUsers() {
  if (!els.users.classList.contains("hidden")) {
    els.users.classList.add("hidden");
    return;
  }
  hidePanelsFor(els.users);
  usersNotice = null;
  els.users.classList.remove("hidden");
  await refreshUsers();
}

async function refreshUsers(error) {
  let data;
  try {
    data = await (await api("/api/users")).json();
  } catch (e) {
    serviceLost("the users");
    return;
  }
  if (!data.users) return;
  renderUsers(data.users, data.me, error);
}

/* One change to one account: the request, and the list again. The
   question asked first is `confirmKey` for `name`, or `question` as it
   is. */
async function userAction(path, options, confirmKey, name, question) {
  return busy("users:" + path + ":" + ((options && options.method) || ""), async () => {
    if (confirmKey && !window.confirm(fmt(confirmKey, { name: name }))) return;
    if (question && !window.confirm(question)) return;
    // a temporary password is shown until the next change, not forever
    usersNotice = null;
    let response;
    try {
      response = await api(path, options);
    } catch (e) {
      serviceLost("changing an account");
      return;
    }
    const answer = await response.json().catch(() => ({}));
    if (!response.ok) {
      await refreshUsers(errorText(answer));
      return;
    }
    if (answer.password) {
      usersNotice = fmt(path.endsWith("/password") ? "userNewPassword" : "userCreated", {
        name: answer.name, password: answer.password,
      });
    } else {
      showToast(t("usersDone"));
    }
    await refreshUsers();
  }, []);
}

function userState(user) {
  const now = Date.now() / 1000;
  const parts = [];
  if (user.disabled) parts.push(h("span", { class: "bad", text: t("stateDisabled") }));
  else if (user.locked_until > now) {
    parts.push(h("span", { class: "bad", text: fmt("stateLocked", { time: fmtTime(user.locked_until) }) }));
  } else parts.push(t("stateActive"));
  if (user.must_change) parts.push(t("stateMustChange"));
  const keys = (user.passkeys || []).length;
  if (user.totp) parts.push(t("userTotpOn"));
  if (keys) parts.push(fmt("userKeys", { n: keys }));
  if (!user.totp && !keys) {
    // an administrator's second factor is TOTP or a key
    if (user.role === "admin") {
      parts.push(h("span", { class: "bad", text: t("userTotpAdminMissing") }));
    } else parts.push(t("userTotpOff"));
  }
  parts.push(user.last_login
    ? fmt("userLastLogin", { time: fmtTime(user.last_login) })
    : t("userNever"));
  parts.push(fmt("userSessions", { n: user.sessions }));
  const line = h("div", { class: "state" });
  parts.forEach((part, i) => {
    if (i) line.append(" · ");
    line.append(part);
  });
  return line;
}

function renderUsers(users, myName, errorMessage) {
  // a menu drawn for the list being replaced acts on stale state
  closeUserMenu(false);
  const name = h("input", {
    autocomplete: "off", autocapitalize: "none", spellcheck: "false",
    required: true,
  });
  const role = h("select", {},
    ...["viewer", "user", "admin"].map((r) => h("option", { value: r, text: t("role_" + r) })));
  const create = h("form", { class: "form new-user", novalidate: true },
    h("label", {}, t("fieldName"), name),
    h("label", {}, t("accountRole"), role),
    h("div", { class: "buttons" }, h("button", { type: "submit", text: t("addBtn") })));
  create.addEventListener("submit", (event) => {
    event.preventDefault();
    userAction("/api/users",
      jsonPost("/api/users", { name: name.value.trim(), role: role.value }));
  });
  const list = h("ul", { class: "user-list" }, ...users.map((user) => {
    const path = "/api/users/" + encodeURIComponent(user.name);
    const select = h("select", {},
      ...["viewer", "user", "admin"].map((r) =>
        h("option", { value: r, text: t("role_" + r), selected: r === user.role })));
    select.addEventListener("change", () =>
      userAction(path, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: select.value }),
      }));
    // everything else done to the account: one ⋯, a menu with reasons
    const more = h("button", {
      type: "button", class: "user-more", title: t("userMenuHint"),
      "aria-label": fmt("userMenuFor", { name: user.name }),
      "aria-haspopup": "menu", "aria-expanded": "false", text: "⋯",
    });
    more.addEventListener("click", (event) => {
      event.stopPropagation();
      if (userMenuButton === more) closeUserMenu(true);
      else openUserMenu(more, user, myName, users);
    });
    const keys = user.passkeys || [];
    const removeKey = (key) => userAction(
      path + "/passkeys/" + key.id, { method: "DELETE" }, null, user.name,
      fmt("confirmRemoveKey", { label: key.label || "#" + key.number, name: user.name }));
    return h("li", {},
      h("div", { class: "who" },
        h("strong", { text: user.name }),
        user.name === myName ? h("span", { class: "hint-line", text: "(" + t("userYou") + ")" }) : null,
        select, more),
      userState(user),
      keys.length ? keyList(keys, removeKey) : null);
  }));
  // replaceChildren would write a null out as the word "null"
  els.usersBody.replaceChildren(...[
    create,
    errorMessage ? h("p", { class: "error", role: "alert", text: errorMessage }) : null,
    usersNotice ? h("p", { class: "secret-once", text: usersNotice }) : null,
    list,
  ].filter(Boolean));
}

/* ---------- one account's actions ----------

   Six buttons per account in two rows — eighteen for three people, with
   "Delete" next to "Sign out everywhere" — became one ⋯ (v0.7.8), the
   cure the header got in v0.7.5. The menu is built from data by the
   same menuButtons as the node menu and the header's Actions; what
   cannot be done to this account is in it greyed out with the reason. */

const USER_GROUPS = ["account", "state", "danger"];

function userMenuItems(user, myName, users) {
  const path = "/api/users/" + encodeURIComponent(user.name);
  const enabledAdmins = users.filter((u) => u.role === "admin" && !u.disabled);
  // what the server would refuse anyway is not offered: one's own
  // account, and the last administrator, without whom the map is open
  const kept = user.name === myName
    ? t("reasonYourAccount")
    : user.role === "admin" && !user.disabled && enabledAdmins.length === 1
    ? t("reasonLastAdmin")
    : null;
  const post = (suffix, confirmKey) => () =>
    userAction(path + suffix, jsonPost(path + suffix), confirmKey, user.name);
  const patch = (body, confirmKey) => () => userAction(path, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, confirmKey, user.name);
  const now = Date.now() / 1000;
  return [
    { key: "password", group: "account", label: t("actNewPassword"),
      run: post("/password", "confirmNewPassword") },
    { key: "totp", group: "account", label: t("actResetTotp"),
      disabled: user.totp ? null : t("reasonNoTotp"),
      run: post("/reset-totp", "confirmResetTotp") },
    { key: "keys", group: "account", label: t("actResetKeys"),
      disabled: (user.passkeys || []).length ? null : t("reasonNoKeys"),
      run: post("/reset-passkeys", "confirmResetKeys") },
    { key: "sessions", group: "account", label: t("actSignOut"),
      disabled: user.sessions ? null : t("reasonNoSessions"),
      run: post("/logout") },
    { key: "unlock", group: "account", label: t("actUnlock"),
      disabled: user.locked_until > now ? null : t("reasonNotLocked"),
      run: post("/unlock") },
    user.disabled
      ? { key: "enable", group: "state", label: t("actEnable"),
          run: patch({ disabled: false }) }
      : { key: "disable", group: "state", label: t("actDisable"),
          disabled: kept, run: patch({ disabled: true }, "confirmDisable") },
    { key: "delete", group: "danger", label: t("actDelete"), danger: true,
      disabled: kept, run: () => userAction(path, { method: "DELETE" },
        "confirmDelete", user.name) },
  ];
}

let userMenuButton = null; // the ⋯ the open menu belongs to

function openUserMenu(button, user, myName, users) {
  closeUserMenu(false);
  closeMoreMenu(false);
  closeNodeMenu();
  userMenuButton = button;
  els.userMenu.setAttribute("aria-label", fmt("userMenuFor", { name: user.name }));
  els.userMenu.replaceChildren(
    ...menuButtons(userMenuItems(user, myName, users), USER_GROUPS, (item) => {
      closeUserMenu(true);
      item.run();
    })
  );
  els.userMenu.classList.remove("hidden");
  button.setAttribute("aria-expanded", "true");
  // under the button, its right edge on the button's
  const at = button.getBoundingClientRect();
  const width = els.userMenu.offsetWidth;
  const height = els.userMenu.offsetHeight;
  els.userMenu.style.left =
    Math.max(4, Math.min(at.right - width, window.innerWidth - width - 8)) + "px";
  // above the button when there is no room under it
  els.userMenu.style.top = (at.bottom + 6 + height > window.innerHeight
    ? Math.max(4, at.top - 6 - height) : at.bottom + 6) + "px";
  const first = els.userMenu.querySelector('[role="menuitem"]');
  if (first) first.focus();
}

function closeUserMenu(returnFocus) {
  if (els.userMenu.classList.contains("hidden")) return;
  els.userMenu.classList.add("hidden");
  els.userMenu.replaceChildren();
  const button = userMenuButton;
  userMenuButton = null;
  if (button) {
    button.setAttribute("aria-expanded", "false");
    if (returnFocus && button.isConnected) button.focus();
  }
}

/* ---------- layout freeze ---------- */

/* The force layout keeps running and slowly tidies the map up; anyone
   who prefers it still can stop it by hand. Dragging works either way. */
const FREEZE_KEY = "moonlan-freeze-layout";
let layoutFrozen = localStorage.getItem(FREEZE_KEY) === "1";

/* ---------- saved layout ----------

   Where a person put a node is kept on the server, not in this
   browser. A map of a network is a shared object: two people looking
   at one network have to see one picture, or "the switch at the bottom
   left" stops meaning anything. The language and the freeze toggle are
   personal settings and stay in localStorage; coordinates are not.

   Only what a person placed is kept. v0.7.1 also wrote down where the
   physics engine happened to leave every other node, once, and v0.7.3
   stopped it: that position went stale the moment somebody moved what
   the node hangs off, and after a reload a cloud of hosts started
   where its switch used to be and was dragged across half the map.
   Everything that is not pinned is laid out again on every load, from
   the pinned nodes outwards (seedPositions). */
let savedLayout = {};
let layoutSavedAt = 0;
// Nodes the mouse is holding right now, from dragStart to dragEnd
const dragging = new Set();

/* The layout is read with every refresh of the map, not once.

   v0.7 put the layout on the server so that everybody looking at the
   network sees one picture — and then read it once, when the page
   opened. A pin set on one screen never reached a page already open
   on another, and the wall monitor that stays open for weeks is the
   very screen that shared layout was for. */
let layoutLoaded = false;
// The last reset of the whole layout this page knows about
let knownClearedAt = 0;
// Node id -> when this page's own write about it was answered, or
// Infinity while it is still on its way. A refresh asked before that
// moment may carry the state from before the write, and must not
// "correct" the page back to it.
const settledAt = new Map();

function writing(ids) {
  for (const id of ids) settledAt.set(id, Infinity);
}

function written(ids) {
  const now = performance.now();
  for (const id of ids) settledAt.set(id, now);
}

/* Brings the page's copy of the layout up to what the server holds.
   `askedAt` is when the request for `data` was sent. Returns "rebuild"
   when somebody reset the whole layout, "render" otherwise. */
function takeServerLayout(data, askedAt) {
  const server = data.nodes || {};
  layoutSavedAt = data.saved_at || 0;
  const clearedAt = data.cleared_at || 0;
  if (!layoutLoaded) {
    layoutLoaded = true;
    knownClearedAt = clearedAt;
    savedLayout = keptOf(server);
    return "render";
  }
  if (clearedAt > knownClearedAt) {
    // Somebody reset the layout. Recognised by the reset itself, not
    // by rows going missing — a node released or forgotten by age
    // takes its row with it just the same. The page starts again from
    // what the server has now.
    knownClearedAt = clearedAt;
    savedLayout = keptOf(server);
    return "rebuild";
  }
  const stale = (id) =>
    dragging.has(id) || (settledAt.get(id) || 0) > askedAt;
  adoptLayout(server, stale);
  for (const id of Object.keys(savedLayout)) {
    if (id in server || stale(id)) continue;
    // Gone from the server without a reset: released somewhere, or
    // forgotten by age. The node stays where it is and goes back to
    // the physics engine.
    delete savedLayout[id];
  }
  return "render";
}

/* What of the server's layout this page keeps: the pinned positions,
   and the offsets recorded around them. A page still running an older
   script may write where its physics left a node; that is not a
   decision and is not taken. */
function keptOf(entries) {
  const kept = {};
  for (const [id, pos] of Object.entries(entries)) {
    if (pos.pinned) {
      kept[id] = { x: pos.x, y: pos.y, pinned: true };
    } else if (pos.anchor) {
      kept[id] = { x: pos.x, y: pos.y, pinned: false, anchor: pos.anchor };
    }
  }
  return kept;
}

/* Where a person put a node, if one did. A pinned node keeps being
   told where it is: it is out of the physics engine, so nothing else
   would hold it there. Every other node gets its start from
   seedPositions. */
function layoutFor(id) {
  const pos = savedLayout[id];
  if (!pos || !pos.pinned) return null;
  return { x: pos.x, y: pos.y, fixed: { x: true, y: true } };
}

/* Applied to every node as it is built: where it goes, whether the
   physics engine may move it, and whether to say a person put it
   there. */
function applyLayout(node) {
  const pinned = isPinned(node.id);
  // A node under the mouse belongs to the drag until it is dropped. A
  // refresh landing mid-drag would otherwise pull a pinned node back
  // to its saved spot, or hand a loose one to the physics engine while
  // somebody is still holding it.
  if (dragging.has(node.id)) {
    if (pinned) node.label = (node.label || "") + " " + t("pinnedMark");
    return node;
  }
  const pos = layoutFor(node.id);
  if (pos) Object.assign(node, pos);
  // Set on EVERY render, including to its off state. vis merges an
  // update field by field, so a property left out keeps whatever it
  // had — and a node just released would go on being held.
  node.fixed = pinned ? { x: true, y: true } : { x: false, y: false };
  if (pinned) node.label = (node.label || "") + " " + t("pinnedMark");
  return node;
}

/* Takes positions somebody else decided on: a node pinned elsewhere
   moves there and is held (layoutFor applies it on every render), a
   node released elsewhere is let go where it stands. */
function adoptLayout(entries, skip) {
  const kept = keptOf(entries);
  for (const id of Object.keys(entries)) {
    if (dragging.has(id) || (skip && skip(id))) continue;
    if (kept[id]) savedLayout[id] = kept[id];
    else delete savedLayout[id];
  }
}

/* One request at a time from whatever writes (v0.8.3). A double click on
   "Save" sent two PUTs and the journal said it twice; Enter in the name
   field did the same. While `fn` runs a second call with the same `key`
   does nothing and `buttons` are disabled; after it — done or failed —
   everything is as it was. A key and not only a button: a menu item is
   gone once clicked, and Enter is not a button at all. */
const busyKeys = new Set();

async function busy(key, fn, buttons = []) {
  if (busyKeys.has(key)) return undefined;
  busyKeys.add(key);
  const held = buttons.filter(Boolean);
  const before = held.map((button) => button.disabled);
  for (const button of held) button.disabled = true;
  try {
    return await fn();
  } finally {
    busyKeys.delete(key);
    held.forEach((button, i) => {
      button.disabled = before[i];
    });
  }
}

/* Pins one or more nodes where they are, and remembers it.

   Pinning says "the physics engine does not get to move this", and
   nothing more. A pinned node is still draggable: asking somebody to
   release a switch before nudging it a centimetre would be a rule
   about our bookkeeping, not about their map. */
async function pinNodes(positions, neighbours) {
  // Unless the caller says otherwise, the nodes around each one are
  // recorded as they stand right now: `P` and the menu pin a node
  // where it is, with what is around it.
  if (neighbours === undefined) neighbours = neighboursAround(Object.keys(positions));
  await storeByHand(positions, true, "pinning a node", neighbours);
}

/* The nodes that keep their place relative to a pinned one.

   Only what hangs DIRECTLY off it, and only structure: a group, a
   switch without SNMP, a switch or a bridge. A cloud of hosts folds
   itself into a ring round its switch whatever it starts from, so an
   offset per host would show nothing and cost hundreds of rows; a
   group keeps roughly to the side it started on, and a random start
   puts the "Offline" group on the right when a person left it on the
   left. Anything further out is laid out from these. */
const STRUCTURE_PREFIXES = ["sw:", "bridge:", "pseudo:", "trunk:", "offline:", "external:"];

function isStructure(id) {
  return STRUCTURE_PREFIXES.some((prefix) => id.startsWith(prefix));
}

/* anchor -> {node -> offset from it} for each of `anchors`, measured
   from `positions` (the current ones when not given). Nodes that are
   pinned themselves, or pinned in the same action, keep their own
   position and are left out. */
function neighboursAround(anchors, positions) {
  const at = positions || (network ? network.getPositions() : {});
  const pinning = new Set(anchors);
  const result = {};
  for (const anchor of anchors) {
    const base = at[anchor];
    if (!base) continue;
    const offsets = {};
    for (const [child, parent] of lastAnchors) {
      if (parent !== anchor || !isStructure(child)) continue;
      if (isPinned(child) || pinning.has(child) || !at[child]) continue;
      offsets[child] = { x: at[child].x - base.x, y: at[child].y - base.y };
    }
    result[anchor] = offsets;
  }
  return result;
}

/* The menu's "Remember the places around it": for a node already
   pinned, when the groups around it have been put right and the node
   itself is where it should be. */
async function rememberNeighbours(anchor) {
  return busy("neighbours:" + anchor, async () => {
    const neighbours = neighboursAround([anchor]);
    const count = Object.keys(neighbours[anchor] || {}).length;
    try {
      await api("/api/layout", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nodes: {}, neighbours: neighbours }),
      });
    } catch (e) {
      serviceLost("remembering the neighbours");
      return;
    }
    serviceBack();
    keepOffsets(neighbours);
    showToast(fmt("neighboursRemembered", { n: count }));
  }, []);
}

/* This page's copy of the offsets, after the server took them: each
   anchor's set replaces the one before it. */
function keepOffsets(neighbours) {
  for (const [anchor, offsets] of Object.entries(neighbours)) {
    for (const [id, pos] of Object.entries(savedLayout)) {
      if (pos.anchor === anchor) delete savedLayout[id];
    }
    for (const [id, offset] of Object.entries(offsets)) {
      savedLayout[id] = { x: offset.x, y: offset.y, pinned: false, anchor: anchor };
    }
  }
}

function pinNode(id, x, y) {
  return pinNodes({ [id]: { x: x, y: y } });
}

/* Releases nodes back to the physics engine. No confirmation: this is
   cheap and reversible in one gesture, and a dialog in front of it
   only makes the cheap thing feel expensive.

   The server forgets a released node's position: only what a person
   placed is kept. It stays where it stands on this page, and on the
   next load it is laid out from the pinned nodes like everything
   else. */
async function unpinNodes(ids) {
  const at = network ? network.getPositions(ids) : {};
  const positions = {};
  for (const id of ids) if (at[id]) positions[id] = at[id];
  await storeByHand(positions, false, "releasing a node");
}

/* One action of the hand — a drop, `P`, a menu item — whatever the
   number of nodes: one request, and one line in the journal. */
async function storeByHand(positions, pinned, what, neighbours) {
  return busy("layout:" + pinned + ":" + Object.keys(positions).sort().join(","), async () => {
    const ids = Object.keys(positions);
    if (!ids.length) return;
    const nodes = {};
    for (const id of ids) {
      nodes[id] = { x: positions[id].x, y: positions[id].y, pinned: pinned };
      if (pinned) {
        savedLayout[id] = nodes[id];
      } else {
        delete savedLayout[id];
        // with nothing pinned to measure from, the offsets around it
        // mean nothing; the server forgets them too
        for (const [other, pos] of Object.entries(savedLayout)) {
          if (pos.anchor === id) delete savedLayout[other];
        }
      }
    }
    writing(ids);
    try {
      await api("/api/layout", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nodes: nodes, neighbours: neighbours || {} }),
      });
    } catch (e) {
      written(ids);
      serviceLost(what);
      return;
    }
    written(ids);
    serviceBack();
    if (neighbours) keepOffsets(neighbours);
    if (!layoutSavedAt) layoutSavedAt = Date.now() / 1000;
    renderGraph();
    updateScanStatus();
  }, [document.getElementById("unpin-btn")]);
}

function unpinNode(id) {
  return unpinNodes([id]);
}

function isPinned(id) {
  return !!(savedLayout[id] && savedLayout[id].pinned);
}

// What stood around the dragged nodes when the drag began (arrange
// mode only), recorded with them at the drop
let dragNeighbours = null;

/* Lets the mouse move pinned nodes.

   Pinning is vis's `fixed`, and `fixed` refuses the hand as firmly as
   it refuses the physics engine: at the start of a drag vis records
   each selected node's `fixed`, and while dragging moves only the
   nodes it recorded as loose. v0.7.1 said a pinned node stays
   draggable and it did not — the drag was reported, the same
   coordinates were saved again, and the box stayed where it was.

   vis emits `dragStart` BEFORE it takes that record, so the pinned
   nodes of the selection are loosened here and it records them as
   free. For the length of the drag vis holds every dragged node itself,
   so nothing else moves them; dragEnd pins them again. */
function loosenForDrag(ids) {
  for (const id of ids) dragging.add(id);
  // In arrange mode the drag will pin these nodes, and what is around
  // each of them is recorded as it stands NOW, before the drag: that
  // is the picture the person is placing. By the drop the groups have
  // only started to follow, and their offsets would be the lag.
  dragNeighbours = arrangeMode ? neighboursAround(ids) : null;
  const held = ids.filter(isPinned);
  if (!held.length) return;
  nodesDs.update(held.map((id) => ({ id: id, fixed: { x: false, y: false } })));
  // The record is taken the moment this handler returns, so the
  // update has to have reached the node by then, not merely been
  // queued. A DataSet delivers synchronously today; if that ever
  // changes, the drag would silently go back to doing nothing — so
  // check, and set it on the node directly if it did not arrive.
  for (const id of held) {
    const body = visInternals.fixed === true && network.body.nodes[id];
    if (body && (body.options.fixed.x || body.options.fixed.y)) {
      console.warn("MoonLan: pinned node not loosened by the data update; setting it directly", id);
      body.options.fixed.x = false;
      body.options.fixed.y = false;
    }
  }
}

async function resetLayout() {
  return busy("layout-reset", async () => {
    if (!window.confirm(t("resetLayoutConfirm"))) return;
    let answer;
    try {
      answer = await (await api("/api/layout", { method: "DELETE" })).json();
    } catch (e) {
      serviceLost("clearing the layout");
      return;
    }
    serviceBack();
    // our own reset, not somebody else's: the next refresh must not
    // start the map over a second time
    knownClearedAt = Math.max(knownClearedAt, answer.cleared_at || 0);
    savedLayout = {};
    layoutSavedAt = 0;
    // Forgetting the positions on the server is only half of it. vis
    // keeps x and y on the node it has already built, and nothing in
    // the update path can take them away again — `setOptions` assigns a
    // coordinate only when one is given, so leaving it out means "keep
    // what you have". The nodes went on standing exactly where they
    // were, `stabilize()` restarted the physics from those same points,
    // and the reset looked like it had done nothing at all until the
    // page was reloaded.
    //
    // So the node set is rebuilt rather than updated: that is what
    // makes vis drop the old bodies and lay the map out afresh.
    rebuildGraph();
    updateScanStatus();
    if (network) network.stabilize();
  }, []);
}

/* Who a node hangs off, taken from the edges that were just built.

   Every node but the root has exactly one edge coming into it from
   the thing it belongs to — a host from its switch or from the group
   node standing in for one, a group from its switch, a switch from
   its parent in the tree. Reading it off the edges rather than
   re-deriving it per node kind means the two can never disagree.  */
function anchorMap(edges) {
  const anchors = new Map();
  for (const edge of edges) {
    if (!anchors.has(edge.to)) anchors.set(edge.to, edge.from);
  }
  return anchors;
}

/* The same map, kept from the last render: the selection needs to
   know what hangs off a container, and re-deriving it from the
   topology would be a second set of rules that can disagree with the
   edges actually drawn. */
let lastAnchors = new Map();

/* Everything hanging off one container — a switch, a switch without
   SNMP, a "beyond the trunk" or "offline" group — following the
   chain down through further containers.

   Switches are left out on purpose: this is "take what is on this
   box", not "take this branch". Selecting a switch's whole subtree
   would move half the map on the first drag.  */
function devicesUnder(id) {
  const children = new Map();
  for (const [child, anchor] of lastAnchors) {
    if (!children.has(anchor)) children.set(anchor, []);
    children.get(anchor).push(child);
  }
  const found = new Set();
  const stack = [id];
  while (stack.length) {
    for (const child of children.get(stack.pop()) || []) {
      if (found.has(child) || child.startsWith("sw:")) continue;
      found.add(child);
      stack.push(child);
    }
  }
  return [...found];
}

/* A number from a string, so a node's offset is the same in every
   browser and does not jump between renders. Math.random() would put
   the same device in a different spot for each person looking. */
function idHash(id) {
  // FNV-1a, then the murmur3 finaliser. The plain `hash * 31 + c` this
  // replaces kept the last character in the lowest bits almost as it
  // was, so sw:10.0.0.21 … sw:10.0.0.24 — ids that differ only there —
  // got angles a degree apart and radii a unit apart, and four
  // switches started on one spot.
  let hash = 0x811c9dc5;
  for (let i = 0; i < id.length; i++) {
    hash ^= id.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  hash ^= hash >>> 16;
  hash = Math.imul(hash, 0x85ebca6b);
  hash ^= hash >>> 13;
  hash = Math.imul(hash, 0xc2b2ae35);
  hash ^= hash >>> 16;
  return hash >>> 0;
}

/* Gives a position to every node that has none.

   Only pinned nodes have a stored position, so on every load this lays
   out everything else, and it does it from the pinned nodes outwards:
   first the nodes hanging directly off a pinned one, then the level
   below them, and so on. Each goes next to what it hangs off, at an
   offset derived from its id — the same spot for everybody, so two
   pages opening one map start from one picture and the physics
   engine, which is not random, takes both the same way.

   Two cases have no pinned node to start from. A switch nobody pinned
   above switches somebody did belongs among them, not wherever a hash
   would put it, so it starts in the middle of its placed children. And
   a part of the map with nothing placed anywhere in it starts around
   the middle of what already stands. */
function seedPositions(nodes, edges) {
  const anchors = anchorMap(edges);
  lastAnchors = anchors;
  const children = new Map();
  for (const [child, anchor] of anchors) {
    if (!children.has(anchor)) children.set(anchor, []);
    children.get(anchor).push(child);
  }
  const known = new Map();
  const existing = network ? network.getPositions() : {};
  for (const node of nodes) {
    if (node.x != null && node.y != null) {
      known.set(node.id, { x: node.x, y: node.y });
    } else if (existing[node.id]) {
      known.set(node.id, existing[node.id]);
    }
  }
  const place = (node, x, y) => {
    node.x = x;
    node.y = y;
    known.set(node.id, { x: x, y: y });
  };
  const around = (node, at) => {
    const hash = idHash(node.id);
    // angle and radius from different bits, or they move together
    const angle = ((hash % 360) * Math.PI) / 180;
    const radius = 70 + ((hash >>> 16) % 50);
    place(node, at.x + Math.cos(angle) * radius, at.y + Math.sin(angle) * radius);
  };
  const middle = (points) => {
    let x = 0;
    let y = 0;
    for (const point of points) {
      x += point.x;
      y += point.y;
    }
    return points.length
      ? { x: x / points.length, y: y / points.length }
      : { x: 0, y: 0 };
  };

  let left = nodes.filter((node) => !known.has(node.id));
  while (left.length) {
    // Outwards, one level at a time: everything whose anchor already
    // stands, taken together before any of them is placed
    const ready = left.filter((node) => known.has(anchors.get(node.id)));
    if (ready.length) {
      for (const node of ready) {
        const at = known.get(anchors.get(node.id));
        // Where a person saw it round a pinned node, if that is still
        // what it hangs off: an offset moves with its anchor, so a
        // group comes back on the side it was left on even after the
        // switch has been moved. A device that has changed switches
        // has nothing to do with the old one's offset.
        const offset = savedLayout[node.id];
        if (
          offset && offset.anchor &&
          offset.anchor === anchors.get(node.id) && isPinned(offset.anchor)
        ) {
          place(node, at.x + offset.x, at.y + offset.y);
        } else {
          around(node, at);
        }
      }
      left = left.filter((node) => !known.has(node.id));
      continue;
    }
    // Upwards: among its children that already stand
    const parent = left.find((node) =>
      (children.get(node.id) || []).some((child) => known.has(child))
    );
    if (parent) {
      const placedKids = children
        .get(parent.id)
        .filter((child) => known.has(child))
        .map((child) => known.get(child));
      around(parent, middle(placedKids));
      left = left.filter((node) => node !== parent);
      continue;
    }
    // Nothing placed touches what is left: start its root — or, for a
    // ring with no root, its first node — around the middle of the map
    const start = left.find((node) => !anchors.has(node.id)) || left[0];
    const centre = middle([...known.values()]);
    const hash = idHash(start.id);
    place(
      start,
      centre.x + (hash % 400) - 200,
      centre.y + ((hash >>> 16) % 400) - 200
    );
    left = left.filter((node) => node !== start);
  }
  return nodes;
}

/* Throws the drawn nodes away and builds them again from the data.

   `renderGraph` updates in place on purpose — it runs every thirty
   seconds and must not disturb the camera or the positions. This is
   the opposite operation, and it exists for the one case that needs
   it. */
function rebuildGraph() {
  if (!network) {
    renderGraph();
    return;
  }
  const { nodes, edges } = buildGraphData();
  seedPositions(nodes, edges);
  nodesDs.clear();
  edgesDs.clear();
  nodesDs.add(nodes);
  edgesDs.add(edges);
}

/* ---------- context menu ----------

   The items are data, not markup: each one says what it is called,
   which group it belongs to, whether it can run and why not, and what
   it does. v0.7.1 built this frame for exactly what goes in it now —
   diagnostic actions, links, the operator's own items from
   config.yaml — each of them more of the same data.

   Every item is handed the whole selection, because a menu that works
   on one node when three are chosen is a menu that surprises people.

   An item that cannot run is shown greyed out with the reason — "IP
   unknown", "no traceroute on the server" — rather than left out. The
   same discipline as "no answer is not zero": what is missing has to
   be visible as missing. */

// In the order the menu shows them, a separator between each
const MENU_GROUPS = ["actions", "links", "custom", "layout"];

// Nodes that stand for a place rather than a device: they have no
// address of their own, and pinging one means pinging what is on it
const GROUP_PREFIXES = ["pseudo:", "trunk:", "offline:", "external:"];

function isGroupNode(id) {
  return GROUP_PREFIXES.some((prefix) => id.startsWith(prefix));
}

/* What a ping can be sent to: devices and switches, never the groups
   standing in for places. */
function addressable(ids) {
  return ids.filter((id) => !isGroupNode(id));
}

/* The reason a link or an action cannot run for want of a value. */
function missingReason(field) {
  return t("reasonNo_" + field);
}

function menuItemsFor(ids, info, tools) {
  const single = ids.length === 1 ? ids[0] : null;
  const node = info ? info.node : null;
  const items = [];
  // The service did not answer: the actions are shown, and say why
  // they cannot run. Links need the node's data and are left out.
  const offline = tools ? null : t("reasonNoService");
  const tooMany = (n) =>
    tools && n > tools.max_targets
      ? fmt("reasonTooMany", { n: n, limit: tools.max_targets })
      : null;
  const noPing = tools && !tools.ping ? t("reasonNoPing") : null;
  // the server acting — a ping, a pin — is for users; a viewer sees
  // the item, greyed out with the role it needs
  const noRole = needRole("user");

  if (single && !isGroupNode(single)) {
    const noIp = node && !node.ip ? missingReason("ip") : null;
    items.push({
      key: "ping", group: "actions", label: t("menuPing"),
      disabled: noRole || offline || noPing || noIp,
      run: () => startAction("ping", [single]),
    });
    items.push({
      key: "traceroute", group: "actions", label: t("menuTraceroute"),
      disabled:
        noRole || offline ||
        (tools && !tools.traceroute ? t("reasonNoTraceroute") : null) || noIp,
      run: () => startAction("traceroute", [single]),
    });
  } else if (single) {
    const devices = addressable(devicesUnder(single));
    items.push({
      key: "pingGroup", group: "actions",
      label: fmt("menuPingGroup", { n: devices.length }),
      disabled:
        noRole || offline || noPing ||
        (devices.length ? null : t("reasonNoDevices")) ||
        tooMany(devices.length),
      run: () => startAction("ping", devices, nodeTitle(single)),
    });
  } else {
    const targets = addressable(ids);
    items.push({
      key: "pingSelection", group: "actions",
      label: fmt("menuPingSelection", { n: targets.length }),
      disabled:
        noRole || offline || noPing ||
        (targets.length ? null : t("reasonNoDevices")) ||
        tooMany(targets.length),
      run: () => startAction("ping", targets),
    });
    // No traceroute for a crowd: its output means something only for
    // one destination at a time
  }

  if (single && node && node.kind !== "group") {
    for (const link of info.links) {
      items.push({
        key: link.key, group: "links", label: t("menu_" + link.key),
        disabled: link.missing ? missingReason(link.missing) : null,
        run: () => openLink(link.url),
      });
    }
    // Remote desktop is for computers, not for network boxes
    if (node.kind === "host") {
      items.push({
        key: "rdp", group: "links", label: t("menu_rdp"),
        disabled: node.ip ? null : missingReason("ip"),
        run: () => downloadRdp(node),
      });
    }
    items.push({
      key: "copyIp", group: "links", label: t("menuCopyIp"),
      disabled: node.ip ? null : missingReason("ip"),
      run: () => copyText(node.ip),
    });
    items.push({
      key: "copyMac", group: "links", label: t("menuCopyMac"),
      disabled: node.mac ? null : missingReason("mac"),
      run: () => copyText(node.mac),
    });
  }

  if (single && info) {
    for (const link of info.custom) {
      items.push({
        key: "custom", group: "custom", label: link.label,
        disabled: link.missing ? missingReason(link.missing) : null,
        run: () => openLink(link.url),
      });
    }
  }

  const loose = ids.filter((id) => !isPinned(id));
  items.push({
    key: "pin", group: "layout",
    label: loose.length ? t("menuPin") : t("menuUnpin"),
    disabled: noRole,
    run: () => togglePinOnSelection(),
  });
  // For a node already pinned: the groups round it have been put right
  // and the node itself should stay where it is
  if (single && isPinned(single)) {
    const around = neighboursAround([single])[single] || {};
    items.push({
      key: "neighbours", group: "layout", label: t("menuRememberNeighbours"),
      disabled:
        noRole || (Object.keys(around).length ? null : t("reasonNoNeighbours")),
      run: () => rememberNeighbours(single),
    });
  }
  if (single) {
    items.push({
      key: "note", group: "layout", label: t("noteBtn"),
      disabled: noteReason(single),
      run: () => {
        setSelectedNode(single);
        openNoteEditor(single);
      },
    });
  }
  // one node, one card: there is nothing to show for a crowd
  if (single) {
    items.push({
      key: "card", group: "layout", label: t("menuOpenCard"),
      run: () => {
        setSelectedNode(single);
        showDetails(single);
      },
    });
  }
  if (single && single.startsWith("sw:")) {
    items.push({
      key: "ports", group: "layout", label: t("menuOpenPorts"),
      run: () => openPorts(single.slice("sw:".length)),
    });
  }
  return items;
}

/* ---------- the header's Actions ----------

   What does something to the whole network or to everybody's picture:
   a rescan, the accounts, a reset. The reset stands below its own line
   — the one thing in it that cannot be undone must not sit among the
   harmless ones — and keeps its confirmation. What a role may not do is
   in the menu all the same, greyed out with the role it needs: a viewer
   opens it and finds every item grey, with the reason. */

const MORE_GROUPS = ["network", "notes", "danger", "about"];

function moreItems() {
  const scanning = isScanning || topology.scanning;
  return [
    {
      key: "rescan", group: "network", label: t("rescanBtn"),
      disabled: needRole("user") || (scanning ? t("reasonScanning") : null),
      run: rescan,
    },
    // reads only: for every role
    { key: "exportMap", group: "network", label: t("menuExportMap"), run: openExportDialog },
    {
      key: "users", group: "network", label: t("usersBtn"),
      disabled: accountsReason(),
      run: toggleUsers,
    },
    {
      key: "orphans", group: "notes",
      label: fmt("menuOrphans", { n: orphanNotes().length }),
      disabled: orphanNotes().length ? null : t("reasonNoOrphans"),
      run: openOrphans,
    },
    {
      key: "import", group: "notes", label: t("menuImportNotes"),
      disabled: needRole("admin"),
      run: chooseNotesFile,
    },
    { key: "export", group: "notes", label: t("menuExportNotes"), run: openNotesExport },
    {
      key: "reset", group: "danger", label: t("resetLayoutBtn"), danger: true,
      disabled: needRole("admin"),
      run: resetLayout,
    },
    // last, on its own, for everybody — sign-in off included
    { key: "about", group: "about", label: t("menuAbout"), run: openAbout },
  ];
}

function openMoreMenu() {
  closeNodeMenu();
  closePortLabelsMenu(false);
  els.moreMenu.replaceChildren(...menuButtons(moreItems(), MORE_GROUPS, (item) => {
    closeMoreMenu(true);
    item.run();
  }));
  els.moreMenu.classList.remove("hidden");
  els.moreBtn.setAttribute("aria-expanded", "true");
  // under the button, its right edge on the button's
  const button = els.moreBtn.getBoundingClientRect();
  const width = els.moreMenu.offsetWidth;
  menuUnder(els.moreMenu, button.bottom + 6);
  els.moreMenu.style.left =
    Math.max(4, Math.min(button.right - width, window.innerWidth - width - 8)) + "px";
  const first = els.moreMenu.querySelector('[role="menuitem"]');
  if (first) first.focus();
}

/* A menu under its button, no taller than the room left under it: on a
   low phone screen it scrolls inside itself (v0.8.2) */
function menuUnder(menu, top) {
  menu.style.top = top + "px";
  menu.style.maxHeight = Math.max(120, window.innerHeight - top - 8) + "px";
}

function closeMoreMenu(returnFocus) {
  if (els.moreMenu.classList.contains("hidden")) return;
  els.moreMenu.classList.add("hidden");
  els.moreMenu.replaceChildren();
  els.moreBtn.setAttribute("aria-expanded", "false");
  if (returnFocus) els.moreBtn.focus();
}

function closeNodeMenu() {
  menuSerial++;
  els.nodeMenu.classList.add("hidden");
  els.nodeMenu.replaceChildren();
}

/* Opens the menu once the server has said what the node offers. Every
   open gets a number, so an answer arriving after the menu was closed
   or opened somewhere else is dropped rather than drawn. */
let menuSerial = 0;
// What the server last said it can run: tools, the ceiling, demo mode
let lastTools = null;

async function openNodeMenu(ids, at) {
  const serial = ++menuSerial;
  let info = null;
  let tools = null;
  try {
    if (ids.length === 1) {
      const response = await api(
        "/api/node-menu?id=" + encodeURIComponent(ids[0])
      );
      if (response.ok) {
        info = await response.json();
        tools = info.tools;
      } else {
        // a node the service no longer has: the map is older than the
        // service's picture of it; the layout items still work
        tools = (await (await api("/api/actions")).json());
      }
    } else {
      tools = await (await api("/api/actions")).json();
    }
  } catch (e) {
    serviceLost("the node menu");
  }
  if (serial !== menuSerial) return;
  if (tools) lastTools = tools;
  renderNodeMenu(ids, menuItemsFor(ids, info, tools), at);
}

/* The items of a menu built from data: the groups in their order with a
   line between them, and an item that cannot run greyed out with the
   reason. Both the node menu and the header's Actions use it. */
function menuButtons(items, groups, pick) {
  const parts = [];
  let drawn = 0;
  for (const group of groups) {
    const members = items.filter((item) => item.group === group);
    if (!members.length) continue;
    if (drawn++) {
      const sep = document.createElement("div");
      sep.className = "context-menu-sep";
      sep.setAttribute("role", "separator");
      parts.push(sep);
    }
    for (const item of members) {
      const button = document.createElement("button");
      button.type = "button";
      button.setAttribute("role", "menuitem");
      button.dataset.key = item.key;
      button.textContent = item.label;
      if (item.checked !== undefined) {
        // one of several modes, the current one ticked — or a switch
        // of its own, ticked when on
        button.setAttribute("role", item.checkbox ? "menuitemcheckbox" : "menuitemradio");
        button.setAttribute("aria-checked", item.checked ? "true" : "false");
        button.classList.add("tick");
      }
      if (item.danger) button.classList.add("danger");
      if (item.disabled) {
        // not the `disabled` attribute: a disabled button shows no
        // tooltip in some browsers, and the reason is the point — and
        // it could not be reached with the arrow keys either
        button.classList.add("off");
        button.setAttribute("aria-disabled", "true");
        button.title = item.disabled;
        const why = document.createElement("span");
        why.className = "why";
        why.textContent = item.disabled;
        button.append(why);
      }
      button.addEventListener("click", () => {
        if (item.disabled) return;
        pick(item);
      });
      parts.push(button);
    }
  }
  return parts;
}

/* Arrow keys, Home and End move through a menu's items; Escape closes
   it (with the focus back where the menu came from); Tab leaves it. */
function menuKeys(event, menu, close) {
  const items = [...menu.querySelectorAll('[role^="menuitem"]')];
  if (!items.length) return;
  const at = items.indexOf(document.activeElement);
  let next = null;
  if (event.key === "ArrowDown") next = (at + 1) % items.length;
  else if (event.key === "ArrowUp") next = (at - 1 + items.length) % items.length;
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = items.length - 1;
  else if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    close(true);
    return;
  } else if (event.key === "Tab") {
    close(false);
    return;
  }
  if (next !== null) {
    event.preventDefault();
    items[next].focus();
  }
}

function renderNodeMenu(ids, items, at) {
  const head = document.createElement("div");
  head.className = "context-menu-head";
  head.textContent =
    ids.length === 1 ? nodeTitle(ids[0]) : fmt("menuSelected", { n: ids.length });
  const parts = [head, ...menuButtons(items, MENU_GROUPS, (item) => {
    closeNodeMenu();
    item.run();
  })];
  els.nodeMenu.replaceChildren(...parts);
  els.nodeMenu.classList.remove("hidden");
  // Keep it on screen: near the bottom or the right edge it flips
  const box = els.nodeMenu.getBoundingClientRect();
  const x = Math.min(at.x, window.innerWidth - box.width - 8);
  const y = Math.min(at.y, window.innerHeight - box.height - 8);
  els.nodeMenu.style.left = Math.max(4, x) + "px";
  els.nodeMenu.style.top = Math.max(4, y) + "px";
}

/* ---------- links, remote desktop, clipboard ---------- */

/* A web interface opens in a new tab; ssh://, winbox:// and the rest
   are handed to whatever program the system has for them, without
   leaving an empty tab behind. The address was checked against the
   scheme whitelist and filled in by the server. */
function openLink(url) {
  const a = document.createElement("a");
  a.href = url;
  if (/^https?:/i.test(url)) {
    a.target = "_blank";
    a.rel = "noopener noreferrer";
  }
  document.body.append(a);
  a.click();
  a.remove();
}

/* Browsers do not agree on rdp:// at all. A .rdp file is the standard
   way, and it opens wherever there is a remote desktop client. Built
   here: it holds nothing but the address. */
function downloadRdp(node) {
  const blob = new Blob(["full address:s:" + node.ip + "\r\n"], {
    type: "application/x-rdp",
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = (node.name || node.ip).replace(/[^A-Za-z0-9._-]+/g, "_") + ".rdp";
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

/* navigator.clipboard exists only on a secure page — https, or
   localhost. MoonLan is usually opened over plain http by address,
   where it is simply undefined, so the old way stays as the fallback. */
async function copyText(text, shown = text) {
  let done = false;
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      done = true;
    } catch (e) {
      done = false;
    }
  }
  if (!done) {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    try {
      done = document.execCommand("copy");
    } catch (e) {
      done = false;
    }
    area.remove();
  }
  showToast(done ? fmt("copied", { text: shown }) : fmt("copyFailed", { text: shown }));
}

let toastTimer = null;

function showToast(text) {
  const toast = document.getElementById("toast");
  toast.textContent = text;
  toast.classList.add("shown");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("shown"), 2500);
}

/* ---------- ping and traceroute ----------

   Run on the MoonLan machine: a browser cannot ping, and the point is
   to see the network from where MoonLan sees it. The page asks by node
   id; the server finds the address itself, refuses what it does not
   know, and runs the tool in the background. The page asks how it is
   going once a second — the map is never held up waiting. */

let actionJob = null;      // the job the panel shows
let actionRefusal = null;  // …or why the server would not start it
let actionTitle = null;   // {action, name, n} for the panel head
let actionTimer = null;

async function startAction(action, ids, title) {
  clearTimeout(actionTimer);
  actionJob = null;
  actionRefusal = null;
  // kept in parts, so the head can be worded again in another language
  actionTitle = {
    action: action,
    name: title || (ids.length === 1 ? nodeTitle(ids[0]) : ""),
    n: ids.length,
  };
  els.actions.classList.remove("hidden");
  renderAction();
  let answer;
  try {
    const response = await api("/api/actions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: action, nodes: ids }),
    });
    answer = await response.json();
  } catch (e) {
    serviceLost("starting " + action);
    answer = { error: "no_service" };
  }
  if (answer.error) {
    actionRefusal = answer;
  } else {
    actionJob = answer;
    followAction();
  }
  renderAction();
}

function followAction() {
  clearTimeout(actionTimer);
  if (!actionJob || actionJob.done) return;
  const id = actionJob.id;
  actionTimer = setTimeout(async () => {
    let job;
    try {
      job = await (await api("/api/actions/" + id)).json();
    } catch (e) {
      serviceLost("the result of a ping");
      followAction();
      return;
    }
    // closed, or replaced by another run, while this was on its way
    if (!actionJob || actionJob.id !== id) return;
    if (job.error) {
      actionRefusal = job;
      actionJob = null;
    } else {
      actionJob = job;
      followAction();
    }
    renderAction();
  }, 1000);
}

function closeActions() {
  clearTimeout(actionTimer);
  actionJob = null;
  actionRefusal = null;
  els.actions.classList.add("hidden");
}

function refusalText(answer) {
  switch (answer.error) {
    case "too_many_targets":
      return fmt("refuseTooMany", { n: answer.count, limit: answer.limit });
    case "busy":
      return fmt("refuseBusy", { limit: answer.limit });
    case "tool_missing":
      return fmt("refuseNoTool", { tool: answer.tool });
    case "unknown_nodes":
      return (answer.addresses || []).length
        ? fmt("refuseAddress", { list: answer.addresses.join(", ") })
        : fmt("refuseUnknown", { list: (answer.nodes || []).join(", ") });
    case "traceroute_one":
      return t("refuseTraceOne");
    case "no_targets":
      return t("reasonNoDevices");
    case "unknown_job":
      return t("refuseJobGone");
    case "no_service":
      return t("reasonNoService");
    default:
      return fmt("refuseOther", { error: answer.error });
  }
}

/* What the continuous monitoring knows about the same address — shown
   as the second source it is, not merged into the one-off result. */
function monitorText(monitor) {
  if (!monitor || monitor.ping_up == null) return t("monitorNone");
  if (monitor.ping_up) {
    return fmt("monitorUp", { time: fmtTime(monitor.last_ping_ok) });
  }
  return monitor.last_ping_ok
    ? fmt("monitorDown", { time: fmtTime(monitor.last_ping_ok) })
    : t("monitorNever");
}

function pingVerdict(target) {
  if (target.status === "no_ip") return ["verdict-other", t("verdictNoIp")];
  if (target.status === "queued" || target.status === "running") {
    return ["verdict-other", "…"];
  }
  if (target.status === "timeout") return ["verdict-none", t("verdictTimeout")];
  if (!target.result) return ["verdict-other", t("verdictFailed")];
  if (target.result.received === 0) return ["verdict-none", t("verdictNoReply")];
  if (target.result.loss > 0) return ["verdict-partial", t("verdictPartial")];
  return ["verdict-ok", t("verdictOk")];
}

function fmtMs(value) {
  return value == null ? "—" : value + " " + t("ms");
}

function renderAction() {
  els.actionsTitle.textContent = actionTitle
    ? t(actionTitle.action === "ping" ? "menuPing" : "menuTraceroute") +
      " · " +
      (actionTitle.name || fmt("actionsNodes", { n: actionTitle.n }))
    : "";
  // names and addresses come from the network, the output from ping or
  // traceroute: all of it escaped by html`…`
  const show = (markup) => {
    els.actionsBody.innerHTML = markup.text;
  };
  if (actionRefusal) {
    show(html`<p class="action-status refused">${refusalText(actionRefusal)}</p>`);
    return;
  }
  const job = actionJob;
  if (!job) {
    show(html`<p class="action-status running">${t("actionStarting")}</p>`);
    return;
  }
  const body = [html`<p class="action-status ${job.done ? "" : "running"}">${
    job.done ? t("actionDone") : t("actionRunning")}</p>`];
  if (lastTools && lastTools.simulated) {
    body.push(html`<p class="hint">${t("actionSimulated")}</p>`);
  }
  if (job.action === "ping" && job.targets.length > 1) {
    const answered = job.targets.filter(
      (target) => target.result && target.result.received > 0
    ).length;
    body.push(html`<p class="hint">${fmt("pingSummary", { n: answered, total: job.targets.length })}</p>`);
    const rows = job.targets.map((target) => {
      const [cls, verdict] = pingVerdict(target);
      const result = target.result || {};
      return html`<tr><td>${target.name}</td><td class="mono">${target.ip || "—"}</td><td class="num">${
        result.loss == null ? "—" : result.loss + "%"}</td><td class="num">${fmtMs(result.avg)}</td><td class="${cls}">${verdict}</td></tr>`;
    });
    body.push(html`<table><thead><tr><th>${t("colNode")}</th><th>IP</th><th>${t("colLoss")}</th><th>${
      t("colRtt")}</th><th>${t("colVerdict")}</th></tr></thead><tbody>${rows}</tbody></table>`);
  } else {
    const target = job.targets[0];
    const rows = [html`<dt>${t("actionTarget")}</dt><dd>${target.name} · ${target.ip || "—"}</dd>`];
    if (job.action === "ping") {
      const result = target.result;
      const [cls, verdict] = pingVerdict(target);
      rows.push(html`<dt>${t("actionThisRun")}</dt><dd><span class="${cls}">${verdict}</span>${
        result
          ? " · " + fmt("pingLoss", { loss: result.loss, received: result.received, sent: result.sent }) +
            " · " + t("pingRtt") + " " + fmtMs(result.min) + " / " + fmtMs(result.avg) + " / " + fmtMs(result.max)
          : ""}</dd>`);
      rows.push(html`<dt>${t("actionMonitor")}</dt><dd>${monitorText(target.monitor)}</dd>`);
    } else {
      rows.push(html`<dt>${t("actionTool")}</dt><dd>${job.tool}</dd>`);
    }
    body.push(html`<dl>${rows}</dl>`);
    if (target.output) body.push(html`<pre>${target.output}</pre>`);
  }
  show(html`${body}`);
}

/* ---------- arrange mode ----------

   Dragging a node is how people look at a map: pull the cloud of hosts
   aside, lift a switch out of the tangle, see what is behind it. v0.7
   read every one of those as a decision and pinned the node for good,
   so the map slowly set like concrete without anyone choosing it.

   Placing a node is a different act from looking at one, and it now
   has a mode of its own. Deliberately not remembered between
   sessions: a mode that comes back on its own after a reload is the
   same trap in another shape. */
let arrangeMode = false;

function applyArrangeMode() {
  els.arrangeBtn.classList.toggle("active", arrangeMode);
  els.arrangeBtn.setAttribute("aria-pressed", arrangeMode ? "true" : "false");
  const name = arrangeMode ? t("arrangeOnBtn") : t("arrangeBtn");
  const pinned = pinnedCount();
  labelIcon(
    els.arrangeBtn, name,
    (els.arrangeBtn.dataset.why || name) + "\n\n" + t("arrangeHint") +
      (pinned
        ? "\n\n" + fmt("arrangePinned", { n: pinned }) + " " + t("layoutPinnedHint")
        : "")
  );
  // The map itself changes, not only the button: a mode you can
  // forget you are in is a mode that edits the map by accident.
  els.network.classList.toggle("arranging", arrangeMode);
}

function toggleArrangeMode() {
  arrangeMode = !arrangeMode;
  applyArrangeMode();
}

function applyFreeze() {
  if (network) network.setOptions({ physics: !layoutFrozen });
  // an icon: what it does now is in the tooltip, and the lit button
  // says it is on
  labelIcon(els.freezeBtn, layoutFrozen ? t("unfreezeBtn") : t("freezeBtn"));
  els.freezeBtn.classList.toggle("active", layoutFrozen);
  els.freezeBtn.setAttribute("aria-pressed", layoutFrozen ? "true" : "false");
}

/* The words behind an icon: the tooltip and what a screen reader says */
function labelIcon(button, label, title) {
  button.setAttribute("aria-label", label);
  button.title = title || label;
}

function toggleFreeze() {
  layoutFrozen = !layoutFrozen;
  localStorage.setItem(FREEZE_KEY, layoutFrozen ? "1" : "0");
  applyFreeze();
}

/* ---------- the list on a phone ----------

   At 720 px and narrower the list and the search stand over the map
   and take up to 40 % of the screen; a bar between the header and the
   map folds them away. Wider than that there is no bar, and the side
   panel is there whatever was stored. */
const SIDEBAR_KEY = "moonlan-sidebar-collapsed";
let sidebarCollapsed = localStorage.getItem(SIDEBAR_KEY) === "1";

function applySidebarCollapsed() {
  els.main.classList.toggle("sidebar-collapsed", sidebarCollapsed);
  els.sidebarToggle.textContent =
    t("sidebarToggle") + (sidebarCollapsed ? " ▾" : " ▴");
  els.sidebarToggle.setAttribute("aria-expanded", sidebarCollapsed ? "false" : "true");
}

function toggleSidebar() {
  // the map gets the room at once, looking at the same spot
  const view = network && {
    position: network.getViewPosition(),
    scale: network.getScale(),
  };
  sidebarCollapsed = !sidebarCollapsed;
  localStorage.setItem(SIDEBAR_KEY, sidebarCollapsed ? "1" : "0");
  applySidebarCollapsed();
  if (network) {
    network.setSize("100%", "100%");
    network.moveTo({ ...view, animation: false });
    network.redraw();
  }
}

/* ---------- helpers ---------- */

/* Host caption: DNS name, else IP, else what LLDP called the device,
   else the MAC. The main router has no IP of ours (routers are kept out
   of the ARP inventory) and no reverse DNS, so before v0.6.2 it was a
   pale dot labelled 00:0e:04:b7:79:ab — while LLDP had been calling it
   "MikroTik" the whole time. */
function hostLabel(h) {
  return h.name || h.ip || lldpLabel(h) || h.mac;
}

/* The name LLDP gave a device, or its first management address */
function lldpLabel(h) {
  const lldp = h.lldp;
  if (!lldp) return "";
  return lldp.sys_name || (lldp.mgmt_ips || [])[0] || "";
}

/* True when the caption on screen came from LLDP rather than from DNS
   or ARP — the card says so, so nobody hunts for a DHCP lease */
function labelFromLldp(h) {
  return !h.name && !h.ip && !!lldpLabel(h);
}

/* "router" / "phone" / … as the device announced itself */
function deviceKind(h) {
  return (h.lldp && h.lldp.kind) || "";
}

/* "11 (ipmi)" — VLAN ID plus name when known */
function vlanLabel(v) {
  if (!v) return "—";
  const name = (topology.vlan_names || {})[v];
  return name ? v + " (" + name + ")" : String(v);
}

function fmtSpeed(mbps) {
  if (!mbps) return "";
  return mbps >= 1000 ? mbps / 1000 + " " + t("gbps") : mbps + " " + t("mbps");
}

function linkId(link) {
  // a second cable between the same two switches (v0.7.9) is told
  // apart by its ports; the first keeps the id it always had
  return "link:" + link.a + "|" + link.b +
    (link.parallel ? "|" + link.a_port + "|" + link.b_port : "");
}

/* "34" for big rates, "3.4" for small ones, "—" when unknown */
function fmtRate(v) {
  if (v == null) return "—";
  return v >= 10 ? String(Math.round(v)) : (+v).toFixed(1);
}

/* Link caption: "LACP 2×1 Gbit/s" for an aggregate, "LAG trunk" for a
   trunk detected via synthetic bridge-ports, otherwise the speed;
   plus the current load ("↓34 ↑12 Mbit/s") when counters know it */
function linkLabel(link, withLoad = true) {
  let label;
  if (link.lag && link.lag.count > 1) {
    // speed_mbps already counts only the active members
    const total = link.lag.count;
    const active = link.lag.active == null ? total : link.lag.active;
    label = active > 0
      ? t("lacp") + " " + active + "×" + fmtSpeed(link.speed_mbps / active)
      : t("lacp");
    if (active < total) {
      label += " " + fmt("lagMembersShort", { active: active, total: total });
    }
  } else if (link.lag && link.lag.trunk) {
    const speed = fmtSpeed(link.speed_mbps);
    label = speed ? t("lagTrunk") + " " + speed : t("lagTrunk");
  } else {
    label = fmtSpeed(link.speed_mbps);
  }
  // the load is a minute's reading: on paper it is untrue an hour later
  if (withLoad && link.load) {
    // out of the parent side = downstream (↓), into it = upstream (↑)
    const load =
      "↓" + fmtRate(link.load.out_mbps) + " ↑" + fmtRate(link.load.in_mbps) +
      " " + t("mbps");
    label = label ? label + " · " + load : load;
  }
  return label;
}

/* Port name in the link card; synthetic trunk ports get a "(LAG)" mark */
function linkPortLabel(link, port) {
  if (link.lag && link.lag.trunk && port.startsWith("bridge-port")) {
    return port + " (LAG)";
  }
  return port;
}

/* Status dot class: alive / silent / no IP (cannot ping) */
function statusClass(h) {
  if (!h.ip) return "noip";
  return h.ping_up ? "up" : "down";
}

function statusColor(h) {
  if (!h.ip) return colors.link;
  return h.ping_up ? colors.ok : colors.dim;
}

function fmtTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString(locale()) : "—";
}

function fmtDate(ts) {
  return ts ? new Date(ts * 1000).toLocaleDateString(locale()) : "—";
}

/* "4 min", "2 h 10 min" — how long ago something was measured. */
function fmtAge(seconds) {
  if (seconds == null) return "—";
  const minutes = Math.round(seconds / 60);
  if (minutes < 1) return t("ageUnderMinute");
  if (minutes < 60) return fmt("ageMinutes", { n: minutes });
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest
    ? fmt("ageHoursMinutes", { h: hours, n: rest })
    : fmt("ageHours", { h: hours });
}

function clock(ts) {
  return new Date(ts * 1000).toLocaleTimeString(locale());
}

/* The status line, in two lengths (see fitHeader): the whole sentence,
   and a short form the header falls back to when one row would not
   hold everything. The sentence, and why, are always in the tooltip. */
function setScanStatus(full, short, cls, hint) {
  els.scanStatus.className = "status" + (cls ? " " + cls : "");
  els.scanStatus.replaceChildren(
    h("span", { class: "st-full", text: full }),
    h("span", { class: "st-short", text: short })
  );
  els.scanStatus.title = hint ? full + "\n\n" + hint : full;
  fitHeader();
}

function updateScanStatus() {
  updateLayoutStatus();
  // Nothing below this line is arriving any more, so it goes first:
  // everything else the header could say is about a picture that has
  // stopped being refreshed.
  if (!serviceReachable) {
    setScanStatus(
      fmt("serviceOffline", {
        time: topology.last_scan ? fmtTime(topology.last_scan) : t("noData"),
      }),
      t("serviceOfflineShort"), "offline", t("serviceOfflineHint")
    );
    return;
  }
  // A scan of eight switches took ten minutes and the interface said
  // nothing at all about it — the only way to find out whether
  // anything was happening was journalctl. One line settles it.
  if (isScanning || topology.scanning) {
    const counted = topology.scan_total > 0;
    const counts = { done: topology.scan_done || 0, total: topology.scan_total };
    setScanStatus(
      counted ? fmt("scanningProgress", counts) : t("scanning"),
      counted ? fmt("scanningShort", counts) : t("scanningShortStart")
    );
    return;
  }
  // A failed scan used to read exactly like a service that had just
  // started. It now says so, and keeps the map from the last good one.
  if (topology.last_error) {
    setScanStatus(
      t("scanFailed") +
        new Date(topology.last_error_ts * 1000).toLocaleString(locale()),
      fmt("scanFailedShort", { time: clock(topology.last_error_ts) }),
      "failed", topology.last_error
    );
    return;
  }
  if (!topology.last_scan) {
    setScanStatus(t("noData"), t("noData"));
    return;
  }
  let full =
    t("scanPrefix") + new Date(topology.last_scan * 1000).toLocaleString(locale());
  // Switches the last scan gave up waiting for. Worth seeing — their
  // part of the map is older than the rest — but not an alarm: they
  // answer, and nothing about them is being claimed. Short of room the
  // amber colour stays, the words go to the tooltip.
  const late = topology.scan_over_budget || [];
  if (late.length) {
    full += "  " + fmt("scanOverBudgetMark", { n: late.length });
  }
  setScanStatus(
    full, clock(topology.last_scan), late.length ? "partial" : "",
    late.length
      ? fmt("scanOverBudgetHint", { switches: late.map(switchName).join(", ") })
      : ""
  );
}

/* How much of this map was arranged by a person — reference, not the
   state of the network, so since v0.7.5 it lives in the tooltip of the
   Arrange button rather than in the status line. */
function pinnedCount() {
  return Object.keys(savedLayout).filter((id) => savedLayout[id].pinned).length;
}

function updateLayoutStatus() {
  applyArrangeMode();
}

/* One row of controls. If it does not fit, the status line gives way
   first — the time of the scan alone — then the tagline beside the
   name; on a phone-sized window the row is allowed to break instead
   (style.css). Measured rather than guessed from the window width: a
   long user name or the other language changes what fits. */
function fitHeader() {
  const header = document.querySelector("header");
  const brand = header.querySelector(".brand");
  const crowded = () =>
    header.scrollWidth > header.clientWidth + 1 ||
    brand.scrollWidth > brand.clientWidth + 1;
  header.classList.remove("compact", "compact-more");
  if (window.innerWidth > 860 && crowded()) {
    header.classList.add("compact");
    if (crowded()) header.classList.add("compact-more");
  }
  tapAreas();
}

/* Tap areas on a phone (v0.8.2). A button of the header is 36 by 34,
   enough for WCAG's AA (24 by 24) and a little small for a finger: an
   invisible ::before takes it to 44 by 44, with nothing on the screen
   changed. As far as the room allows — the header wraps differently
   at every width, and its rows are 6 px apart: each side reaches
   halfway across the gap to whatever can be tapped beside it, and
   what one side cannot take the other does (RU and EN touch, so
   theirs reach outwards). Two areas that would still meet at a corner
   stop at the middle of the gap between their buttons. */
const TAP_SIZE = 44;

function tapAreas() {
  const targets = [...document.querySelectorAll("header button, #sidebar-toggle")]
    .filter((el) => el.offsetParent !== null);
  if (!window.matchMedia("(max-width: 720px)").matches) {
    for (const el of targets) el.style.removeProperty("--tap");
    return;
  }
  const others = [...targets, ...document.querySelectorAll("#search, header a, #notice a")]
    .filter((el) => el.offsetParent !== null);
  const box = new Map(others.map((el) => [el, el.getBoundingClientRect()]));
  const zones = new Map();
  for (const el of targets) {
    const r = box.get(el);
    const wantX = Math.max(0, TAP_SIZE - r.width);
    const wantY = Math.max(0, TAP_SIZE - r.height);
    const room = { top: wantY, right: wantX, bottom: wantY, left: wantX };
    for (const [other, o] of box) {
      if (other === el) continue;
      if (o.top < r.bottom + wantY && r.top - wantY < o.bottom) {
        if (o.left >= r.right) room.right = Math.min(room.right, (o.left - r.right) / 2);
        if (o.right <= r.left) room.left = Math.min(room.left, (r.left - o.right) / 2);
      }
      if (o.left < r.right + wantX && r.left - wantX < o.right) {
        if (o.top >= r.bottom) room.bottom = Math.min(room.bottom, (o.top - r.bottom) / 2);
        if (o.bottom <= r.top) room.top = Math.min(room.top, (r.top - o.bottom) / 2);
      }
    }
    // half each way, and what one side has no room for, the other
    const split = (want, a, b) => {
      let first = Math.min(a, want / 2);
      let second = Math.min(b, want / 2);
      first = Math.min(a, want - second);
      second = Math.min(b, want - first);
      return [first, second];
    };
    const [left, right] = split(wantX, room.left, room.right);
    const [top, bottom] = split(wantY, room.top, room.bottom);
    zones.set(el, { top: r.top - top, right: r.right + right, bottom: r.bottom + bottom, left: r.left - left });
  }
  const list = [...zones];
  for (let i = 0; i < list.length; i++) {
    for (let j = i + 1; j < list.length; j++) {
      const [ea, a] = list[i];
      const [eb, b] = list[j];
      if (!(a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom)) continue;
      const ra = box.get(ea);
      const rb = box.get(eb);
      const apartX = Math.max(rb.left - ra.right, ra.left - rb.right);
      const apartY = Math.max(rb.top - ra.bottom, ra.top - rb.bottom);
      if (apartX >= apartY) {
        const [l, r] = rb.left >= ra.right ? [a, b] : [b, a];
        const [rl, rr] = rb.left >= ra.right ? [ra, rb] : [rb, ra];
        const middle = (rl.right + rr.left) / 2;
        l.right = Math.min(l.right, middle);
        r.left = Math.max(r.left, middle);
      } else {
        const [u, d] = rb.top >= ra.bottom ? [a, b] : [b, a];
        const [ru, rd] = rb.top >= ra.bottom ? [ra, rb] : [rb, ra];
        const middle = (ru.bottom + rd.top) / 2;
        u.bottom = Math.min(u.bottom, middle);
        d.top = Math.max(d.top, middle);
      }
    }
  }
  for (const [el, z] of zones) {
    const r = box.get(el);
    const css = getComputedStyle(el);
    // ::before is placed from inside the border
    const inset = [
      z.top - r.top - parseFloat(css.borderTopWidth),
      r.right - z.right - parseFloat(css.borderRightWidth),
      r.bottom - z.bottom - parseFloat(css.borderBottomWidth),
      z.left - r.left - parseFloat(css.borderLeftWidth),
    ].map((v) => Math.round(v * 10) / 10 + "px");
    el.style.setProperty("--tap", inset.join(" "));
  }
}

/* The service can go away — restarted, redeployed, or the machine this
   page is open from lost the route to it. Every periodic request has
   to survive that: an unhandled rejection every thirty seconds is not
   information, and a map that simply stops changing looks exactly like
   a quiet network where nothing is happening.

   So: the last good picture stays on screen, the header says the data
   is no longer arriving and how old it is, and the console gets one
   line when the connection is lost and one when it comes back. */
let serviceReachable = true;

function serviceLost(what) {
  if (serviceReachable) {
    serviceReachable = false;
    console.warn(
      "MoonLan: the service is not answering (" + what + "). The map " +
      "below is the last picture that arrived; polling continues."
    );
    updateScanStatus();
  }
}

function serviceBack() {
  if (!serviceReachable) {
    serviceReachable = true;
    console.info("MoonLan: the service is answering again.");
    updateScanStatus();
  }
}

/* While a scan runs the header counts switches off. The map itself is
   only refreshed every REFRESH_MS, which is no use to somebody
   watching a scan that may last minutes. */
let scanWatcher = null;

function watchScan() {
  if (scanWatcher) return;
  scanWatcher = setInterval(async () => {
    if (gateWait) return;
    let status;
    try {
      status = await (await api("/api/status")).json();
      serviceBack();
    } catch (e) {
      serviceLost("scan progress");
      return; // keep the timer: the next tick may well succeed
    }
    topology.scanning = status.scanning;
    topology.scan_done = status.scan_done;
    topology.scan_total = status.scan_total;
    topology.scan_over_budget = status.scan_over_budget;
    updateScanStatus();
    if (!status.scanning) {
      clearInterval(scanWatcher);
      scanWatcher = null;
      isScanning = false;
      await loadTopology();
    }
  }, 2000);
}

/* ---------- data loading and rendering ---------- */

let topologyLoading = false;

async function loadTopology() {
  // The refresh timer keeps ticking while the sign-in form is up; one
  // request waiting behind the form is enough
  if (topologyLoading) return;
  topologyLoading = true;
  try {
    await loadTopologyOnce();
  } finally {
    topologyLoading = false;
  }
}

async function loadTopologyOnce() {
  let topo;
  let alarms;
  let layout;
  let noteList;
  const askedAt = performance.now();
  try {
    [topo, alarms, layout, noteList] = await Promise.all([
      api("/api/topology").then((r) => r.json()),
      fetchAlarms("active=1"),
      api("/api/layout").then((r) => r.json()),
      api("/api/notes").then((r) => r.json()),
    ]);
  } catch (e) {
    // Whatever is on screen stays there. This is the moment somebody
    // is most likely to be looking at it.
    serviceLost("the map");
    return;
  }
  serviceBack();
  topology = topo;
  activeAlarms = alarms;
  allNotes = noteList.notes || [];
  if (!els.notesPanel.classList.contains("hidden") && notesPanelShows === "orphans") {
    renderOrphans();
  }
  renderBadge();
  renderSidebar();
  if (takeServerLayout(layout, askedAt) === "rebuild" && network) {
    rebuildGraph();
    // Started over from nothing, the same as a reset done here
    if (!Object.keys(savedLayout).length) network.stabilize();
  } else {
    renderGraph();
  }
  updateScanStatus();
  // A scan the operator did not start is worth counting off too: the
  // periodic one is when they are most likely to wonder why nothing
  // has moved for ten minutes.
  if (topology.scanning) watchScan();
  els.emptyState.classList.toggle("hidden", topology.switches.length > 0);
}

function renderBadge() {
  labelIcon(
    els.alarmsBtn,
    activeAlarms.length
      ? t("alarmsBtn") + ": " + activeAlarms.length
      : t("alarmsBtn")
  );
  els.alarmsBadge.textContent = activeAlarms.length;
  els.alarmsBadge.classList.toggle("hidden", activeAlarms.length === 0);
  els.alarmsBadge.classList.toggle(
    "critical",
    activeAlarms.some((a) => a.severity === "critical")
  );
}

function switchHasAlarm(ip) {
  return activeAlarms.some(
    (a) => a.type === "switch_down" && a.subject === ip
  );
}

/* A row of the lists: a button the width of the row (v0.8.2). The map
   is a canvas, and these lists are the keyboard's only way onto it: a
   button takes Tab, Enter and space by itself and says what it is to
   a screen reader. Each list takes one Tab (listTabStop); the arrows go
   through the rest of it. `key` is the node, to keep the focus on its
   row when the list is drawn again. */
function li(main, sub, dotClass, onClick, searchText, cssClass, title, key) {
  const item = document.createElement("li");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "li-btn";
  button.tabIndex = -1;
  if (title) button.title = title;
  if (dotClass) {
    const dot = document.createElement("span");
    dot.className = "dot " + dotClass;
    button.append(dot);
  }
  const text = document.createElement("span");
  text.className = "li-text";
  const name = document.createElement("span");
  name.textContent = main;
  text.append(name);
  if (sub) {
    const extra = document.createElement("span");
    extra.className = "sub";
    extra.textContent = sub;
    text.append(extra);
  }
  button.append(text);
  button.addEventListener("click", onClick);
  item.append(button);
  item.dataset.search = (searchText || main + " " + sub).toLowerCase();
  if (key) item.dataset.key = key;
  if (cssClass) item.classList.add(cssClass);
  return item;
}

/* The one row of a list that Tab stops at: `row`, or the first one the
   search leaves */
function listTabStop(list, row) {
  for (const button of list.querySelectorAll(".li-btn")) button.tabIndex = -1;
  const target = row || [...list.children].find((item) => !item.classList.contains("hidden-by-search"));
  const button = target && target.querySelector(".li-btn");
  if (button) button.tabIndex = 0;
}

/* ↑ and ↓ to the next row the search leaves, Home and End to the first
   and the last: hundreds of rows are not a Tab each. The focus stays
   in the list after Enter, so the next row is one key away. */
function listKeys(event) {
  const button = event.target.closest && event.target.closest(".device-list .li-btn");
  if (!button) return;
  const list = button.closest("ul");
  const rows = [...list.children].filter((item) => !item.classList.contains("hidden-by-search"));
  const at = rows.indexOf(button.parentElement);
  let next = null;
  if (event.key === "ArrowDown") next = rows[Math.min(at + 1, rows.length - 1)];
  else if (event.key === "ArrowUp") next = rows[Math.max(at - 1, 0)];
  else if (event.key === "Home") next = rows[0];
  else if (event.key === "End") next = rows[rows.length - 1];
  else return;
  event.preventDefault();
  if (!next) return;
  listTabStop(list, next);
  next.querySelector(".li-btn").focus();
}

/* A name and a note are searched like the rest of what is known */
function noteSearchText(item) {
  return item && item.note ? [item.note.label, item.note.note].filter(Boolean).join(" ") : "";
}

/* The nodes with a name that no list below has: they would not be found
   by their name otherwise — a switch without SNMP is in no list at all */
function namedNodes() {
  const rows = [];
  const add = (item, kind, where) => {
    if (!nameOf(item)) return;
    rows.push({ id: item.id, name: nameOf(item), line: t("kindLine_" + kind) + " · " + where, item });
  };
  for (const ps of topology.pseudo_switches || []) add(ps, "pseudo", switchName(ps.switch) + " " + ps.port);
  for (const bridge of topology.bridges || []) add(bridge, "bridge", bridge.name);
  for (const ext of topology.external_networks || []) add(ext, "external", switchName(ext.switch) + " " + ext.port);
  for (const group of topology.offline_groups || []) add(group, "offline", switchName(group.switch) + " " + group.port);
  return rows.sort((a, b) => a.name.localeCompare(b.name, locale()));
}

function renderSidebar() {
  // a refresh draws the lists anew: the row with the focus keeps it
  const active = document.activeElement;
  const kept = active && active.classList && active.classList.contains("li-btn")
    ? { list: active.closest("ul"), key: active.parentElement.dataset.key }
    : null;
  const named = namedNodes();
  els.namedList.replaceChildren(
    ...named.map((row) =>
      li(row.name, row.line, null, () => focusNode(row.id),
         [row.name, row.line, noteSearchText(row.item)].join(" "), "", "", row.id))
  );
  els.namedSection.classList.toggle("hidden", named.length === 0);
  els.switchList.replaceChildren(
    ...topology.switches.map((sw) =>
      li(
        // a name a person gave it first, what MoonLan calls it under it
        nameOf(sw) || sw.name,
        // same rule as the map caption: no address over the address
        [nameOf(sw) ? sw.name : "", sw.named === false ? sw.model || "" : sw.ip]
          .filter(Boolean).join(" · "),
        sw.ping_up ? "up" : "down",
        () => focusNode("sw:" + sw.ip),
        // what the card says about it is what finds it: "d-link" finds
        // the D-Links by their maker, "es3528m" one by its sysDescr
        [sw.name, sw.ip, sw.mac, sw.vendor, sw.model, sw.descr, noteSearchText(sw)]
          .filter(Boolean).join(" "),
        // greyed the way an old host record is: the switch is there,
        // its numbers are from the last poll that finished
        sw.over_budget ? "stale" : "",
        sw.over_budget_scans >= 2
          ? fmt("staleSwitchScans", { n: sw.over_budget_scans }) +
            " · " + t("dataFrom") + " " + fmtTime(sw.polled_at)
          : "",
        "sw:" + sw.ip
      )
    )
  );
  els.hostList.replaceChildren(
    ...topology.hosts.map((h) =>
      li(
        (nameOf(h) || hostLabel(h)) + (h.monitored ? " ★" : ""),
        [nameOf(h) ? hostLabel(h) : "", h.ip, h.mac, h.random_mac ? t("randomMac") : "",
         h.vlan ? "VLAN " + h.vlan : ""]
          .filter((part, i, all) => part && all.indexOf(part) === i)
          .join(" · "),
        statusClass(h),
        () => focusNode(h.merged_into || "host:" + h.mac),
        // VLAN is intentionally excluded from search; the maker is in:
        // "mercusys" finds every MERCUSYS device
        [hostLabel(h), h.ip, h.mac, h.vendor, noteSearchText(h)].filter(Boolean).join(" "),
        h.stale ? "stale" : "", "", "host:" + h.mac
      )
    )
  );
  // devices nothing draws on the map: known from ARP or seen on a port
  // too long ago — searchable inventory, not graph nodes
  const unlocated = topology.unlocated || [];
  els.unlocatedList.replaceChildren(
    ...unlocated.map((h) =>
      li(
        (nameOf(h) || hostLabel(h)) + (h.monitored ? " ★" : ""),
        [nameOf(h) ? hostLabel(h) : "", h.ip, h.mac, h.random_mac ? t("randomMac") : ""]
          .filter((part, i, all) => part && all.indexOf(part) === i)
          .join(" · "),
        statusClass(h),
        () => showDetails("unloc:" + h.mac),
        [hostLabel(h), h.ip, h.mac, h.vendor, noteSearchText(h)].filter(Boolean).join(" "),
        "stale", "", "unloc:" + h.mac
      )
    )
  );
  els.unlocatedSection.classList.toggle("hidden", unlocated.length === 0);
  applySearchFilter();
  const row = kept && [...kept.list.children].find((item) => item.dataset.key === kept.key);
  if (row) {
    listTabStop(kept.list, row);
    row.querySelector(".li-btn").focus({ preventScroll: true });
  }
}

/* Caption style of a node; the selected one is brighter than the rest.
   A node with a name a person gave it reads its caption as markup
   (namedCaption): the name in bold, a line in the small style. */
function nodeFont(id) {
  const base = plainNodeFont(id);
  if (!nodeNames.has(id)) return base;
  const size = base.size || 14;
  return {
    ...base,
    multi: "html",
    bold: { color: colors.text, size: size + 1 },
    ital: { color: colors.dim, size: Math.max(size - 2, 9), mod: "" },
  };
}

function plainNodeFont(id) {
  const selected = id === selectedNodeId;
  if (id.startsWith("sw:")) {
    return { color: colors.text, face: "system-ui" };
  }
  // its own size here, not in the node: a font set on the node itself
  // missed the markup of a name (v0.8.1), and a selection changes the
  // brightness, not the size
  if (id.startsWith("bridge:")) {
    return { color: selected ? colors.text : colors.dim, size: 12 };
  }
  if (id.startsWith("host:")) {
    return {
      color: selected ? colors.text : colors.dim,
      size: 11,
      face: "ui-monospace",
      strokeWidth: 0,
    };
  }
  return { color: selected ? colors.text : colors.dim, size: 11 };
}

// node id -> the name a person gave it (v0.8.0), as of the last
// buildGraphData; nodeFont reads it
let nodeNames = new Map();

/* Text into a caption vis reads as markup. vis knows <b>, <i> and
   <code> there, lowercase and exact, and no entities at all: "&lt;"
   would be drawn as it is. So text from outside — a name a person
   typed, a DNS or LLDP name — cannot be escaped, only kept from making
   a tag: a zero-width space after every "<" breaks it and shows as
   nothing. (Markdown, the other mode, is no better: vis crashes on a
   backslash before a star.) */
function markupSafe(text) {
  return String(text).replace(/</g, "<\u200b");
}

/* The caption of a node with a name: the name first, in bold — a
   person's word — and under it what MoonLan calls the node, in the
   small style when `small`. */
function namedCaption(name, lines, small) {
  return [
    "<b>" + markupSafe(name) + "</b>",
    ...lines.filter(Boolean).map((line) =>
      small ? "<i>" + markupSafe(line) + "</i>" : markupSafe(line)),
  ].join("\n");
}

/* A caption as words: the markup of a named one taken out */
function plainCaption(label) {
  return String(label || "").replace(/<\/?(b|i|code)>/g, "").replace(/\u200b/g, "");
}

/* The name a person gave a node, or "" */
function nameOf(item) {
  return (item && item.note && item.note.label) || "";
}

function buildGraphData() {
  const nodes = [];
  const edges = [];
  nodeNames = new Map();
  const named = (id, item) => {
    const name = nameOf(item);
    if (name) nodeNames.set(id, name);
    return name;
  };

  // Ports the switches' own Loop Detection reports a loop on, as
  // "<switch ip>|<port name>". Polled on the counters cycle, so this
  // is fresher than the rest of the map.
  const loopedPorts = new Set();
  for (const sw of topology.switches) {
    for (const port of (sw.loop && sw.loop.looped_ports) || []) {
      loopedPorts.add(sw.ip + "|" + port);
    }
  }
  const isLooped = (ip, port) =>
    !!ip && !!port && loopedPorts.has(ip + "|" + port);

  /* Paints the edge that leaves a looping port. Only the segment that
     starts at the switch is marked: what hangs further down the chain
     is behind the loop, not in it. */
  const markLoop = (edge, ip, port) => {
    if (!isLooped(ip, port)) return edge;
    if (edge.from !== "sw:" + ip && edge.to !== "sw:" + ip) return edge;
    edge.color = { color: colors.alarm, opacity: 1 };
    edge.width = Math.max(edge.width || 1, 3);
    edge.dashes = false;
    edge.label = (edge.label ? edge.label + " · " : "") + t("loopMark");
    if (edge.paperLabel !== undefined) {
      edge.paperLabel = (edge.paperLabel ? edge.paperLabel + " · " : "") + t("loopMark");
    }
    edge.font = { color: colors.alarm, size: 11, strokeWidth: 0 };
    if (edge.semantic && !edge.semantic.includes("loop")) edge.semantic.push("loop");
    return edge;
  };

  /* Which port is at which end, for the tags on the lines. Only an end
     that is the switch itself: the line of a device behind a group or
     a bridge starts there, and the port is on the line into the switch
     already. */
  const prefixOf = new Map(
    topology.switches.map((sw) => [sw.ip, sw.port_prefix || null])
  );
  const markPort = (edge, ip, port, members) => {
    if (!ip) return edge;
    const end = "sw:" + ip;
    if (edge.from !== end && edge.to !== end) return edge;
    const prefix = prefixOf.get(ip) || null;
    edge.portAt = { ...edge.portAt, [end]: { port, members: members || null, prefix } };
    return edge;
  };
  // a line out of a switch's port: the loop mark and the port's tag
  const fromPort = (edge, ip, port) => markPort(markLoop(edge, ip, port), ip, port);

  for (const sw of topology.switches) {
    // a switch_down alarm paints the node border red; so does a loop,
    // which is the one thing on this map that is taking a segment down
    // right now. The root bridge of a working tree gets a thicker
    // outline instead.
    const looping = !!(sw.loop && sw.loop.status === "loop");
    const border = switchHasAlarm(sw.ip) || looping
      ? colors.alarm
      : sw.stp_root
      ? colors.ok
      : colors.moon;
    // A switch with an empty sysName is captioned by its address, and
    // repeating the address underneath tells nobody anything. The
    // model out of sysDescr goes there instead, or nothing at all.
    const second = sw.named === false ? sw.model || "" : sw.ip;
    const swLines = [
      sw.name, second, sw.stp_root ? t("stpRootMark") : "",
      looping ? t("loopMark") : "",
    ].filter(Boolean);
    const swName = named("sw:" + sw.ip, sw);
    nodes.push(applyLayout({
      id: "sw:" + sw.ip,
      // what it is, for a picture with other colours than these (export)
      semantic: ["switch", ...(sw.stp_root ? ["root"] : []), ...(looping ? ["loop"] : [])],
      label: swName ? namedCaption(swName, swLines) : swLines.join("\n"),
      shape: "box",
      color: {
        background: colors.panel,
        border: border,
        // A selected node has to look selected. The highlight border
        // used to repeat the normal one, so with several nodes chosen
        // nothing on the map said which.
        highlight: { background: "#1c2739", border: colors.link },
      },
      font: nodeFont("sw:" + sw.ip),
      borderWidth: switchHasAlarm(sw.ip) || sw.stp_root || looping ? 3 : 2,
      margin: 10,
    }));
  }

  for (const link of topology.links) {
    const isLacp = link.lag && link.lag.count > 1;
    const isTrunk = link.lag && link.lag.trunk;
    // LLDP-confirmed links are drawn solid and at full strength: both
    // devices named each other, there is nothing being guessed at
    const confirmed = link.source === "lldp" || link.source === "both";
    const edge = {
      id: linkId(link),
      from: "sw:" + link.a,
      to: "sw:" + link.b,
      label: linkLabel(link),
      // the caption on paper: the speed, LACP, the trunk — no load (v0.8.2)
      paperLabel: linkLabel(link, false),
      color: { color: colors.moon, opacity: confirmed ? 1 : 0.8 },
      width: isLacp ? 5 : isTrunk ? 4 : 3,
      font: { color: colors.dim, size: 11, strokeWidth: 0 },
      dashes: false,
      // what the line means, not its colour: the export draws it on paper
      semantic: [
        confirmed ? "lldp" : "fdb",
        ...(isLacp ? ["lacp"] : []), ...(isTrunk ? ["trunk"] : []),
        ...(link.order_unknown ? ["guess"] : []),
        ...(link.cycle_unresolved ? ["ring"] : []),
        ...(link.stp_blocking ? ["blocking"] : []),
        ...(link.parallel ? ["parallel"] : []),
      ],
    };
    // The MAC tables could not say which of several switches behind
    // one port is on the cable, so they are all hung off it. A guess
    // has no business looking like a measured cable: dashed, thinner,
    // and the tooltip says what is unknown about it.
    if (link.order_unknown) {
      edge.dashes = [4, 6];
      edge.color = { color: colors.moon, opacity: 0.55 };
      edge.width = 2;
      edge.title = t("linkOrderUnknown");
    }
    // …and a ring nothing could account for: not removed, not believed
    if (link.cycle_unresolved) {
      edge.dashes = [2, 4];
      edge.color = { color: colors.dim, opacity: 0.7 };
      edge.title = t("linkCycleUnresolved");
    }
    // a port STP is holding in discarding carries no traffic at all
    if (link.stp_blocking) {
      edge.color = { color: colors.alarm, opacity: 1 };
      edge.dashes = [6, 4];
      edge.label = (edge.label ? edge.label + " · " : "") + t("stpBlocking");
      edge.paperLabel = (edge.paperLabel ? edge.paperLabel + " · " : "") + t("stpBlocking");
      edge.title = t("linkStpBlocking");
    }
    markLoop(edge, link.a, link.a_port);
    markLoop(edge, link.b, link.b_port);
    // an aggregate is tagged with its members on each side
    markPort(edge, link.a, link.a_port, isLacp && link.lag.a_members);
    markPort(edge, link.b, link.b_port, isLacp && link.lag.b_members);
    edges.push(edge);
  }

  for (const ps of topology.pseudo_switches || []) {
    nodes.push(applyLayout({
      id: ps.id,
      semantic: ["pseudo"],
      // named: the name, and what kind of node it is, small
      label: named(ps.id, ps)
        ? namedCaption(nameOf(ps), [t("pseudoKind")], true)
        : t("pseudoTitle"),
      shape: "square",
      size: 14,
      color: {
        background: "#3a4356",
        border: colors.dim,
        highlight: { background: "#4a5468", border: colors.moon },
      },
      shapeProperties: { borderDashes: [4, 4] },
      borderWidth: 2,
      font: nodeFont(ps.id),
    }));
    edges.push(fromPort({
      id: "psedge:" + ps.id,
      semantic: ["pseudo"],
      from: "sw:" + ps.switch,
      to: ps.id,
      dashes: [4, 4],
      color: { color: colors.dim, opacity: 0.7 },
      width: 2,
    }, ps.switch, ps.port));
  }

  // switches LLDP found behind our ports that nobody polls: a named
  // node replaces the anonymous "switch without SNMP" on that port
  for (const bridge of topology.bridges || []) {
    // an unreachable bridge is worth seeing at a glance, the same way
    // an unreachable host is
    const border = bridge.ip
      ? bridge.ping_up
        ? colors.ok
        : colors.dim
      : colors.link;
    // the address under the name is the one the operator uses, not
    // whichever of the announced ones came back first
    const bridgeAddress = bridge.router_ip || bridge.ip || bridge.mgmt_ip;
    nodes.push(applyLayout({
      id: bridge.id,
      semantic: ["bridge"],
      label: named(bridge.id, bridge)
        ? namedCaption(nameOf(bridge), [bridge.name, bridgeAddress])
        : bridge.name + (bridgeAddress ? "\n" + bridgeAddress : ""),
      shape: "box",
      color: {
        background: "#1b2436",
        border: border,
        highlight: { background: "#26314a", border: colors.moon },
      },
      shapeProperties: { borderDashes: [5, 3] },
      borderWidth: 2,
      margin: 8,
      font: nodeFont(bridge.id),
    }));
    edges.push(fromPort({
      id: "bredge:" + bridge.id,
      semantic: ["bridge"],
      from: "sw:" + bridge.switch,
      to: bridge.id,
      dashes: [5, 3],
      color: { color: colors.link, opacity: 0.6 },
      width: 2,
    }, bridge.switch, bridge.port));
  }

  // one node per port that leaves the network: what is behind the
  // provider's handover is not ours to draw device by device
  for (const external of topology.external_networks || []) {
    nodes.push(applyLayout({
      id: external.id,
      semantic: ["external"],
      label: named(external.id, external)
        ? namedCaption(nameOf(external), [t("externalNetwork") + " · " + external.count])
        : t("externalNetwork") + " · " + external.count,
      shape: "hexagon",
      size: 16,
      color: {
        background: "#2b2438",
        border: colors.warn || "#d9a86b",
        highlight: { background: "#3a3049", border: colors.moon },
      },
      borderWidth: 2,
      font: nodeFont(external.id),
    }));
    edges.push(fromPort({
      id: "extedge:" + external.id,
      semantic: ["external"],
      // the provider's switch is on the cable and everything else is
      // behind IT: mb0 -> CE6851 -> external network
      from: external.via || "sw:" + external.switch,
      to: external.id,
      color: { color: colors.warn || "#d9a86b", opacity: 0.7 },
      width: 3,
    }, external.switch, external.port));
  }

  // one node per port whose devices are all offline, so the switches
  // are not surrounded by a cloud of grey dots
  // one node per trunk carrying devices that have no place of their
  // own. Deliberately not shaped like a switch: nobody knows whether
  // there is one, only that these addresses come through this cable
  for (const group of topology.trunk_groups || []) {
    nodes.push(applyLayout({
      id: group.id,
      semantic: ["trunk"],
      // no name: a group behind a trunk is a guess, not a place (v0.8.1)
      label: t("trunkGroup") + " · " + group.count,
      shape: "ellipse",
      color: {
        background: "#232c3d",
        border: colors.link,
        highlight: { background: "#2e3b52", border: colors.moon },
      },
      shapeProperties: { borderDashes: [4, 3] },
      borderWidth: 2,
      margin: 6,
      font: nodeFont(group.id),
    }));
    edges.push(fromPort({
      id: "trunkedge:" + group.id,
      semantic: ["trunk-group"],
      // off whatever stands on that cable, if anything does
      from: group.via || "sw:" + group.switch,
      to: group.id,
      dashes: [4, 3],
      color: { color: colors.link, opacity: 0.5 },
      width: 2,
    }, group.switch, group.port));
  }

  for (const group of topology.offline_groups || []) {
    nodes.push(applyLayout({
      id: group.id,
      semantic: ["offline"],
      // standing in for a named switch without SNMP: the room's name,
      // and what it is tonight, small
      label: named(group.id, group)
        ? namedCaption(nameOf(group), [t("offlineGroup") + " · " + group.count], true)
        : t("offlineGroup") + " · " + group.count,
      shape: "square",
      size: 13,
      color: {
        background: "#2a2f3d",
        border: colors.dim,
        highlight: { background: "#3a4152", border: colors.moon },
      },
      shapeProperties: { borderDashes: [2, 3] },
      borderWidth: 2,
      font: nodeFont(group.id),
    }));
    edges.push(fromPort({
      id: "offedge:" + group.id,
      semantic: ["offline"],
      // the live devices of this port hang off the bridge or the
      // unmanaged switch on its cable; the quiet ones belong there too
      from: group.via || "sw:" + group.switch,
      to: group.id,
      dashes: [3, 3],
      color: { color: colors.dim, opacity: 0.4 },
      width: 1,
    }, group.switch, group.port));
  }

  for (const host of topology.hosts) {
    // this device IS one of the bridge nodes above — drawing it again
    // as a bare MAC beside itself is what v0.6.1 did
    if (host.merged_into) continue;
    const c = statusColor(host);
    // stale = drawn from the grace window, not from a fresh MAC table
    // a router is infrastructure, not a workstation: same status
    // colour, but a shape that is picked out at a glance
    const isRouter = deviceKind(host) === "router";
    // a router named by LLDP has no address of ours; the one in
    // `routers:` is how anybody reaches it, so it goes under the name
    const caption = hostLabel(host);
    const second =
      host.router_ip && host.router_ip !== caption ? "\n" + host.router_ip : "";
    nodes.push(applyLayout({
      id: "host:" + host.mac,
      semantic: [isRouter ? "router" : "host", ...(host.stale ? ["stale"] : []),
                 ...(host.approximate ? ["approximate"] : [])],
      label: named("host:" + host.mac, host)
        ? namedCaption(nameOf(host), [caption, second.trim()])
        : caption + second,
      shape: isRouter ? "diamond" : "dot",
      size: isRouter ? 14 : 9,
      opacity: host.stale ? 0.4 : 1,
      color: {
        background: c,
        border: isRouter ? colors.moon : c,
        highlight: { background: c, border: colors.moon },
      },
      borderWidth: isRouter ? 2 : 1,
      font: nodeFont("host:" + host.mac),
    }));
    edges.push(fromPort({
      id: "hostedge:" + host.mac,
      semantic: ["device", ...(host.stale ? ["stale"] : []),
                 ...(host.approximate ? ["approximate"] : [])],
      from: host.via || "sw:" + host.switch,
      to: "host:" + host.mac,
      color: { color: colors.link, opacity: host.stale ? 0.15 : 0.35 },
      width: 1,
      // approximate = seen only through a trunk: the device is real,
      // the port it hangs on is a guess
      dashes: host.stale ? [3, 3] : host.approximate ? [2, 4] : false,
      // remembered ≠ approximate: the port is known, the sighting is
      // second-hand, so the edge stays solid
    }, host.switch, host.port));
  }

  curveParallelEdges(edges);
  portDataVersion++;
  portEdges = new Map(
    edges.filter((edge) => edge.portAt).map((edge) => [edge.id, {
      id: edge.id, from: edge.from, to: edge.to, ends: edge.portAt,
      // a red line gets red tags
      alarm: !!(edge.color && edge.color.color === colors.alarm),
    }])
  );

  // The physics engine walks its nodes in the order they were added.
  // It is not random, so two pages that start from the same positions
  // in the same order end up with the same picture — and the order of
  // the lists the server sends is not something to rely on. The edges
  // keep theirs: the first edge into a node names what it hangs off.
  nodes.sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  return { nodes, edges };
}

/* A line is vis's "dynamic" curve: it hangs on an invisible support
   node of the physics, and the supports push everything else apart —
   that is what keeps branches and groups of devices spread out.
   v0.7.9 made lines straight and the map shrank; v0.7.10 put the
   curves back and moves the supports of a dragged node itself
   (followCurves). Two links between one pair would lie on each other,
   though, so those get fixed curves computed from their ends alone:
   alternately to either side, further out for every next pair. */
function curveParallelEdges(edges) {
  const byPair = new Map();
  for (const edge of edges) {
    const key = [edge.from, edge.to].sort().join("\u0000");
    if (!byPair.has(key)) byPair.set(key, []);
    byPair.get(key).push(edge);
  }
  for (const group of byPair.values()) {
    if (group.length < 2) {
      // said outright: a refresh merges into the edge vis already has,
      // and a curve it once had would stay
      group[0].smooth = { enabled: true, type: "dynamic" };
      continue;
    }
    group.forEach((edge, i) => {
      // "clockwise" is relative to the edge's own direction: an edge
      // drawn the other way round bends to the other side
      const reversed = edge.from > edge.to;
      const clockwise = (i % 2 === 0) !== reversed;
      edge.smooth = {
        enabled: true,
        type: clockwise ? "curvedCW" : "curvedCCW",
        roundness: 0.15 * (Math.floor(i / 2) + 1),
      };
    });
  }
}

/* ---------- the curve of a line under a dragged node ----------

   With the physics off — the layout frozen — nothing moves the support
   of a line, and v0.7.8 drew the lines of a dragged node in arcs
   through the spot it had left. Now the supports of a dragged node's
   lines are moved with it, to the same place relative to the line:
   the line turns and stretches and keeps its bend. With the physics
   on, vis moves them itself. */

// edge id -> {before: {from, to}, via}, from the start of a drag with
// the physics off; null otherwise
let heldCurves = null;

/* Where a support goes when its line moves from `before` to `after`
   ({from, to} points): the same share along the line and across it.
   A line of no length has no direction; its support keeps its offset
   from the line's middle. */
function movedVia(before, after, via) {
  const dx = before.to.x - before.from.x;
  const dy = before.to.y - before.from.y;
  const length2 = dx * dx + dy * dy;
  if (!length2) {
    return {
      x: (after.from.x + after.to.x) / 2 + via.x - before.from.x,
      y: (after.from.y + after.to.y) / 2 + via.y - before.from.y,
    };
  }
  const rx = via.x - before.from.x;
  const ry = via.y - before.from.y;
  const along = (rx * dx + ry * dy) / length2;
  const across = (ry * dx - rx * dy) / length2;
  const ex = after.to.x - after.from.x;
  const ey = after.to.y - after.from.y;
  return {
    x: after.from.x + along * ex - across * ey,
    y: after.from.y + along * ey + across * ex,
  };
}

/* A line's support node, or null for a line without one (a parallel
   link's fixed curve). The one place that touches it: edgeType.via and
   positionBezierNode are internal API of vis-network 9.1.9, which
   index.html pins, like getPoint below. */
function curveSupport(edgeId) {
  if (visInternals.curves !== true) return null;
  const edge = network.body.edges[edgeId];
  const type = edge && edge.edgeType;
  if (!type || !type.via || !edge.from || !edge.to) return null;
  return {
    line: () => ({
      from: { x: edge.from.x, y: edge.from.y },
      to: { x: edge.to.x, y: edge.to.y },
    }),
    via: () => ({ x: type.via.x, y: type.via.y }),
    moveTo: (point) => {
      type.via.x = point.x;
      type.via.y = point.y;
    },
    toMiddle: () => type.positionBezierNode(),
  };
}

function curvesOf(nodeIds) {
  const ids = new Set();
  for (const id of nodeIds) {
    for (const edgeId of network.getConnectedEdges(id)) ids.add(edgeId);
  }
  return ids;
}

function holdCurves(nodeIds) {
  heldCurves = null;
  if (!layoutFrozen || !nodeIds.length) return;
  heldCurves = new Map();
  for (const edgeId of curvesOf(nodeIds)) {
    const support = curveSupport(edgeId);
    if (support) heldCurves.set(edgeId, { before: support.line(), via: support.via() });
  }
}

function followCurves(nodeIds) {
  if (!heldCurves || !nodeIds.length) return;
  for (const edgeId of curvesOf(nodeIds)) {
    const support = curveSupport(edgeId);
    if (!support) continue;
    const held = heldCurves.get(edgeId);
    // a line that appeared during the drag has no bend to keep
    if (held) support.moveTo(movedVia(held.before, support.line(), held.via));
    else support.toMiddle();
  }
}

/* ---------- port numbers on the lines ----------

   A tag at the end of a line with the number of the port it leaves
   from, at the node the port belongs to (v0.7.9): off, for the
   selection, or everywhere. Drawn on the canvas after vis has drawn
   the map, at 11 px on the screen whatever the zoom. */

const PORT_LABELS_KEY = "moonlan-port-labels";
const PORT_LABEL_MODES = ["off", "selected", "all"];
let portLabelMode = PORT_LABEL_MODES.includes(localStorage.getItem(PORT_LABELS_KEY))
  ? localStorage.getItem(PORT_LABELS_KEY)
  : "selected";
// "numbers only": the bare number of a port where it names one port
const PORT_NUMBERS_KEY = "moonlan-port-labels-numbers";
let portNumbersOnly = localStorage.getItem(PORT_NUMBERS_KEY) === "1";
// edge id -> {id, from, to, ends: {node id: {port, members, prefix}}, alarm}
let portEdges = new Map();
// changes with every rebuild of portEdges: a cached placement of the
// tags is for one version of the map
let portDataVersion = 0;
// "Everywhere" stops below this zoom: the whole network in tags is a
// green rash, not information
const PORT_LABELS_MIN_SCALE = 0.5;
const PORT_TAG_FONT = 11; // px on the screen
const PORT_TAG_PAD_X = 4;
const PORT_TAG_HEIGHT = 15;

/* A port's name as it fits on a tag. A number or a short name as it
   is; a long one by the number it ends with ("GigabitEthernet1/0/3" →
   "1/0/3", "… Port 3" → "3"); a LAG trunk's synthetic "bridge-port N"
   as "LAG"; an aggregate by the members of this side, up to two in
   full ("3,4"), more as the first and how many others ("25+3") — and
   past 8 characters without the prefix they share ("Gi0/3,Gi0/4" →
   "3,4"); no port at all as "?". */
function portShort(port, members) {
  if (members && members.length) {
    let names = members.map((member) => portShort(member));
    const text = (list) =>
      list.length <= 2 ? list.join(",") : list[0] + "+" + (list.length - 1);
    if (text(names).length > 8) {
      let shared = names[0];
      for (const name of names) {
        while (!name.startsWith(shared)) shared = shared.slice(0, -1);
      }
      // a number is not cut in two: "Gi0/25", "Gi0/26" share "Gi0/"
      shared = shared.replace(/\d+$/, "");
      if (names.every((name) => name.length > shared.length)) {
        names = names.map((name) => name.slice(shared.length));
      }
    }
    return text(names);
  }
  const name = port == null ? "" : String(port).trim();
  if (!name) return "?";
  if (/^bridge-port \d+$/.test(name)) return "LAG";
  if (name.length <= 8) return name;
  const tail = name.match(/(\d+(?:[/.:]\d+)*)(?!.*\d)/);
  return tail ? tail[1] : name.slice(0, 7) + "…";
}

/* The text of a tag (v0.7.10): portShort(), or with "numbers only" on
   a switch whose physical ports all share one beginning (`prefix`,
   from the server), the number after it — "Slot0/25" → "25", an
   aggregate's members the same way, "23,24". A name without that
   beginning — an aggregate's own, a LAG trunk's bridge-port — is as
   portShort() has it; so is every port of a switch with no prefix. */
function portTagText(port, members, prefix, numbersOnly) {
  const number = (name) => {
    const text = name == null ? "" : String(name).trim();
    if (!numbersOnly || !prefix || !text.startsWith(prefix)) return null;
    const rest = text.slice(prefix.length);
    return /^\d+$/.test(rest) ? rest : null;
  };
  if (members && members.length) {
    const numbers = members.map(number);
    if (numbers.every((n) => n !== null)) {
      return numbers.length <= 2
        ? numbers.join(",")
        : numbers[0] + "+" + (numbers.length - 1);
    }
    return portShort(port, members);
  }
  return number(port) ?? portShort(port, members);
}

/* Which tags to draw, as [edge entry, node id] pairs. A selected switch
   gets its own ends only — never the far end of its lines, which is
   somebody else's port. A selected device, group or bridge gets the
   switch end of its line: the port it is plugged into. A selected line
   gets both ends, but only while no node is selected: vis selects
   every line of a selected node along with it. */
function portTagsToDraw() {
  return portTagsFor(portLabelMode);
}

function portTagsFor(mode) {
  const tags = [];
  if (mode === "all") {
    for (const entry of portEdges.values()) {
      for (const end of Object.keys(entry.ends)) tags.push([entry, end]);
    }
    return tags;
  }
  const nodes = network.getSelectedNodes();
  if (nodes.length) {
    const seen = new Set();
    for (const id of nodes) {
      for (const edgeId of network.getConnectedEdges(id)) {
        const entry = portEdges.get(edgeId);
        if (!entry) continue;
        const ends = id in entry.ends
          ? [id]
          : id.startsWith("sw:")
          ? []
          : Object.keys(entry.ends);
        for (const end of ends) {
          const key = edgeId + "\u0000" + end;
          if (seen.has(key)) continue;
          seen.add(key);
          tags.push([entry, end]);
        }
      }
    }
    return tags;
  }
  for (const edgeId of network.getSelectedEdges()) {
    const entry = portEdges.get(edgeId);
    if (!entry) continue;
    for (const end of Object.keys(entry.ends)) tags.push([entry, end]);
  }
  return tags;
}

/* A point of a line as it is drawn, t running from `start` (0) to the
   other end (1). A straight line is a segment between the two nodes;
   a parallel link is a curve of vis's own, and its points come from
   vis — edgeType.getPoint, internal API of vis-network 9.1.9, which
   index.html pins. */
function edgePointFn(entry, start, positions) {
  const other = start === entry.from ? entry.to : entry.from;
  const a = positions[start];
  const b = positions[other];
  if (!a || !b) return null;
  const edge = visInternals.curvePoints === true && network.body.edges[entry.id];
  const smooth = edge && edge.options && edge.options.smooth;
  if (smooth && smooth.enabled && edge.edgeType && edge.edgeType.getPoint) {
    const reversed = start !== entry.from;
    return (t) => edge.edgeType.getPoint(reversed ? 1 - t : t);
  }
  return (t) => ({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });
}

/* Where the tags go, in screen pixels: [{w, h, line, own, far, shared,
   rank, node}] -> [{x, y} | null], in the order given. Pure, so it can
   be run on made-up lines (tests/test_port_tags.py).

   `line` is the line as drawn, a polyline from the tag's own node to
   the far one; `own` and `far` are the two nodes' boxes. A tag never
   touches its own node's box: it starts just past it, GAP pixels clear,
   and walks along its own line, STEP at a time, until it is clear of
   every tag placed before it by MARGIN. It goes no further than the
   middle of the free part of the line when the line has a tag at its
   other end too (`shared`: switch to switch, the speed is there), and
   up to the far node's box when it has not. Two nodes that leave no
   free line between them get the tag just past the own box, wherever
   that is. The tags of the selection go first (`rank`), then those of
   links between switches, then the rest — each node's by the angle its
   lines leave at, so a fan of lines becomes a staircase. A tag with no
   room is left out when every line is tagged ("all") and put at its
   limit anyway for the selection: those are the ports asked for.

   The tags already placed are kept in a grid of cells one tag high, so
   a try looks at its neighbours, not at every tag on the map. */
function placePortTags(tags, mode) {
  const GAP = 6;
  const STEP = 3;
  const MARGIN = 2;
  const placed = tags.map(() => null);
  if (!tags.length) return placed;
  const cell = Math.max(...tags.map((tag) => tag.h)) || 15;
  const grid = new Map();
  const cellsOf = (r) => {
    const keys = [];
    const i1 = Math.floor((r.x2 + MARGIN) / cell);
    const j1 = Math.floor((r.y2 + MARGIN) / cell);
    for (let i = Math.floor((r.x1 - MARGIN) / cell); i <= i1; i++) {
      for (let j = Math.floor((r.y1 - MARGIN) / cell); j <= j1; j++) {
        keys.push(i * 65536 + j);
      }
    }
    return keys;
  };
  const hits = (r) => {
    for (const key of cellsOf(r)) {
      for (const o of grid.get(key) || []) {
        if (r.x1 < o.x2 + MARGIN && o.x1 < r.x2 + MARGIN &&
            r.y1 < o.y2 + MARGIN && o.y1 < r.y2 + MARGIN) return true;
      }
    }
    return false;
  };
  const add = (r) => {
    for (const key of cellsOf(r)) {
      if (!grid.has(key)) grid.set(key, []);
      grid.get(key).push(r);
    }
  };
  const inBox = (box, gx, gy) => (p) =>
    p.x > box.left - gx && p.x < box.right + gx &&
    p.y > box.top - gy && p.y < box.bottom + gy;
  // a polyline walked by the distance along it
  const route = (points) => {
    const at = [0];
    for (let i = 1; i < points.length; i++) {
      at.push(at[i - 1] + Math.hypot(points[i].x - points[i - 1].x,
                                     points[i].y - points[i - 1].y));
    }
    const length = at[at.length - 1];
    const segment = (s) => {
      let i = 1;
      while (i < at.length - 1 && at[i] < s) i++;
      return i;
    };
    return {
      length,
      point(s) {
        if (s <= 0 || !length) return points[0];
        if (s >= length) return points[points.length - 1];
        const i = segment(s);
        const k = (s - at[i - 1]) / ((at[i] - at[i - 1]) || 1);
        return {
          x: points[i - 1].x + (points[i].x - points[i - 1].x) * k,
          y: points[i - 1].y + (points[i].y - points[i - 1].y) * k,
        };
      },
      direction(s) {
        const i = segment(Math.min(Math.max(s, 0), length));
        const dx = points[i].x - points[i - 1].x;
        const dy = points[i].y - points[i - 1].y;
        const d = Math.hypot(dx, dy) || 1;
        return { x: dx / d, y: dy / d };
      },
    };
  };
  // where the line leaves a region it starts in, or null if it never does
  const leaves = (way, inside) => {
    if (inside(way.point(way.length))) return null;
    if (!inside(way.point(0))) return 0;
    let lo = 0;
    let hi = way.length;
    for (let k = 0; k < 24; k++) {
      const mid = (lo + hi) / 2;
      if (inside(way.point(mid))) lo = mid;
      else hi = mid;
    }
    return hi;
  };
  // the last point before the line enters a region it ends in
  const enters = (way, inside) => {
    if (inside(way.point(0))) return null;
    if (!inside(way.point(way.length))) return way.length;
    let lo = 0;
    let hi = way.length;
    for (let k = 0; k < 24; k++) {
      const mid = (lo + hi) / 2;
      if (inside(way.point(mid))) hi = mid;
      else lo = mid;
    }
    return lo;
  };
  // just past the grown box, straight from its middle towards `to`
  const pastBox = (box, from, to, gx, gy) => {
    let dx = to.x - from.x;
    let dy = to.y - from.y;
    if (!dx && !dy) dy = -1;
    const reach = (low, high, origin, d) =>
      d > 0 ? (high - origin) / d : d < 0 ? (low - origin) / d : Infinity;
    const t = Math.min(
      reach(box.left - gx, box.right + gx, from.x, dx),
      reach(box.top - gy, box.bottom + gy, from.y, dy),
    ) * 1.0001;
    return { x: from.x + dx * t, y: from.y + dy * t };
  };

  const order = tags.map((tag, index) => {
    const hw = tag.w / 2;
    const hh = tag.h / 2;
    const way = route(tag.line);
    const onOwn = inBox(tag.own, GAP + hw, GAP + hh);
    const start = leaves(way, onOwn);
    const first = start === null
      ? pastBox(tag.own, tag.line[0], tag.line[tag.line.length - 1], GAP + hw, GAP + hh)
      : way.point(start);
    let limit = start;
    if (start !== null) {
      if (tag.shared) {
        const from = leaves(way, inBox(tag.own, 0, 0)) ?? way.length;
        const to = tag.far ? enters(way, inBox(tag.far, 0, 0)) ?? 0 : way.length;
        const middle = (from + to) / 2;
        const u = way.direction(middle);
        limit = middle - (hw * Math.abs(u.x) + hh * Math.abs(u.y));
      } else if (tag.far) {
        limit = enters(way, inBox(tag.far, GAP + hw, GAP + hh)) ?? start;
      } else {
        limit = way.length;
      }
    }
    const angle = Math.atan2(first.y - tag.line[0].y, first.x - tag.line[0].x);
    return { index, tag, hw, hh, way, onOwn, start, first, limit, angle };
  });
  order.sort((a, b) =>
    a.tag.rank - b.tag.rank ||
    (a.tag.node < b.tag.node ? -1 : a.tag.node > b.tag.node ? 1 : 0) ||
    a.angle - b.angle);

  for (const o of order) {
    const box = (c) => ({ x1: c.x - o.hw, x2: c.x + o.hw, y1: c.y - o.hh, y2: c.y + o.hh, c });
    let chosen = null;
    let fallback = box(o.first);
    if (o.start === null) {
      if (!hits(fallback)) chosen = fallback;
    } else {
      // no free line at all: the first point only
      const last = Math.max(o.start, o.limit);
      fallback = box(o.way.point(last));
      for (let s = o.start, tries = 0; s <= last + 1e-6 && tries < 400; s += STEP, tries++) {
        const c = o.way.point(s);
        if (o.onOwn(c)) continue;
        const r = box(c);
        if (!hits(r)) {
          chosen = r;
          break;
        }
      }
    }
    if (!chosen && mode === "selected") chosen = fallback;
    if (!chosen) continue;
    add(chosen);
    placed[o.index] = chosen.c;
  }
  return placed;
}

// how many points of a curved line the placement walks along
const PORT_TAG_SAMPLES = 16;
// how far past the edge of the screen "everywhere" still places tags,
// in screen pixels: a tag stands well within that of its node
const PORT_TAG_VIEW_MARGIN = 300;
// the last placement and what it was made for
let portTagPlacement = { key: null, tags: [] };

/* The tags of this frame where they go: placed again only when the map
   moved, the zoom, the selection, the mode or the data changed. A
   frame drawn for a hover or a redraw draws what is there. */
function portTagsInPlace(ctx) {
  let drawn = portTagsToDraw();
  if (!drawn.length) return [];
  const scale = network.getScale();
  const positions = network.getPositions();
  // "everywhere" places what is on the screen and around it: most of a
  // big map is off it, and placing its tags would be work for nothing
  let view = "";
  if (portLabelMode === "all") {
    const margin = PORT_TAG_VIEW_MARGIN;
    const a = network.DOMtoCanvas({ x: -margin, y: -margin });
    const b = network.DOMtoCanvas({
      x: els.network.clientWidth + margin, y: els.network.clientHeight + margin,
    });
    drawn = drawn.filter(([, end]) => {
      const p = positions[end];
      return p && p.x >= a.x && p.x <= b.x && p.y >= a.y && p.y <= b.y;
    });
    view = [a.x, a.y, b.x, b.y].map((v) => Math.round(v * scale)).join(",");
  }
  // where the nodes of these lines are, to a quarter of a pixel
  let moved = 0;
  for (const [entry] of drawn) {
    for (const id of [entry.from, entry.to]) {
      const p = positions[id];
      if (!p) continue;
      moved = (moved * 31 + Math.round(p.x * scale * 4)) | 0;
      moved = (moved * 31 + Math.round(p.y * scale * 4)) | 0;
    }
  }
  const key = [
    portLabelMode, portNumbersOnly, scale, portDataVersion, moved, drawn.length, view,
    network.getSelectedNodes().join(), network.getSelectedEdges().join(),
  ].join("|");
  if (key === portTagPlacement.key) return portTagPlacement.tags;
  const selected = portLabelMode === "all"
    ? new Set(portTagsFor("selected").map(([entry, end]) => entry.id + "\u0000" + end))
    : null;
  const tags = [];
  for (const [entry, end] of drawn) {
    const point = edgePointFn(entry, end, positions);
    if (!point) continue;
    const other = end === entry.from ? entry.to : entry.from;
    const line = [];
    for (let i = 0; i <= PORT_TAG_SAMPLES; i++) {
      const p = point(i / PORT_TAG_SAMPLES);
      line.push({ x: p.x * scale, y: p.y * scale });
    }
    const box = (id) => {
      const b = network.getBoundingBox(id);
      return b && {
        left: b.left * scale, right: b.right * scale,
        top: b.top * scale, bottom: b.bottom * scale,
      };
    };
    const own = box(end);
    if (!own) continue;
    const at = entry.ends[end];
    const text = portTagText(at.port, at.members, at.prefix, portNumbersOnly);
    const link = entry.id.startsWith("link:");
    const chosen = !selected || selected.has(entry.id + "\u0000" + end);
    tags.push({
      text, alarm: entry.alarm,
      w: portTagWidth(ctx, text) + PORT_TAG_PAD_X * 2,
      h: PORT_TAG_HEIGHT,
      line, own, far: box(other),
      shared: Object.keys(entry.ends).length > 1,
      rank: (chosen ? 0 : 2) + (link ? 0 : 1),
      node: end,
    });
  }
  const at = placePortTags(tags, portLabelMode);
  const result = [];
  tags.forEach((tag, i) => {
    if (at[i]) result.push({ ...tag, x: at[i].x / scale, y: at[i].y / scale });
  });
  portTagPlacement = { key, tags: result };
  return result;
}

// tag text -> its width at PORT_TAG_FONT px, measured once
const portTagWidths = new Map();

function portTagFont(size) {
  return `600 ${size}px system-ui, sans-serif`;
}

function portTagWidth(ctx, text) {
  let width = portTagWidths.get(text);
  if (width === undefined) {
    ctx.save();
    ctx.font = portTagFont(PORT_TAG_FONT);
    width = ctx.measureText(text).width;
    ctx.restore();
    portTagWidths.set(text, width);
  }
  return width;
}

/* Drawn from afterDrawing, over the lines and the nodes */
function drawPortTags(ctx) {
  if (portLabelMode === "off" || !portEdges.size) return;
  const scale = network.getScale();
  if (portLabelMode === "all" && scale < PORT_LABELS_MIN_SCALE) return;
  const tags = portTagsInPlace(ctx);
  if (!tags.length) return;
  ctx.save();
  ctx.font = portTagFont(PORT_TAG_FONT / scale);
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (const tag of tags) {
    const w = tag.w / scale;
    const h = tag.h / scale;
    ctx.fillStyle = tag.alarm ? colors.alarm : colors.ok;
    roundedRect(ctx, tag.x - w / 2, tag.y - h / 2, w, h, 3 / scale);
    ctx.fill();
    ctx.fillStyle = colors.panel;
    ctx.fillText(tag.text, tag.x, tag.y + 0.5 / scale);
  }
  ctx.restore();
}

function applyPortLabelMode() {
  // the menu's item, in the middle of a sentence
  const item = t("portLabels_" + portLabelMode);
  const mode = item.charAt(0).toLocaleLowerCase(lang) + item.slice(1);
  labelIcon(els.portLabelsBtn, fmt("portLabelsBtn", { mode }));
  els.portLabelsBtn.classList.toggle("active", portLabelMode !== "off");
  els.portLabelsMenu.setAttribute("aria-label", t("portLabelsMenu"));
}

function setPortLabelMode(mode) {
  portLabelMode = mode;
  localStorage.setItem(PORT_LABELS_KEY, mode);
  applyPortLabelMode();
  if (network) network.redraw();
}

function portLabelItems() {
  return [
    ...PORT_LABEL_MODES.map((mode) => ({
      key: mode, group: "mode", label: t("portLabels_" + mode),
      checked: mode === portLabelMode,
    })),
    // below a line, and kept with the tags off too: it changes nothing
    // then, and is there when they come back
    { key: "numbers", group: "text", label: t("portLabelsNumbers"),
      checkbox: true, checked: portNumbersOnly },
  ];
}

function setPortNumbersOnly(on) {
  portNumbersOnly = on;
  localStorage.setItem(PORT_NUMBERS_KEY, on ? "1" : "0");
  if (network) network.redraw();
}

function openPortLabelsMenu() {
  closeNodeMenu();
  closeMoreMenu(false);
  els.portLabelsMenu.replaceChildren(...menuButtons(portLabelItems(), ["mode", "text"], (item) => {
    closePortLabelsMenu(true);
    if (item.key === "numbers") setPortNumbersOnly(!portNumbersOnly);
    else setPortLabelMode(item.key);
  }));
  els.portLabelsMenu.classList.remove("hidden");
  els.portLabelsBtn.setAttribute("aria-expanded", "true");
  // under the button, its left edge on the button's
  const button = els.portLabelsBtn.getBoundingClientRect();
  const width = els.portLabelsMenu.offsetWidth;
  menuUnder(els.portLabelsMenu, button.bottom + 6);
  els.portLabelsMenu.style.left =
    Math.max(4, Math.min(button.left, window.innerWidth - width - 8)) + "px";
  const current = els.portLabelsMenu.querySelector('[aria-checked="true"]');
  if (current) current.focus();
}

function closePortLabelsMenu(returnFocus) {
  if (els.portLabelsMenu.classList.contains("hidden")) return;
  els.portLabelsMenu.classList.add("hidden");
  els.portLabelsMenu.replaceChildren();
  els.portLabelsBtn.setAttribute("aria-expanded", "false");
  if (returnFocus) els.portLabelsBtn.focus();
}

/* ---------- vis-network's internals ----------

   What MoonLan needs from vis that vis does not publish, in one place
   (v0.8.3), each looked for once the network is made:

   - curves: edgeType.via and positionBezierNode — the bends of a
     dragged node's lines follow it while the layout is frozen;
   - curvePoints: edgeType.getPoint — port tags along a curved line;
   - hold: body.eventListeners and canvas.hammer — a long press is a
     press, not a selection that adds;
   - labels: labelModule.size — the backdrop under the selected caption;
   - geometry: shape.resize, labelModule.getTextSize, canvas.frame — the
     size of a node and its caption for an export;
   - fixed: options.fixed of a node — the check that a pinned node was
     loosened for a drag.

   vis-network 9.1.9 is served by MoonLan itself and does not change
   under the page; a later version might. What is missing is said once
   in the console and its feature is off — a straight line for the
   tags, the bounding box for an export, no backdrop — and the map
   still draws, moves and selects. */
const visInternals = {
  curves: false, curvePoints: false, hold: false, labels: false, geometry: false, fixed: false,
};

function checkVisInternals(net) {
  const body = net.body || {};
  const nodes = Object.values(body.nodes || {});
  const edges = Object.values(body.edges || {});
  const node = nodes.find((n) => n && n.options) || {};
  const types = edges.map((e) => e && e.edgeType).filter(Boolean);
  const canvas = net.canvas || {};
  const found = {
    curves: !!body.edges && types.every((type) => !type.via || typeof type.positionBezierNode === "function"),
    curvePoints: !!body.edges && types.every((type) => typeof type.getPoint === "function"),
    hold: !!(body.eventListeners && typeof body.eventListeners.onTap === "function" &&
      canvas.hammer && typeof canvas.hammer.on === "function"),
    labels: !!body.nodes && (!nodes.length || !!(node.labelModule && "size" in node.labelModule)),
    geometry: !!(canvas.frame && canvas.frame.canvas && typeof canvas.frame.canvas.getContext === "function") &&
      (!nodes.length || !!(node.shape && typeof node.shape.resize === "function" &&
        node.labelModule && typeof node.labelModule.getTextSize === "function")),
    fixed: !!body.nodes && (!nodes.length || !!(node.options && node.options.fixed)),
  };
  for (const [name, ok] of Object.entries(found)) {
    if (!ok && visInternals[name] !== "warned") {
      console.warn(`MoonLan: vis-network has no ${name} internals here; that feature is off`);
    }
    visInternals[name] = ok ? true : "warned";
  }
}

/* ---------- label backdrop for the selected node ---------- */

/* Where the node's caption is drawn, in canvas coordinates. vis knows
   it exactly (the label was measured on the previous frame); the
   fallback measures the text under the node's bounding box. */
function labelBox(ctx, nodeId) {
  const node = visInternals.labels === true && network.body.nodes[nodeId];
  const measured = node && node.labelModule && node.labelModule.size;
  if (measured && measured.width) {
    return {
      x: measured.left,
      y: measured.top,
      w: measured.width,
      h: measured.height,
    };
  }
  const item = nodesDs.get(nodeId);
  const pos = network.getPositions([nodeId])[nodeId];
  if (!item || !pos) return null;
  const box = network.getBoundingBox(nodeId);
  const font = (item.font || {}).size || 14;
  ctx.font = font + "px " + ((item.font || {}).face || "system-ui");
  const lines = String(item.label || "").split("\n");
  const w = Math.max(...lines.map((line) => ctx.measureText(line).width));
  const h = lines.length * font * 1.25;
  return { x: pos.x - w / 2, y: box.bottom - h, w, h };
}

function roundedRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  if (ctx.roundRect) {
    ctx.roundRect(x, y, w, h, r);
    return;
  }
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/* Drawn from beforeDrawing, i.e. under the nodes and their captions */
function drawLabelBackdrop(ctx, nodeId, strong) {
  const box = labelBox(ctx, nodeId);
  if (!box || !box.w) return;
  const padX = 6;
  const padY = 4;
  ctx.save();
  ctx.globalAlpha = strong ? 0.85 : 0.5;
  ctx.fillStyle = colors.panel;
  roundedRect(ctx, box.x - padX, box.y - padY, box.w + padX * 2, box.h + padY * 2, 4);
  ctx.fill();
  if (strong) {
    ctx.globalAlpha = 1;
    ctx.strokeStyle = colors.line;
    ctx.lineWidth = 1;
    ctx.stroke();
  }
  ctx.restore();
}

/* The selected caption is also brighter than the others */
function setSelectedNode(id) {
  const previous = selectedNodeId;
  selectedNodeId = id;
  const updates = [];
  for (const nodeId of [previous, id]) {
    if (nodeId && nodesDs && nodesDs.get(nodeId)) {
      updates.push({ id: nodeId, font: nodeFont(nodeId) });
    }
  }
  if (updates.length) nodesDs.update(updates);
  if (network) network.redraw();
}

/* The drop of a drag. In arrange mode a drag pins the node. Outside
   it, a drag is somebody looking at the map — unless the node was
   already pinned, in which case it keeps its pin and takes its new
   coordinates with it. */
function handleDragEnd(params) {
  if (!params.nodes.length) return;
  const at = network.getPositions(params.nodes);
  const moved = {};
  for (const id of params.nodes) {
    if (!at[id]) continue;
    if (arrangeMode || isPinned(id)) moved[id] = at[id];
  }
  const ids = Object.keys(moved);
  if (!ids.length) return;
  // A viewer can look by dragging, but a pinned node is the shared
  // picture: it goes back where it was, and the page says why.
  if (needRole("user")) {
    nodesDs.update(ids.map((id) => ({
      id: id, x: savedLayout[id].x, y: savedLayout[id].y,
      fixed: { x: true, y: true },
    })));
    showToast(fmt("dragNotSaved", { role: t("role_user") }));
    return;
  }
  // vis has just put back the `fixed` it recorded at dragStart —
  // which for a pinned node is the `false` loosenForDrag gave it.
  // Hold the nodes again here and now: saving is a round trip, and
  // the physics engine would carry them off while it is under way.
  nodesDs.update(ids.map((id) => ({
    id: id, x: moved[id].x, y: moved[id].y, fixed: { x: true, y: true },
  })));
  // In arrange mode the neighbours go with the pin, as they stood
  // when the drag began. Moving a pinned node outside it leaves
  // what was recorded alone: offsets move with their node anyway.
  const around = {};
  if (arrangeMode && dragNeighbours) {
    for (const id of ids) if (dragNeighbours[id]) around[id] = dragNeighbours[id];
  }
  dragNeighbours = null;
  pinNodes(moved, around);
}

function renderGraph() {
  const { nodes, edges } = buildGraphData();
  // Anything nobody pinned starts next to what it is plugged into, laid
  // out from the pinned nodes outwards
  seedPositions(nodes, edges);

  if (!network) {
    nodesDs = new vis.DataSet(nodes);
    edgesDs = new vis.DataSet(edges);
    const options = {
      physics: {
        solver: "forceAtlas2Based",
        forceAtlas2Based: { gravitationalConstant: -60, springLength: 90 },
        stabilization: { iterations: 200 },
      },
      // Ctrl+click adds and removes a node, and dragging one of
      // several selected nodes moves the whole selection — both of
      // them vis's own behaviour, which is better than a reimplementation
      interaction: { hover: true, multiselect: true },
      nodes: { borderWidthSelected: 4 },
      // curves on support nodes of the physics, as vis has them by
      // default; parallel links are curved one by one (curveParallelEdges)
      edges: { smooth: { type: "dynamic" } },
      // Every node arrives with a position, but whatever vis still
      // decides on its own should come out the same in every tab
      layout: { randomSeed: 7 },
    };
    network = new vis.Network(
      els.network,
      { nodes: nodesDs, edges: edgesDs },
      options
    );
    // No guard against "the click that ends a drag": there is no such
    // click. vis takes a gesture as a tap or as a pan, never both, and
    // v0.7.1's guard waited for a click that never came — so it
    // swallowed the next real one, and after moving a node the first
    // click on anything opened nothing.
    network.on("dragStart", (params) => {
      loosenForDrag(params.nodes);
      holdCurves(params.nodes);
    });
    network.on("dragging", (params) => followCurves(params.nodes));
    // Double click on a container takes everything on it: the cloud of
    // devices behind one switch is the thing people want to move out
    // of the way in one go.
    network.on("doubleClick", (params) => {
      if (!params.nodes.length) return;
      const ids = devicesUnder(params.nodes[0]);
      if (!ids.length) return;
      network.selectNodes(ids, false);
      setSelectedNode(null);
      hideDetails();
    });
    network.on("dragEnd", (params) => {
      dragging.clear();
      handleDragEnd(params);
      // wherever the drop left the nodes — a viewer's pinned node goes
      // back to its place — their lines go along
      followCurves(params.nodes);
      heldCurves = null;
    });
    network.on("oncontext", (params) => {
      params.event.preventDefault();
      const id = network.getNodeAt(params.pointer.DOM);
      // In arrange mode the right button is the undo of the left one:
      // it releases the node, and asks nothing.
      if (arrangeMode && id && isPinned(id)) {
        unpinNode(id);
        return;
      }
      if (!id) {
        closeNodeMenu();
        return;
      }
      // Right-clicking inside the selection works on all of it;
      // right-clicking outside it means that node instead
      const selected = network.getSelectedNodes();
      const ids = selected.includes(id) ? selected : [id];
      if (!selected.includes(id)) network.selectNodes([id], false);
      openNodeMenu(ids, {
        x: params.event.clientX,
        y: params.event.clientY,
      });
    });
    network.on("click", (params) => {
      closeNodeMenu();
      // Ctrl+click is "add to the selection", not "look at this one"
      if (params.event && (params.event.srcEvent || {}).ctrlKey) return;
      if (params.nodes.length) {
        setSelectedNode(params.nodes[0]);
        showDetails(params.nodes[0]);
      } else if (
        params.edges.length &&
        String(params.edges[0]).startsWith("link:")
      ) {
        setSelectedNode(null);
        showLinkDetails(params.edges[0]);
      } else {
        setSelectedNode(null);
        hideDetails();
      }
    });
    checkVisInternals(network);
    if (visInternals.hold === true) holdIsAPress();
    network.on("beforeDrawing", (ctx) => {
      if (hoveredNodeId && hoveredNodeId !== selectedNodeId) {
        drawLabelBackdrop(ctx, hoveredNodeId, false);
      }
      if (selectedNodeId) drawLabelBackdrop(ctx, selectedNodeId, true);
    });
    network.on("afterDrawing", drawPortTags);
    // the tags follow the selection
    network.on("select", () => network.redraw());
    network.on("deselect", () => network.redraw());
    network.on("hoverNode", (params) => {
      hoveredNodeId = params.node;
      network.redraw();
    });
    network.on("blurNode", () => {
      hoveredNodeId = null;
      network.redraw();
    });
    applyFreeze();  // a freeze chosen earlier survives a reload
    return;
  }

  // Silent update: change data inside the DataSet without recreating
  // the Network, so node positions and the camera are preserved
  const nodeIds = new Set(nodes.map((n) => n.id));
  const edgeIds = new Set(edges.map((e) => e.id));
  nodesDs.remove(nodesDs.getIds().filter((id) => !nodeIds.has(id)));
  edgesDs.remove(edgesDs.getIds().filter((id) => !edgeIds.has(id)));
  nodesDs.update(nodes);
  edgesDs.update(edges);
}

/* A long press is a press.

   With `multiselect` on — v0.7.1 turned it on for Ctrl+click — vis
   reads a press held for a quarter of a second as "add this node to
   the selection", Ctrl or not, and on a node already selected as "take
   it out". People rarely move the mouse the instant the button goes
   down. So pressing a switch unhurriedly and dragging it carried along
   whatever had been selected before — a pinned router included, saved
   in its new place — and pressing unhurriedly on one node of a group
   dropped it from the group just before the group was to be dragged.

   Nothing needs to happen at the moment of holding. If a drag follows,
   vis picks the node under the pointer itself, or the whole selection
   when that node is part of it. If the button comes up where it went
   down, it was a click, and vis's own tap handling does the rest —
   Ctrl included.

   Two internals, both of vis-network 9.1.9, which index.html pins:
   the canvas looks `onHold` up in body.eventListeners on every press,
   and the Hammer instance is canvas.hammer. */
function holdIsAPress() {
  network.body.eventListeners.onHold = () => {};
  network.canvas.hammer.on("pressup", (event) =>
    network.body.eventListeners.onTap(event)
  );
}

function focusNode(id) {
  if (!network) return;
  network.focus(id, { scale: 1.2, animation: true });
  network.selectNodes([id]);
  setSelectedNode(id);
  showDetails(id);
}

/* ---------- detail cards ---------- */

/* Host behind a details id: "host:<mac>" on the map, "unloc:<mac>" off it */
function findHost(nodeId) {
  const mac = nodeId.slice(nodeId.indexOf(":") + 1);
  const list = nodeId.startsWith("unloc:")
    ? topology.unlocated || []
    : topology.hosts;
  return list.find((h) => h.mac === mac);
}

/* The "Loop Detection" row of a switch card: what the switch says,
   or that it says nothing. "No loops" is a claim; a model that keeps
   no loop state in any readable MIB does not get to make it. */
function loopCardLine(loop) {
  if (!loop || loop.polled === false) {
    // the counters cycle has not run since this switch was polled
    return t("loopCardNotPolled");
  }
  if (loop.supported === false) {
    return t("loopCardUnsupported");
  }
  if (loop.status === "loop") {
    return fmt("loopCardLoop", {
      ports: (loop.looped_ports || []).join(", "),
    });
  }
  // three-valued: on, off, and "the switch did not tell us". Never a
  // bare truthiness test — `!loop.enabled` reads the third as the
  // second, which is how three switches were reported as unprotected
  if (loop.enabled === false) return t("loopCardOff");
  if (loop.enabled !== true) return t("loopCardUnknown");
  const base = fmt("loopCardOn", {
    interval: loop.interval == null ? "—" : loop.interval,
    recover: loop.recover_time == null ? "—" : loop.recover_time,
  });
  const detail =
    loop.status === "partial" ? ", " + t("loopCardPartial") : "";
  return base + detail;
}

/* The sysObjectID and which profile it matched: the model identity
   behind the row, and the first thing needed to add a new profile. */
function loopCardTitle(loop) {
  if (!loop || !loop.sys_object_id) return "";
  const head = "sysObjectID " + loop.sys_object_id;
  if (!loop.supported) return head;
  return (
    head + "\n" +
    fmt("loopProfileHint", {
      profile: loop.profile,
      // "sysObjectID" or "probe": a word of the page, not the server's
      how: t("loopHow_" + loop.matched_by) === "loopHow_" + loop.matched_by
        ? loop.matched_by : t("loopHow_" + loop.matched_by),
    })
  );
}

/* A switch's name for a card, falling back to its address */
/* What to call a node in a confirmation dialog: its caption if the
   graph has one, its id otherwise. */
function nodeTitle(id) {
  const node = nodesDs && nodesDs.get(id);
  const label = node && node.label ? plainCaption(String(node.label).split("\n")[0]) : "";
  // the pin mark is a state of the node, not a part of its name
  return label.replace(" " + t("pinnedMark"), "").trim() || id;
}

function switchName(ip) {
  const sw = (topology.switches || []).find((s) => s.ip === ip);
  return sw ? sw.name : ip;
}

/* Text from outside the page into HTML: the five characters that could
   end an attribute or open a tag. */
function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
}

/* Markup with everything from outside escaped (v0.8.3). The cards were
   template strings with an escapeHtml() here and there, and the ones
   left out put a switch's sysName, an LLDP neighbour's name or a DNS
   name into the page as markup: a laptop announcing itself as
   <img src=x onerror=…> ran a script in whoever opened its card. In
   html`…` every ${…} is escaped — text, a number, an attribute value;
   markup goes in only as another html`…` or as raw(…), so what can be
   forgotten now is raw, and raw is visible. An array of either is
   joined as it is. */
class Markup {
  constructor(text) {
    this.text = text;
  }

  toString() {
    return this.text;
  }
}

function raw(text) {
  return new Markup(text == null ? "" : String(text));
}

function htmlValue(value) {
  if (value instanceof Markup) return value.text;
  if (Array.isArray(value)) return value.map(htmlValue).join("");
  if (value == null || value === false) return "";
  return escapeHtml(value);
}

function html(strings, ...values) {
  let out = strings[0];
  values.forEach((value, i) => {
    out += htmlValue(value) + strings[i + 1];
  });
  return new Markup(out);
}

/* Markup items with text between them: "Gi0/1, Gi0/2" of two dots */
function htmlJoin(items, separator) {
  return raw(items.map(htmlValue).join(escapeHtml(separator)));
}

/* fmt() for markup: the translation is ours and may hold markup, the
   parameters are not — a switch's name, a port — and are escaped */
function fmtHtml(key, params = {}) {
  const escaped = {};
  for (const [name, value] of Object.entries(params)) escaped[name] = htmlValue(value);
  return raw(fmt(key, escaped));
}

/* Who made a device, from `vendor` and `vendor_status`: the name as the
   IEEE register has it, or which of the other cases it is — a random
   or hand-set address has no maker at all, which is not the same as one
   the register does not list. Plain text, for a tooltip or a search. */
function vendorText(item) {
  if (!item || !item.vendor_status) return "";
  return item.vendor_status === "found" ? item.vendor : t("vendor_" + item.vendor_status);
}

/* The card's "Maker" row; none for an address that is no MAC. */
function vendorRow(item) {
  if (!item || !item.vendor_status || item.vendor_status === "invalid") return "";
  // words, not an address: wrapped between words, unlike the MAC above
  let value = html`<span class="vendor${item.vendor_status === "found" ? "" : " muted"}">${
    vendorText(item)}</span>`;
  if (item.vendor_status === "no_registry") {
    // the cure is a command on the server: said to whoever can run it
    value = html`<span class="muted" title="${fmt("vendorNoRegistryHint", {
      command: "python -m moonlan.oui update" })}">${t("vendor_no_registry")}</span>`;
    if (me && (me.sign_in === false || me.role === "admin")) {
      value = html`${value}<div class="hint-line"><code>python -m moonlan.oui update</code></div>`;
    }
  }
  return html`<dt>${t("vendorLabel")}</dt><dd>${value}</dd>`;
}

/* An explanation on a card, in one line that names the fact itself —
   what is unusual or unknown about this thing. Why, and what to do
   about it, is the full text: a tooltip on the line, and behind ⓘ a
   paragraph that opens in place, because a phone has no hover. */
function hintBlock(shortText, longText) {
  return html`<p class="hint" title="${longText}">${shortText} <button type="button" class="hint-more" aria-expanded="false" aria-label="${t("hintMore")}">ⓘ</button></p><p class="hint hint-full hidden">${longText}</p>`;
}

/* hintBlock() for a key whose short line is `<key>Short` */
function hintKey(key, params = {}) {
  return hintBlock(fmt(key + "Short", params), fmt(key, params));
}

/* ---------- the map, exported (v0.8.1) ----------

   The export takes what is on the screen: the same nodes, lines and
   layout. The layout lives in this browser only — the physics lays out
   whatever nobody pinned on every viewer's screen — so the picture is
   made here, not on the server. exportModel() is that picture as plain
   data; the Draw.io, SVG and print builders below draw it on paper with
   a light palette, by what each node and line means (`semantic`), not
   by its colour on the dark map. */

/* `n` with the word that goes with it: "1 device", "23 devices",
   "23 устройства", "25 устройств" */
function plural(key, n) {
  const form = new Intl.PluralRules(locale()).select(n);
  const text = t(key + "_" + form);
  return (text === key + "_" + form ? t(key + "_other") : text).replace("{n}", n);
}

/* How many devices a switch or a switch without SNMP has behind it —
   the line under it when the devices are not drawn */
function deviceCount(nodeId) {
  if (nodeId.startsWith("sw:")) {
    const ip = nodeId.slice(3);
    return topology.hosts.filter((h) => h.switch === ip && !h.merged_into).length;
  }
  if (nodeId.startsWith("pseudo:")) return topology.hosts.filter((h) => h.via === nodeId).length;
  return null;
}

/* A node's shape and caption, measured: its own size, and its caption
   inside it (a box, an ellipse) or under it (a dot, a square, a
   diamond, a hexagon). Not getBoundingBox: vis places a caption only
   when it draws the node, and it draws only what is on the screen —
   the box of a node off the screen reaches back to wherever its caption
   was last drawn. shape.resize and labelModule.getTextSize are
   internal API of vis-network 9.1.9, which index.html pins. */
function nodeGeometry(id) {
  if (visInternals.geometry !== true) {
    // without vis's own measures: the box vis reports, caption and all
    const b = network.getBoundingBox(id);
    const box = { left: b.left, right: b.right, top: b.top, bottom: b.bottom };
    return { w: b.right - b.left, h: b.bottom - b.top, inside: true, text: { width: 0, height: 0 }, box };
  }
  const node = network.body.nodes[id];
  const ctx = network.canvas.frame.canvas.getContext("2d");
  node.shape.resize(ctx, false, false);
  const text = node.labelModule.getTextSize(ctx, false, false);
  const inside = ["box", "ellipse", "circle", "database", "text"].includes(node.options.shape);
  const w = node.shape.width;
  const h = node.shape.height;
  const box = inside
    ? { left: node.x - w / 2, right: node.x + w / 2, top: node.y - h / 2, bottom: node.y + h / 2 }
    : {
      left: node.x - Math.max(w, text.width) / 2, right: node.x + Math.max(w, text.width) / 2,
      top: node.y - h / 2, bottom: node.y + h / 2 + 2 + text.height,
    };
  return { w, h, inside, text, box };
}

/* The physics is still laying the map out: an export now is the
   picture of this moment. Not by vis's `stabilized` (v0.8.2): the
   physics never stops here, and on a big map a few nodes keep creeping
   a pixel or two a second, above vis's minVelocity — the flag never
   came, nor did a "stabilized" event, and the window said "still being
   laid out" over a map that stood still to the eye. Measured on the
   demo grown to 683 nodes: hundreds of pixels a second after a refresh,
   at most 2.5 once it had settled. So what counts is what the eye
   sees: a node faster than LAYOUT_STILL_PX a second on the screen
   within the last LAYOUT_STILL_MS (watchLayoutMotion, every second). */
const LAYOUT_STILL_PX = 3;
const LAYOUT_STILL_MS = 2500;
let layoutMovedAt = 0;
let layoutSample = null;

function watchLayoutMotion() {
  if (!network) return;
  const now = performance.now();
  const positions = network.getPositions();
  if (layoutSample) {
    const seconds = (now - layoutSample.at) / 1000 || 1;
    const scale = network.getScale();
    for (const [id, p] of Object.entries(positions)) {
      // a node that was not there a second ago is placed, not laid out
      const before = layoutSample.positions[id];
      if (!before) continue;
      if (Math.hypot(p.x - before.x, p.y - before.y) * scale / seconds > LAYOUT_STILL_PX) {
        layoutMovedAt = now;
        break;
      }
    }
  }
  layoutSample = { at: now, positions };
}

function layoutSettling() {
  if (!network || layoutFrozen) return false;
  // not measured yet: the map has only just been drawn
  return !layoutSample || performance.now() - layoutMovedAt < LAYOUT_STILL_MS;
}

/* The map as data. `contents`: "infrastructure" — switches, switches
   without SNMP, bridges, external networks, groups, with the number of
   devices under a switch — or "all", as on the screen. `ports`: the
   port numbers at the ends of the lines, as the map words them. */
function exportModel({ contents, ports }) {
  const all = contents === "all";
  const positions = network.getPositions();
  const nodes = [];
  const kept = new Set();
  for (const node of nodesDs.get()) {
    if (!all && node.id.startsWith("host:")) continue;
    const pos = positions[node.id];
    if (!pos) continue;
    const shape = nodeGeometry(node.id);
    const item = noteItemOf(node.id) || {};
    const name = nodeNames.get(node.id) || "";
    let lines = plainCaption(node.label || "").replace(" " + t("pinnedMark"), "")
      .split("\n").map((line) => line.trim()).filter(Boolean);
    if (name) lines = lines.slice(1);
    const devices = all ? null : deviceCount(node.id);
    if (devices) lines.push(plural("exportDevices", devices));
    const semantic = node.semantic || [node.id.slice(0, node.id.indexOf(":"))];
    nodes.push({
      id: node.id, kind: semantic[0], flags: semantic.slice(1),
      // the shape's own size; the caption is inside it or under it
      x: pos.x, y: pos.y, w: shape.w, h: shape.h, captionInside: shape.inside,
      name, lines, note: (item.note && item.note.note) || "",
      ip: item.ip || item.mgmt_ip || "", mac: item.mac || item.chassis_id || "",
    });
    kept.add(node.id);
  }
  const edges = [];
  for (const edge of edgesDs.get()) {
    if (!kept.has(edge.from) || !kept.has(edge.to)) continue;
    const a = positions[edge.from];
    const b = positions[edge.to];
    const point = edgePointFn(edge, edge.from, positions);
    const mid = point ? point(0.5) : { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    const entry = portEdges.get(edge.id);
    const tags = {};
    if (ports && entry) {
      for (const [end, at] of Object.entries(entry.ends)) {
        tags[end] = portTagText(at.port, at.members, at.prefix, portNumbersOnly);
      }
    }
    edges.push({
      id: edge.id, from: edge.from, to: edge.to,
      // the bend, as the control point of a quadratic curve through mid
      mid: { x: mid.x, y: mid.y },
      control: { x: 2 * mid.x - (a.x + b.x) / 2, y: 2 * mid.y - (a.y + b.y) / 2 },
      label: plainCaption((edge.paperLabel === undefined ? edge.label : edge.paperLabel) || ""),
      ports: tags, semantic: edge.semantic || [],
    });
  }
  return { nodes, edges };
}

/* Text into XML or HTML: the five characters that could end an attribute
   or open a tag. A line break stays a line break inside an attribute
   only as a character reference — XML turns a bare one into a space. */
function xmlEscape(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;")
    .replace(/\r?\n/g, "&#10;").replace(/\t/g, "&#9;");
}

/* The paper palette, by what a thing is — one for every format, so a
   dashed line means the same in the Draw.io file, the SVG and the PDF,
   and the legend can say what. */
const EXPORT_SHAPES = {
  switch: { fill: "#ffffff", stroke: "#333333", width: 2 },
  pseudo: { fill: "#eeeeee", stroke: "#666666", width: 1.5, dash: "4 3" },
  bridge: { fill: "#eef4fb", stroke: "#4a7fb0", width: 1.5, dash: "5 3" },
  external: { fill: "#fbf3e6", stroke: "#b07a2a", width: 1.5 },
  trunk: { fill: "#eef4fb", stroke: "#4a7fb0", width: 1.5, dash: "4 3" },
  offline: { fill: "#dddddd", stroke: "#999999", width: 1.5, dash: "2 3" },
  host: { fill: "#7fc98f", stroke: "#4f8f5d", width: 1 },
  router: { fill: "#7fc98f", stroke: "#333333", width: 1.5 },
};

function exportNodeLook(kind, flags) {
  const look = { ...(EXPORT_SHAPES[kind] || EXPORT_SHAPES.bridge) };
  if (flags.includes("root")) Object.assign(look, { stroke: "#3a9a50", width: 3 });
  if (flags.includes("loop")) Object.assign(look, { stroke: "#d23f3f", width: 3 });
  if (flags.includes("stale")) Object.assign(look, { fill: "#cccccc", stroke: "#aaaaaa" });
  return look;
}

/* A line on paper: measured (LLDP darker than the MAC tables), guessed
   or unexplained (dashed), held by STP or looping (red), aggregated
   (thicker), a device's (thin), not seen for a while (pale) */
function exportLineLook(semantic) {
  const has = (tag) => semantic.includes(tag);
  let color = "#555555";
  let width = 2;
  let dash = "";
  if (has("lldp")) color = "#222222";
  if (has("fdb")) color = "#6b6b6b";
  if (has("device")) { color = "#8fb3cf"; width = 1; }
  if (has("pseudo") || has("offline")) color = "#888888";
  if (has("bridge") || has("trunk-group")) color = "#4a7fb0";
  if (has("external")) color = "#b07a2a";
  if (has("lacp")) width = 4;
  else if (has("trunk")) width = 3;
  if (has("ring")) color = "#999999";
  if (has("stale")) color = "#c8c8c8";
  if (has("blocking") || has("loop")) color = "#d23f3f";
  if (has("loop")) width = Math.max(width, 3);
  if (has("guess")) dash = "4 6";
  else if (has("ring")) dash = "2 4";
  else if (has("blocking")) dash = "6 4";
  else if (has("approximate")) dash = "2 4";
  else if (["stale", "pseudo", "offline", "bridge", "trunk-group"].some(has)) dash = "3 3";
  return { color, width, dash };
}

/* The map as a Draw.io file (diagrams.net, draw.io desktop): an
   uncompressed mxfile. Pure — the model in, the text out.

   A node is an <object> — label, moonlan_id, ip, mac, note, tooltip,
   which draw.io shows under "Edit Data" and searches — holding an
   mxCell styled by its kind. Its label is HTML (html=1): the name in
   bold, the node's own lines small under it. Text written by people and
   devices goes through two levels of escaping: as HTML into the label,
   then the label as XML into the attribute — "<b>x</b> & \"y\"" is
   read back as exactly that.

   A line has no arrows; its style says what it means; its speed is its
   value; its bend is one point the curve goes through; the port numbers
   are labels of the line itself (relative, ±0.8), so they go with it
   when somebody moves the nodes in draw.io. */
function drawioXml(model, title) {
  const SHAPE = {
    switch: "rounded=1;arcSize=12;fontColor=#111111;fontSize=12;",
    pseudo: "rounded=0;fontColor=#222222;fontSize=11;",
    bridge: "rounded=0;fontColor=#222222;fontSize=11;",
    external: "shape=hexagon;perimeter=hexagonPerimeter2;size=0.25;fontColor=#222222;fontSize=11;",
    trunk: "ellipse;fontColor=#222222;fontSize=10;",
    offline: "rounded=0;fontColor=#555555;fontSize=10;",
    host: "ellipse;aspect=fixed;fontColor=#333333;fontSize=9;",
    router: "rhombus;fontColor=#333333;fontSize=9;",
  };
  // a caption under a small shape is not wrapped to the shape's width
  const INSIDE = "whiteSpace=wrap;";
  const BELOW = "verticalLabelPosition=bottom;verticalAlign=top;labelPosition=center;align=center;";
  const edgeStyle = (semantic) => {
    const look = exportLineLook(semantic);
    return "endArrow=none;startArrow=none;html=1;curved=1;rounded=0;fontSize=10;fontColor=#333333;labelBackgroundColor=#ffffff;" +
      `strokeColor=${look.color};strokeWidth=${look.width};` +
      (look.dash ? `dashed=1;dashPattern=${look.dash};` : "");
  };
  const html = (text) => xmlEscape(text).replace(/&#10;/g, "<br>");
  const label = (node) => {
    const lines = node.lines.map(html);
    if (node.name) {
      return `<b>${html(node.name)}</b>` +
        (lines.length ? `<br><span style="font-size:9px">${lines.join("<br>")}</span>` : "");
    }
    return (lines[0] || "") + (lines.length > 1
      ? `<br><span style="font-size:9px">${lines.slice(1).join("<br>")}</span>` : "");
  };
  const xs = model.nodes.flatMap((n) => [n.x - n.w / 2, n.x + n.w / 2]);
  const ys = model.nodes.flatMap((n) => [n.y - n.h / 2, n.y + n.h / 2 + (n.captionInside ? 0 : 40)]);
  const left = Math.min(0, ...xs) - 40;
  const top = Math.min(0, ...ys) - 40;
  const width = Math.max(800, Math.max(...xs, 0) - left + 40);
  const height = Math.max(600, Math.max(...ys, 0) - top + 40);
  const at = (v, origin) => Math.round((v - origin) * 10) / 10;
  const ids = new Map(model.nodes.map((node, i) => [node.id, "n" + i]));
  const cells = [];
  for (const node of model.nodes) {
    const look = exportNodeLook(node.kind, node.flags);
    const style = (SHAPE[node.kind] || SHAPE.bridge) + "html=1;" +
      (node.captionInside ? INSIDE : BELOW) +
      `fillColor=${look.fill};strokeColor=${look.stroke};strokeWidth=${look.width};` +
      (look.dash ? `dashed=1;dashPattern=${look.dash};` : "");
    cells.push(
      `<object id="${ids.get(node.id)}" label="${xmlEscape(label(node))}" ` +
      `moonlan_id="${xmlEscape(node.id)}" ip="${xmlEscape(node.ip)}" ` +
      `mac="${xmlEscape(node.mac)}" note="${xmlEscape(node.note)}" ` +
      `tooltip="${xmlEscape(node.note)}">` +
      `<mxCell style="${xmlEscape(style)}" vertex="1" parent="1">` +
      `<mxGeometry x="${at(node.x - node.w / 2, left)}" y="${at(node.y - node.h / 2, top)}" ` +
      `width="${Math.round(node.w)}" height="${Math.round(node.h)}" as="geometry"/>` +
      `</mxCell></object>`);
  }
  model.edges.forEach((edge, i) => {
    const id = "e" + i;
    cells.push(
      `<mxCell id="${id}" value="${xmlEscape(html(edge.label))}" ` +
      `style="${xmlEscape(edgeStyle(edge.semantic))}" edge="1" parent="1" ` +
      `source="${ids.get(edge.from)}" target="${ids.get(edge.to)}">` +
      `<mxGeometry relative="1" as="geometry"><Array as="points">` +
      `<mxPoint x="${at(edge.mid.x, left)}" y="${at(edge.mid.y, top)}"/>` +
      `</Array></mxGeometry></mxCell>`);
    for (const [end, text] of Object.entries(edge.ports)) {
      const x = end === edge.from ? -0.8 : 0.8;
      cells.push(
        `<mxCell id="${id}${x < 0 ? "a" : "b"}" value="${xmlEscape(html(text))}" ` +
        `style="edgeLabel;html=1;align=center;verticalAlign=middle;resizable=0;points=[];` +
        `fontSize=10;fontColor=#1d3b24;labelBackgroundColor=#cdeed4;" ` +
        `vertex="1" connectable="0" parent="${id}">` +
        `<mxGeometry x="${x}" relative="1" as="geometry"><mxPoint as="offset"/></mxGeometry>` +
        `</mxCell>`);
    }
  });
  return '<?xml version="1.0" encoding="UTF-8"?>\n' +
    `<mxfile host="MoonLan" agent="MoonLan" type="device">` +
    `<diagram id="moonlan" name="${xmlEscape(title)}">` +
    `<mxGraphModel dx="0" dy="0" grid="0" gridSize="10" guides="1" tooltips="1" ` +
    `connect="1" arrows="1" fold="1" page="1" pageScale="1" ` +
    `pageWidth="${Math.round(width)}" pageHeight="${Math.round(height)}" ` +
    `math="0" shadow="0" background="#ffffff"><root>` +
    `<mxCell id="0"/><mxCell id="1" parent="0"/>` +
    cells.join("") +
    `</root></mxGraphModel></diagram></mxfile>\n`;
}

const EXPORT_FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";

/* A node's shape in SVG, centred on (x, y), by its kind */
function svgShape(kind, x, y, width, height, look) {
  const n = (v) => Math.round(v * 10) / 10;
  const paint = `fill="${look.fill}" stroke="${look.stroke}" stroke-width="${look.width}"` +
    (look.dash ? ` stroke-dasharray="${look.dash}"` : "");
  const hw = width / 2;
  const hh = height / 2;
  if (kind === "host") return `<circle cx="${n(x)}" cy="${n(y)}" r="${n(Math.min(hw, hh))}" ${paint}/>`;
  if (kind === "trunk") return `<ellipse cx="${n(x)}" cy="${n(y)}" rx="${n(hw)}" ry="${n(hh)}" ${paint}/>`;
  if (kind === "router") {
    return `<polygon points="${n(x)},${n(y - hh)} ${n(x + hw)},${n(y)} ${n(x)},${n(y + hh)} ${n(x - hw)},${n(y)}" ${paint}/>`;
  }
  if (kind === "external") {
    const q = hw / 2;
    return `<polygon points="${n(x - hw)},${n(y)} ${n(x - q)},${n(y - hh)} ${n(x + q)},${n(y - hh)} ` +
      `${n(x + hw)},${n(y)} ${n(x + q)},${n(y + hh)} ${n(x - q)},${n(y + hh)}" ${paint}/>`;
  }
  return `<rect x="${n(x - hw)}" y="${n(y - hh)}" width="${n(width)}" height="${n(height)}" ` +
    `rx="${kind === "switch" ? n(Math.min(6, hh / 2)) : 0}" ${paint}/>`;
}

/* What the legend shows for one of its items, in a box 30 by 14 */
function legendSwatch(item) {
  if (item.line) {
    const look = exportLineLook(item.line);
    return `<line x1="1" y1="7" x2="29" y2="7" stroke="${look.color}" stroke-width="${look.width}"` +
      (look.dash ? ` stroke-dasharray="${look.dash}"` : "") + "/>";
  }
  const look = exportNodeLook(item.kind, item.flags || []);
  const small = item.kind === "host" || item.kind === "router";
  return svgShape(item.kind, 15, 7, small ? 11 : 26, small ? 11 : 12, look);
}

// the meanings of lines a legend can have, each drawn as it looks alone
const LEGEND_LINES = [
  ["lldp", ["lldp"]], ["fdb", ["fdb"]], ["lacp", ["lldp", "lacp"]],
  ["trunk", ["lldp", "trunk"]], ["guess", ["fdb", "guess"]], ["ring", ["fdb", "ring"]],
  ["blocking", ["lldp", "blocking"]], ["loop", ["lldp", "loop"]],
  // not seen for a while: the grey device says it, its line goes with it
  ["approximate", ["device", "approximate"]],
];

/* The legend of a drawing: the kinds of nodes and lines it has, and
   nothing it has not — [{kind, flags} | {line}, text] */
function exportLegend(model) {
  const items = [];
  const kinds = new Set(model.nodes.map((node) => node.kind));
  for (const kind of Object.keys(EXPORT_SHAPES)) {
    if (kinds.has(kind)) items.push({ kind, text: t("legend_" + kind) });
  }
  for (const flag of ["root", "loop", "stale"]) {
    const node = model.nodes.find((n) => n.flags.includes(flag));
    if (node) items.push({ kind: node.kind, flags: [flag], text: t("legendFlag_" + flag) });
  }
  for (const [tag, line] of LEGEND_LINES) {
    if (model.edges.some((edge) => edge.semantic.includes(tag))) {
      items.push({ line, text: t("legendLine_" + tag) });
    }
  }
  return items;
}

/* The map as SVG, from the model — for the file and for the printed
   page. Pure: `measure(text, size, bold)` gives a text's width (a
   canvas in the page, an estimate in a test); `legend` is
   exportLegend's items with their title, or null for none. Nodes by
   their kind, lines with their bend (a quadratic curve through the
   middle), the speed in the middle, the port numbers placed as on the
   map — placePortTags, in the drawing's own units; those with no room
   are left out, as "everywhere" leaves them — and under the drawing
   the legend: on paper a dashed line means nothing unless it says
   what. */
function schemaSvg(model, { measure, legend = null }) {
  const esc = xmlEscape;
  const ROW = 13;
  // a caption: the name in bold, then the node's own lines
  const caption = (node) => [
    ...(node.name ? [{ text: node.name, size: 12, bold: true }] : []),
    ...node.lines.map((text, i) => ({
      text, size: node.name || i ? 10 : 11, bold: !node.name && i === 0 && node.captionInside,
    })),
  ];
  const boxOf = (node) => {
    if (node.captionInside) {
      return { left: node.x - node.w / 2, right: node.x + node.w / 2,
               top: node.y - node.h / 2, bottom: node.y + node.h / 2 };
    }
    const rows = caption(node);
    const width = Math.max(node.w, ...rows.map((r) => measure(r.text, r.size, r.bold)));
    return { left: node.x - width / 2, right: node.x + width / 2,
             top: node.y - node.h / 2, bottom: node.y + node.h / 2 + 4 + rows.length * ROW };
  };
  const byId = new Map(model.nodes.map((node) => [node.id, node]));
  const boxes = new Map(model.nodes.map((node) => [node.id, boxOf(node)]));

  // the port numbers, placed along their lines from their own end
  const curve = (a, c, b, t) => ({
    x: (1 - t) * (1 - t) * a.x + 2 * (1 - t) * t * c.x + t * t * b.x,
    y: (1 - t) * (1 - t) * a.y + 2 * (1 - t) * t * c.y + t * t * b.y,
  });
  const tags = [];
  for (const edge of model.edges) {
    const ends = Object.entries(edge.ports);
    for (const [end, text] of ends) {
      const own = byId.get(end);
      const far = byId.get(end === edge.from ? edge.to : edge.from);
      if (!own || !far) continue;
      const points = [];
      for (let i = 0; i <= PORT_TAG_SAMPLES; i++) points.push(curve(own, edge.control, far, i / PORT_TAG_SAMPLES));
      tags.push({
        text, alarm: edge.semantic.includes("blocking") || edge.semantic.includes("loop"),
        w: measure(text, 10, false) + PORT_TAG_PAD_X * 2, h: 14, line: points,
        own: boxes.get(own.id), far: boxes.get(far.id), shared: ends.length > 1,
        // between switches first, as on the map
        rank: edge.id.startsWith("link:") ? 0 : 1, node: end,
      });
    }
  }
  const placed = placePortTags(tags, "all");

  // the extent, with the captions under the shapes and a margin
  const all = boxes.size ? [...boxes.values()] : [{ left: 0, right: 0, top: 0, bottom: 0 }];
  const left = Math.min(...all.map((b) => b.left)) - 30;
  const top = Math.min(...all.map((b) => b.top)) - 30;
  const width = Math.max(...all.map((b) => b.right)) + 30 - left;
  let height = Math.max(...all.map((b) => b.bottom)) + 30 - top;
  const X = (v) => Math.round((v - left) * 10) / 10;
  const Y = (v) => Math.round((v - top) * 10) / 10;
  const out = [];

  for (const edge of model.edges) {
    const a = byId.get(edge.from);
    const b = byId.get(edge.to);
    const look = exportLineLook(edge.semantic);
    out.push(`<path d="M${X(a.x)} ${Y(a.y)} Q${X(edge.control.x)} ${Y(edge.control.y)} ${X(b.x)} ${Y(b.y)}" ` +
      `fill="none" stroke="${look.color}" stroke-width="${look.width}"` +
      (look.dash ? ` stroke-dasharray="${look.dash}"` : "") + "/>");
  }
  for (const edge of model.edges) {
    if (!edge.label) continue;
    out.push(`<text x="${X(edge.mid.x)}" y="${Y(edge.mid.y + 3)}" font-size="9" text-anchor="middle" ` +
      `fill="#444444" stroke="#ffffff" stroke-width="3" paint-order="stroke">${esc(edge.label)}</text>`);
  }
  for (const node of model.nodes) {
    const look = exportNodeLook(node.kind, node.flags);
    out.push(svgShape(node.kind, X(node.x), Y(node.y), node.w, node.h, look));
    const rows = caption(node);
    let y = node.captionInside ? node.y - (rows.length * ROW) / 2 + ROW - 3 : node.y + node.h / 2 + ROW;
    for (const row of rows) {
      out.push(`<text x="${X(node.x)}" y="${Y(y)}" font-size="${row.size}" text-anchor="middle" fill="#111111"` +
        (row.bold ? ' font-weight="bold"' : "") +
        (node.captionInside ? "" : ' stroke="#ffffff" stroke-width="3" paint-order="stroke"') +
        `>${esc(row.text)}</text>`);
      y += ROW;
    }
  }
  placed.forEach((at, i) => {
    if (!at) return;
    const tag = tags[i];
    out.push(`<rect x="${X(at.x - tag.w / 2)}" y="${Y(at.y - tag.h / 2)}" width="${Math.round(tag.w * 10) / 10}" ` +
      `height="${tag.h}" rx="3" fill="${tag.alarm ? "#f5c6c6" : "#cdeed4"}" ` +
      `stroke="${tag.alarm ? "#d23f3f" : "#4f8f5d"}" stroke-width="0.6"/>` +
      `<text x="${X(at.x)}" y="${Y(at.y + 3.5)}" font-size="10" text-anchor="middle" ` +
      `fill="${tag.alarm ? "#7a1d1d" : "#1d3b24"}">${esc(tag.text)}</text>`);
  });

  if (legend && legend.items.length) {
    const column = Math.max(...legend.items.map((item) => measure(item.text, 10, false))) + 52;
    const columns = Math.max(1, Math.floor((width - 40) / column));
    const rows = Math.ceil(legend.items.length / columns);
    const at = height;
    out.push(`<text x="20" y="${at + 12}" font-size="11" font-weight="bold" fill="#111111">${esc(legend.title)}</text>`);
    legend.items.forEach((item, i) => {
      const x = 20 + Math.floor(i / rows) * column;
      const y = at + 22 + (i % rows) * 18;
      out.push(`<g transform="translate(${x} ${y})">${legendSwatch(item)}</g>` +
        `<text x="${x + 38}" y="${y + 11}" font-size="10" fill="#333333">${esc(item.text)}</text>`);
    });
    height = at + 30 + rows * 18;
  }
  const sizeX = Math.ceil(width);
  const sizeY = Math.ceil(height);
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${sizeX} ${sizeY}" ` +
    `width="${sizeX}" height="${sizeY}" font-family="${esc(EXPORT_FONT)}">` +
    `<rect width="${sizeX}" height="${sizeY}" fill="#ffffff"/>` +
    out.join("") + "</svg>\n";
}

/* A text's width in the export's font, measured by a canvas of the page */
function exportMeasure() {
  const ctx = document.createElement("canvas").getContext("2d");
  return (text, size, bold) => {
    ctx.font = `${bold ? "bold " : ""}${size}px ${EXPORT_FONT}`;
    return ctx.measureText(text).width;
  };
}

// sheets of paper, in millimetres, the short side first
const PAPER = { A4: [210, 297], A3: [297, 420], A2: [420, 594] };
const PAPER_MARGIN = 10;

/* The map on paper, through the browser's own printing: a PDF comes out
   of "Save as PDF" vector, with the fonts in it — Cyrillic included,
   which no PDF library would bring without a font file of its own. The
   sheet is a block of this page shown only in print (@media print); the
   size of the paper is an @page rule written here. First page: the
   title, when, who and which MoonLan, the drawing fitted to the paper
   and its legend; then the names and notes of the named nodes on it,
   the sheet for the cabinet door. */
async function printSchema({ contents, ports, notesTable, title, paper, orientation }) {
  const model = exportModel({ contents, ports });
  const svg = schemaSvg(model, { measure: exportMeasure() });
  let version = "";
  try {
    version = (await (await api("/api/health")).json()).version || "";
  } catch (e) { /* the sheet says less, and still prints */ }
  const [short, long] = PAPER[paper] || PAPER.A3;
  const [pageW, pageH] = orientation === "portrait" ? [short, long] : [long, short];
  const legend = exportLegend(model);
  const meta = [
    t("sheetScan") + " " + (topology.last_scan
      ? new Date(topology.last_scan * 1000).toLocaleString(locale()) : t("noData")),
    t("sheetExported") + " " + new Date().toLocaleString(locale()),
    me && me.name ? t("sheetBy") + " " + me.name : "",
    version ? "MoonLan " + version : "MoonLan",
  ].filter(Boolean);
  const first = h("section", {
    class: "sheet-first",
    // a millimetre short of the page: a page and a hair would be two
    style: `width:${pageW - 2 * PAPER_MARGIN}mm;height:${pageH - 2 * PAPER_MARGIN - 1}mm`,
  },
    h("div", { class: "sheet-head" },
      h("h1", { text: title }),
      h("div", { class: "sheet-meta", text: meta.join("  ·  ") })),
    h("div", { class: "sheet-map" }),
    legend.length ? h("div", { class: "sheet-legend" },
      h("strong", { text: t("legendTitle") }),
      ...legend.map((item) => {
        const row = h("span", { class: "sheet-legend-item" });
        row.innerHTML = `<svg viewBox="0 0 30 14" width="30" height="14">${legendSwatch(item)}</svg>`;
        row.append(item.text);
        return row;
      })) : null,
  );
  first.querySelector(".sheet-map").innerHTML = svg;
  const parts = [first];
  const named = model.nodes.filter((node) => node.name)
    .sort((a, b) => a.name.localeCompare(b.name, locale()));
  if (notesTable && named.length) {
    parts.push(h("section", { class: "sheet-notes" },
      h("h2", { text: t("sheetNotesTitle") }),
      h("table", {},
        h("thead", {}, h("tr", {},
          h("th", { text: t("sheetColName") }), h("th", { text: t("sheetColWhat") }),
          h("th", { text: t("sheetColNote") }))),
        h("tbody", {}, ...named.map((node) =>
          h("tr", {}, h("td", { text: node.name }), h("td", { text: nodeWhere(node.id) }),
            h("td", { class: "sheet-note", text: node.note })))))));
  }
  els.printSheet.replaceChildren(...parts);
  els.printPage.textContent =
    `@page { size: ${pageW}mm ${pageH}mm; margin: ${PAPER_MARGIN}mm; }`;
  // printed once the sheet is laid out; cleared when the dialog closes
  window.addEventListener("afterprint", () => {
    els.printSheet.replaceChildren();
    els.printPage.textContent = "";
  }, { once: true });
  requestAnimationFrame(() => requestAnimationFrame(() => window.print()));
}

/* What a named node is and where, for the sheet of names: "switch
   without SNMP behind access-sw-4 Gi0/5", a device with its port */
function nodeWhere(nodeId) {
  const words = nodeWords(nodeId);
  if (!nodeId.startsWith("host:")) return words;
  const host = findHost(nodeId);
  return host && host.switch ? words + " · " + switchName(host.switch) + " " + (host.port || "") : words;
}

/* ---------- the export window (v0.8.1) ----------

   What to export and how, remembered in this browser. Exporting reads
   and changes nothing: there is no journal entry, and every role may. */
const EXPORT_KEY = "moonlan-export";
const EXPORT_DEFAULTS = {
  format: "pdf", contents: "infrastructure", ports: true, notes: true,
  title: "", paper: "A3", orientation: "landscape",
};

const EXPORT_FORMATS = {
  pdf: { name: "PDF" },
  drawio: { name: "Draw.io", ext: "drawio", type: "application/vnd.jgraph.mxfile" },
  svg: { name: "SVG", ext: "svg", type: "image/svg+xml" },
};

function exportChoices() {
  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem(EXPORT_KEY)) || {};
  } catch (e) { /* what was kept is unreadable: the defaults, then */ }
  return { ...EXPORT_DEFAULTS, ...saved };
}

// today, for a file name: moonlan-2026-10-23.drawio
function exportDate() {
  const now = new Date();
  return [now.getFullYear(), now.getMonth() + 1, now.getDate()]
    .map((part) => String(part).padStart(2, "0")).join("-");
}

function downloadText(text, name, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = h("a", { href: url, download: name, class: "hidden" });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

function openExportDialog() {
  renderExportDialog(exportChoices());
  els.exportDialog.querySelector('input[name="export-format"]:checked').focus();
}

// the title is a default in either language: kept as "no title of one's own"
function defaultSheetTitle(text) {
  return Object.values(I18N).some((words) => words.sheetTitleDefault === text);
}

/* The window in the language just chosen, with what was picked in it
   and the focus where it was */
function redrawExportDialog() {
  const form = els.exportBody.querySelector("form");
  if (!form) return;
  const focused = document.activeElement && form.contains(document.activeElement)
    ? document.activeElement.name : "";
  renderExportDialog(exportPicked(form));
  const again = focused && els.exportBody.querySelector(
    `[name="${focused}"]${focused === "export-format" || focused === "export-contents" ? ":checked" : ""}`);
  if (again) again.focus();
}

/* What the window's fields say now */
function exportPicked(form) {
  const value = (name) => form.elements["export-" + name];
  const typed = value("title").value.trim();
  return {
    format: value("format").value, contents: value("contents").value,
    ports: value("ports").checked, notes: value("notes").checked,
    title: defaultSheetTitle(typed) ? "" : typed,
    paper: value("paper").value, orientation: value("orientation").value,
  };
}

function renderExportDialog(choices) {
  const radios = (name, values, words) => h("fieldset", { class: "export-choice" },
    h("legend", { text: t(words) }),
    ...values.map((value) => h("label", {},
      h("input", { type: "radio", name: "export-" + name, value, checked: choices[name] === value }),
      " " + (name === "format" ? EXPORT_FORMATS[value].name : t(words + "_" + value)))));
  const check = (name, words) => h("label", { class: "export-check" },
    h("input", { type: "checkbox", name: "export-" + name, checked: choices[name] }), " " + t(words));
  const select = (name, values, words, text) => h("label", {}, t(words),
    h("select", { name: "export-" + name },
      ...values.map((value) => h("option", { value, selected: choices[name] === value, text: text(value) }))));
  const title = h("input", {
    type: "text", name: "export-title", maxlength: 120,
    value: choices.title || t("sheetTitleDefault"),
  });
  // "everything" on paper: 682 device captions on A2 come out at 2 pt
  const allOnPaper = h("p", { class: "hint-line export-all-hint", text: t("exportAllPaperHint") });
  const pdfOnly = h("div", { class: "export-pdf" },
    check("notes", "exportNotes"),
    h("label", {}, t("exportTitleField"), title),
    h("div", { class: "export-paper" },
      select("paper", Object.keys(PAPER), "exportPaper", (value) => value),
      select("orientation", ["landscape", "portrait"], "exportOrientation",
             (value) => t("exportOrientation_" + value))),
    h("p", { class: "hint-line", text: t("exportPdfHint") }));
  const go = h("button", { type: "submit" });
  const form = h("form", { class: "form export-form" },
    radios("format", Object.keys(EXPORT_FORMATS), "exportFormat"),
    radios("contents", ["infrastructure", "all"], "exportContents"),
    allOnPaper,
    check("ports", "exportPorts"),
    pdfOnly,
    // the physics may still be moving things: say it, and export anyway
    layoutSettling() ? h("p", { class: "export-settling", text: t("exportSettling") }) : null,
    h("div", { class: "buttons" }, go,
      h("button", { type: "button", text: t("noteCancel"), onclick: closeExportDialog })));
  const shown = () => {
    const format = form.elements["export-format"].value;
    pdfOnly.classList.toggle("hidden", format !== "pdf");
    allOnPaper.classList.toggle("hidden",
      format !== "pdf" || form.elements["export-contents"].value !== "all");
    go.textContent = t(format === "pdf" ? "exportPrint" : "exportDownload");
  };
  form.addEventListener("change", shown);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    // the default title is the default in either language, so it is not kept
    const picked = exportPicked(form);
    localStorage.setItem(EXPORT_KEY, JSON.stringify(picked));
    closeExportDialog();
    runExport(picked);
  });
  shown();
  els.exportTitle.textContent = t("exportMapTitle");
  els.exportBody.replaceChildren(form);
  els.exportDialog.classList.remove("hidden");
}

function closeExportDialog() {
  if (els.exportDialog.classList.contains("hidden")) return;
  els.exportDialog.classList.add("hidden");
  els.exportBody.replaceChildren();
  els.moreBtn.focus();
}

function runExport(picked) {
  const title = picked.title || t("sheetTitleDefault");
  if (picked.format === "pdf") {
    printSchema({
      contents: picked.contents, ports: picked.ports, notesTable: picked.notes,
      title, paper: picked.paper, orientation: picked.orientation,
    });
    return;
  }
  const model = exportModel({ contents: picked.contents, ports: picked.ports });
  const format = EXPORT_FORMATS[picked.format];
  const text = picked.format === "drawio"
    ? drawioXml(model, title)
    : schemaSvg(model, {
      measure: exportMeasure(),
      legend: { title: t("legendTitle"), items: exportLegend(model) },
    });
  downloadText(text, `moonlan-${exportDate()}.${format.ext}`, format.type);
}

/* The window is a dialog: Esc closes it, Tab goes round inside it, and
   no key in it reaches the map (P would pin the selection) */
els.exportDialog.addEventListener("keydown", (event) => {
  event.stopPropagation();
  if (event.key === "Escape") {
    event.preventDefault();
    closeExportDialog();
    return;
  }
  if (event.key !== "Tab") return;
  const stops = [...els.exportDialog.querySelectorAll("input, select, button")]
    .filter((el) => !el.disabled && el.offsetParent !== null &&
      // of a group of radios, only the checked one takes the focus
      !(el.type === "radio" && !el.checked));
  if (!stops.length) return;
  const first = stops[0];
  const last = stops[stops.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});
// a click beside the window closes it
els.exportDialog.addEventListener("click", (event) => {
  if (event.target === els.exportDialog) closeExportDialog();
});
els.exportClose.addEventListener("click", closeExportDialog);

/* ---------- About (v0.8.1) ----------

   What MoonLan is, which build is running, what it runs on, who wrote
   it and under which licence, where to report a bug — and all of that
   as plain text for the bug report. The server's own block (Python,
   the system, uptime) the service sends to an administrator only. */
const REPO_URL = "https://github.com/neonight-d/MoonLan";
const AUTHOR_URL = "https://github.com/neonight-d";
let aboutData = null;
let licenseText = null;
// the licence text shown, kept by a redraw in another language
let licenceOpen = false;

async function openAbout() {
  hidePanelsFor(els.about);
  licenceOpen = false;
  els.about.classList.remove("hidden");
  try {
    aboutData = await (await api("/api/about")).json();
  } catch (e) {
    serviceLost("about");
    return;
  }
  renderAbout();
}

// "3 d 4 h" for a service up for days, "2 h 5 m" for less
function fmtUptime(seconds) {
  const days = Math.floor(seconds / 86400);
  if (!days) return fmtDuration(seconds);
  return days + t("durD") + " " + Math.floor((seconds % 86400) / 3600) + t("durH");
}

/* A sentence with links in it: "Built with {claude} by {anthropic}" */
function withLinks(text, links) {
  return text.split(/(\{\w+\})/).map((part) => {
    const link = links[part.slice(1, -1)];
    return link && /^\{\w+\}$/.test(part)
      ? h("a", { href: link[1], target: "_blank", rel: "noopener noreferrer", text: link[0] })
      : part;
  });
}

function renderAbout() {
  const data = aboutData;
  if (!data) return;
  const link = (href, text) =>
    h("a", { href, target: "_blank", rel: "noopener noreferrer", text });
  const rows = (pairs) => h("dl", { class: "about-rows" },
    ...pairs.flatMap(([term, value]) => [h("dt", { text: term }), h("dd", {}, value)]));
  const changelog = `${REPO_URL}/blob/v${data.version}/` +
    (lang === "ru" ? "CHANGELOG_RU.md" : "CHANGELOG.md");

  const licence = h("div", { class: "about-license" + (licenceOpen ? "" : " hidden") });
  // the file's own line breaks are for an 80-column terminal: its
  // paragraphs, each as one, fit the panel
  const reflowed = (text) => (text || "").replace(/([^\n])\n(?!\n)/g, "$1 ");
  if (licenceOpen) licence.textContent = reflowed(licenseText);
  const showLicence = h("button", {
    type: "button", class: "panel-btn", "aria-expanded": licenceOpen ? "true" : "false",
    text: t(licenceOpen ? "aboutHideLicense" : "aboutShowLicense"),
    onclick: async () => {
      const open = licence.classList.toggle("hidden") === false;
      licenceOpen = open;
      showLicence.setAttribute("aria-expanded", open ? "true" : "false");
      showLicence.textContent = t(open ? "aboutHideLicense" : "aboutShowLicense");
      if (!open || licenseText !== null) {
        licence.textContent = reflowed(licenseText);
        return;
      }
      try {
        const response = await api("/api/about/license");
        licenseText = response.ok ? await response.text() : t("aboutNoLicense");
      } catch (e) {
        serviceLost("the licence");
        licenseText = null;
      }
      licence.textContent = reflowed(licenseText);
    },
  });

  const components = h("table", { class: "about-components" }, h("tbody", {},
    ...data.components.map((c) => h("tr", {},
      h("td", { text: c.name + (c.where === "browser" ? " " + t("aboutInBrowser") : "") }),
      h("td", {
        class: c.version ? "" : "muted",
        text: c.version || t(c.name === "fido2" ? "aboutFido2Missing" : "aboutNotInstalled"),
      }),
      h("td", { class: "muted", text: c.license })))));

  // the cure is a command on the server: said, as on a card, to whoever
  // can run it, and in a tooltip to the rest
  const canRun = me && (me.sign_in === false || me.role === "admin");
  const ieee = data.ieee.loaded
    ? fmt("aboutIeeeLoaded", { date: fmtDate(data.ieee.date) })
    : canRun
    ? h("span", {}, t("aboutIeeeMissing") + ": ", h("code", { text: data.ieee.command }))
    : h("span", { title: fmt("vendorNoRegistryHint", { command: data.ieee.command }),
                  text: t("aboutIeeeMissing") });

  const server = data.server && [
    h("h4", { class: "card-sub", text: t("aboutServer") }),
    rows([
      ["Python", data.server.python],
      [t("aboutOs"), data.server.os],
      [t("aboutUptime"), fmt("aboutUptimeSince", {
        uptime: fmtUptime(data.server.uptime), time: fmtTime(data.server.started_at),
      })],
      [t("aboutSignIn"), t(data.server.sign_in ? "aboutSignInOn" : "aboutSignInOff")],
      ["HTTPS", t("aboutHttps_" + data.server.https)],
    ]),
  ];

  els.aboutBody.replaceChildren(
    h("div", { class: "about-head" },
      h("span", { class: "moon", "aria-hidden": "true" }),
      h("div", {},
        h("div", {}, h("strong", { text: "MoonLan" }), " ",
          h("span", { class: "muted", text: t("tagline") })),
        h("div", { class: "about-version" }, fmt("aboutVersion", { version: data.version }),
          data.build ? h("span", { class: "muted", text: " · " + fmt("aboutBuild", { hash: data.build }) }) : null))),
    h("p", { text: t("aboutDescription") }),
    h("p", { class: "about-principle", text: t("aboutPrinciple") }),
    h("p", {}, data.copyright ? "© " : "", data.copyright
      ? link(AUTHOR_URL, data.copyright) : null),
    h("p", { class: "about-mit", text: t("aboutMit") }),
    showLicence, licence,
    h("ul", { class: "about-links" },
      h("li", {}, link(REPO_URL, t("aboutRepo"))),
      h("li", {}, link(REPO_URL + "/issues", t("aboutIssues"))),
      h("li", {}, link(REPO_URL + "/blob/main/SECURITY.md", t("aboutSecurity"))),
      h("li", {}, link(changelog, fmt("aboutChangelog", { version: data.version }))),
      h("li", {}, link(REPO_URL + "/tree/main/docs", t("aboutDocs")))),
    h("p", { class: "about-thanks" }, ...withLinks(t("aboutThanks"), {
      claude: ["Claude Code", "https://github.com/anthropics/claude-code"],
      anthropic: ["Anthropic", "https://www.anthropic.com/"],
    })),
    h("h4", { class: "card-sub", text: t("aboutComponents") }),
    components,
    h("h4", { class: "card-sub", text: t("aboutData") }),
    rows([[t("aboutIeee"), ieee]]),
    ...(server || []),
    h("button", {
      type: "button", class: "panel-btn", text: t("aboutCopy"),
      onclick: () => copyText(aboutReport(data), t("aboutCopied")),
    }),
  );
}

/* The facts a bug report is asked for first, as plain text — in English
   whatever the page's language, so that every report reads the same */
function aboutReport(data) {
  const lines = [`MoonLan ${data.version}` + (data.build ? `, build ${data.build}` : "")];
  lines.push("", "Components:");
  for (const c of data.components) {
    lines.push(`  ${c.name} ${c.version || "not installed"}` + (c.where === "browser" ? " (browser)" : ""));
  }
  lines.push("", `Browser: ${navigator.userAgent}`, `Interface language: ${lang}`);
  if (data.server) {
    const s = data.server;
    lines.push("", "Server:", `  Python ${s.python}`, `  OS: ${s.os}`,
      `  Uptime: ${Math.floor(s.uptime / 3600)} h ${Math.floor((s.uptime % 3600) / 60)} min`,
      `  Sign-in: ${s.sign_in ? "on" : "off"}`, `  HTTPS: ${s.https}`);
  }
  return lines.join("\n") + "\n";
}

els.aboutClose.addEventListener("click", () => els.about.classList.add("hidden"));

/* ---------- a node's name and note (v0.8.0) ----------

   In the card under the title: the name, the note, who changed them
   and when, and "Name and note…", which opens an editor in the card
   itself — no window over it. */

// the card whose editor is open, or null
let noteEditorFor = null;

/* The key a card's note is kept under: a device off the map is the
   same device */
function noteKeyOf(nodeId) {
  if (nodeId.startsWith("unloc:")) return "host:" + nodeId.slice("unloc:".length);
  // an offline group standing in for a named switch without SNMP keeps
  // the name where it is: on that switch (v0.8.1)
  if (nodeId.startsWith("offline:")) return (noteItemOf(nodeId) || {}).note_key || nodeId;
  return nodeId;
}

/* Why this node can get no name or note from here, or null */
function noteReason(nodeId) {
  if (nodeId.startsWith("trunk:")) return t("reasonTrunkGroup");
  if (nodeId.startsWith("offline:") && !(noteItemOf(nodeId) || {}).note_key) {
    return t("reasonOfflineGroup");
  }
  return needRole("user");
}

/* The item of the topology a card is about, the one with `note` */
function noteItemOf(nodeId) {
  const kind = nodeId.slice(0, nodeId.indexOf(":"));
  if (kind === "sw") return topology.switches.find((s) => "sw:" + s.ip === nodeId) || null;
  if (kind === "host" || kind === "unloc") return findHost(nodeId) || null;
  const list = {
    bridge: topology.bridges, pseudo: topology.pseudo_switches,
    external: topology.external_networks, trunk: topology.trunk_groups,
    offline: topology.offline_groups,
  }[kind];
  return (list || []).find((node) => node.id === nodeId) || null;
}

/* What is in the open editor of this card, or null */
function noteDraft(nodeId) {
  if (noteEditorFor !== nodeId) return null;
  const label = document.getElementById("note-label");
  const text = document.getElementById("note-text");
  const error = document.querySelector("#note-slot .note-error");
  if (!label || !text) return null;
  return { label: label.value, note: text.value, error: error ? error.textContent : "" };
}

/* "Changed: neonight, 22.10.2026 11:30", or "Import, 22.10.2026" */
function noteMeta(note) {
  if (note.source === "import") return fmt("noteImported", { date: fmtDate(note.updated_at) });
  return note.updated_by
    ? fmt("noteChangedBy", { user: note.updated_by, time: fmtTime(note.updated_at) })
    : fmt("noteChanged", { time: fmtTime(note.updated_at) });
}

function renderNoteSlot(nodeId, draft) {
  const slot = document.getElementById("note-slot");
  if (!slot) return;
  if (noteEditorFor === nodeId && !noteReason(nodeId)) {
    slot.replaceChildren(noteEditor(nodeId, draft));
    return;
  }
  if (noteEditorFor === nodeId) noteEditorFor = null;
  const note = (noteItemOf(nodeId) || {}).note;
  const parts = [];
  if (note) {
    const rows = [];
    if (note.label) rows.push(h("dt", { text: t("noteLabel") }), h("dd", { class: "note-label", text: note.label }));
    if (note.note) rows.push(h("dt", { text: t("noteText") }), h("dd", { class: "note-text", text: note.note }));
    parts.push(h("dl", { class: "note-dl" }, ...rows), h("p", { class: "note-meta", text: noteMeta(note) }));
  }
  const button = h("button", { id: "note-btn", class: "panel-btn", type: "button", text: t("noteBtn") });
  gateButton(button, noteReason(nodeId));
  button.addEventListener("click", () => allowed(button) && openNoteEditor(nodeId));
  parts.push(button);
  slot.replaceChildren(h("div", { class: "note-block" }, ...parts));
}

function openNoteEditor(nodeId) {
  const why = noteReason(nodeId);
  if (why) {
    showToast(why);
    return;
  }
  noteEditorFor = nodeId;
  if (shownDetails && shownDetails.type === "node" && shownDetails.id === nodeId &&
      !els.details.classList.contains("hidden")) {
    renderNoteSlot(nodeId, null);
  } else {
    showDetails(nodeId);
  }
  const label = document.getElementById("note-label");
  if (label) label.focus();
}

function closeNoteEditor(nodeId) {
  noteEditorFor = null;
  renderNoteSlot(nodeId, null);
}

/* The names of the devices of a switch without SNMP, for its own name:
   the switch itself is often one of them — named by LLDP — but which
   one, MoonLan cannot tell, so they are offered and the person picks.
   LLDP names first, then DNS, each by address; at most eight. */
function groupNames(nodeId) {
  const ipKey = (ip) => (ip || "").split(".").reduce((n, part) => n * 256 + (+part || 0), 0) || Infinity;
  const seen = new Set();
  return topology.hosts
    .filter((h) => h.via === nodeId)
    .map((h) => {
      const lldp = h.lldp && h.lldp.sys_name;
      if (lldp) return { name: lldp, by: 0, ip: h.ip };
      if (h.name) return { name: h.name, by: 1, ip: h.ip };
      return null;
    })
    .filter(Boolean)
    .sort((a, b) => a.by - b.by || ipKey(a.ip) - ipKey(b.ip))
    .filter((entry) => !seen.has(entry.name) && seen.add(entry.name))
    .slice(0, 8);
}

function noteEditor(nodeId, draft) {
  const note = (noteItemOf(nodeId) || {}).note;
  const label = h("input", {
    id: "note-label", type: "text", maxlength: 40, autocomplete: "off",
    "aria-label": t("noteLabel"),
  });
  label.value = draft ? draft.label : note ? note.label : "";
  const count = h("span", { class: "note-count" });
  const counted = () => { count.textContent = fmt("noteCount", { n: label.value.length, max: 40 }); };
  counted();
  label.addEventListener("input", counted);
  const text = h("textarea", { id: "note-text", rows: 5, maxlength: 2000, "aria-label": t("noteText") });
  text.value = draft ? draft.note : note ? note.note : "";
  const error = h("p", { class: "note-error" + (draft && draft.error ? "" : " hidden"), role: "alert",
                         text: (draft && draft.error) || "" });
  const save = () => saveNote(nodeId, false);
  const parts = [
    h("label", { class: "note-field", for: "note-label" }, h("span", { text: t("noteLabel") }), count),
    label,
  ];
  // a switch without SNMP: the names of its devices, to pick from
  if (nodeId.startsWith("pseudo:")) {
    const names = groupNames(nodeId);
    const list = h("div", { class: "note-pick-list hidden", role: "menu" },
      ...names.map((entry) => h("button", {
        type: "button", role: "menuitem", class: "note-pick-item",
        text: entry.name + (entry.ip ? " · " + entry.ip : ""),
        onclick: () => {
          label.value = entry.name;
          counted();
          list.classList.add("hidden");
          label.focus();
        },
      })));
    const pick = h("button", {
      type: "button", class: "note-pick panel-btn", "aria-haspopup": "menu",
      text: t("notePickName"),
      onclick: () => allowed(pick) && list.classList.toggle("hidden"),
    });
    gateButton(pick, names.length ? null : t("reasonNoGroupNames"));
    parts.push(h("div", { class: "note-pick-wrap" }, pick, list));
  }
  parts.push(
    h("label", { class: "note-field", for: "note-text" }, h("span", { text: t("noteText") })),
    text,
    error,
    moveHereMenu(nodeId),
    h("div", { class: "note-actions" },
      h("button", { type: "button", id: "note-save", class: "panel-btn", text: t("noteSave"), onclick: save }),
      h("button", { type: "button", id: "note-cancel", class: "panel-btn", text: t("noteCancel"),
                    onclick: () => closeNoteEditor(nodeId) }),
      note ? h("button", { type: "button", id: "note-delete", class: "panel-btn danger", text: t("noteDelete"),
                           onclick: () => saveNote(nodeId, true) }) : null),
  );
  const editor = h("div", { class: "note-editor" }, ...parts);
  // Enter in the name saves, Escape anywhere in the editor cancels
  label.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      save();
    }
  });
  editor.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeNoteEditor(nodeId);
    }
  });
  return editor;
}

/* ---------- notes without a node ----------

   A note stays when its node goes — a cable moved to another port, a
   port renamed in the firmware — because what it says is known nowhere
   else. After three scans without its node it is listed here, with the
   node it was on in words and since when; it can be deleted, or put on
   a node of the map from that node's editor. A node that comes back
   takes its note back by itself. */

// every note, from the last refresh
let allNotes = [];
// what the notes panel shows: "orphans", "import" or "export"
let notesPanelShows = "";

function orphanNotes() {
  return allNotes.filter((note) => note.state === "missing" || note.state === "not_found");
}

/* A node by its id, in words, whether or not it is on the map:
   "switch without SNMP behind access-sw-3 Gi0/9" */
function nodeWords(nodeId) {
  const at = nodeId.indexOf(":");
  const kind = nodeId.slice(0, at);
  const rest = nodeId.slice(at + 1);
  if (kind === "pseudo" || kind === "external" || kind === "trunk" || kind === "offline") {
    const cut = rest.indexOf(":");
    const ip = rest.slice(0, cut);
    return fmt("nodeWords_" + kind, { switch: switchName(ip), port: rest.slice(cut + 1) });
  }
  if (kind === "sw") return fmt("nodeWords_sw", { name: switchName(rest) });
  if (kind === "host") {
    const host = findHost(nodeId) || findHost("unloc:" + rest);
    return fmt("nodeWords_host", { name: host ? hostLabel(host) : rest });
  }
  if (kind === "bridge") {
    const bridge = (topology.bridges || []).find((b) => b.id === nodeId);
    return fmt("nodeWords_bridge", { name: (bridge && bridge.name) || rest });
  }
  return nodeId;
}

function openOrphans() {
  hidePanelsFor(els.notesPanel);
  notesPanelShows = "orphans";
  els.notesTitle.textContent = t("orphansTitle");
  els.notesPanel.classList.remove("hidden");
  renderOrphans();
}

function renderOrphans() {
  const section = (title, notes) => {
    if (!notes.length) return [];
    return [
      h("h4", { class: "card-sub", text: title }),
      h("ul", { class: "orphan-list" }, ...notes.map(orphanRow)),
    ];
  };
  const orphans = orphanNotes();
  const parts = [
    ...section(t("orphansMissing"), orphans.filter((n) => n.state === "missing")),
    ...section(t("orphansNotFound"), orphans.filter((n) => n.state === "not_found")),
  ];
  els.notesBody.replaceChildren(...(parts.length ? parts : [h("p", { class: "hint", text: t("orphansNone") })]));
}

function orphanRow(note) {
  const remove = h("button", { type: "button", class: "panel-btn danger", text: t("noteDelete") });
  gateButton(remove, needRole("user"));
  remove.addEventListener("click", () => busy("note:" + note.node_id, async () => {
    if (!allowed(remove) || !window.confirm(t("noteDeleteConfirm"))) return;
    const response = await api("/api/notes/" + nodePath(note.node_id), { method: "DELETE" });
    if (!response.ok) {
      showToast(noteErrorText((await response.json().catch(() => ({}))).error));
      return;
    }
    await loadTopologyOnce();
    renderOrphans();
  }, [remove]));
  return h("li", {},
    note.label ? h("div", { class: "orphan-label", text: note.label }) : null,
    note.note ? h("div", { class: "orphan-note", text: note.note }) : null,
    h("div", { class: "orphan-where", text: nodeWords(note.node_id) }),
    h("div", { class: "orphan-since", text: note.retired
      ? t("orphanRetired")
      : note.state === "missing"
      ? fmt("orphanSince", { time: fmtTime(note.missing_since) })
      : fmt("orphanImported", { date: fmtDate(note.updated_at) }) }),
    remove);
}

/* In a node's editor: the notes without a node, to put on this one */
function moveHereMenu(nodeId) {
  const orphans = orphanNotes();
  if (!orphans.length) return null;
  const list = h("div", { class: "note-pick-list hidden", role: "menu" },
    ...orphans.map((note) => h("button", {
      type: "button", role: "menuitem", class: "note-pick-item",
      text: (note.label || note.note.split("\n")[0]) + " · " + nodeWords(note.node_id),
      onclick: () => {
        list.classList.add("hidden");
        moveNoteHere(note.node_id, nodeId);
      },
    })));
  const open = h("button", {
    type: "button", class: "note-pick panel-btn", "aria-haspopup": "menu",
    text: t("noteMoveHere"),
    onclick: () => list.classList.toggle("hidden"),
  });
  return h("div", { class: "note-pick-wrap" }, open, list);
}

async function moveNoteHere(source, nodeId) {
  return busy("note:" + noteKeyOf(nodeId), async () => {
    const target = noteKeyOf(nodeId);
    const own = (noteItemOf(nodeId) || {}).note;
    if (own && !window.confirm(t("noteReplaceConfirm"))) return;
    const response = await api("/api/notes/" + nodePath(source) + "/move",
      jsonPost("", { to: target, replace: !!own }));
    const data = await response.json().catch(() => ({}));
    const error = document.querySelector("#note-slot .note-error");
    if (!response.ok) {
      if (error) {
        error.textContent = noteErrorText(data.error);
        error.classList.remove("hidden");
      }
      return;
    }
    noteEditorFor = null;
    await loadTopologyOnce();
    showDetails(nodeId);
  }, [...document.querySelectorAll("#note-slot button")]);
}

/* ---------- names and notes from a file, and into one ----------

   A CSV of mac;label;note — what Excel saves. Nothing is written before
   the person has seen what would be: the file goes to the server once
   for a preview and once more for real. */

function chooseNotesFile() {
  const input = h("input", { type: "file", accept: ".csv,text/csv,text/plain" });
  input.addEventListener("change", () => {
    if (input.files && input.files[0]) previewNotesImport(input.files[0]);
  });
  input.click();
}

function showNotesPanel(kind, title) {
  hidePanelsFor(els.notesPanel);
  notesPanelShows = kind;
  els.notesTitle.textContent = title;
  els.notesPanel.classList.remove("hidden");
}

async function sendNotesFile(data, dryRun) {
  const response = await api("/api/notes/import" + (dryRun ? "?dry_run=1" : ""), {
    method: "POST", headers: { "Content-Type": "text/csv" }, body: data,
  });
  const body = await response.json().catch(() => ({}));
  return response.ok ? body : { failed: body.error || String(response.status) };
}

// the import on show — the file, what the server said of it, whether
// it was written — and the export's count: what a redraw draws again
let notesImport = null;
let notesExport = null;

async function previewNotesImport(file) {
  const data = await file.arrayBuffer();
  showNotesPanel("import", t("importTitle"));
  notesImport = { name: file.name, data, report: null, failed: "", written: false };
  renderNotesImport();
  const report = await sendNotesFile(data, true);
  if (report.failed) notesImport.failed = report.failed;
  else notesImport.report = report;
  renderNotesImport();
}

function renderNotesImport() {
  const job = notesImport;
  if (!job) return;
  if (job.failed) {
    els.notesBody.replaceChildren(h("p", { class: "note-error", text: noteErrorText(job.failed) }));
    return;
  }
  if (!job.report) {
    els.notesBody.replaceChildren(h("p", { class: "hint", text: job.name }));
    return;
  }
  if (job.written) {
    renderImportReport(job.name, job.report, []);
    return;
  }
  const go = h("button", { type: "button", class: "panel-btn", text: t("importGo") });
  go.disabled = job.report.written === 0;
  go.addEventListener("click", () => busy("notes-import", async () => {
    const done = await sendNotesFile(job.data, false);
    if (done.failed) {
      showToast(noteErrorText(done.failed));
      return;
    }
    job.report = done;
    job.written = true;
    renderNotesImport();
    await loadTopologyOnce();
  }, [go]));
  const cancel = h("button", { type: "button", class: "panel-btn", text: t("noteCancel"),
                               onclick: () => els.notesPanel.classList.add("hidden") });
  renderImportReport(job.name, job.report, [go, cancel]);
}

function renderImportReport(name, report, buttons) {
  const lines = (rows, words) => rows.length
    ? [h("ul", { class: "import-lines" }, ...rows.map((row) =>
        h("li", { text: fmt("importLine", { line: row.line }) + " " + words(row) })))]
    : [];
  const problem = (row) => {
    const text = t("csvErr_" + row.error);
    return (text === "csvErr_" + row.error ? row.error : text) + (row.mac ? " · " + row.mac : "");
  };
  els.notesBody.replaceChildren(
    h("p", { class: "hint", text: name }),
    h("p", { class: "import-sum", text: fmt(report.dry_run ? "importWill" : "importDone",
      { n: report.written, on_map: report.on_map, not_found: report.not_found }) }),
    h("p", { class: "import-sum", text: fmt("importSkipped", { n: report.skipped_manual.length }) }),
    ...lines(report.skipped_manual, (row) => row.mac),
    h("p", { class: "import-sum", text: fmt("importErrors", { n: report.errors.length }) }),
    ...lines(report.errors, problem),
    ...(report.warnings.length
      ? [h("p", { class: "import-sum", text: fmt("importWarnings", { n: report.warnings.length }) }),
         ...lines(report.warnings, (row) => fmt("csvWarn_" + row.error, { mac: row.mac, first: row.first }))]
      : []),
    h("div", { class: "note-actions" }, ...buttons),
  );
}

async function openNotesExport() {
  showNotesPanel("export", t("exportTitle"));
  notesExport = null;
  try {
    notesExport = await (await api("/api/notes/export?preview=1")).json();
  } catch (e) {
    serviceLost("the notes");
    return;
  }
  renderNotesExport();
}

function renderNotesExport() {
  const summary = notesExport;
  if (!summary) return;
  els.notesBody.replaceChildren(
    h("p", { class: "import-sum", text: fmt("exportWill", { n: summary.exported }) }),
    summary.left_out
      ? h("p", { class: "hint", text: fmt("exportLeftOut", { n: summary.left_out }) })
      : null,
    h("div", { class: "note-actions" },
      h("a", { class: "panel-btn", href: "/api/notes/export", download: "moonlan-notes.csv",
               text: t("exportGo") })),
  );
}

/* The notes panel in the language just chosen: whichever of its three
   it shows */
function redrawNotesPanel() {
  if (notesPanelShows === "orphans") {
    els.notesTitle.textContent = t("orphansTitle");
    renderOrphans();
  } else if (notesPanelShows === "import") {
    els.notesTitle.textContent = t("importTitle");
    renderNotesImport();
  } else if (notesPanelShows === "export") {
    els.notesTitle.textContent = t("exportTitle");
    renderNotesExport();
  }
}

/* The words for a refusal of the server, or the code itself */
function noteErrorText(code) {
  const text = t("noteErr_" + code);
  return text === "noteErr_" + code ? fmt("noteErrOther", { code: code || "?" }) : text;
}

/* A node id in the path of a request: a port name may have a slash */
function nodePath(nodeId) {
  return nodeId.split("/").map(encodeURIComponent).join("/");
}

/* The map and the lists take a note as soon as the server has it */
function takeNote(nodeId, note) {
  const key = noteKeyOf(nodeId);
  const items = [noteItemOf(nodeId), noteItemOf(key)];
  for (const item of items) {
    if (!item) continue;
    if (note) item.note = note;
    else {
      delete item.note;
      // an offline group whose room has no name now stands for nothing
      delete item.note_key;
    }
  }
  renderGraph();
  renderSidebar();
}

async function saveNote(nodeId, remove) {
  return busy("note:" + noteKeyOf(nodeId), async () => {
    const key = noteKeyOf(nodeId);
    const label = document.getElementById("note-label");
    const text = document.getElementById("note-text");
    const error = document.querySelector("#note-slot .note-error");
    if (remove && !window.confirm(t("noteDeleteConfirm"))) return;
    let response;
    let data = {};
    try {
      response = await api("/api/notes/" + nodePath(key), remove
        ? { method: "DELETE" }
        : {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label: label ? label.value : "", note: text ? text.value : "" }),
        });
      data = await response.json().catch(() => ({}));
    } catch (e) {
      response = null;
    }
    if (!response || !response.ok) {
      // what was typed stays where it is; the reason goes under it
      if (error) {
        error.textContent = response ? noteErrorText(data.error) : t("noteErrNetwork");
        error.classList.remove("hidden");
      }
      return;
    }
    noteEditorFor = null;
    takeNote(nodeId, remove ? null : data.note);
    showDetails(nodeId);
  }, [...document.querySelectorAll("#note-slot button")]);
}

/* Fills the details card, hoisting its own <h3> into the panel head.
   Each branch below writes its title as part of the HTML — that is
   where it belongs, next to the thing it titles — and the head is
   where it has to end up, so it stays put while the card scrolls.
   Markup only (html`…`): a plain string is shown as the text it is. */
function setDetails(card) {
  els.detailsBody.innerHTML = card instanceof Markup ? card.text : escapeHtml(card);
  const heading = els.detailsBody.querySelector("h3");
  els.detailsTitle.replaceChildren(
    ...(heading ? [...heading.childNodes] : [])
  );
  if (heading) heading.remove();
}

/* Every line drawn from this switch: the neighbour, our port, whether
   it is up, and where the link came from. The tooltip on an edge
   answers that one edge at a time; "how many lines does this box have
   and why" is a question about the box, and belongs on its card. */
function switchLinksHtml(ip) {
  const rows = [];
  for (const link of topology.links) {
    const side = link.a === ip ? "a" : link.b === ip ? "b" : null;
    if (!side) continue;
    const otherIp = link[side === "a" ? "b" : "a"];
    const port = link[side + "_port"];
    const source = link.source || "fdb";
    const mark = link.cycle_unresolved
      ? " ⚠"
      : link.order_unknown
      ? " ?"
      : "";
    const title = link.cycle_unresolved
      ? t("linkCycleUnresolved")
      : link.order_unknown
      ? t("linkOrderUnknown")
      : link.stp_blocking
      ? t("linkStpBlocking")
      : "";
    const how = source === "both" ? t("srcBoth") : source === "lldp" ? t("srcLldp") : t("srcFdb");
    rows.push(html`<li${title ? html` title="${title}"` : ""}><span>${port}${mark} → ${switchName(otherIp)}</span><span class="sub">${how}${
      link.speed_mbps ? " · " + fmtSpeed(link.speed_mbps) : ""}${
      link.stp_blocking ? " · " + t("stpBlocking") : ""}</span></li>`);
  }
  if (!rows.length) return "";
  return html`<h4 class="card-sub">${fmt("switchLinks", { n: rows.length })}</h4><ul class="offline-list">${rows}</ul>`;
}

/* The devices of a group, each a row that leads to its node */
function memberRows(members, first, second) {
  return members.map((h) => html`<li data-mac="${h.mac}" title="${h.mac + " · " + vendorText(h)}"><span>${first(h)}</span>
          <span class="sub">${second(h)}</span></li>`);
}

function showDetails(nodeId) {
  // what is being typed into an open editor of this card survives the
  // card being drawn again — a change of language or of role
  const draft = noteDraft(nodeId);
  let card = raw("");
  if (nodeId.startsWith("sw:")) {
    const sw = topology.switches.find((s) => "sw:" + s.ip === nodeId);
    if (!sw) return;
    const loop = sw.loop || {};
    card = html`<h3>${sw.name}</h3>
      ${sw.over_budget
        ? hintKey(
            sw.over_budget_scans >= 2 ? "staleSwitchHint" : "overBudgetHint",
            { time: fmtTime(sw.polled_at), n: sw.over_budget_scans }
          )
        : ""}${loop.supported === false
        ? hintKey("loopUnsupportedHint", { oid: loop.sys_object_id || "—" })
        : ""}<dl>
      <dt>${t("ipAddr")}</dt><dd>${sw.ip}</dd>
      <dt>${t("bridgeMac")}</dt><dd>${sw.mac || "—"}</dd>
      ${vendorRow(sw)}
      <dt>${t("portsUpTotal")}</dt><dd>${sw.ports_up} / ${sw.ports_total}</dd>
      <dt>${t("lastReply")}</dt><dd>${fmtTime(sw.last_ping_ok)}</dd>
      ${sw.over_budget
        ? html`<dt>${t("dataFrom")}</dt><dd>${fmtTime(sw.polled_at)}${
            sw.over_budget_scans >= 2
              ? " · " + fmt("staleSwitchScans", { n: sw.over_budget_scans })
              : ""
          }</dd>`
        : ""}
      <dt>${t("loopDetection")}</dt><dd${
        loop.status === "loop" ? raw(' class="loop-alarm"') : ""
      } title="${loopCardTitle(loop)}">${loopCardLine(loop)}</dd>
      <dt>${t("descr")}</dt><dd>${sw.descr || "—"}</dd></dl>
      ${switchLinksHtml(sw.ip)}
      <button type="button" id="ports-btn" class="panel-btn">${t("portsBtn")}</button>`;
  } else if (nodeId.startsWith("offline:")) {
    const group = (topology.offline_groups || []).find((g) => g.id === nodeId);
    if (!group) return;
    const members = topology.hosts.filter((h) => h.via === nodeId);
    const rows = memberRows(members, hostLabel, (h) => fmtTime(h.last_seen));
    card = html`<h3>${fmt("offlineGroupTitle", { n: group.count })}</h3>
      ${group.approximate
        ? hintKey("offlineGroupGuessHint")
        : html`<p class="hint">${t("offlineGroupHint")}</p>`}<dl>
      <dt>${t("switchLabel")}</dt><dd>${group.switch}</dd>
      <dt>${t("portLabel")}</dt><dd>${group.port}</dd>
      ${group.via_name
        ? html`<dt>${t("behindBridge")}</dt><dd>${group.via_name}</dd>`
        : ""}
      <dt>${t("lastSeenLabel")}</dt><dd>${fmtTime(group.last_seen_max)}</dd>
      </dl><ul class="offline-list">${rows}</ul>`;
  } else if (nodeId.startsWith("trunk:")) {
    const group = (topology.trunk_groups || []).find((g) => g.id === nodeId);
    if (!group) return;
    const members = topology.hosts.filter((h) => h.via === nodeId);
    const rows = memberRows(members, hostLabel, (h) => (h.ip ? h.mac : ""));
    card = html`<h3>${fmt("trunkGroupTitle", { n: group.count })}</h3>
      <p class="hint">${fmtHtml("trunkGroupHint", {
        switch: switchName(group.switch),
        port: group.port,
      })}</p><dl>
      <dt>${t("switchLabel")}</dt><dd>${group.switch}</dd>
      <dt>${t("portLabel")}</dt><dd>${group.port}</dd>
      ${group.via_name
        ? html`<dt>${t("behindBridge")}</dt><dd>${group.via_name}</dd>`
        : ""}
      <dt>${t("devicesBehindPort")}</dt><dd>${group.count}</dd>
      ${group.silent
        ? html`<dt>${t("trunkGroupSilent")}</dt><dd>${group.silent}</dd>`
        : ""}
      </dl><ul class="offline-list">${rows}</ul>`;
  } else if (nodeId.startsWith("bridge:")) {
    const bridge = (topology.bridges || []).find((b) => b.id === nodeId);
    if (!bridge) return;
    const caps = (bridge.capabilities || []).join(", ");
    // every address the device announced, the first one clickable
    const addresses = bridge.mgmt_ips && bridge.mgmt_ips.length
      ? bridge.mgmt_ips
      : bridge.mgmt_ip
      ? [bridge.mgmt_ip]
      : [];
    const link = addressList(addresses);
    // one hint, not several: "the devices behind this port hang off
    // this node", "they stay on the unmanaged switch" and "everything
    // behind here is somebody else's" cannot all be true at once
    const bridgeHint = bridge.external
      ? "externalHint"
      : bridge.cap_assumed
      ? "bridgeAssumedHint"
      : bridge.shares_port
      ? "bridgeSharesPortHint"
      : "bridgeHint";
    card = html`<h3>${bridge.name}</h3>
      ${hintKey(bridgeHint)}
      ${bridge.lldp_crowded && !bridge.shares_port
        ? hintKey("lldpCrowdedHint") : ""}
      ${bridge.lldp_forwarded ? hintKey("lldpForwardedHint") : ""}
      <dl>
      <dt>${t("descr")}</dt><dd>${bridge.sys_desc || "—"}</dd>
      <dt>${t("chassisId")}</dt><dd>${bridge.chassis_id}</dd>
      ${bridge.vendor_status === "found" || bridge.vendor_status === "unregistered"
        || bridge.vendor_status === "no_registry" ? vendorRow(bridge) : ""}
      ${bridge.dns_name ? html`<dt>${t("name")}</dt><dd>${bridge.dns_name}</dd>` : ""}
      <dt>${t("mgmtIp")}</dt><dd>${link}</dd>
      ${bridge.ip && !(bridge.mgmt_ips || []).includes(bridge.ip)
        ? html`<dt>${t("ipAddr")}</dt><dd>${bridge.ip}</dd>` : ""}
      ${bridge.ip
        ? html`<dt>${t("lastReply")}</dt><dd>${fmtTime(bridge.last_ping_ok)}</dd>`
        : ""}
      ${(bridge.also_ips || []).length
        ? html`<dt>${t("sameDevice")}</dt><dd>${bridge.also_ips.join(", ")}</dd>`
        : ""}
      <dt>${t("switchLabel")}</dt><dd>${bridge.switch}</dd>
      <dt>${t("portLabel")}</dt><dd>${bridge.port}</dd>
      <dt>${t("remotePort")}</dt><dd>${bridge.remote_port || "—"}</dd>
      <dt>${t("capabilities")}</dt><dd>${
        bridge.cap_known ? `${t("kind_" + (bridge.kind || "other"))} (${caps})`
        : t("capsUnknown")
      }</dd>
      ${bridge.shares_port || bridge.external
        ? ""
        : html`<dt>${t("devicesBehindPort")}</dt><dd>${bridge.host_count ?? 0}</dd>`}
      <dt>${t("firstSeen")}</dt><dd>${fmtTime(bridge.first_seen)}</dd>
      <dt>${t("lastSeenLabel")}</dt><dd>${fmtTime(bridge.last_seen)}</dd>
      </dl>`;
  } else if (nodeId.startsWith("external:")) {
    const external = (topology.external_networks || []).find(
      (e) => e.id === nodeId
    );
    if (!external) return;
    // the provider's own switch is drawn as its own node, not counted
    // among the addresses living behind it
    const members = topology.hosts.filter(
      (h) => h.via === nodeId && !h.merged_into
    );
    // addresses first: past the handover the address is all there is,
    // and the name column would just repeat it
    const rows = memberRows(members, (h) => h.ip || hostLabel(h), (h) => (h.ip ? h.mac : ""));
    card = html`<h3>${t("externalNetwork")}</h3>
      ${hintKey("externalHint")}<dl>
      <dt>${t("switchLabel")}</dt><dd>${external.switch}</dd>
      <dt>${t("portLabel")}</dt><dd>${external.port}</dd>
      ${external.via_name
        ? html`<dt>${t("behindBridge")}</dt><dd>${external.via_name}</dd>`
        : ""}
      <dt>${t("devicesBehindPort")}</dt><dd>${external.count}</dd>
      </dl><ul class="offline-list">${rows}</ul>`;
  } else if (nodeId.startsWith("pseudo:")) {
    const ps = (topology.pseudo_switches || []).find((p) => p.id === nodeId);
    if (!ps) return;
    card = html`<h3>${t("pseudoTitle")}</h3>
      <p class="hint">${fmt("pseudoHint", { n: ps.host_count })}</p><dl>
      <dt>${t("switchLabel")}</dt><dd>${ps.switch}</dd>
      <dt>${t("portLabel")}</dt><dd>${ps.port}</dd>
      <dt>${t("devicesBehindPort")}</dt><dd>${fmt("devicesCount", {
        n: ps.host_count,
        live: ps.host_count_live == null ? ps.host_count : ps.host_count_live,
      })}</dd></dl>`;
  } else {
    const host = findHost(nodeId);
    if (!host) return;
    const offMap = host.unlocated;
    // A device seen only on uplinks is a third case: it was seen this
    // very scan, and every sighting was of the cable it is NOT behind.
    const hint = host.uplink_only
      ? hintKey("uplinkOnlyHint")
      : offMap
      ? host.ip
        ? hintKey("unlocatedHint")
        : html`<p class="hint">${t("offMapHint")}</p>`
      : host.stale
      ? html`<p class="hint">${t("staleHint")}</p>`
      : "";
    // a stale record whose address ARP still confirms is not a dead
    // host: the device most likely changed its MAC
    const aliveByIp = host.stale && !offMap && host.ping_up && host.ip_confirmed;
    const savedHint = host.from_saved
      ? fmt("hostFromSavedHint", { switch: switchName(host.switch), time: fmtTime(host.reading_at) })
      : "";
    card = html`<h3>${hostLabel(host)}</h3>
      ${labelFromLldp(host) ? hintKey("nameFromLldpHint") : ""}
      ${host.approximate
        ? hintBlock(t("approximateHintShort"), t("approximateHint") + " " +
            fmt("approximateWhy", { switch: switchName(host.switch), port: host.port }))
        : ""}
      ${host.remembered ? hintKey("rememberedHint") : ""}
      ${host.from_saved
        ? hintKey("hostFromSavedHint", {
            switch: switchName(host.switch),
            time: fmtTime(host.reading_at),
          })
        : ""}
      ${aliveByIp ? hintKey("staleButAliveHint") : ""}
      ${hint}<dl>
      <dt>${t("name")}</dt><dd>${host.name || "—"}</dd>
      <dt>${t("ipAddr")}</dt><dd>${host.ip || "—"}</dd>
      <dt>${t("ipConfirmedLabel")}</dt><dd>${
        host.ip_confirmed ? fmtTime(host.ip_confirmed) : t("ipNotConfirmed")
      }</dd>
      <dt>${t("macAddr")}</dt><dd>${host.mac}${
        host.random_mac ? html` <span class="chip" title="${t("randomMacHint")}">${t("randomMac")}</span>` : ""
      }</dd>
      ${vendorRow(host)}
      ${(host.seen_on || []).length
        ? html`<dt>${t("uplinkOnlySeenOn")}</dt><dd>${host.seen_on
            .map((seen) => `${switchName(seen.switch)} ${seen.port}`)
            .join(", ")}</dd>`
        : ""}
      <dt>${t("switchLabel")}</dt><dd>${host.switch || "—"}</dd>
      <dt>${t("portLabel")}</dt><dd>${host.port || "—"}${
        host.approximate ? html` <span class="chip">${t("approximate")}</span>` : ""
      }${
        host.remembered ? html` <span class="chip">${t("remembered")}</span>` : ""
      }${
        host.from_saved ? html` <span class="chip" title="${savedHint}">${t("fromSaved")}</span>` : ""
      }</dd>
      <dt>${t("vlan")}</dt><dd>${vlanLabel(host.vlan)}</dd>
      ${host.router_ip
        ? html`<dt>${t("routerAddr")}</dt><dd>${addressLink(host.router_ip)}</dd>`
        : ""}
      ${host.lldp ? html`<dt>${t("lldpLabel")}</dt><dd>${lldpHostLine(host.lldp)}</dd>` : ""}
      ${(host.lldp && (host.lldp.mgmt_ips || []).length > 1)
        ? html`<dt>${t("mgmtIp")}</dt><dd>${addressList(host.lldp.mgmt_ips)}</dd>`
        : ""}
      <dt>${t("lastReply")}</dt><dd>${fmtTime(host.last_ping_ok)}</dd>
      <dt>${t("lastSeenLabel")}</dt><dd>${fmtTime(host.last_seen)}</dd>
      ${host.from_saved
        ? html`<dt>${t("readingFrom")}</dt><dd>${fmtTime(host.reading_at)}</dd>`
        : ""}
      ${offMap ? html`<dt>${t("lastArpLabel")}</dt><dd>${fmtTime(host.last_arp)}</dd>` : ""}
      <dt>${t("firstSeen")}</dt><dd>${fmtDate(host.first_seen)}</dd></dl>
      <button type="button" id="monitor-btn" class="panel-btn${host.monitored ? " active" : ""}${
        needRole("user") ? " off" : ""}">
        ${host.monitored ? "★" : "☆"} ${t("monitorBtn")}</button>`;
  }
  // Pinned: say so, and offer to hand it back to the physics engine.
  // The map must answer "did MoonLan arrange this or did I" without
  // anyone having to guess.
  if (savedLayout[nodeId] && savedLayout[nodeId].pinned) {
    card = html`${card}${hintKey("pinnedHint")}<button type="button" id="unpin-btn" class="panel-btn${
      needRole("user") ? " off" : ""}">${t("unpinBtn")}</button>`;
  }
  // the node's name and note, right under the title: after the first
  // </h3>, which is ours — one in a name is escaped
  card = raw(card.text.replace("</h3>", '</h3><div id="note-slot"></div>'));
  shownDetails = { type: "node", id: nodeId };
  setDetails(card);
  els.details.classList.remove("hidden");
  els.journal.classList.add("hidden");
  hideAccountPanels();
  els.alarms.classList.add("hidden");
  els.stp.classList.add("hidden");
  closePorts();
  renderNoteSlot(nodeId, draft);
  const unpinBtn = document.getElementById("unpin-btn");
  if (unpinBtn) {
    gateButton(unpinBtn, needRole("user"));
    unpinBtn.addEventListener("click", () => allowed(unpinBtn) && unpinNode(nodeId));
  }
  const portsBtn = document.getElementById("ports-btn");
  if (portsBtn) {
    portsBtn.addEventListener("click", () =>
      openPorts(nodeId.slice("sw:".length))
    );
  }
  for (const more of els.detailsBody.querySelectorAll(".addr-more")) {
    more.addEventListener("click", () => {
      const rest = more.nextElementSibling;
      if (rest) rest.classList.remove("hidden");
      more.remove();
    });
  }
  const monitorBtn = document.getElementById("monitor-btn");
  if (monitorBtn) {
    gateButton(monitorBtn, needRole("user"));
    monitorBtn.addEventListener("click", () => allowed(monitorBtn) && toggleMonitor(nodeId));
  }
  // a device of the group: its node is always on the map
  for (const row of els.detailsBody.querySelectorAll(".offline-list li")) {
    row.addEventListener("click", () => focusNode("host:" + row.dataset.mac));
  }
}

/* "10.0.0.1 and 37 more" — a router announces one management address
   per VLAN interface, and all of them belong to the same device */
function fmtAddresses(addresses, limit) {
  const list = addresses || [];
  const keep = limit || 3;
  if (list.length <= keep) return list.join(", ");
  return (
    list.slice(0, keep).join(", ") +
    " " + fmt("andMore", { n: list.length - keep })
  );
}

/* A management address as a link to the device's web interface.
   IPv6 needs brackets in a URL, and a hostname that is not an address
   is left as plain text rather than turned into a broken link. */
function addressLink(address) {
  const text = String(address);
  const host = text.includes(":") ? "[" + text + "]" : text;
  const linkable = /^[0-9.]+$/.test(text) || /^[0-9a-fA-F:.]+$/.test(text);
  if (!linkable) return html`<span>${text}</span>`;
  return html`<a href="${"http://" + host}" target="_blank" rel="noopener">${text}</a>`;
}

/* Management addresses as a column: the first few as links, the rest
   behind "and N more". A router announces one per VLAN interface, and
   38 of them run together into an unreadable wall. */
function addressList(addresses, limit) {
  const list = addresses || [];
  if (!list.length) return "—";
  const keep = limit || 3;
  const shown = list.slice(0, keep).map((a) => html`<li>${addressLink(a)}</li>`);
  if (list.length <= keep) return html`<ul class="addr-list">${shown}</ul>`;
  const rest = list.slice(keep).map((a) => html`<li>${addressLink(a)}</li>`);
  return html`<ul class="addr-list">${shown}</ul><button type="button" class="addr-more">${
    fmt("andMore", { n: list.length - keep })}</button><ul class="addr-list addr-rest hidden">${rest}</ul>`;
}

/* What LLDP says about a device that is already a host on the map:
   the switch heard it on this very port, so the data belongs here
   rather than on a node of its own */
function lldpHostLine(lldp) {
  const kind = lldp.cap_known ? t("kind_" + (lldp.kind || "other")) : "";
  const parts = [
    lldp.sys_name,
    kind,
    lldp.port_id ? t("portLabel") + " " + lldp.port_id : "",
    (lldp.mgmt_ips || []).length === 1 ? lldp.mgmt_ips[0] : "",
    lldp.sys_desc,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : t("lldpUnidentified");
}

/* Flip the host_down alarm flag of a host and re-render */
async function toggleMonitor(nodeId) {
  return busy("monitor:" + nodeId, async () => {
    const host = findHost(nodeId);
    if (!host) return;
    const res = await api("/api/host/" + encodeURIComponent(host.mac), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ monitored: !host.monitored }),
    });
    if (!res.ok) return;
    host.monitored = (await res.json()).monitored;
    renderSidebar();
    showDetails(nodeId);
  }, [document.getElementById("monitor-btn")]);
}

function hideDetails() {
  els.details.classList.add("hidden");
  shownDetails = null;
}

/* ---------- ports panel ---------- */

async function openPorts(ip, highlightPort) {
  // the timer goes before the request: two switches opened one after
  // the other used to leave the first one's timer running
  clearInterval(portsTimer);
  portsTimer = null;
  portsIp = ip;
  portsHighlight = highlightPort || null;
  hideDetails();
  els.journal.classList.add("hidden");
  hideAccountPanels();
  els.alarms.classList.add("hidden");
  els.stp.classList.add("hidden");
  const stillAsked = askPanel();
  await refreshPorts();
  // closed, another switch opened or another panel shown meanwhile
  if (!stillAsked() || portsIp !== ip) return;
  els.ports.classList.remove("hidden");
  clearInterval(portsTimer);
  portsTimer = setInterval(refreshPorts, REFRESH_MS);
}

function closePorts() {
  els.ports.classList.add("hidden");
  clearInterval(portsTimer);
  portsTimer = null;
  portsIp = null;
  panelAsked = null;
}

async function refreshPorts() {
  // its 30 s timer waits for nobody: the request behind the sign-in
  // form is the one that goes on
  const ip = portsIp;
  if (!ip || gateWait) return;
  let data;
  try {
    const res = await api(
      "/api/switch/" + encodeURIComponent(ip) + "/ports"
    );
    data = await res.json();
  } catch (e) {
    // the panel keeps the last table, the header says why
    serviceLost("the ports panel");
    return;
  }
  serviceBack();
  // another switch's table is not drawn over this one's
  if (portsIp !== ip) return;
  lastPorts = data;
  renderPorts(lastPorts);
}

/* "Gi0/2" before "Gi0/10": compare digit runs as numbers */
function naturalCompare(a, b) {
  const chunks = (s) => String(s).match(/\d+|\D+/g) || [];
  const left = chunks(a);
  const right = chunks(b);
  for (let i = 0; i < Math.max(left.length, right.length); i++) {
    const x = left[i];
    const y = right[i];
    if (x === undefined) return -1;
    if (y === undefined) return 1;
    const nx = parseInt(x, 10);
    const ny = parseInt(y, 10);
    if (!isNaN(nx) && !isNaN(ny)) {
      if (nx !== ny) return nx - ny;
    } else if (x !== y) {
      return x < y ? -1 : 1;
    }
  }
  return 0;
}

/* Column the user picked; without one the server order stands
   (active ports first, then by port number) */
function sortPorts(ports) {
  if (!portsSort) return ports;
  const value = {
    speed: (p) => p.speed_mbps,
    in: (p) => p.in_mbps,
    out: (p) => p.out_mbps,
    err: (p) => p.errors_per_min,
    disc: (p) => p.discards_per_min,
    // a loop first, then the ports nothing is known about, then the
    // quiet ones — the order someone sorting by this column wants
    loop: (p) => LOOP_RANK[(p.loop || {}).state] ?? -1,
  }[portsSort.key];
  const compare = value
    ? (a, b) => (value(a) ?? -1) - (value(b) ?? -1)
    : (a, b) => naturalCompare(a.name, b.name);
  // the direction goes into the comparator, not a reverse() afterwards,
  // so ports with equal values keep their natural order
  return [...ports].sort((a, b) => portsSort.dir * compare(a, b));
}

function setPortsSort(key) {
  // a second click on the same column flips the direction; numbers
  // start with the largest, names with the first port
  const dir = portsSort && portsSort.key === key ? -portsSort.dir
    : key === "name" ? 1 : -1;
  portsSort = { key, dir };
  for (const th of document.querySelectorAll("#ports th[data-sort]")) {
    th.classList.toggle("sorted-asc", th.dataset.sort === key && dir > 0);
    th.classList.toggle("sorted-desc", th.dataset.sort === key && dir < 0);
  }
  if (lastPorts) renderPorts(lastPorts);
}

/* A column header's tooltip: its full name — a narrow screen shows
   "In", "Err" (v0.8.3) — and for the two counters what they count */
function columnTitle(th) {
  const full = th.querySelector(".th-full");
  const meaning = th.dataset.sort === "err" ? t("errTooltip")
    : th.dataset.sort === "disc" ? t("discTooltip") : "";
  return [full ? full.textContent : "", meaning].filter(Boolean).join("\n\n");
}

/* A column the agent never answered for is a column of dashes that
   means "unknown", not "zero". The header says which, and why. */
function markSilentColumns(columns) {
  for (const th of document.querySelectorAll("#ports th[data-sort]")) {
    const info = (columns || {})[th.dataset.sort];
    const silent = info && !info.answered;
    // answered, but not for every port — the ports at the end of the
    // table are the ones that go missing
    const partial = info && info.answered && info.partial;
    th.classList.toggle("no-answer", !!silent);
    th.classList.toggle("partial", !!partial);
    const base = columnTitle(th);
    if (!silent && !partial) {
      th.title = base;
      continue;
    }
    const head = silent
      ? fmt("colNoAnswer", { oids: (info.oids || []).join(", ") })
      : fmt("colPartial", {
          oids: (info.oids || []).join(", "),
          filled: info.filled || 0,
        });
    th.title =
      head +
      (info.error ? "\n" + info.error : "") +
      (base ? "\n\n" + base : "");
  }
}

/* Sort order of the loop column: a loop first, then unknown, then
   the ports that are fine, then the ones nobody is watching */
const LOOP_RANK = { loop: 3, unknown: 2, ok: 1, off: 0 };

/* One "Loop" cell. Four states, and the difference between the last
   two is the whole point: "no loop" is an answer, "no data" is not. */
function loopCell(tr, loop, report) {
  const cell = document.createElement("td");
  const state = loop ? loop.state : "";
  if (!loop && report && report.polled === false) {
    // nobody has asked yet: unknown, not "the model says nothing"
    cell.textContent = t("loopNoData");
    cell.className = "loop-unknown";
    cell.title = t("loopCardNotPolled");
  } else if (!loop) {
    // the model reports nothing at all — see the switch card
    cell.textContent = "—";
    cell.className = "loop-none";
    cell.title = fmt("loopUnsupportedHint", {
      oid: (report && report.sys_object_id) || "—",
    });
  } else if (state === "loop") {
    cell.textContent = t("loopYes");
    cell.className = "loop-alarm";
    cell.title = fmt("loopRawHint", { raw: loop.raw || "—" });
  } else if (state === "ok") {
    cell.textContent = t("loopOk");
    cell.className = "loop-ok";
  } else if (state === "off") {
    cell.textContent = t("loopOff");
    cell.className = "loop-none";
    cell.title = t("loopOffHint");
  } else {
    cell.textContent = t("loopNoData");
    cell.className = "loop-unknown";
    cell.title = t("loopNoDataHint");
  }
  tr.append(cell);
}

function renderPorts(data) {
  els.portsTitle.textContent = fmt("portsTitle", {
    name: data.name || data.switch,
  });
  markSilentColumns(data.columns);
  const limits = data.thresholds || {};
  // Past this age a rate is shown dimmed with its age beside it; past
  // `hide_after_seconds` the server withholds the number itself. Both
  // come from the service, which is where the intervals live.
  const staleAfter = (data.rates || {}).stale_after_seconds ?? Infinity;
  let highlighted = null;
  // physical ports only; the server puts active ones first
  const rows = sortPorts(data.ports.filter((p) => p.is_physical)).map((p) => {
      const tr = document.createElement("tr");
      if (!p.oper_up) tr.className = "port-down";
      if (portsHighlight && p.name === portsHighlight) {
        tr.classList.add("highlight");
        highlighted = tr;
      }
      const td = (content, cls) => {
        const cell = document.createElement("td");
        if (cls) cell.className = cls;
        cell.append(content);
        tr.append(cell);
      };
      // A rate cell carries its own age. The number is real but was
      // measured a while ago — the counters cycle skipped this switch,
      // or it is busy being scanned — and dropping it would draw the
      // same "—" as a column the agent never answers. Those two say
      // very different things about a switch.
      const rateTd = (value, cls) => {
        const stale =
          p.rate_age_seconds != null && p.rate_age_seconds >= staleAfter;
        // expired: a dash that has something to say, so it asks to be
        // hovered — a dash with nothing behind it does not
        const expired = value == null && p.rate_age_seconds != null;
        td(
          fmtRate(value),
          (cls || "") +
            (stale && value != null ? " stale-rate" : "") +
            (expired ? " rate-expired" : "")
        );
        const cell = tr.lastChild;
        if (expired) {
          // measured once, too long ago to show: say when, do not
          // leave a bare dash that reads as "never polled"
          cell.title = fmt("rateTooOld", {
            when: fmtAge(p.rate_age_seconds),
          });
        } else if (stale) {
          cell.title = fmt("rateMeasured", {
            when: fmtAge(p.rate_age_seconds),
          });
        }
      };
      let name = p.name;
      if (p.lag) name += " (" + p.lag + ")";
      if (p.monitored_hosts) {
        // monitored devices behind this port
        name += " ★" + (p.monitored_hosts > 1 ? p.monitored_hosts : "");
      }
      // the name, and what is known about the port after it; on a phone
      // the marks go to a line of their own under the name (v0.8.3)
      const nameCell = document.createElement("td");
      const nameText = document.createElement("span");
      nameText.className = "port-name";
      nameText.textContent = name;
      const marks = document.createElement("span");
      marks.className = "port-marks";
      nameCell.append(nameText, marks);
      // lldpLocPortDesc: what the operator called this port on the
      // switch itself ("Library", "403 audit")
      if (p.uplink_hint) {
        const chip = document.createElement("span");
        chip.className = "chip warn";
        chip.textContent = t("uplinkHintChip");
        chip.title =
          fmt("uplinkHint", { switch: data.switch, port: p.name }) +
          "\n\n" +
          fmt("uplinkWhy_" + p.uplink_hint.kind, {
            // "10.8.0.1 and 3 more", the count from the server
            ip: p.uplink_hint.ip + (p.uplink_hint.more
              ? " " + fmt("andMore", { n: p.uplink_hint.more }) : ""),
            name: p.uplink_hint.name,
          });
        marks.append(" ", chip);
      }
      if (p.external) {
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.textContent = t("externalPort");
        chip.title = t("externalHint");
        marks.append(" ", chip);
      }
      if (p.label) {
        const label = document.createElement("span");
        label.className = "port-label";
        label.textContent = p.label;
        label.title = t("portLabelName") + "\n" + p.label;
        marks.append(" ", label);
      }
      // addresses this port's damaged frames invented, with the real
      // one each of them is a distortion of
      if (p.flaps) {
        const chip = document.createElement("span");
        chip.className = "chip warn";
        chip.textContent = "⇅ " + p.flaps;
        chip.title = fmt("portFlappingHint", {
          n: p.flaps,
          when: p.flaps_last ? fmtTime(p.flaps_last) : "—",
        });
        marks.append(" ", chip);
      }
      const neighbours = p.lldp || [];
      if (neighbours.length) {
        const chip = document.createElement("span");
        chip.className = "chip" + (p.lldp_forwarded ? " warn" : "");
        const first = neighbours[0];
        chip.textContent =
          "⇄ " + (first.sys_name || first.chassis_id) +
          (neighbours.length > 1 ? " +" + (neighbours.length - 1) : "");
        chip.title =
          neighbours
            .map((n) =>
              [
                t("lldpNeighbour") + ": " + (n.sys_name || n.chassis_id),
                n.vendor || "",
                n.port_id ? t("portLabel") + " " + n.port_id : "",
                fmtAddresses(n.mgmt_ips),
                n.cap_known
                  ? (n.capabilities || []).join(", ")
                  : t("lldpUnidentified"),
                n.sys_desc,
              ]
                .filter(Boolean)
                .join(" · ")
            )
            .join("\n") +
          (p.lldp_forwarded
            ? "\n\n" + t("lldpForwardedHint")
            : p.lldp_crowded
            ? "\n\n" + t("lldpCrowdedHint")
            : "");
        marks.append(" ", chip);
      }
      const suspect = p.suspect_macs || [];
      if (suspect.length) {
        const chip = document.createElement("span");
        chip.className = "chip warn";
        chip.textContent = "⚠ " + suspect.length;
        chip.title =
          fmt("suspectMacs", { n: suspect.length }) + "\n" +
          t("suspectMacsHint") + "\n" +
          suspect
            .map((s) => s.mac + " ← " + s.sample + " (" + s.distance + ")")
            .join("\n");
        marks.append(" ", chip);
      }
      // cut short on a phone: the whole of it in the tooltip
      if (marks.childNodes.length) marks.title = marks.textContent.trim();
      else marks.remove();
      tr.append(nameCell);
      const dot = document.createElement("span");
      dot.className = "dot " + (p.oper_up ? "up" : "down");
      td(dot);
      td(p.oper_up && p.speed_mbps ? fmtSpeed(p.speed_mbps) : "—");
      rateTd(p.in_mbps, "num");
      rateTd(p.out_mbps, "num");
      // damaged frames are a fault, discards are usually filtering
      const overErr = p.errors_per_min > (limits.errors_per_minute ?? Infinity);
      const overDisc =
        p.discards_per_min > (limits.discards_per_minute ?? Infinity);
      rateTd(p.errors_per_min, "num" + (overErr ? " over-error" : ""));
      rateTd(p.discards_per_min, "num" + (overDisc ? " over-discard" : ""));
      loopCell(tr, p.loop, data.loop_detection);
      return tr;
    });
  els.portsBody.replaceChildren(...rows);
  if (highlighted) highlighted.scrollIntoView({ block: "center" });
}

/* Link card: ports of both ends, speed, aggregate members */
function showLinkDetails(edgeId) {
  const link = topology.links.find((l) => linkId(l) === edgeId);
  if (!link) return;
  const swName = switchName;
  const rows = [html`<dt>${fmt("portOnSide", { name: swName(link.a) })}</dt><dd>${linkPortLabel(link, link.a_port)}</dd>
    <dt>${fmt("portOnSide", { name: swName(link.b) })}</dt><dd>${linkPortLabel(link, link.b_port)}</dd>`];
  // trunk speed is unknown — do not show a meaningless dash row
  if (!(link.lag && link.lag.trunk) || link.speed_mbps) {
    rows.push(html`<dt>${t("speed")}</dt><dd>${fmtSpeed(link.speed_mbps) || "—"}</dd>`);
  }
  if (link.lag && link.lag.count > 1) {
    rows.push(html`<dt>${t("lagAggregate")}</dt><dd>${linkLabel(link)}</dd>`);
    // each member with its oper state: green = up, grey = down
    const memberList = (members, states) => html`${htmlJoin(members.map((m, i) => {
      const up = !states || states[i] == null || states[i];
      return html`<span class="dot ${up ? "up" : "down"} inline-dot"></span>${m}`;
    }), ", ")} (${t("lacp")})`;
    if ((link.lag.a_members || []).length)
      rows.push(html`<dt>${fmt("portsOf", { name: swName(link.a) })}</dt><dd>${memberList(link.lag.a_members, link.lag.a_states)}</dd>`);
    if ((link.lag.b_members || []).length)
      rows.push(html`<dt>${fmt("portsOf", { name: swName(link.b) })}</dt><dd>${memberList(link.lag.b_members, link.lag.b_states)}</dd>`);
  }
  const source = link.source || "fdb";
  const sourceLabel =
    source === "both" ? t("srcBoth") : source === "lldp" ? t("srcLldp") : t("srcFdb");
  const sourceHint = source === "fdb" ? t("srcHintFdb") : t("srcHintLldp");
  rows.push(html`<dt>${t("linkSource")}</dt><dd title="${sourceHint}">${sourceLabel}</dd>`);
  const hints = [];
  if (link.stp_blocking) hints.push(hintKey("stpBlockingHint"));
  // What is uncertain about this line, said in the card and not only
  // in a tooltip somebody has to know to hover
  if (link.order_unknown) hints.push(hintKey("linkOrderUnknown"));
  if (link.cycle_unresolved) hints.push(hintKey("linkCycleUnresolved"));
  shownDetails = { type: "link", id: edgeId };
  setDetails(html`<h3>${swName(link.a)} — ${swName(link.b)}</h3><dl>${rows}</dl>${hints}`);
  els.details.classList.remove("hidden");
  els.journal.classList.add("hidden");
  hideAccountPanels();
  els.alarms.classList.add("hidden");
  els.stp.classList.add("hidden");
  closePorts();
}

/* ---------- alarms ---------- */

function fmtDuration(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  if (seconds < 60) return seconds + t("durS");
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return minutes + t("durM");
  const hours = Math.floor(minutes / 60);
  return hours + t("durH") + " " + (minutes % 60) + t("durM");
}

/* One alarms request; also picks up the flap window for the tooltip */
async function fetchAlarms(query) {
  const data = await (await api("/api/alarms?" + query)).json();
  if (data.flap_window_hours) flapWindowHours = data.flap_window_hours;
  return data.alarms || [];
}

/* Reloads both lists and repaints the panel and the badge */
async function refreshAlarms() {
  const [active, cleared] = await Promise.all([
    fetchAlarms("active=1"),
    fetchAlarms("active=0&limit=50"),
  ]);
  lastAlarms = { active, cleared };
  activeAlarms = active;
  renderBadge();
  renderAlarms();
}

async function toggleAlarms() {
  if (!els.alarms.classList.contains("hidden")) {
    els.alarms.classList.add("hidden");
    return;
  }
  const stillAsked = askPanel();
  await refreshAlarms();
  if (!stillAsked()) return;
  hideDetails();
  els.journal.classList.add("hidden");
  hideAccountPanels();
  els.stp.classList.add("hidden");
  closePorts();
  els.alarms.classList.remove("hidden");
}

/* Manually clear one active alarm (with confirmation), then refresh */
async function clearAlarm(id) {
  return busy("alarm:" + id, async () => {
    if (!confirm(t("clearConfirm"))) return;
    const res = await api("/api/alarms/" + id + "/clear", { method: "POST" });
    if (!res.ok) return;
    await refreshAlarms();
  }, []);
}

function renderAlarms() {
  const body = els.alarmsBody;
  body.replaceChildren();
  const now = Date.now() / 1000;
  const section = (titleKey, alarms, isActive) => {
    const h = document.createElement("h4");
    h.textContent = t(titleKey);
    body.append(h);
    if (!alarms.length) {
      const empty = document.createElement("p");
      empty.className = "no-alarms";
      empty.textContent = t(titleKey === "alarmsActive" ? "noAlarms" : "noEvents");
      body.append(empty);
      return;
    }
    const ul = document.createElement("ul");
    ul.className = "alarm-list";
    for (const a of alarms) {
      const li = document.createElement("li");
      // head: kind of alarm, the FLAP mark and the clear button
      const head = document.createElement("div");
      head.className = "alarm-head";
      const sev = document.createElement("span");
      sev.className = "sev sev-" + a.severity;
      sev.textContent = t("al_" + a.type);
      head.append(sev);
      if (a.flapping) {
        const flap = document.createElement("span");
        flap.className = "flap-chip";
        flap.textContent = t("flapChip");
        flap.title = fmt("flapTooltip", {
          n: a.raise_count,
          h: flapWindowHours,
        });
        const since = document.createElement("span");
        since.className = "flap-since";
        since.textContent = t("lastRaiseLabel") + " " + fmtTime(a.last_raise);
        head.append(flap, since);
      }
      if (isActive) {
        const clear = document.createElement("button");
        clear.className = "alarm-clear";
        clear.textContent = "✕ " + t("clearBtn");
        gateButton(clear, needRole("user"));
        clear.addEventListener("click", () => allowed(clear) && clearAlarm(a.id));
        head.append(clear);
      }
      // device on its own line, wrapped in full — never truncated
      const subject = document.createElement("div");
      subject.className = "alarm-subject";
      subject.textContent = a.display || a.subject;
      li.append(head, subject);
      if (a.port) {
        const port = document.createElement("div");
        port.className = "alarm-port";
        port.textContent = t("portLabel") + ": " + a.port;
        li.append(port);
      }
      // counter alarms: jump straight to the port they are about
      if (
        a.switch_ip &&
        (a.type === "port_errors" ||
          a.type === "port_discards" ||
          a.type === "port_frame_corruption")
      ) {
        const link = document.createElement("button");
        link.className = "alarm-link";
        link.textContent = t("openPortsLink");
        link.addEventListener("click", () => openPorts(a.switch_ip, a.port));
        li.append(link);
      }
      const sub = document.createElement("div");
      sub.className = "alarm-sub";
      sub.textContent = isActive
        ? fmtTime(a.ts_raised) + " · " + fmtDuration(now - a.ts_raised)
        : fmtTime(a.ts_raised) + " · " + t("clearedAt") + fmtTime(a.ts_cleared);
      li.append(sub);
      ul.append(li);
    }
    body.append(ul);
  };
  section("alarmsActive", lastAlarms.active, true);
  section("alarmsCleared", lastAlarms.cleared, false);
}

/* ---------- STP panel ---------- */

/* "1 h 12 m", or "—" when the switch never reported a change */
function fmtSince(seconds) {
  if (seconds == null) return "—";
  return fmtDuration(seconds);
}

/* Why a switch is said to run a tree or not, in the page's language
   (v0.8.2). The server sends which branch of its verdict decided and
   the numbers in it; a code this page does not know — an older or a
   newer server — falls back to the server's own sentence, not to an
   empty tooltip. */
function stpReasonText(sw) {
  const key = "stpReason_" + sw.reason_code;
  if (!sw.reason_code || t(key) === key) return sw.reason || "";
  const p = sw.reason_params || {};
  return fmt(key, {
    root: p.root || t("stpReasonEmpty"),
    cost: p.cost,
    changes: p.changes,
    since: p.since == null ? "—" : fmtUptime(p.since),
    uptime: p.uptime == null ? "—" : fmtUptime(p.uptime),
    count: p.count,
    neighbours: (p.neighbours || []).map(switchName).join(", "),
  });
}

function stpVerdictText(v) {
  if (!v || v.verdict === "not_operating") return t("stpVerdictNone");
  if (v.verdict === "single") {
    const root = Object.keys(v.roots)[0] || "";
    return fmt("stpVerdictSingle", { root: root });
  }
  return fmt("stpVerdictFragmented", { n: Object.keys(v.roots).length });
}

function renderStp() {
  const body = els.stpBody;
  body.replaceChildren();
  const data = lastStp || { verdict: null, switches: [] };
  const verdict = document.createElement("p");
  verdict.className =
    "stp-verdict" +
    (!data.verdict || data.verdict.verdict === "not_operating"
      ? " none"
      : data.verdict.verdict === "fragmented"
      ? " bad"
      : " ok");
  verdict.textContent = stpVerdictText(data.verdict);
  body.append(verdict);
  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent = t("stpHint");
  body.append(hint);
  // several roots on a physically connected network is a legal state,
  // not necessarily a fault — BPDUs travel inside the VLAN of the port
  // they arrive on, so segments in different VLANs form separate trees
  // by design. Said here because the word "fragmented" implies damage.
  if (data.verdict && data.verdict.verdict === "fragmented") {
    const why = document.createElement("p");
    why.className = "hint";
    why.textContent = t("stpFragmentedVlanHint");
    body.append(why);
    // …and the VLANs each island's trunks sit in, which is usually the
    // whole answer to "why are there two of them"
    const byIp = Object.fromEntries(data.switches.map((sw) => [sw.ip, sw]));
    const list = document.createElement("ul");
    list.className = "stp-roots";
    for (const [root, ips] of Object.entries(data.verdict.roots || {})) {
      const vlans = [
        ...new Set(ips.flatMap((ip) => (byIp[ip] || {}).trunk_vlans || [])),
      ].sort((a, b) => a - b);
      const li = document.createElement("li");
      li.textContent =
        root + " — " + ips.map((ip) => (byIp[ip] || {}).name || ip).join(", ") +
        (vlans.length ? "  (" + t("vlan") + " " + vlans.join(", ") + ")" : "");
      list.append(li);
    }
    if (list.childElementCount) body.append(list);
  }

  if (!data.switches.length) {
    const empty = document.createElement("p");
    empty.className = "no-alarms";
    empty.textContent = t("noData");
    body.append(empty);
    return;
  }
  const table = document.createElement("table");
  table.className = "ports-table stp-table";
  const head = document.createElement("tr");
  for (const key of [
    "switchLabel", "stpState", "stpPriority", "stpRoot", "stpCost",
    "stpRootPort", "stpChanges", "stpLastChange",
  ]) {
    const th = document.createElement("th");
    th.textContent = t(key);
    head.append(th);
  }
  const thead = document.createElement("thead");
  thead.append(head);
  table.append(thead);
  // The root of the tree, as the network agreed it. A switch that had
  // to be identified as root by its neighbours reports zeros about
  // itself, and printing those in its own row would undo the finding.
  const agreedRoot = Object.keys((data.verdict || {}).roots || {})[0] || "";
  const tbody = document.createElement("tbody");
  for (const sw of data.switches) {
    const tr = document.createElement("tr");
    const cells = sw.confirmed_root
      ? [
          sw.name,
          t("stpStateRoot"),
          // it never told us its priority; the root's identity came
          // from the switches that follow it
          "—",
          agreedRoot || sw.designated_root || "—",
          "—",
          "—",
          "—",
          "—",
        ]
      : sw.operating
      ? [
          sw.name,
          sw.is_root ? t("stpStateRoot") : t("stpStateMember"),
          String(sw.priority),
          sw.designated_root || "—",
          String(sw.root_cost),
          sw.root_port || "—",
          String(sw.top_changes),
          fmtSince(sw.time_since_change),
        ]
      : // root, cost and root port are meaningless here and are shown
        // as such rather than as the zeros the switch reports
        [sw.name, t("stpStateOff"), "—", "—", "—", "—", "—", "—"];
    cells.forEach((value, i) => {
      const td = document.createElement("td");
      td.textContent = value;
      // the basis for the verdict, either way: an operator who cannot
      // see why a switch is called operating cannot check the claim
      if (i === 1) td.title = stpReasonText(sw);
      if (i === 1 && !sw.operating) td.className = "stp-off";
      if (i === 1 && sw.is_root) td.className = "stp-root";
      if (i === 3 && sw.root_nonstandard) {
        // the agent put the priority in the low byte and MoonLan put
        // it back; the number here is not the one the switch prints
        td.className = "stp-corrected";
        td.title = t("stpBridgeIdFixed");
      }
      tr.append(td);
    });
    tbody.append(tr);
    // Only for a switch whose tree we say is operating. "BLOCKING" is
    // a statement about a spanning tree, and printing one under a row
    // of dashes that says there is no tree contradicts it in place.
    if (sw.operating && sw.blocking_ports && sw.blocking_ports.length) {
      const note = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 8;
      td.className = "stp-blocking";
      td.textContent =
        t("stpBlocking") + ": " + sw.blocking_ports.join(", ");
      note.append(td);
      tbody.append(note);
    }
  }
  table.append(tbody);
  // only the table scrolls sideways; the verdict and the hint above it
  // are prose and have no business moving off the left edge
  const scroller = document.createElement("div");
  scroller.className = "table-scroll";
  scroller.append(table);
  body.append(scroller);
  // Switches that answer dot1dStp* and name no root. They are not a
  // tree of their own — counting them as one turned three RouterOS
  // boxes into a second spanning tree called "unknown" — but they must
  // not quietly disappear from the panel either.
  const noRoot = (data.verdict || {}).rootless || [];
  if (noRoot.length) {
    const byIp = Object.fromEntries(data.switches.map((sw) => [sw.ip, sw]));
    const line = document.createElement("p");
    line.className = "hint";
    line.textContent =
      fmt("stpRootless", {
        switches: noRoot.map((ip) => (byIp[ip] || {}).name || ip).join(", "),
      });
    body.append(line);
  }
}

async function toggleStp() {
  if (!els.stp.classList.contains("hidden")) {
    els.stp.classList.add("hidden");
    return;
  }
  const stillAsked = askPanel();
  lastStp = await (await api("/api/stp")).json();
  renderStp();
  if (!stillAsked()) return;
  hideDetails();
  closePorts();
  hideAccountPanels();
  els.journal.classList.add("hidden");
  els.alarms.classList.add("hidden");
  els.stp.classList.remove("hidden");
}

/* ---------- journal ---------- */

function renderJournal(events) {
  els.journalList.replaceChildren(
    ...events.map((ev) => {
      const item = document.createElement("li");
      const time = document.createElement("span");
      time.className = "ev-time";
      time.textContent = fmtTime(ev.ts);
      const type = document.createElement("span");
      type.className =
        {
          new_mac: "ev-new",
          host_down: "ev-down",
          host_up: "ev-up",
          alarm_raised: "ev-down",
          alarm_cleared: "ev-up",
        }[ev.event] || "";
      // events arrive as codes; unknown codes are shown as-is
      if (ev.event === "account_locked") type.className = "ev-down";
      const translated = t("ev_" + ev.event);
      type.textContent = translated === "ev_" + ev.event ? ev.event : translated;
      const host = document.createElement("span");
      host.className = "ev-host";
      host.textContent = accountEventText(ev) || noteEventText(ev) ||
        linkDroppedText(ev) || ev.name || ev.ip || ev.mac || layoutEventText(ev);
      if (ev.vendor) {
        // a new device says who made it; any other event keeps it to
        // the tooltip
        if (ev.event === "new_mac") host.textContent += " · " + ev.vendor;
        host.title = (ev.mac || "") + " · " + ev.vendor;
      }
      item.append(time, type, host);
      // who did it: a pin, a reset, a cleared alarm, somebody else's
      // account — not a sign-in, where it would only repeat the name
      if (ev.user && !(ACCOUNT_EVENTS.has(ev.event) && ev.user === ev.mac)) {
        const who = document.createElement("span");
        who.className = "ev-user";
        who.textContent = fmt("evBy", {
          user: ev.user === "@console" ? t("evConsole") : ev.user,
        });
        item.append(who);
      }
      return item;
    })
  );
  if (!events.length) {
    const empty = document.createElement("li");
    empty.textContent = t("noEvents");
    els.journalList.append(empty);
  }
}

/* Sign-ins and account changes: the account, then what the details
   say — the address, the role before and after, a temporary password. */
const ACCOUNT_EVENTS = new Set([
  "login", "logout", "account_locked", "user_added", "user_role",
  "user_password", "user_disabled", "user_enabled", "user_totp_reset",
  "user_totp_enabled", "user_unlocked", "user_sessions_closed",
  "user_deleted", "user_passkey_added", "user_passkey_removed",
  "user_passkeys_reset", "passkey_clone_suspected",
]);

function accountEventText(ev) {
  if (!ACCOUNT_EVENTS.has(ev.event)) return "";
  let data = {};
  try {
    data = ev.details ? JSON.parse(ev.details) : {};
  } catch (e) {
    data = {};
  }
  const parts = [ev.mac];
  if (data.role) {
    parts.push(
      (data.was ? t("role_" + data.was) + " → " : "") + t("role_" + data.role)
    );
  }
  if (data.temporary) parts.push(t("evTemporary"));
  if (data.label) parts.push("“" + data.label + "”");
  if (data.passwordless) parts.push(t("keyPasswordlessShort"));
  if (data.address) parts.push(data.address);
  if (data.recovery_left !== undefined) {
    parts.push(fmt("evRecoveryUsed", { n: data.recovery_left }));
  } else if (data.method && t("evMethod_" + data.method) !== "evMethod_" + data.method) {
    parts.push(t("evMethod_" + data.method));
  }
  if (data.minutes) parts.push(fmt("evLockedFor", { n: data.minutes }));
  return parts.join(" · ");
}

/* "3 nodes: access-sw-1, access-sw-2, gw" for a pin or a release.
   The server records ids and a count, not a sentence, so the entry
   reads in the page's language and by the captions on the map. */
function layoutEventText(ev) {
  if (ev.event !== "layout_pinned" && ev.event !== "layout_released") {
    return "";
  }
  let data;
  try {
    data = JSON.parse(ev.details);
  } catch (e) {
    return ev.details || "";
  }
  const names = (data.ids || []).map(nodeTitle);
  const more = data.n - names.length;
  return fmt(more > 0 ? "evNodesMore" : "evNodes", {
    n: data.n, names: names.join(", "), more: more,
  });
}

/* A link the inference took back: which, and why. Recorded as data
   since v0.8.2 and worded here; an older entry is the English line it
   was written as, shown as it is. */
function linkDroppedText(ev) {
  if (ev.event !== "link_dropped") return "";
  let data = null;
  try {
    data = JSON.parse(ev.details);
  } catch (e) {
    data = null;
  }
  if (!data || typeof data !== "object") return ev.details || "";
  const source = t("evLinkSource_" + data.source);
  const link = fmt("evLinkEnds", {
    a: switchName(data.a), a_port: data.a_port, b: switchName(data.b), b_port: data.b_port,
    source: source === "evLinkSource_" + data.source ? data.source || "" : source,
  });
  const key = "evLinkWhy_" + data.reason;
  const why = t(key) === key
    ? data.reason || ""
    : fmt(key, {
      a: switchName(data.a), b: switchName(data.b),
      behind: switchName(data.behind || ""), cycle: data.cycle || "",
    });
  return why ? link + ": " + why : link;
}

/* A name or a note changed, deleted or moved: the node, then what
   happened to it — the data is the server's, the words the page's */
function noteEventText(ev) {
  if (!["note_changed", "note_moved", "notes_imported"].includes(ev.event)) return "";
  let data = {};
  try {
    data = ev.details ? JSON.parse(ev.details) : {};
  } catch (e) {
    data = {};
  }
  if (ev.event === "notes_imported") return fmt("evNotesImported", data);
  // a node no longer on the map is said in words, not by its id
  const title = (id) => (nodesDs && nodesDs.get(id) ? nodeTitle(id) : nodeWords(id));
  if (ev.event === "note_moved") {
    return fmt("evNoteMoved", { from: title(data.from || ""), to: title(data.to || ev.mac) });
  }
  const what = [];
  if (data.deleted) what.push(t("evNoteDeleted"));
  if (data.label !== undefined) {
    what.push(data.label ? fmt("evNoteLabel", { label: data.label }) : t("evNoteLabelGone"));
  }
  if (data.note !== undefined) what.push(t("evNoteText"));
  return [title(ev.mac), ...what].join(" · ");
}

async function toggleJournal() {
  if (!els.journal.classList.contains("hidden")) {
    els.journal.classList.add("hidden");
    return;
  }
  const stillAsked = askPanel();
  const res = await api("/api/journal?limit=100");
  const data = await res.json();
  lastEvents = data.events;
  renderJournal(lastEvents);
  if (!stillAsked()) return;
  hideDetails();
  closePorts();
  hideAccountPanels();
  els.alarms.classList.add("hidden");
  els.stp.classList.add("hidden");
  els.journal.classList.remove("hidden");
}

/* ---------- search and rescan ---------- */

/* The lists that the search goes through, each with its counter */
function searchedLists() {
  return [
    [els.namedList, els.namedCount],
    [els.switchList, els.switchCount],
    [els.hostList, els.hostCount],
    [els.unlocatedList, els.unlocatedCount],
  ];
}

/* Hides what the query does not match. With a query, a header counts
   what is left of how many: "Switches 2 of 9". */
function applySearchFilter() {
  const q = els.search.value.trim().toLowerCase();
  for (const [list, count] of searchedLists()) {
    const items = list.querySelectorAll("li");
    let shown = 0;
    for (const item of items) {
      const hidden = q !== "" && !item.dataset.search.includes(q);
      item.classList.toggle("hidden-by-search", hidden);
      if (!hidden) shown++;
    }
    count.textContent = q
      ? fmt("searchCount", { n: shown, total: items.length })
      : String(items.length);
    // Tab from the search goes to the first row it found
    listTabStop(list);
  }
}

async function rescan() {
  isScanning = true;
  updateScanStatus();
  await api("/api/scan", { method: "POST" });
  watchScan();
}

els.search.addEventListener("input", applySearchFilter);
els.sidebar.addEventListener("keydown", listKeys);

els.detailsClose.addEventListener("click", hideDetails);
// ⓘ after a card's one-line hint opens the full text under it, and
// closes it again; a button, so Enter and Space do the same
els.detailsBody.addEventListener("click", (event) => {
  const more = event.target.closest(".hint-more");
  if (!more) return;
  const full = more.parentElement.nextElementSibling;
  const open = !full.classList.toggle("hidden");
  more.setAttribute("aria-expanded", String(open));
});
els.journalBtn.addEventListener("click", toggleJournal);
els.notesClose.addEventListener("click", () => els.notesPanel.classList.add("hidden"));
els.journalClose.addEventListener("click", () =>
  els.journal.classList.add("hidden")
);
els.portsClose.addEventListener("click", closePorts);
for (const th of document.querySelectorAll("#ports th[data-sort]")) {
  th.addEventListener("click", () => setPortsSort(th.dataset.sort));
}
els.freezeBtn.addEventListener("click", toggleFreeze);
els.sidebarToggle.addEventListener("click", toggleSidebar);
els.arrangeBtn.addEventListener("click", () => allowed(els.arrangeBtn) && toggleArrangeMode());
els.portLabelsBtn.addEventListener("click", () => {
  if (els.portLabelsMenu.classList.contains("hidden")) openPortLabelsMenu();
  else closePortLabelsMenu(true);
});
els.portLabelsMenu.addEventListener("keydown", (event) =>
  menuKeys(event, els.portLabelsMenu, closePortLabelsMenu)
);
document.addEventListener("click", (event) => {
  if (!els.nodeMenu.contains(event.target)) closeNodeMenu();
  if (!els.userMenu.contains(event.target)) closeUserMenu(false);
  if (!els.portLabelsMenu.contains(event.target) &&
      !els.portLabelsBtn.contains(event.target)) {
    closePortLabelsMenu(false);
  }
  if (!els.moreMenu.contains(event.target) && !els.moreBtn.contains(event.target)) {
    closeMoreMenu(false);
  }
});
els.moreBtn.addEventListener("click", () => {
  if (els.moreMenu.classList.contains("hidden")) openMoreMenu();
  else closeMoreMenu(true);
});
els.moreMenu.addEventListener("keydown", (event) =>
  menuKeys(event, els.moreMenu, closeMoreMenu)
);
els.userMenu.addEventListener("keydown", (event) =>
  menuKeys(event, els.userMenu, closeUserMenu)
);
els.usersClose.addEventListener("click", () => {
  closeUserMenu(false);
  els.users.classList.add("hidden");
});
els.userChip.addEventListener("click", toggleAccount);
els.accountClose.addEventListener("click", () => els.account.classList.add("hidden"));
els.actionsClose.addEventListener("click", closeActions);
els.alarmsBtn.addEventListener("click", toggleAlarms);
els.alarmsClose.addEventListener("click", () =>
  els.alarms.classList.add("hidden")
);
els.stpBtn.addEventListener("click", toggleStp);
els.stpClose.addEventListener("click", () => els.stp.classList.add("hidden"));
window.addEventListener("resize", fitHeader);
els.langRu.addEventListener("click", () => setLang("ru"));
els.langEn.addEventListener("click", () => setLang("en"));

/* Keyboard. `P` pins or releases whatever is selected — but only when
   the focus is not in a field, or it would fire while somebody types
   a MAC into the search box. */
document.addEventListener("keydown", (event) => {
  const target = event.target;
  const typing =
    target &&
    (target.tagName === "INPUT" ||
      target.tagName === "TEXTAREA" ||
      target.isContentEditable);
  // …nor inside a menu, where the keys move through the items
  if (typing || (target && target.closest && target.closest('[role="menu"]'))) return;
  if (event.key === "p" || event.key === "P") {
    togglePinOnSelection();
  } else if (event.key === "Escape") {
    if (network) network.unselectAll();
    setSelectedNode(null);
    closeNodeMenu();
    closeMoreMenu(false);
    closePortLabelsMenu(false);
  }
});

/* `P` on the selection: if anything in it is loose, pin the lot;
   otherwise unpin the lot. One key, and its effect is predictable
   from what is on screen. */
function togglePinOnSelection() {
  if (!network) return;
  const ids = network.getSelectedNodes();
  if (!ids.length) return;
  const why = needRole("user");
  if (why) {
    showToast(why);
    return;
  }
  const loose = ids.filter((id) => !isPinned(id));
  if (loose.length) {
    const at = network.getPositions(loose);
    const moved = {};
    for (const id of loose) if (at[id]) moved[id] = at[id];
    pinNodes(moved);
  } else {
    unpinNodes(ids);
  }
}

redrawOnLanguage(els.details, () => {
  if (!shownDetails) return;
  if (shownDetails.type === "link") showLinkDetails(shownDetails.id);
  else showDetails(shownDetails.id);
});
redrawOnLanguage(els.journal, () => lastEvents && renderJournal(lastEvents));
redrawOnLanguage(els.notesPanel, redrawNotesPanel);
redrawOnLanguage(els.ports, () => lastPorts && renderPorts(lastPorts));
redrawOnLanguage(els.alarms, () => lastAlarms && renderAlarms());
redrawOnLanguage(els.stp, () => lastStp && renderStp());
redrawOnLanguage(els.actions, renderAction);
redrawOnLanguage(els.account, renderAccount);
redrawOnLanguage(els.users, refreshUsers);
redrawOnLanguage(els.about, renderAbout);
redrawOnLanguage(els.exportDialog, redrawExportDialog);
// the sign-in form is redrawn, with the name typed so far; a form half
// way through a code or a password is left alone
redrawOnLanguage(els.gate, () => {
  if (gateView !== "signIn") return;
  const typed = els.gateBody.querySelector('input[name="username"]');
  renderSignIn(undefined, typed && typed.value);
});

applyStatic();
applyArrangeMode();
loadMe();
// The layout arrives with the first map, so nodes start where they
// were left rather than where the physics engine throws them and then
// get yanked into place a moment later.
loadTopology();
setInterval(loadTopology, REFRESH_MS);
setInterval(watchLayoutMotion, 1000);
