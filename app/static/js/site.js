(function () {
  "use strict";
  document.documentElement.classList.remove("no-js");

  const header = document.querySelector(".site-header");
  if (header) {
    const onScroll = () => header.classList.toggle("scrolled", window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
  }

  const items = document.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window && items.length) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("is-visible"); io.unobserve(e.target); } });
    }, { rootMargin: "0px 0px -8% 0px" });
    items.forEach((el) => io.observe(el));
  } else {
    items.forEach((el) => el.classList.add("is-visible"));
  }

  // Confirmação para ações destrutivas no painel
  document.addEventListener("submit", (ev) => {
    const msg = ev.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) ev.preventDefault();
  });

  // Copiar para a área de transferência
  document.querySelectorAll("[data-copy]").forEach((b) => {
    b.addEventListener("click", () => {
      const target = document.querySelector(b.dataset.copy);
      if (target && navigator.clipboard) {
        navigator.clipboard.writeText(target.value || target.textContent.trim()).then(() => { b.textContent = "Copiado"; });
      }
    });
  });
})();
