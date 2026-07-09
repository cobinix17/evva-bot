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
    const numFontSize = String(num).length > 2 ? 20 : 30;
    badges += `<text x="${x.toFixed(1)}" y="${(y + (numFontSize > 20 ? 11 : 8)).toFixed(1)}" text-anchor="middle" font-family="Head" font-size="${numFontSize}" fill="var(--head)">${num}</text>`;
    // длинные подписи (2 слова) переносим на две строки, иначе текст обрезается
    // краем viewBox у крайних левой/правой точек октаграммы
    const words = short.toUpperCase().split(" ");
    if (words.length > 1 && short.length > 6) {
      labels += `<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" text-anchor="${anchor}" font-family="Body" font-size="10.5" letter-spacing="1.2" fill="var(--accent)">`
              + `<tspan x="${lx.toFixed(1)}" dy="0">${words[0]}</tspan>`
              + `<tspan x="${lx.toFixed(1)}" dy="12">${words.slice(1).join(" ")}</tspan>`
              + `</text>`;
    } else {
      labels += `<text x="${lx.toFixed(1)}" y="${(ly + 4).toFixed(1)}" text-anchor="${anchor}" font-family="Body" font-size="11" letter-spacing="1.6" fill="var(--accent)">${short.toUpperCase()}</text>`;
    }
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
    <text x="${cx}" y="${cy + 24}" text-anchor="middle" font-family="Head" font-size="62" fill="var(--head)">${destiny}</text>
    ${badges}${labels}
  </svg>`;
}

const PIN_AGES = ["0–32", "32–41", "41–50", "50+"];

function renderMatrix(m, birthDate) {
  const points = [];
  (m.cards || []).slice(0, 6).forEach(c => points.push([
    c.value,
    c.label.replace("Число ", "").replace("Кармическое", "Карма").replace(/Личный год \d+/, m => "Год " + m.match(/\d+/)[0]),
  ]));
  if (m.life_arcana) points.push([m.life_arcana.roman, "Аркан"]);
  if (m.year_arcana) points.push([m.year_arcana.roman, "Аркан года"]);
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
      <h1>${escapeHtml(ME.first_name || "Твоя матрица")}</h1>
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
        <button id="reading-regen-btn" style="margin-top:18px">🔁 Заказать заново</button>
      </div>
    `;
    document.getElementById("reading-back").addEventListener("click", render);
    document.getElementById("reading-regen-btn").addEventListener("click", () => regenerateReading(key));
  } catch (e) {
    app.innerHTML = `
      <button class="back-btn" id="reading-back">← Назад</button>
      <div class="empty">${e.message || "Разбор ещё готовится"}.<br>Открой его в чате с ботом 🌸</div>
    `;
    document.getElementById("reading-back").addEventListener("click", render);
  }
}

async function regenerateReading(key) {
  try {
    const res = await api(`/api/reading/${key}/regenerate`, { method: "POST" });
    if (res.needs_birthdate) {
      tg?.showAlert("Сначала укажи дату рождения в чате с ботом 🌸");
      return;
    }
    tg?.showAlert("Открой чат с ботом — там можно выбрать дату и разбор придёт заново 🌸");
  } catch (e) {
    tg?.showAlert(e.message || "Не удалось заказать заново");
  }
}

async function buyWithStars(key) {
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

async function buyWithBalance(key) {
  try {
    const res = await api(`/api/balance/buy/${key}`, { method: "POST" });
    if (res.already_purchased) { await boot(); return; }
    tg?.showAlert("Оплачено балансом! Открой чат с ботом — там нужно подтвердить дату рождения, и разбор будет готов 🌸");
    await boot();
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
  const item    = findCatalogItem(key);
  const balance = ME.ref_balance || 0;
  if (item && balance >= item.price && tg?.showPopup) {
    tg.showPopup({
      title: item.title,
      message: `У тебя ${balance} ⭐ на бонусном балансе — хватает на этот разбор (${item.price} ⭐). Как оплатить?`,
      buttons: [
        { id: "balance", type: "default", text: `Балансом (${item.price} ⭐)` },
        { id: "stars",   type: "default", text: "Звёздами Telegram" },
        { id: "cancel",  type: "cancel" },
      ],
    }, (buttonId) => {
      if (buttonId === "balance") buyWithBalance(key);
      else if (buttonId === "stars") buyWithStars(key);
    });
    return;
  }
  await buyWithStars(key);
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
      <div class="topbar"><div class="eyebrow">Ева Премиум</div><h1>💎 Открой всё</h1></div>
      <div class="onboard">
        <p>До 30 разборов в месяц без поштучной покупки, личный прогноз каждое утро,
        приоритетная генерация и безлимитный AI-чат «Спроси Еву» по твоим числам.</p>
        <button id="premium-buy-btn">Оформить за 399 ⭐/мес</button>
      </div>
    ` : (() => {
      const until = ME.premium_until ? new Date(ME.premium_until).toLocaleDateString("ru-RU") : "";
      return `
        <div class="topbar"><div class="eyebrow">Премиум активен${until ? " до " + until : ""}</div><h1>💬 Спроси Еву</h1></div>
        <div class="onboard">
          <p>Задай любой вопрос по своим числам — отвечу лично. Например:
          «что с деньгами в марте?» или «стоит ли сейчас менять работу?»</p>
          <div id="ask-answer"></div>
          <input id="ask-input" placeholder="Твой вопрос..." maxlength="300">
          <button id="ask-send-btn">Спросить</button>
        </div>
      `;
    })();

  return premiumBlock + `
    <div class="cycles">
      <div class="cyc-head"><span class="cyc-rule"></span><span class="cyc-t">Реферальная программа</span><span class="cyc-rule"></span></div>
    </div>
    <div id="ref-block" class="onboard"><p>Загрузка…</p></div>
  `;
}

async function loadReferralBlock() {
  const el = document.getElementById("ref-block");
  if (!el) return;
  try {
    const r = await api("/api/referral");
    el.innerHTML = `
      <p>Приглашай подруг — получай ${r.bonus_percent}% звёздами с каждой их покупки.</p>
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
  out.innerHTML = "";
  try {
    const res = await api("/api/ask", { method: "POST", body: JSON.stringify({ question }) });
    out.innerHTML = `<div class="ask-bubble">${escapeHtml(res.answer)}</div>`;
    input.value = "";
  } catch (e) {
    out.innerHTML = `<div class="ask-bubble ask-error">${escapeHtml(e.message || "Ошибка")}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Спросить";
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

// ── настройки, промокод, обратная связь ───────────────────────────────────────
function renderMore() {
  return `
    <div class="topbar"><div class="eyebrow">Настройки</div><h1>⚙️ Ещё</h1></div>

    <div class="onboard">
      <div class="section-t" style="margin-bottom:10px">Имя</div>
      <p>Сейчас я называю тебя «${escapeHtml(ME.first_name || "не указано")}».</p>
      <input id="name-input" placeholder="Новое имя" maxlength="30">
      <button id="name-save-btn">Сохранить</button>
    </div>

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
      <div class="section-t" style="margin-bottom:10px">Промокод</div>
      <div id="promo-block">
        <input id="promo-code-input" placeholder="КОД" maxlength="20" style="text-transform:uppercase">
        <button id="promo-check-btn">Проверить</button>
      </div>
    </div>

    <div class="onboard">
      <div class="section-t" style="margin-bottom:10px">Обратная связь</div>
      <p>Есть пожелание или нашла недочёт? Напиши — я лично прочитаю.</p>
      <input id="feedback-input" placeholder="Твоё сообщение" maxlength="800">
      <button id="feedback-send-btn">Отправить</button>
    </div>
  `;
}

async function saveName() {
  const input = document.getElementById("name-input");
  const name = input.value.trim();
  if (name.length < 2) { tg?.showAlert("Введи имя — от 2 символов 🙂"); return; }
  try {
    await api("/api/me/name", { method: "POST", body: JSON.stringify({ name }) });
    tg?.showAlert("Готово! 🌸");
    await boot();
  } catch (e) {
    tg?.showAlert(e.message || "Ошибка");
  }
}

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

async function sendFeedback() {
  const input = document.getElementById("feedback-input");
  const text = input.value.trim();
  if (text.length < 3) { tg?.showAlert("Напиши текстом, хотя бы пару слов 🙂"); return; }
  try {
    await api("/api/feedback", { method: "POST", body: JSON.stringify({ text }) });
    input.value = "";
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
  } else if (currentTab === "catalog") {
    app.innerHTML = renderCatalog();
    app.querySelectorAll(".item").forEach(el => el.addEventListener("click", () => onItemClick(el.dataset.key)));
  } else if (currentTab === "mine") {
    app.innerHTML = renderMine();
    app.querySelectorAll(".item").forEach(el => el.addEventListener("click", () => onItemClick(el.dataset.key)));
  } else if (currentTab === "premium") {
    app.innerHTML = renderPremium();
    document.getElementById("premium-buy-btn")?.addEventListener("click", buyPremium);
    document.getElementById("ask-send-btn")?.addEventListener("click", sendAskQuestion);
    loadReferralBlock();
  } else if (currentTab === "more") {
    app.innerHTML = renderMore();
    document.getElementById("name-save-btn").addEventListener("click", saveName);
    document.getElementById("notif-toggle").addEventListener("change", toggleNotifications);
    document.getElementById("promo-check-btn").addEventListener("click", checkPromo);
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
