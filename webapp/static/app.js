// app.js — Telegram Mini App логика: авторизация через initData, отрисовка
// матрицы-октаграммы, каталог разборов, покупка через Stars.
const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  document.documentElement.setAttribute(
    "data-theme", tg.colorScheme === "dark" ? "dark" : "light"
  );
  tg.onEvent("themeChanged", () => {
    document.documentElement.setAttribute("data-theme", tg.colorScheme === "dark" ? "dark" : "light");
  });
}

const initData = tg?.initData || "";

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

async function api(path, opts = {}) {
  const headers = { "X-Telegram-Init-Data": initData, ...(opts.headers || {}) };
  if (opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { ...opts, headers });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
  return res.json();
}

const app = document.getElementById("app");
const tabbar = document.getElementById("tabbar");
let ME = null;
let CATALOG = null;

// ── SVG-октаграмма ──────────────────────────────────────────────────────────
function octagramSVG(points, destiny) {
  const cx = 200, cy = 200, R = 132, badgeR = 27, labelR = R + 34;
  const pt = (i, r) => {
    const a = (-90 + i * 45) * Math.PI / 180;
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  };
  const poly = idxs => idxs.map(i => pt(i, R).join(",")).join(" ");
  const sq1 = poly([0, 2, 4, 6]), sq2 = poly([1, 3, 5, 7]);
  let badges = "", labels = "", spokes = "";
  points.forEach(([num, short], i) => {
    const [x, y] = pt(i, R);
    const [lx, ly] = pt(i, labelR);
    const ca = Math.cos((-90 + i * 45) * Math.PI / 180);
    const anchor = ca > 0.35 ? "start" : ca < -0.35 ? "end" : "middle";
    const [sx, sy] = pt(i, R - badgeR);
    spokes += `<line x1="${cx}" y1="${cy}" x2="${sx.toFixed(1)}" y2="${sy.toFixed(1)}" stroke="var(--border)" stroke-width="0.8"/>`;
    badges += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${badgeR}" fill="var(--bg)" stroke="url(#gold)" stroke-width="1.6"/>`;
    badges += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${badgeR - 4}" fill="none" stroke="var(--border)" stroke-width="1"/>`;
    badges += `<text x="${x.toFixed(1)}" y="${(y + 11).toFixed(1)}" text-anchor="middle" font-family="Head" font-size="30" fill="var(--head)">${num}</text>`;
    labels += `<text x="${lx.toFixed(1)}" y="${(ly + 4).toFixed(1)}" text-anchor="${anchor}" font-family="Body" font-size="11" letter-spacing="1.6" fill="var(--accent)">${short.toUpperCase()}</text>`;
  });
  return `<svg viewBox="0 0 400 400" class="octa">
    <defs><linearGradient id="gold" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#D8BC77"/><stop offset="0.5" stop-color="#B08A3E"/><stop offset="1" stop-color="#C7A64E"/>
    </linearGradient></defs>
    ${spokes}
    <polygon points="${sq1}" fill="none" stroke="url(#gold)" stroke-width="1.5"/>
    <polygon points="${sq2}" fill="none" stroke="url(#gold)" stroke-width="1.5"/>
    <circle cx="${cx}" cy="${cy}" r="46" fill="var(--bg2)" stroke="url(#gold)" stroke-width="2.2"/>
    <circle cx="${cx}" cy="${cy}" r="40" fill="none" stroke="var(--border)" stroke-width="1"/>
    <text x="${cx}" y="${cy - 14}" text-anchor="middle" font-family="Body" font-size="9.5" letter-spacing="2.6" fill="var(--accent)">СУДЬБА</text>
    <text x="${cx}" y="${cy + 24}" text-anchor="middle" font-family="Head" font-size="62" fill="var(--head)">${destiny}</text>
    ${badges}${labels}
  </svg>`;
}

const PIN_AGES = ["0–32", "32–41", "41–50", "50+"];

function renderMatrix(m, birthDate) {
  const points = [];
  (m.cards || []).slice(0, 6).forEach(c => points.push([c.value, c.label.replace("Число ", "").replace("Кармическое", "Карма")]));
  while (points.length < 8) points.push([m.destiny, "—"]);
  const pins = (m.pinnacles || []).map((n, i) =>
    `<div class="pin"><div class="pin-n">${n}</div><div class="pin-a">${PIN_AGES[i] || ""} лет</div></div>`
  ).join("");
  const cards = (m.cards || []).map(c =>
    `<div class="card"><div class="big">${c.value}</div><div class="lbl">${c.label}</div></div>`
  ).join("");
  return `
    <div class="topbar">
      <div class="eyebrow">Карта твоих чисел</div>
      <h1>${ME.first_name || "Твоя матрица"}</h1>
    </div>
    <div class="octawrap">${octagramSVG(points.slice(0, 8), m.destiny)}</div>
    <div class="destiny-line">${m.destiny_title || ""}</div>
    <div class="cycles">
      <div class="cyc-head"><span class="cyc-rule"></span><span class="cyc-t">Жизненные циклы</span><span class="cyc-rule"></span></div>
      <div class="pins">${pins}</div>
    </div>
    <div class="cards">${cards}</div>
  `;
}

function renderOnboard() {
  return `
    <div class="topbar"><div class="eyebrow">Личный кабинет</div><h1>Привет 🌸</h1></div>
    <div class="onboard">
      <p>Введи дату рождения — и Ева соберёт твою матрицу чисел прямо здесь.</p>
      <input id="ob-date" placeholder="ДД.ММ.ГГГГ" inputmode="numeric">
      <button id="ob-submit">Построить матрицу</button>
    </div>
  `;
}

async function submitBirthdate() {
  const val = document.getElementById("ob-date").value.trim();
  try {
    await api("/api/me/birthdate", { method: "POST", body: JSON.stringify({ birth_date: val, first_name: ME.first_name || "" }) });
    await boot();
  } catch (e) {
    tg?.showAlert(e.message || "Ошибка");
  }
}

// ── каталог ──────────────────────────────────────────────────────────────────
function renderCatalog() {
  if (!CATALOG) return `<div class="empty">Загрузка…</div>`;
  const purchased = new Set(ME.purchased || []);
  return CATALOG.sections.map(sec => `
    <div class="section">
      <div class="section-t">${sec.title}</div>
      ${sec.items.map(it => `
        <div class="item ${purchased.has(it.key) ? "owned" : ""}" data-key="${it.key}">
          <div>
            <div class="item-t">${it.title}</div>
            <div class="item-d">${it.desc}</div>
          </div>
          <div class="item-p">${purchased.has(it.key) ? "Открыт ✓" : it.price + " ⭐"}</div>
        </div>
      `).join("")}
    </div>
  `).join("");
}

async function openReading(key) {
  app.innerHTML = `<div class="empty">Открываю разбор…</div>`;
  try {
    const r = await api(`/api/reading/${key}`);
    app.innerHTML = `
      <button class="back-btn" id="reading-back">← Назад</button>
      <div class="reading-view">
        <h2>${escapeHtml(r.title)}</h2>
        <div class="reading-text">${escapeHtml(r.text)}</div>
      </div>
    `;
    document.getElementById("reading-back").addEventListener("click", render);
  } catch (e) {
    app.innerHTML = `
      <button class="back-btn" id="reading-back">← Назад</button>
      <div class="empty">${e.message || "Разбор ещё готовится"}.<br>Открой его в чате с ботом 🌸</div>
    `;
    document.getElementById("reading-back").addEventListener("click", render);
  }
}

async function onItemClick(key) {
  const purchased = new Set(ME.purchased || []);
  if (purchased.has(key)) {
    await openReading(key);
    return;
  }
  try {
    const res = await api(`/api/buy/${key}`, { method: "POST" });
    if (res.already_purchased) { await boot(); return; }
    tg.openInvoice(res.invoice_url, (status) => {
      if (status === "paid") {
        tg?.showAlert("Оплата прошла! Разбор готовится — через минуту откроется здесь и придёт в чат с ботом 🌸");
        boot();
      }
    });
  } catch (e) {
    tg?.showAlert(e.message || "Не удалось начать оплату");
  }
}

function renderMine() {
  const purchased = ME.purchased || [];
  if (!purchased.length) return `<div class="empty">Пока нет открытых разборов —<br>загляни во вкладку «Разборы» 🔮</div>`;
  if (!CATALOG) return `<div class="empty">Загрузка…</div>`;
  const all = CATALOG.sections.flatMap(s => s.items);
  return `<div class="section"><div class="section-t">Мои разборы</div>${
    purchased.map(key => {
      const it = all.find(x => x.key === key);
      if (!it) return "";
      return `<div class="item owned" data-key="${key}">
        <div><div class="item-t">${it.title}</div></div>
        <div class="item-p">Открыть →</div>
      </div>`;
    }).join("")
  }</div>`;
}

// ── навигация ────────────────────────────────────────────────────────────────
let currentTab = "matrix";

function render() {
  if (currentTab === "matrix") {
    app.innerHTML = ME.matrix ? renderMatrix(ME.matrix) : renderOnboard();
    document.getElementById("ob-submit")?.addEventListener("click", submitBirthdate);
  } else if (currentTab === "catalog") {
    app.innerHTML = renderCatalog();
    app.querySelectorAll(".item").forEach(el => el.addEventListener("click", () => onItemClick(el.dataset.key)));
  } else if (currentTab === "mine") {
    app.innerHTML = renderMine();
    app.querySelectorAll(".item").forEach(el => el.addEventListener("click", () => onItemClick(el.dataset.key)));
  }
}

document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    currentTab = btn.dataset.tab;
    render();
  });
});

async function boot() {
  try {
    ME = await api("/api/me");
    if (!CATALOG) CATALOG = await api("/api/catalog");
    document.getElementById("loading").style.display = "none";
    tabbar.style.display = "flex";
    render();
  } catch (e) {
    app.innerHTML = `<div class="empty">Не удалось загрузить данные.<br>Открой кабинет из бота ещё раз 🙏</div>`;
  }
}

boot();
