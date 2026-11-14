(() => {
  const input = document.querySelector('input[type="file"]');
  const preview = document.getElementById('photoPreview');
  if (!input || !preview) return;
  let url;
  input.addEventListener('change', () => {
    if (url) URL.revokeObjectURL(url);
    preview.hidden = true;
    const file = input.files[0];
    const status = document.getElementById('photoStatus');
    status.textContent = '';
    if (!file) return;
    if (file.size > 4 * 1024 * 1024) { status.textContent = 'Escolha uma foto de até 4 MB.'; input.value = ''; return; }
    url = URL.createObjectURL(file);
    preview.src = url;
    preview.hidden = false;
  });
})();
