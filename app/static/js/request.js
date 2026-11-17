(() => {
  const form = document.getElementById('requestForm');
  if (!form) return;
  const button = form.querySelector('button[type="submit"]');
  const status = document.getElementById('requestStatus');
  let busy = false;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    button.disabled = true;
    status.textContent = 'Salvando seus dados…';
    try {
      const response = await fetch(form.action, { method: 'POST', body: new FormData(form), headers: { Accept: 'application/json' } });
      if (!response.ok) {
        if (response.status === 422) { const data = await response.json(); status.textContent = Object.values(data.errors).flat().join(' '); button.disabled = false; busy = false; return; }
        throw new Error('save failed');
      }
      const data = await response.json();
      status.textContent = 'Dados salvos. Abrindo o WhatsApp…';
      if (data.whatsapp) {
        status.replaceChildren(document.createTextNode('Dados salvos. '));
        const receipt = document.createElement('a'); receipt.href = data.next; receipt.textContent = 'Ver sua solicitação'; status.appendChild(receipt);
      }
      window.location.assign(data.whatsapp || data.next);
    } catch (error) {
      status.textContent = 'Não foi possível confirmar o envio. Tente novamente; seus dados não serão duplicados.';
      button.disabled = false; busy = false;
    }
  });
})();
