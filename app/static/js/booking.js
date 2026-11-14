/* Agendamento online: seleção de atendimento, calendário acessível e horários livres. */
(function () {
  "use strict";
  const root = document.getElementById("booking");
  if (!root) return;

  const MONTHS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"];
  const WEEKDAYS = ["domingo", "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado"];

  const els = {
    form: document.getElementById("bookingForm"),
    service: document.getElementById("f-service"),
    start: document.getElementById("f-start"),
    live: document.getElementById("live"),
    days: document.getElementById("calDays"),
    label: document.getElementById("calLabel"),
    prev: document.getElementById("calPrev"),
    next: document.getElementById("calNext"),
    slots: document.getElementById("slots"),
    slotsTitle: document.getElementById("slotsTitle"),
    toStep2: document.getElementById("toStep2"),
    toStep3: document.getElementById("toStep3"),
    sumService: document.getElementById("sumService"),
    sumDate: document.getElementById("sumDate"),
    sumTime: document.getElementById("sumTime"),
  };

  let monthRequest = 0, slotRequest = 0;
  const status = document.getElementById("calStatus");
  const retry = document.getElementById("calRetry");
  const today = parseISODate(root.dataset.today);
  const state = { serviceId: "", serviceName: "", month: new Date(today.getFullYear(), today.getMonth(), 1),
                  maxDate: null, available: new Set(), date: "", slotIso: "", slotLabel: "" };

  function parseISODate(s) { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); }
  function iso(d) { return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; }
  function announce(msg) { els.live.textContent = ""; window.setTimeout(() => { els.live.textContent = msg; }, 50); }
  function longDate(s) { const d = parseISODate(s); return `${WEEKDAYS[d.getDay()]}, ${d.getDate()} de ${MONTHS[d.getMonth()]}`; }

  function goTo(step) {
    root.querySelectorAll("[data-step]").forEach((el) => { el.hidden = el.dataset.step !== String(step); });
    root.querySelectorAll("[data-step-ind]").forEach((li) => {
      const n = Number(li.dataset.stepInd);
      li.classList.toggle("is-current", n === step);
      li.classList.toggle("is-done", n < step);
      if (n === step) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
    });
    const title = root.querySelector(`[data-step="${step}"] .step-title`);
    if (title) title.focus({ preventScroll: true });
    root.scrollIntoView({ behavior: "smooth", block: "start" });
    if (step === 2) { els.toStep3.disabled = true; els.start.value = ""; state.slotIso = ""; state.slotLabel = ""; slotRequest++; els.slots.replaceChildren(); els.slotsTitle.textContent = "Selecione um dia no calendário"; loadMonth(); }
    if (step === 3) updateSummary();
  }

  function updateSummary() {
    els.sumService.textContent = state.serviceName || "—";
    els.sumDate.textContent = state.date ? longDate(state.date) : "—";
    els.sumTime.textContent = state.slotLabel || "—";
  }

  // ---- Etapa 1: serviço
  root.querySelectorAll('input[name="svc_choice"]').forEach((r) => {
    r.addEventListener("change", () => {
      if (state.serviceId !== r.value) { state.date = ""; state.slotIso = ""; state.slotLabel = ""; els.start.value = ""; els.toStep3.disabled = true; slotRequest++; els.slots.replaceChildren(); els.slotsTitle.textContent = "Selecione um dia no calendário"; }
      state.serviceId = r.value; state.serviceName = r.dataset.name;
      els.service.value = r.value;
      els.toStep2.disabled = false;
    });
  });

  root.querySelectorAll("[data-next]").forEach((b) => b.addEventListener("click", () => goTo(Number(b.dataset.next))));
  root.querySelectorAll("[data-prev]").forEach((b) => b.addEventListener("click", () => goTo(Number(b.dataset.prev))));

  // ---- Etapa 2: calendário
  async function loadMonth() {
    if (!state.serviceId) return;
    const requestId = ++monthRequest;
    const m = state.month;
    const mn = MONTHS[m.getMonth()]; els.label.textContent = `${mn.charAt(0).toUpperCase() + mn.slice(1)} de ${m.getFullYear()}`;
    els.days.setAttribute("aria-busy", "true");
    els.days.replaceChildren();
    els.prev.disabled = els.next.disabled = true;
    status.textContent = "Consultando disponibilidade…";
    retry.hidden = true;
    const ym = `${m.getFullYear()}-${String(m.getMonth() + 1).padStart(2, "0")}`;
    try {
      const r = await fetch(`${root.dataset.daysUrl}?servico=${encodeURIComponent(state.serviceId)}&mes=${ym}`, { headers: { Accept: "application/json" } });
      const data = await r.json();
      if (!r.ok) throw new Error(data.erro || "Erro");
      if (requestId !== monthRequest) return;
      state.available = new Set(data.dias);
      state.maxDate = parseISODate(data.max);
    } catch (e) {
      if (requestId !== monthRequest) return;
      state.available = new Set();
      renderMonth();
      els.days.removeAttribute("aria-busy");
      status.textContent = "Não foi possível carregar a agenda. Tente novamente.";
      retry.hidden = false;
      return;
      announce("Não foi possível carregar a agenda. Tente novamente.");
    }
    renderMonth();
    els.days.removeAttribute("aria-busy");
    const count = state.available.size;
    status.textContent = count ? `${count} dias disponíveis. Escolha uma data destacada.` : "Nenhum horário neste mês. Consulte o próximo mês disponível.";
    announce(count ? `${count} dia(s) com horários disponíveis em ${MONTHS[m.getMonth()]}.` : `Sem horários disponíveis em ${MONTHS[m.getMonth()]}.`);
  }

  function renderMonth() {
    const m = state.month;
    els.days.innerHTML = "";
    const first = new Date(m.getFullYear(), m.getMonth(), 1);
    const offset = (first.getDay() + 6) % 7; // semana começa na segunda
    for (let i = 0; i < offset; i++) {
      const s = document.createElement("span"); s.setAttribute("aria-hidden", "true"); els.days.appendChild(s);
    }
    const daysInMonth = new Date(m.getFullYear(), m.getMonth() + 1, 0).getDate();
    for (let d = 1; d <= daysInMonth; d++) {
      const date = new Date(m.getFullYear(), m.getMonth(), d);
      const key = iso(date);
      const b = document.createElement("button");
      b.type = "button";
      b.className = "cal-day";
      b.textContent = String(d);
      const avail = state.available.has(key);
      const label = `${WEEKDAYS[date.getDay()]}, ${d} de ${MONTHS[m.getMonth()]}`;
      if (avail) {
        b.classList.add("is-available");
        b.setAttribute("aria-label", `${label}, com horários disponíveis`);
        b.addEventListener("click", () => selectDay(key, b));
      } else {
        b.disabled = true;
        b.setAttribute("aria-label", `${label}, sem horários`);
      }
      if (key === iso(today)) b.classList.add("is-today");
      if (key === state.date) { b.classList.add("is-selected"); b.setAttribute("aria-pressed", "true"); }
      els.days.appendChild(b);
    }
    els.prev.disabled = m <= new Date(today.getFullYear(), today.getMonth(), 1);
    els.next.disabled = state.maxDate ? new Date(m.getFullYear(), m.getMonth() + 1, 1) > state.maxDate : false;
  }

  // Navegação por setas entre os dias disponíveis
  els.days.addEventListener("keydown", (ev) => {
    const keys = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 7, ArrowUp: -7 };
    if (!(ev.key in keys)) return;
    const buttons = Array.from(els.days.querySelectorAll("button"));
    const idx = buttons.indexOf(document.activeElement);
    if (idx < 0) return;
    ev.preventDefault();
    let j = idx + keys[ev.key];
    while (j >= 0 && j < buttons.length && buttons[j].disabled) j += Math.sign(keys[ev.key]);
    if (buttons[j]) buttons[j].focus();
  });

  els.prev.addEventListener("click", () => { state.month = new Date(state.month.getFullYear(), state.month.getMonth() - 1, 1); loadMonth(); });
  els.next.addEventListener("click", () => { state.month = new Date(state.month.getFullYear(), state.month.getMonth() + 1, 1); loadMonth(); });

  async function selectDay(key, btn) {
    const requestId = ++slotRequest;
    state.date = key; state.slotIso = ""; state.slotLabel = ""; els.start.value = ""; els.toStep3.disabled = true;
    els.days.querySelectorAll(".is-selected").forEach((x) => { x.classList.remove("is-selected"); x.removeAttribute("aria-pressed"); });
    if (btn) { btn.classList.add("is-selected"); btn.setAttribute("aria-pressed", "true"); }
    els.slotsTitle.textContent = "Carregando horários…";
    els.slots.innerHTML = "";
    els.slots.setAttribute("aria-busy", "true");
    try {
      const r = await fetch(`${root.dataset.slotsUrl}?servico=${encodeURIComponent(state.serviceId)}&data=${key}`, { headers: { Accept: "application/json" } });
      const data = await r.json();
      if (!r.ok) throw new Error(data.erro || "Erro");
      if (requestId !== slotRequest) return;
      renderSlots(data);
    } catch (e) {
      if (requestId !== slotRequest) return;
      const again = document.createElement("button");
      again.type = "button"; again.className = "btn btn-outline-primary"; again.textContent = "Tentar novamente";
      again.addEventListener("click", () => selectDay(key)); els.slots.appendChild(again);
      els.slotsTitle.textContent = "Não foi possível carregar os horários.";
    } finally { if (requestId === slotRequest) els.slots.removeAttribute("aria-busy"); }
  }

  retry.addEventListener("click", loadMonth);

  function renderSlots(data) {
    els.slots.innerHTML = "";
    if (!data.horarios.length) {
      els.slotsTitle.textContent = "Os horários deste dia acabaram de ser preenchidos. Escolha outra data.";
      announce(els.slotsTitle.textContent);
      return;
    }
    els.slotsTitle.textContent = `Horários disponíveis — ${data.rotulo}`;
    data.horarios.forEach((h) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "slot-btn"; b.textContent = h.rotulo;
      b.setAttribute("aria-pressed", h.inicio === state.slotIso ? "true" : "false");
      b.setAttribute("aria-label", `${h.rotulo}`);
      b.addEventListener("click", () => {
        els.slots.querySelectorAll(".slot-btn").forEach((x) => x.setAttribute("aria-pressed", "false"));
        b.setAttribute("aria-pressed", "true");
        state.slotIso = h.inicio; state.slotLabel = h.rotulo; els.start.value = h.inicio;
        els.toStep3.disabled = false;
        announce(`Horário ${h.rotulo} selecionado.`);
      });
      els.slots.appendChild(b);
    });
    announce(`${data.horarios.length} horário(s) disponível(is).`);
  }

  // ---- Validação leve no cliente (o servidor valida tudo novamente)
  els.form.addEventListener("submit", (ev) => {
    if (!els.service.value || !els.start.value) { ev.preventDefault(); goTo(!els.service.value ? 1 : 2); announce("Selecione o atendimento e o horário."); return; }
    const btn = els.form.querySelector('button[type="submit"]');
    btn.disabled = true; btn.textContent = "Enviando…";
  });

  // ---- Estado inicial (inclui retorno com erros de validação)
  const initService = root.dataset.initialService;
  if (initService) {
    const radio = root.querySelector(`input[name="svc_choice"][value="${CSS.escape(initService)}"]`);
    if (radio) { radio.checked = true; radio.dispatchEvent(new Event("change")); }
  }
  const initStart = root.dataset.initialStart;
  const initStep = Number(root.dataset.initialStep || 1);
  if (initStart && initStep === 3 && state.serviceId) {
    state.slotIso = initStart; els.start.value = initStart;
    state.date = initStart.slice(0, 10); state.slotLabel = initStart.slice(11, 16);
    state.month = new Date(Number(state.date.slice(0, 4)), Number(state.date.slice(5, 7)) - 1, 1);
    els.toStep3.disabled = false;
    goTo(3);
  } else if (initStep >= 2 && state.serviceId) {
    els.start.value = "";
    goTo(2);
  }
})();
