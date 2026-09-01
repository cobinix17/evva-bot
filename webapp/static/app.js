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

// Экранируем и кавычки тоже. Через textContent их не экранирует ни один
// браузер, а результат мы подставляем не только в текст, но и внутрь
// атрибутов (value="${...}"). Сейчас туда попадают только проверенные на
// сервере значения — дата и email, — но защита не должна держаться на том,
// что где-то дальше стоит нужная валидация.
const _ESCAPE_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, ch => _ESCAPE_MAP[ch]);
}

async function api(path, opts = {}) {
  const headers = { "X-Telegram-Init-Data": initData, ...(opts.headers || {}) };
  if (opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { ...opts, headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const err = new Error(body.error || res.statusText);
    // Тело ответа кладём на ошибку целиком: часть отказов несёт данные для UI
    // (например code:"redate_required" — предложение купить разбор на другую дату).
    err.body = body;
    err.status = res.status;
    throw err;
  }
  return res.json();
}

const CHANNEL_URL         = "https://t.me/eva_numerologg";
const REVIEWS_CHANNEL_URL = "https://t.me/eva_numerolog_otz";

const app = document.getElementById("app");
const tabbar = document.getElementById("tabbar");
let ME = null;
let CATALOG = null;

// ── SVG-октаграмма ──────────────────────────────────────────────────────────
// points: [num, shortLabel, description][]. Каждая точка — кликабельная
// <g data-i="i">: тап разворачивает пояснение под схемой (см. toggleOctaPoint).
// Бейджи появляются по очереди (staggered fade+scale через CSS-задержку) —
// ощущение "карты, которую исследуешь", а не мгновенно отрисованная картинка.
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
    const delay = (i * 70).toFixed(0);
    spokes += `<line x1="${cx}" y1="${cy}" x2="${sx.toFixed(1)}" y2="${sy.toFixed(1)}" stroke="var(--border)" stroke-width="0.8"/>`;
    const numLen = String(num).length;
    const numFontSize = numLen >= 4 ? 15 : numLen === 3 ? 19 : numLen === 2 ? 24 : 30;
    badges += `<g class="octa-pt" data-i="${i}" style="animation-delay:${delay}ms">`
            + `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${badgeR}" fill="var(--bg)" stroke="url(#gold)" stroke-width="1.6"/>`
            + `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${badgeR - 4}" fill="none" stroke="var(--border)" stroke-width="1"/>`
            + `<text x="${x.toFixed(1)}" y="${(y + (numFontSize <= 20 ? 6 : 8)).toFixed(1)}" text-anchor="middle" font-family="Head" font-size="${numFontSize}" fill="var(--head)" style="font-variant-numeric:lining-nums">${num}</text>`
            + `</g>`;
    // длинные подписи (2 слова) переносим на две строки, иначе текст обрезается
    // краем viewBox у крайних левой/правой точек октаграммы
    const words = short.toUpperCase().split(" ");
    const labelBody = (words.length > 1 && short.length > 6)
      ? `<tspan x="${lx.toFixed(1)}" dy="0">${words[0]}</tspan><tspan x="${lx.toFixed(1)}" dy="12">${words.slice(1).join(" ")}</tspan>`
      : words.join(" ");
    const labelY = (words.length > 1 && short.length > 6) ? ly : ly + 4;
    labels += `<g class="octa-pt" data-i="${i}" style="animation-delay:${delay}ms">`
            + `<text x="${lx.toFixed(1)}" y="${labelY.toFixed(1)}" text-anchor="${anchor}" font-family="Body" font-size="11" letter-spacing="1.6" fill="var(--accent)">${labelBody}</text>`
            + `</g>`;
  });
  return `<svg viewBox="-65 -65 530 530" class="octa">
    <defs><linearGradient id="gold" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#D8BC77"/><stop offset="0.5" stop-color="#B08A3E"/><stop offset="1" stop-color="#C7A64E"/>
    </linearGradient></defs>
    ${spokes}
    <polygon points="${sq1}" fill="none" stroke="url(#gold)" stroke-width="1.5"/>
    <polygon points="${sq2}" fill="none" stroke="url(#gold)" stroke-width="1.5"/>
    <circle cx="${cx}" cy="${cy}" r="46" fill="var(--bg2)" stroke="url(#gold)" stroke-width="2.2"/>
    <circle cx="${cx}" cy="${cy}" r="40" fill="none" stroke="var(--border)" stroke-width="1"/>
    <text x="${cx}" y="${cy - 14}" text-anchor="middle" font-family="Body" font-size="9.5" letter-spacing="2.6" fill="var(--accent)">СУДЬБА</text>
    <text x="${cx}" y="${cy + 24}" text-anchor="middle" font-family="Head" font-size="62" fill="var(--head)" style="font-variant-numeric:lining-nums">${destiny}</text>
    ${badges}${labels}
  </svg>`;
}

let _octaDescs = [];
let _octaOpenIndex = null;

function toggleOctaPoint(i) {
  const panel = document.getElementById("octa-info");
  if (!panel) return;
  if (_octaOpenIndex === i) {
    _octaOpenIndex = null;
    panel.classList.remove("open");
    return;
  }
  _octaOpenIndex = i;
  const d = _octaDescs[i] || {};
  panel.innerHTML = `<div class="octa-info-t">${escapeHtml(d.label || "")}</div><div class="octa-info-d">${escapeHtml(d.desc || "Нет описания")}</div>`;
  panel.classList.add("open");
}

const PIN_AGES = ["0–32", "32–41", "41–50", "50+"];

function renderMatrix(m, birthDate) {
  const points = [];
  const descs  = [];
  (m.cards || []).slice(0, 6).forEach(c => {
    const shortLabel = c.label.replace("Число ", "").replace("Кармическое", "Карма").replace(/Личный год \d+/, mm => "Год " + mm.match(/\d+/)[0]);
    points.push([c.value, shortLabel]);
    descs.push({ label: c.label, desc: c.desc });
  });
  if (m.life_arcana) {
    points.push([m.life_arcana.num, "Аркан"]);
    descs.push({ label: `Аркан — ${m.life_arcana.name}`, desc: m.life_arcana.keyword });
  }
  if (m.year_arcana) {
    points.push([m.year_arcana.num, "Аркан года"]);
    descs.push({ label: `Аркан года — ${m.year_arcana.name}`, desc: m.year_arcana.keyword });
  }
  while (points.length < 8) { points.push([m.destiny, "—"]); descs.push({ label: "—", desc: "" }); }
  _octaDescs = descs;
  _octaOpenIndex = null;

  // Если число повторяется среди 8 точек октаграммы 3+ раза — это не баг, а
  // усиленная энергия (так трактуют повторы в нумерологии). Озвучиваем явно,
  // иначе выглядит как «сломанный» расчёт.
  const freq = {};
  points.forEach(([n]) => { freq[n] = (freq[n] || 0) + 1; });
  const repeated = Object.entries(freq).find(([, c]) => c >= 3);
  const repeatNote = repeated
    ? `<div class="octa-repeat">✨ Число ${repeated[0]} повторяется у тебя ${repeated[1]} раза в матрице — эта энергия усилена</div>`
    : "";

  _pinDescs = m.pinnacles_info || (m.pinnacles || []).map((n, i) => ({ value: n, age: PIN_AGES[i] || "", desc: "" }));
  _pinOpenIndex = null;
  const pins = _pinDescs.map((p, i) =>
    `<div class="pin pin-tap" data-i="${i}"><div class="pin-n">${p.value}</div><div class="pin-a">${p.age} лет</div></div>`
  ).join("");
  const cardList = [...(m.cards || [])];
  if (m.life_arcana) cardList.push({ value: m.life_arcana.num, label: `Аркан — ${m.life_arcana.name}`, desc: m.life_arcana.keyword });
  if (m.year_arcana) cardList.push({ value: m.year_arcana.num, label: `Аркан года — ${m.year_arcana.name}`, desc: m.year_arcana.keyword });
  _cardDescs = cardList;
  _cardOpenIndex = null;
  const cards = cardList.map((c, i) =>
    `<div class="card card-tap" data-i="${i}"><div class="big">${c.value}</div><div class="lbl">${c.label}</div></div>`
  ).join("");
  const spinCard = ME.can_spin
    ? `<div class="spin-card">
        <div><div class="spin-t">🎰 Ежедневный бонус</div><div class="spin-d">Забирай звёзды раз в день — трать на разборы и премиум</div></div>
        <button id="spin-btn">Забрать</button>
      </div>`
    : `<div class="spin-card spin-done">
        <div><div class="spin-t">🎰 Ежедневный бонус</div><div class="spin-d">Уже забрано — приходи завтра 🌸</div></div>
      </div>`;
  const digestCard = ME.digest_count < ME.digest_min
    ? `<div class="spin-card spin-done">
        <div><div class="spin-t">📋 Твой портрет</div><div class="spin-d">Куплено ${ME.digest_count}/${ME.digest_min} разборов — собери ещё, чтобы открыть общий портрет</div></div>
      </div>`
    : `<div class="spin-card">
        <div><div class="spin-t">📋 Твой портрет</div><div class="spin-d">${ME.digest_ready ? "Готов — выжимка по всем купленным разборам" : "Собери связную выжимку из всех купленных разборов"}</div></div>
        <button id="digest-btn">${ME.digest_ready ? "Открыть" : "Собрать"}</button>
      </div>`;
  const d = ME.day_today;
  const dayCard = d ? `
    <div class="day-card">
      <div class="day-num">${d.number}</div>
      <div class="day-body">
        <div class="day-t">🌟 Число дня — ${d.energy}</div>
        <div class="day-d">${escapeHtml(d.advice.charAt(0).toUpperCase() + d.advice.slice(1))}.</div>
        ${d.destiny_note ? `<div class="day-note">💎 ${escapeHtml(d.destiny_note)}</div>` : ""}
      </div>
    </div>` : "";
  const yesnoCard = `
    <div class="spin-card">
      <div><div class="spin-t">🎱 Да / Нет</div><div class="spin-d">Задай вопрос — отвечу по твоим числам</div></div>
      <button id="yesno-open-btn">Спросить</button>
    </div>`;
  const matrixCard = `
    <div class="spin-card">
      <div><div class="spin-t">🔷 Матрица судьбы</div><div class="spin-d">Твоя схема арканов по дате рождения — бесплатно</div></div>
      <button id="destiny-matrix-btn">Открыть</button>
    </div>`;
  const psychoCard = `
    <div class="spin-card">
      <div><div class="spin-t">🔲 Квадрат Пифагора</div><div class="spin-d">Твой характер по цифрам даты рождения — бесплатно</div></div>
      <button id="psycho-btn">Открыть</button>
    </div>`;
  const achieveCard = `
    <div class="spin-card">
      <div><div class="spin-t">🏆 Достижения</div><div class="spin-d">Открывай значки за активность</div></div>
      <button id="achieve-btn">Открыть</button>
    </div>`;
  return `
    <div class="topbar">
      <div class="eyebrow">Карта твоих чисел</div>
      <h1>${escapeHtml(ME.first_name || "Твоя матрица")}</h1>
    </div>
    ${dayCard}
    <div class="section" style="margin-bottom:0">${matrixCard}</div>
    <div class="section" style="margin-bottom:0; margin-top:10px;">${psychoCard}</div>
    <div class="section" style="margin-bottom:0; margin-top:10px;">${yesnoCard}</div>
    <div class="section" style="margin-bottom:0; margin-top:10px;">${spinCard}</div>
    <div class="section" style="margin-bottom:0; margin-top:10px;">${achieveCard}</div>
    <div class="section" style="margin-bottom:0; margin-top:10px;">${digestCard}</div>
    <div class="octawrap">${octagramSVG(points.slice(0, 8), m.destiny)}</div>
    <div class="octa-hint">✦ Нажми на число, чтобы узнать больше</div>
    <div id="octa-info" class="octa-info"></div>
    <div class="destiny-line">${m.destiny_title || ""}</div>
    ${repeatNote}
    <div class="cycles">
      <div class="cyc-head"><span class="cyc-rule"></span><span class="cyc-t">Жизненные циклы</span><span class="cyc-rule"></span></div>
      <div class="octa-hint" style="margin:-2px 0 10px">✦ Нажми на цикл, чтобы узнать больше</div>
      <div class="pins">${pins}</div>
      <div id="pin-info" class="octa-info"></div>
    </div>
    <div class="octa-hint" style="margin:22px 0 -6px">✦ Нажми на карточку, чтобы узнать больше</div>
    <div class="cards">${cards}</div>
    <div id="card-info" class="octa-info" style="margin:8px 24px 4px"></div>
  `;
}

let _cardDescs = [];
let _cardOpenIndex = null;
function toggleCard(i) {
  const panel = document.getElementById("card-info");
  if (!panel) return;
  if (_cardOpenIndex === i) { _cardOpenIndex = null; panel.classList.remove("open"); return; }
  _cardOpenIndex = i;
  const c = _cardDescs[i] || {};
  panel.innerHTML = `<div class="octa-info-t">${escapeHtml(c.label || "")} — ${c.value}</div><div class="octa-info-d">${escapeHtml(c.desc || "Нет описания")}</div>`;
  panel.classList.add("open");
}

let _pinDescs = [];
let _pinOpenIndex = null;
function togglePin(i) {
  const panel = document.getElementById("pin-info");
  if (!panel) return;
  if (_pinOpenIndex === i) { _pinOpenIndex = null; panel.classList.remove("open"); return; }
  _pinOpenIndex = i;
  const p = _pinDescs[i] || {};
  panel.innerHTML = `<div class="octa-info-t">Цикл ${i + 1} · ${p.age} лет — число ${p.value}</div><div class="octa-info-d">${escapeHtml(p.desc || "Нет описания")}</div>`;
  panel.classList.add("open");
}

// Цветной ромб «Матрицы судьбы» (Ладини): 8 внешних точек звезды по кругу,
// 4 внутренних ядра, центральный Аркан предназначения. Тап по точке —
// значение аркана под схемой.
let _matrixPoints = [];
let _matrixOpen = null;
function destinyMatrixSVG(m) {
  const cx = 200, cy = 200, Rout = 150, Rin = 78, badge = 24;
  // 8 внешних точек: чередуем цвета «база / производная»
  const palette = ["#B08A3E", "#7C5CBF", "#C25E7A", "#4E8C86", "#C99A3E", "#5E7CC2", "#9B5EC2", "#C27A4E"];
  const pt = (i, r, n) => {
    const a = (-90 + i * (360 / n)) * Math.PI / 180;
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  };
  _matrixPoints = [];
  let outer = "", inner = "", diamonds = "";
  // два наложенных квадрата (ромб + прямой) через 8 внешних точек
  const sq1 = [0,2,4,6].map(i => pt(i, Rout, 8).map(v=>v.toFixed(1)).join(",")).join(" ");
  const sq2 = [1,3,5,7].map(i => pt(i, Rout, 8).map(v=>v.toFixed(1)).join(",")).join(" ");
  diamonds += `<polygon points="${sq1}" fill="none" stroke="url(#mg)" stroke-width="1.4"/>`;
  diamonds += `<polygon points="${sq2}" fill="none" stroke="url(#mg)" stroke-width="1.4"/>`;
  m.outer.forEach((p, i) => {
    const [x, y] = pt(i, Rout, 8);
    const col = palette[i % palette.length];
    const idx = _matrixPoints.length;
    _matrixPoints.push(p);
    outer += `<g class="mx-pt" data-i="${idx}">`
      + `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${badge}" fill="${col}"/>`
      + `<text x="${x.toFixed(1)}" y="${(y+6).toFixed(1)}" text-anchor="middle" font-family="Head" font-size="20" fill="#fff" style="font-variant-numeric:lining-nums">${p.num}</text>`
      + `</g>`;
  });
  m.inner.forEach((p, i) => {
    const [x, y] = pt(i, Rin, 4);
    const idx = _matrixPoints.length;
    _matrixPoints.push(p);
    inner += `<g class="mx-pt" data-i="${idx}">`
      + `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${badge-4}" fill="var(--bg)" stroke="url(#mg)" stroke-width="1.4"/>`
      + `<text x="${x.toFixed(1)}" y="${(y+5).toFixed(1)}" text-anchor="middle" font-family="Head" font-size="16" fill="var(--head)" style="font-variant-numeric:lining-nums">${p.num}</text>`
      + `</g>`;
  });
  const cIdx = _matrixPoints.length;
  _matrixPoints.push(m.center);
  const centerG = `<g class="mx-pt" data-i="${cIdx}">`
    + `<circle cx="${cx}" cy="${cy}" r="30" fill="#B08A3E"/>`
    + `<text x="${cx}" y="${cy+9}" text-anchor="middle" font-family="Head" font-size="26" fill="#fff" style="font-variant-numeric:lining-nums">${m.center.num}</text>`
    + `</g>`;
  return `<svg viewBox="0 0 400 400" class="octa">
    <defs><linearGradient id="mg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#D8BC77"/><stop offset="1" stop-color="#B08A3E"/>
    </linearGradient></defs>
    ${diamonds}${outer}${inner}${centerG}
  </svg>`;
}

function toggleMatrixPoint(i) {
  const panel = document.getElementById("mx-info");
  if (!panel) return;
  if (_matrixOpen === i) { _matrixOpen = null; panel.classList.remove("open"); return; }
  _matrixOpen = i;
  const p = _matrixPoints[i] || {};
  panel.innerHTML = `<div class="octa-info-t">${escapeHtml(p.label || "")} — ${p.num} · ${escapeHtml(p.name || "")}</div><div class="octa-info-d">${escapeHtml(p.keyword || "")}</div>`;
  panel.classList.add("open");
}

async function openDestinyMatrix() {
  app.innerHTML = `<div class="empty">Строю твою матрицу судьбы…</div>`;
  try {
    const r = await api("/api/destiny_matrix");
    _matrixOpen = null;
    const cta = r.owned
      ? `<button id="mx-open-reading">📖 Открыть полный разбор</button>`
      : `<button id="mx-buy">🔮 Полное толкование матрицы — ${priceHtml(r, true)}</button>`;
    app.innerHTML = `
      <button class="back-btn" id="mx-back">← Назад</button>
      <div class="topbar"><div class="eyebrow">Матрица судьбы</div><h1>🔷 Твои арканы</h1></div>
      <div class="octawrap">${destinyMatrixSVG(r.matrix)}</div>
      <div class="octa-hint">✦ Нажми на аркан, чтобы узнать его значение</div>
      <div id="mx-info" class="octa-info"></div>
      <div class="onboard" style="margin-top:8px">
        <p>Это твоя карта Старших Арканов по дате рождения. Полное толкование —
        как арканы влияют на судьбу, деньги, отношения и предназначение — в подробном разборе.</p>
        ${cta}
      </div>
    `;
    document.getElementById("mx-back").addEventListener("click", render);
    app.querySelectorAll(".mx-pt").forEach(el =>
      el.addEventListener("click", () => toggleMatrixPoint(parseInt(el.dataset.i, 10)))
    );
    document.getElementById("mx-buy")?.addEventListener("click", () => renderPayScreen("matrix_full"));
    document.getElementById("mx-open-reading")?.addEventListener("click", () => openReading("matrix_full"));
  } catch (e) {
    tg?.showAlert(e.message || "Не удалось построить матрицу");
    render();
  }
}

// ── Квадрат Пифагора (психоматрица) ─────────────────────────────────────────
let _psychoCells = [];
let _psychoOpen = null;
function togglePsychoCell(i) {
  const panel = document.getElementById("psy-info");
  if (!panel) return;
  if (_psychoOpen === i) { _psychoOpen = null; panel.classList.remove("open"); return; }
  _psychoOpen = i;
  const c = _psychoCells[i] || {};
  panel.innerHTML = `<div class="octa-info-t">${escapeHtml(c.title || "")} — цифра ${c.digit} (${c.count})</div><div class="octa-info-d">${escapeHtml(c.desc || "")}</div>`;
  panel.classList.add("open");
}

let _psychoLines = [];
let _psychoLineOpen = null;
function togglePsychoLine(i) {
  const panel = document.getElementById("psy-line-info");
  if (!panel) return;
  if (_psychoLineOpen === i) { _psychoLineOpen = null; panel.classList.remove("open"); return; }
  _psychoLineOpen = i;
  const l = _psychoLines[i] || {};
  panel.innerHTML = `<div class="octa-info-t">${escapeHtml(l.title || "")} — ${l.count}</div><div class="octa-info-d">${escapeHtml(l.desc || "")}</div>`;
  panel.classList.add("open");
}

async function openPsychomatrix() {
  app.innerHTML = `<div class="empty">Строю твой квадрат Пифагора…</div>`;
  try {
    const r = await api("/api/psychomatrix");
    _psychoCells = r.cells;
    _psychoLines = r.lines;
    _psychoOpen = null;
    _psychoLineOpen = null;
    // Ячейки идут по цифрам 1..9; классическая раскладка столбцами:
    //   1 4 7 / 2 5 8 / 3 6 9. cells[digit-1] соответствует цифре.
    const idx = d => d - 1;
    const cell = d => {
      const c = r.cells[idx(d)];
      const filled = c.count > 0;
      return `<div class="psy-cell ${filled ? "psy-filled" : "psy-empty"}" data-i="${idx(d)}">
        <div class="psy-rep">${filled ? escapeHtml(c.repeat) : "—"}</div>
        <div class="psy-lbl">${escapeHtml(c.title)}</div>
      </div>`;
    };
    const grid = `<div class="psy-grid">
      ${cell(1)}${cell(4)}${cell(7)}
      ${cell(2)}${cell(5)}${cell(8)}
      ${cell(3)}${cell(6)}${cell(9)}
    </div>`;
    const lines = r.lines.map((l, i) =>
      `<div class="psy-line psy-line-tap" data-i="${i}"><span class="psy-line-t">${escapeHtml(l.title)}</span><span class="psy-line-n">${l.count}</span></div>`
    ).join("");
    const cta = r.owned
      ? `<button id="psy-open-reading">📖 Открыть полный психопортрет</button>`
      : `<button id="psy-buy">🔲 Подробный психопортрет — ${priceHtml(r, true)}</button>`;
    app.innerHTML = `
      <button class="back-btn" id="psy-back">← Назад</button>
      <div class="topbar"><div class="eyebrow">Квадрат Пифагора</div><h1>🔲 Твой характер по цифрам</h1></div>
      ${grid}
      <div class="octa-hint">✦ Нажми на ячейку, чтобы узнать значение</div>
      <div id="psy-info" class="octa-info"></div>
      <div class="cycles">
        <div class="cyc-head"><span class="cyc-rule"></span><span class="cyc-t">Линии характера</span><span class="cyc-rule"></span></div>
        <div class="octa-hint" style="margin:-2px 0 10px">✦ Нажми на линию, чтобы узнать больше</div>
        <div class="psy-lines">${lines}</div>
        <div id="psy-line-info" class="octa-info"></div>
      </div>
      <div class="onboard" style="margin-top:8px">
        <p>Это твой характер, разложенный на 9 качеств по силе. Полный психопортрет —
        что делать с сильными и слабыми сторонами, темперамент и отношения — в подробном разборе.</p>
        ${cta}
      </div>
    `;
    document.getElementById("psy-back").addEventListener("click", render);
    app.querySelectorAll(".psy-cell").forEach(el =>
      el.addEventListener("click", () => togglePsychoCell(parseInt(el.dataset.i, 10)))
    );
    app.querySelectorAll(".psy-line-tap").forEach(el =>
      el.addEventListener("click", () => togglePsychoLine(parseInt(el.dataset.i, 10)))
    );
    document.getElementById("psy-buy")?.addEventListener("click", () => renderPayScreen("psychomatrix"));
    document.getElementById("psy-open-reading")?.addEventListener("click", () => openReading("psychomatrix"));
  } catch (e) {
    tg?.showAlert(e.message || "Не удалось построить квадрат");
    render();
  }
}

async function openAchievements() {
  app.innerHTML = `<div class="empty">Загружаю достижения…</div>`;
  try {
    const r = await api("/api/achievements");
    const earned = r.items.filter(a => a.earned).length;
    const grid = r.items.map(a => `
      <div class="ach ${a.earned ? "ach-on" : "ach-off"}">
        <div class="ach-emoji">${a.earned ? a.emoji : "🔒"}</div>
        <div class="ach-t">${escapeHtml(a.title)}</div>
        <div class="ach-d">${escapeHtml(a.desc)}</div>
      </div>`).join("");
    app.innerHTML = `
      <button class="back-btn" id="ach-back">← Назад</button>
      <div class="topbar"><div class="eyebrow">Открыто ${earned} из ${r.items.length}</div><h1>🏆 Достижения</h1></div>
      <div class="ach-grid">${grid}</div>
    `;
    document.getElementById("ach-back").addEventListener("click", render);
  } catch (e) {
    tg?.showAlert(e.message || "Не удалось загрузить");
    render();
  }
}

function renderYesNo() {
  app.innerHTML = `
    <button class="back-btn" id="yn-back">← Назад</button>
    <div class="onboard">
      <div class="eyebrow">🎱 Да / Нет</div>
      <p>Задай вопрос, на который нужен ответ Да или Нет — отвечу по твоим числам.</p>
      <div id="yn-answer"></div>
      <input id="yn-input" placeholder="Например: стоит ли соглашаться?" maxlength="300">
      <button id="yn-send">Спросить</button>
    </div>
  `;
  document.getElementById("yn-back").addEventListener("click", render);
  document.getElementById("yn-send").addEventListener("click", askYesNo);
}

async function askYesNo() {
  const input = document.getElementById("yn-input");
  const q = (input?.value || "").trim();
  if (q.length < 3) { tg?.showAlert("Напиши вопрос текстом 🙂"); return; }
  const btn = document.getElementById("yn-send");
  const box = document.getElementById("yn-answer");
  if (btn) { btn.disabled = true; btn.textContent = "Смотрю в числа…"; }
  if (box) box.innerHTML = "";
  try {
    const r = await api("/api/yesno", { method: "POST", body: JSON.stringify({ question: q }) });
    if (box) box.innerHTML = `<div class="reading-text" style="margin-bottom:14px">${escapeHtml(r.answer)}</div>`;
  } catch (e) {
    tg?.showAlert(e.message || "Не удалось ответить");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Спросить ещё"; }
  }
}

async function openDigest() {
  const btn = document.getElementById("digest-btn");
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  app.innerHTML = `<div class="empty">Собираю портрет из всех купленных разборов…</div>`;
  try {
    const r = await api("/api/profile_digest", { method: "POST" });
    ME.digest_ready = true;
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
      <div class="empty">${e.message || "Не получилось собрать портрет"}</div>
    `;
    document.getElementById("reading-back").addEventListener("click", render);
  }
}

async function spinDaily() {
  const btn  = document.getElementById("spin-btn");
  const card = btn?.closest(".spin-card");
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = "✨";
  card?.classList.add("charging");
  try {
    // Небольшая намеренная пауза — "энергия собирается", не мгновенный
    // попап. API обычно отвечает быстрее, поэтому ждём оба параллельно.
    const [res] = await Promise.all([
      api("/api/spin", { method: "POST" }),
      new Promise(r => setTimeout(r, 550)),
    ]);
    ME.ref_balance = (ME.ref_balance || 0) + res.amount;
    ME.can_spin = false;
    if (card) {
      card.classList.remove("charging");
      card.innerHTML = `<div class="spin-reveal">+${res.amount} <span class="spin-reveal-star">⭐</span></div>`;
      setTimeout(render, 1300);
    } else {
      render();
    }
  } catch (e) {
    tg?.showAlert(e.message || "Не получилось");
    card?.classList.remove("charging");
    btn.disabled = false;
    btn.textContent = "Забрать";
  }
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
// Цена с учётом акции: если действует скидка — новая цена, а старая зачёркнута.
// withRub=true добавляет рублёвую цену (тоже со старой зачёркнутой).
function priceHtml(it, withRub) {
  const sale = it.base && it.base !== it.price;
  let s = sale ? `<span class="price-old">${it.base} ⭐</span> ${it.price} ⭐` : `${it.price} ⭐`;
  if (withRub && it.price_rub) {
    s += " / " + (sale && it.base_rub
      ? `<span class="price-old">${it.base_rub}₽</span> ${it.price_rub}₽`
      : `${it.price_rub}₽`);
  }
  return s;
}

function renderCatalog() {
  if (!CATALOG) return `<div class="empty">Загрузка…</div>`;
  const purchased = new Set(ME.purchased || []);
  const banner = CATALOG.discount
    ? `<div class="sale-banner">🔥 Акция: −${CATALOG.discount}% на все разборы</div>`
    : "";
  return banner + CATALOG.sections.map(sec => `
    <div class="section">
      <div class="section-t">${sec.title}</div>
      ${sec.items.map(it => `
        <div class="item ${purchased.has(it.key) ? "owned" : ""}" data-key="${it.key}">
          <div>
            <div class="item-t">${it.title}</div>
            <div class="item-d">${it.desc}</div>
          </div>
          <div class="item-p">${purchased.has(it.key) ? "Открыт ✓" : priceHtml(it)}</div>
        </div>
      `).join("")}
    </div>
  `).join("");
}

async function openReading(key) {
  app.innerHTML = `<div class="empty">Открываю разбор…</div>`;
  try {
    const r = await api(`/api/reading/${key}`);
    renderReadingResult(key, r.title, r.text);
  } catch (e) {
    // 404 — разбор куплен, но ещё не сгенерирован: показываем форму даты,
    // чтобы получить его прямо здесь, без ухода в чат с ботом.
    renderReadingDateForm(key);
  }
}

function renderReadingResult(key, title, text) {
  app.innerHTML = `
    <button class="back-btn" id="reading-back">← Назад</button>
    <div class="reading-view">
      <h2>${escapeHtml(title)}</h2>
      <div class="reading-text">${escapeHtml(text)}</div>
      <button id="reading-regen-btn" style="margin-top:18px">🔁 Другая дата</button>
    </div>
  `;
  document.getElementById("reading-back").addEventListener("click", render);
  document.getElementById("reading-regen-btn").addEventListener("click", () => renderReadingDateForm(key, true));
}

// Разборы на ДВЕ даты — тот же список, что TWO_DATE_KEYS в config.py.
const TWO_DATE_KEYS = new Set(["compat"]);

function renderReadingDateForm(key, forceNew) {
  const item = findCatalogItem(key);
  const title = item ? item.title : "Разбор";
  const isCompat = TWO_DATE_KEYS.has(key);
  let inner;
  if (key === "business_name") {
    inner = `
      <p>Введи название бизнеса, бренда или проекта — Ева разберёт его по числам.</p>
      <input id="rd-subject" placeholder="Например: Ромашка, EvaShop" maxlength="50">
    `;
  } else if (isCompat) {
    inner = `
      <p>Введи две даты рождения — Ева составит разбор совместимости.</p>
      <input id="rd-date1" placeholder="Твоя дата: ДД.ММ.ГГГГ" inputmode="numeric" value="${(!forceNew && ME.birth_date) ? escapeHtml(ME.birth_date) : ""}">
      <input id="rd-date2" placeholder="Дата партнёра: ДД.ММ.ГГГГ" inputmode="numeric" style="margin-top:10px">
    `;
  } else {
    const prefill = (!forceNew && ME.birth_date) ? escapeHtml(ME.birth_date) : "";
    // Разбор для другого человека: спрашиваем имя. Без него числа имени,
    // души и личности посчитались бы по имени владельца аккаунта, и текст
    // обращался бы к чужому человеку его именем.
    const nameField = forceNew ? `
      <input id="rd-name" placeholder="Имя человека (можно пропустить)" maxlength="30" style="margin-top:10px">
    ` : "";
    inner = `
      <p>${forceNew
            ? "Для кого этот разбор? Введи дату рождения и имя."
            : "Для кого делаем разбор? Введи дату рождения."}</p>
      <input id="rd-date" placeholder="ДД.ММ.ГГГГ" inputmode="numeric" value="${prefill}">
      ${nameField}
    `;
  }
  app.innerHTML = `
    <button class="back-btn" id="reading-back">← Назад</button>
    <div class="onboard">
      <div class="eyebrow">${escapeHtml(title)}</div>
      ${inner}
      <button id="rd-submit" style="margin-top:14px">🔮 Получить разбор</button>
    </div>
  `;
  document.getElementById("reading-back").addEventListener("click", render);
  document.getElementById("rd-submit").addEventListener("click", () => generateReading(key));
}

async function generateReading(key) {
  let body;
  if (key === "business_name") {
    const s = document.getElementById("rd-subject").value.trim();
    if (s.length < 2) { tg?.showAlert("Введи название 🌸"); return; }
    body = { subject: s };
  } else if (TWO_DATE_KEYS.has(key)) {
    const d1 = document.getElementById("rd-date1").value.trim();
    const d2 = document.getElementById("rd-date2").value.trim();
    if (!d1 || !d2) { tg?.showAlert("Введи обе даты 🌸"); return; }
    body = { date1: d1, date2: d2 };
  } else {
    const d = document.getElementById("rd-date").value.trim();
    if (!d) { tg?.showAlert("Введи дату рождения 🌸"); return; }
    body = { date: d };
    const nm = document.getElementById("rd-name")?.value.trim();
    if (nm) body.name = nm;
  }
  app.innerHTML = `<div class="empty">⏳ Ева составляет разбор…<br>Это занимает до минуты 🔮</div>`;
  try {
    const r = await api(`/api/reading/${key}/generate`, { method: "POST", body: JSON.stringify(body) });
    await boot();               // подтянуть обновлённую дату рождения/каталог
    renderReadingResult(key, r.title, r.text);
  } catch (e) {
    if (e.body && e.body.code === "redate_required") {
      renderRedateOffer(e.body);
      return;
    }
    tg?.showAlert(e.message || "Не удалось составить разбор");
    renderReadingDateForm(key, true);
  }
}

// Оплаченная дата по этому разбору уже использована — предлагаем разбор для
// другого человека как НОВУЮ возможность (со скидкой), а не как отказ.
function renderRedateOffer(o) {
  const title = (CATALOG && findCatalogItem(o.key)?.title) || "Разбор";
  const line  = `${o.price} ⭐${o.price_rub ? ` / ${o.price_rub}₽` : ""}`;
  app.innerHTML = `
    <button class="back-btn" id="rdo-back">← Назад</button>
    <div class="onboard">
      <div class="eyebrow">💞 Разбор для близкого человека</div>
      <h2>${escapeHtml(title)}</h2>
      <p>Твой разбор готов и всегда доступен в «Мои разборы».</p>
      <p>Эта дата — уже другой человек, а значит и разбор другой: числа,
         характер и судьба у него свои. Составлю так же подробно.</p>
      <p style="margin-top:10px">
        <span class="price-old">${o.full} ⭐</span>
        <strong>${line}</strong> — со скидкой −${o.discount}%
      </p>
      <button id="rdo-buy">💞 Разобрать другого человека — ${line}</button>
    </div>`;
  document.getElementById("rdo-back").onclick = () => renderTab("readings");
  document.getElementById("rdo-buy").onclick  = () => buyRedate(o.key);
}

async function buyRedate(key) {
  try {
    const res = await api(`/api/redate/${key}`, { method: "POST" });
    tg.openInvoice(res.invoice_url, (status) => {
      if (status === "paid") {
        tg?.showAlert("Оплата прошла 🌸 Теперь введи дату рождения человека.");
        boot().then(() => openReading(key));
      }
    });
  } catch (e) {
    tg?.showAlert(e.message || "Не удалось открыть оплату");
  }
}

async function buyWithStars(key) {
  try {
    const res = await api(`/api/buy/${key}`, { method: "POST" });
    if (res.already_purchased) { await boot(); return; }
    tg.openInvoice(res.invoice_url, (status) => {
      if (status === "paid") {
        tg?.showAlert("Оплата прошла 🌸 Открой разбор — введи дату и получишь его прямо здесь.");
        boot();
      }
    });
  } catch (e) {
    tg?.showAlert(e.message || "Не удалось начать оплату");
  }
}

async function buyWithBalance(key) {
  try {
    const res = await api(`/api/balance/buy/${key}`, { method: "POST" });
    if (res.already_purchased) { await boot(); return; }
    tg?.showAlert("Оплачено балансом 🌸 Открой разбор — введи дату и получишь его прямо здесь.");
    await boot();
    await openReading(key);
  } catch (e) {
    tg?.showAlert(e.message || "Не удалось оплатить балансом");
  }
}

function findCatalogItem(key) {
  if (!CATALOG) return null;
  for (const sec of CATALOG.sections) {
    const it = sec.items.find(x => x.key === key);
    if (it) return it;
  }
  return null;
}

async function onItemClick(key) {
  const purchased = new Set(ME.purchased || []);
  if (purchased.has(key)) {
    await openReading(key);
    return;
  }
  renderPayScreen(key);
}

// Экран выбора способа оплаты разбора — звёзды / карта ₽ / СБП ₽ / баланс.
function renderPayScreen(key) {
  const item    = findCatalogItem(key);
  if (!item) { buyWithStars(key); return; }
  const balance = ME.ref_balance || 0;
  const rub     = item.price_rub;
  const priceLine = priceHtml(item, true);
  const emailVal = ME.email ? escapeHtml(ME.email) : "";
  let btns = "";
  if (balance >= item.price)
    btns += `<button class="pay-btn" data-m="balance">⭐ Балансом (${item.price} ⭐)</button>`;
  btns += `<button class="pay-btn" data-m="stars">⭐ ${item.price} звёзд Telegram</button>`;
  if (rub) {
    btns += `<button class="pay-btn" data-m="card">💳 Картой — ${rub}₽</button>`;
    btns += `<button class="pay-btn" data-m="sbp">📱 СБП / QR — ${rub}₽</button>`;
  }
  app.innerHTML = `
    <button class="back-btn" id="pay-back">← Назад</button>
    <div class="onboard">
      <div class="eyebrow">${escapeHtml(item.title)} — ${priceLine}</div>
      <p>${escapeHtml(item.desc || "")}</p>
      ${rub ? `<input id="pay-email" placeholder="email для чека (для оплаты ₽)" inputmode="email" value="${emailVal}" style="margin-bottom:6px">` : ""}
      <div class="pay-btns">${btns}</div>
    </div>
  `;
  document.getElementById("pay-back").addEventListener("click", render);
  app.querySelectorAll(".pay-btn").forEach(b => b.addEventListener("click", () => {
    const m = b.dataset.m;
    if (m === "balance") buyWithBalance(key);
    else if (m === "stars") buyWithStars(key);
    else buyWithRub(key, m === "card" ? "bank_card" : "sbp");
  }));
}

async function buyWithRub(key, method) {
  const emailEl = document.getElementById("pay-email");
  const email = (emailEl?.value || ME.email || "").trim();
  if (!email) { tg?.showAlert("Введи email — на него придёт чек об оплате 🌸"); return; }
  try {
    const res = await api(`/api/buy_rub/${key}`, {
      method: "POST", body: JSON.stringify({ method, email }),
    });
    if (res.already_purchased || res.already_premium) { await boot(); return; }
    ME.email = email;
    tg?.showAlert("Открываю страницу оплаты. После оплаты вернись сюда — разбор откроется 🌸");
    if (res.confirmation_url) tg.openLink(res.confirmation_url);
  } catch (e) {
    tg?.showAlert(e.message || "Не удалось создать оплату");
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

// ── премиум + AI-чат «Спроси Еву» ─────────────────────────────────────────────
function renderPremium() {
  const premiumBlock = !ME.is_premium ? `
      <div class="topbar"><div class="eyebrow">Ева Премиум</div><h1>💎 Я рядом каждый день</h1></div>
      <div class="onboard">
        <ul class="prem-list">
          <li>💬 «Спроси Еву» без лимита — вопросы по числам когда угодно</li>
          <li>🎱 «Да / Нет» без лимита — а не 3 в день</li>
          <li>🌟 Число дня с личным толкованием под твоё число судьбы</li>
          <li>🌅 Личный прогноз каждое утро</li>
          <li>✨ До 30 разборов в месяц без поштучной покупки</li>
          <li>⚡ Приоритетная генерация без очереди</li>
        </ul>
        ${ME.yookassa ? `<input id="pay-email" placeholder="email для чека (для оплаты ₽)" inputmode="email" value="${ME.email ? escapeHtml(ME.email) : ""}" style="margin-bottom:6px">` : ""}
        <button id="premium-buy-btn">⭐ Оформить за 399 ⭐/мес</button>
        ${ME.yookassa ? `
        <button id="premium-card-btn" style="margin-top:8px">💳 Картой — ${ME.premium_price_rub}₽ (на месяц)</button>
        <button id="premium-sbp-btn" style="margin-top:8px">📱 СБП / QR — ${ME.premium_price_rub}₽ (на месяц)</button>` : ""}
      </div>
    ` : (() => {
      const until = ME.premium_until ? new Date(ME.premium_until).toLocaleDateString("ru-RU") : "";
      return `
        <div class="topbar"><div class="eyebrow">Премиум активен${until ? " до " + until : ""}</div><h1>💬 Спроси Еву</h1></div>
        <div class="onboard">
          <p>Спроси о чём угодно — я твой личный нумеролог. Знаю твои числа, период
          и все разборы, что делала для тебя, и помню наш разговор. Любовь, деньги,
          работа, важное решение — пиши как ${ME.gender === "m" ? "близкому другу" : "близкой подруге"}.
          Например: «что с деньгами в марте?» или «стоит ли сейчас менять работу?»</p>
          <div id="ask-answer"></div>
          <input id="ask-input" placeholder="Твой вопрос..." maxlength="300">
          <button id="ask-send-btn">Спросить</button>
        </div>
      `;
    })();

  // Вкладка «Ева» — только AI-раздел «Спроси Еву» / оффер премиума.
  // Реферальная программа перенесена в «Ещё» (см. renderMore).
  return premiumBlock;
}

async function loadReferralBlock() {
  const el = document.getElementById("ref-block");
  if (!el) return;
  try {
    const r = await api("/api/referral");
    el.innerHTML = `
      <p>🎁 Тому, кого пригласишь — ${r.welcome_bonus} ⭐ на баланс сразу.<br>
         💰 Тебе — ${r.bonus_percent}% звёздами с каждой его покупки.</p>
      <div class="ref-stats">
        <div class="ref-stat"><div class="ref-stat-n">${r.count}</div><div class="ref-stat-l">приглашено</div></div>
        <div class="ref-stat"><div class="ref-stat-n">${r.earned}</div><div class="ref-stat-l">заработано ⭐</div></div>
        <div class="ref-stat"><div class="ref-stat-n">${r.balance}</div><div class="ref-stat-l">баланс ⭐</div></div>
      </div>
      <button id="ref-copy-btn">Скопировать ссылку</button>
    `;
    document.getElementById("ref-copy-btn").addEventListener("click", () => {
      navigator.clipboard?.writeText(r.ref_link).then(() => tg?.showAlert("Ссылка скопирована 🌸"));
    });
  } catch (e) {
    el.innerHTML = `<p>${e.message || "Не удалось загрузить"}</p>`;
  }
}

async function sendAskQuestion() {
  const input = document.getElementById("ask-input");
  const btn   = document.getElementById("ask-send-btn");
  const out   = document.getElementById("ask-answer");
  const question = input.value.trim();
  if (question.length < 3) { tg?.showAlert("Напиши вопрос текстом, хотя бы пару слов 🙂"); return; }
  btn.disabled = true;
  btn.textContent = "Ева думает…";
  // Лента диалога: НЕ затираем предыдущие реплики, а добавляем пузырьками —
  // видно всю переписку, пока болтаешь с Евой.
  out.insertAdjacentHTML("beforeend", `<div class="ask-bubble ask-user">${escapeHtml(question)}</div>`);
  const think = document.createElement("div");
  think.className = "ask-bubble ask-think";
  think.textContent = "Ева думает…";
  out.appendChild(think);
  input.value = "";
  think.scrollIntoView({ behavior: "smooth", block: "end" });
  try {
    const res = await api("/api/ask", { method: "POST", body: JSON.stringify({ question }) });
    think.classList.remove("ask-think");
    think.textContent = res.answer;
  } catch (e) {
    think.classList.remove("ask-think");
    think.classList.add("ask-error");
    think.textContent = e.message || "Ошибка";
  } finally {
    btn.disabled = false;
    btn.textContent = "Спросить";
    think.scrollIntoView({ behavior: "smooth", block: "end" });
  }
}

async function buyPremium() {
  try {
    const res = await api("/api/premium/buy", { method: "POST" });
    if (res.already_premium) { await boot(); return; }
    tg.openInvoice(res.invoice_url, (status) => {
      if (status === "paid") {
        tg?.showAlert("Добро пожаловать в Премиум! 💎");
        boot();
      }
    });
  } catch (e) {
    tg?.showAlert(e.message || "Не удалось начать оплату");
  }
}

async function buyPremiumRub(method) {
  const emailEl = document.getElementById("pay-email");
  const email = (emailEl?.value || ME.email || "").trim();
  if (!email) { tg?.showAlert("Введи email — на него придёт чек об оплате 🌸"); return; }
  try {
    const res = await api("/api/buy_rub/premium", {
      method: "POST", body: JSON.stringify({ method, email }),
    });
    if (res.already_premium) { await boot(); return; }
    ME.email = email;
    tg?.showAlert("Открываю страницу оплаты. После оплаты премиум активируется автоматически 🌸");
    if (res.confirmation_url) tg.openLink(res.confirmation_url);
  } catch (e) {
    tg?.showAlert(e.message || "Не удалось создать оплату");
  }
}

// ── настройки, промокод, обратная связь ───────────────────────────────────────
function renderMore() {
  return `
    <div class="topbar"><div class="eyebrow">Настройки</div><h1>⚙️ Ещё</h1></div>

    <div class="cycles">
      <div class="cyc-head"><span class="cyc-rule"></span><span class="cyc-t">Реферальная программа</span><span class="cyc-rule"></span></div>
    </div>
    <div id="ref-block" class="onboard"><p>Загрузка…</p></div>

    <div class="onboard">
      <div class="toggle-row">
        <div>
          <div class="section-t" style="margin-bottom:4px">Утренние уведомления</div>
          <p style="margin:0">Личный нумерологический прогноз каждое утро</p>
        </div>
        <label class="switch">
          <input type="checkbox" id="notif-toggle" ${ME.notifications ? "checked" : ""}>
          <span class="switch-track"></span>
        </label>
      </div>
    </div>

    <div class="onboard">
      <div class="section-t" style="margin-bottom:10px">Каналы Евы</div>
      <p>Прогнозы и разборы — в основном канале. Живые отзывы тех,
         кто уже разбирался, — в отдельном.</p>
      <button id="ch-main-btn" style="margin-top:10px">📢 Наш канал</button>
      <button id="ch-rev-btn" style="margin-top:8px">💬 Отзывы о Еве</button>
    </div>

    <div class="onboard">
      <div class="section-t" style="margin-bottom:10px">Промокод</div>
      <div id="promo-block">
        <input id="promo-code-input" placeholder="КОД" maxlength="20" style="text-transform:uppercase">
        <button id="promo-check-btn">Проверить</button>
      </div>
    </div>

    <div class="onboard">
      <div class="section-t" style="margin-bottom:10px">Обратная связь</div>
      <div id="feedback-choice">
        <p>Есть идея, чего не хватает, или нашла ошибку? Читаю каждое сообщение лично.</p>
        <button id="feedback-idea-btn">💡 Предложить идею</button>
        <button id="feedback-bug-btn" style="margin-top:8px">🐞 Сообщить об ошибке</button>
      </div>
      <div id="feedback-form" style="display:none">
        <p id="feedback-hint"></p>
        <input id="feedback-input" placeholder="Твоё сообщение" maxlength="800">
        <button id="feedback-send-btn">Отправить</button>
      </div>
    </div>
  `;
}

// Смена имени убрана из веба — имя для чужого разбора теперь спрашивается в
// боте при вводе другой даты, а смена своего имени осталась в профиле бота.

async function toggleNotifications(e) {
  try {
    await api("/api/me/notifications", { method: "POST", body: JSON.stringify({ enabled: e.target.checked }) });
  } catch (err) {
    tg?.showAlert(err.message || "Ошибка");
    e.target.checked = !e.target.checked;
  }
}

async function checkPromo() {
  const code = document.getElementById("promo-code-input").value.trim().toUpperCase();
  if (!code) { tg?.showAlert("Введи код промокода"); return; }
  const block = document.getElementById("promo-block");
  try {
    const res = await api("/api/promo/check", { method: "POST", body: JSON.stringify({ code }) });
    const all = CATALOG ? CATALOG.sections.flatMap(s => s.items) : [];
    const purchased = new Set(ME.purchased || []);
    const options = all.filter(it => !purchased.has(it.key));
    block.innerHTML = `
      <p>✅ Промокод активен — осталось использований: ${res.remaining}. Выбери разбор:</p>
      <div id="promo-options">${options.map(it =>
        `<div class="item" data-key="${it.key}"><div class="item-t">${it.title}</div></div>`
      ).join("")}</div>
    `;
    block.querySelectorAll("#promo-options .item").forEach(el =>
      el.addEventListener("click", () => redeemPromo(code, el.dataset.key))
    );
  } catch (e) {
    tg?.showAlert(e.message || "Промокод не найден");
  }
}

async function redeemPromo(code, key) {
  try {
    const res = await api("/api/promo/redeem", { method: "POST", body: JSON.stringify({ code, key }) });
    if (res.needs_birthdate) {
      tg?.showAlert("Разбор добавлен! Сначала укажи дату рождения на вкладке «Матрица», затем открой чат с ботом.");
    } else {
      tg?.showAlert("Готово! Открой чат с ботом, чтобы подтвердить дату — и разбор будет готов 🌸");
    }
    await boot();
  } catch (e) {
    tg?.showAlert(e.message || "Не удалось активировать промокод");
  }
}

const _FEEDBACK_HINTS = {
  idea: "💡 Есть идея, чего не хватает или что было бы круто добавить? Пиши — читаю лично.",
  bug:  "🐞 Что-то не работает или ведёт себя странно? Опиши, что произошло.",
};
let _feedbackCategory = "idea";

function chooseFeedbackCategory(category) {
  _feedbackCategory = category;
  document.getElementById("feedback-choice").style.display = "none";
  document.getElementById("feedback-form").style.display = "block";
  document.getElementById("feedback-hint").textContent = _FEEDBACK_HINTS[category];
  document.getElementById("feedback-input").focus();
}

async function sendFeedback() {
  const input = document.getElementById("feedback-input");
  const text = input.value.trim();
  if (text.length < 3) { tg?.showAlert("Напиши текстом, хотя бы пару слов 🙂"); return; }
  try {
    await api("/api/feedback", { method: "POST", body: JSON.stringify({ text, category: _feedbackCategory }) });
    input.value = "";
    document.getElementById("feedback-form").style.display = "none";
    document.getElementById("feedback-choice").style.display = "block";
    tg?.showAlert("Спасибо! Обязательно учту 🌸");
  } catch (e) {
    tg?.showAlert(e.message || "Ошибка");
  }
}

// ── навигация ────────────────────────────────────────────────────────────────
let currentTab = "matrix";

function render() {
  if (currentTab === "matrix") {
    app.innerHTML = ME.matrix ? renderMatrix(ME.matrix) : renderOnboard();
    document.getElementById("ob-submit")?.addEventListener("click", submitBirthdate);
    document.getElementById("spin-btn")?.addEventListener("click", spinDaily);
    document.getElementById("digest-btn")?.addEventListener("click", openDigest);
    document.getElementById("yesno-open-btn")?.addEventListener("click", renderYesNo);
    document.getElementById("destiny-matrix-btn")?.addEventListener("click", openDestinyMatrix);
    document.getElementById("psycho-btn")?.addEventListener("click", openPsychomatrix);
    document.getElementById("achieve-btn")?.addEventListener("click", openAchievements);
    app.querySelectorAll(".octa-pt").forEach(el =>
      el.addEventListener("click", () => toggleOctaPoint(parseInt(el.dataset.i, 10)))
    );
    app.querySelectorAll(".pin-tap").forEach(el =>
      el.addEventListener("click", () => togglePin(parseInt(el.dataset.i, 10)))
    );
    app.querySelectorAll(".card-tap").forEach(el =>
      el.addEventListener("click", () => toggleCard(parseInt(el.dataset.i, 10)))
    );
  } else if (currentTab === "catalog") {
    app.innerHTML = renderCatalog();
    app.querySelectorAll(".item").forEach(el => el.addEventListener("click", () => onItemClick(el.dataset.key)));
  } else if (currentTab === "mine") {
    app.innerHTML = renderMine();
    app.querySelectorAll(".item").forEach(el => el.addEventListener("click", () => onItemClick(el.dataset.key)));
  } else if (currentTab === "premium") {
    app.innerHTML = renderPremium();
    document.getElementById("premium-buy-btn")?.addEventListener("click", buyPremium);
    document.getElementById("premium-card-btn")?.addEventListener("click", () => buyPremiumRub("bank_card"));
    document.getElementById("premium-sbp-btn")?.addEventListener("click", () => buyPremiumRub("sbp"));
    document.getElementById("ask-send-btn")?.addEventListener("click", sendAskQuestion);
  } else if (currentTab === "more") {
    app.innerHTML = renderMore();
    loadReferralBlock();
    document.getElementById("notif-toggle").addEventListener("change", toggleNotifications);
    // openTelegramLink открывает канал внутри Telegram, не выкидывая в браузер.
    document.getElementById("ch-main-btn")?.addEventListener("click",
      () => tg?.openTelegramLink(CHANNEL_URL));
    document.getElementById("ch-rev-btn")?.addEventListener("click",
      () => tg?.openTelegramLink(REVIEWS_CHANNEL_URL));
    document.getElementById("promo-check-btn").addEventListener("click", checkPromo);
    document.getElementById("feedback-idea-btn").addEventListener("click", () => chooseFeedbackCategory("idea"));
    document.getElementById("feedback-bug-btn").addEventListener("click", () => chooseFeedbackCategory("bug"));
    document.getElementById("feedback-send-btn").addEventListener("click", sendFeedback);
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
    // #loading существует только внутри исходного #app — после первого
    // render() он уже стёрт (render всегда перезаписывает app.innerHTML),
    // поэтому при повторных вызовах boot() (после сохранения имени, покупки,
    // промокода и т.п.) элемента может не быть — тогда просто пропускаем.
    const loadingEl = document.getElementById("loading");
    if (loadingEl) loadingEl.style.display = "none";
    tabbar.style.display = "flex";
    render();
  } catch (e) {
    app.innerHTML = `<div class="empty">Не удалось загрузить данные.<br>Открой кабинет из бота ещё раз 🙏</div>`;
  }
}

boot();
