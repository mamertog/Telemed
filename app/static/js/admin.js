(function () {
  "use strict";
  const side = document.getElementById("adminSide");
  const toggle = document.getElementById("sideToggle");
  if (side && toggle) {
    toggle.addEventListener("click", () => {
      const open = side.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") side.classList.remove("open"); });
  }

  // Sugestão de horários livres nos formulários de agendamento/reagendamento
  const box = document.getElementById("slotSuggestions");
  if (box) {
    const dateInput = document.getElementById(box.dataset.dateField);
    const timeInput = document.getElementById(box.dataset.timeField);
    const serviceInput = box.dataset.serviceField ? document.getElementById(box.dataset.serviceField) : null;
    const load = async () => {
      const servico = serviceInput ? serviceInput.value : box.dataset.service;
      if (!dateInput.value || !servico) { box.innerHTML = ""; return; }
      const params = new URLSearchParams({ servico, data: dateInput.value });
      if (box.dataset.exclude) params.set("excluir", box.dataset.exclude);
      try {
        const r = await fetch(`${box.dataset.url}?${params}`, { headers: { Accept: "application/json" } });
        const data = await r.json();
        box.innerHTML = "";
        const label = document.createElement("p");
        label.className = "small text-muted-2 mb-2";
        label.textContent = data.horarios.length ? "Horários livres dentro da disponibilidade:" : "Nenhum horário livre na disponibilidade configurada para esta data.";
        box.appendChild(label);
        data.horarios.forEach((h) => {
          const b = document.createElement("button");
          b.type = "button"; b.className = "btn btn-sm btn-sage me-1 mb-1"; b.textContent = h;
          b.addEventListener("click", () => { timeInput.value = h; timeInput.focus(); });
          box.appendChild(b);
        });
      } catch (e) { box.innerHTML = ""; }
    };
    dateInput.addEventListener("change", load);
    if (serviceInput) serviceInput.addEventListener("change", load);
    load();
  }
})();
