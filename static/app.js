document.querySelectorAll('tr[data-href]').forEach(row => {
  row.addEventListener('click', event => {
    if (!event.target.closest('a,button,input,select') && !window.getSelection().toString()) {
      window.location.assign(row.dataset.href);
    }
  });
});
document.querySelectorAll('form').forEach(form => {
  form.addEventListener('submit', () => {
    // The request key provides server-side idempotency; this also prevents accidental double clicks.
    const submit = form.querySelector('button:not([name])');
    if (submit && form.checkValidity()) {
      submit.disabled = true;
      setTimeout(() => { submit.disabled = false; }, 5000);
    }
  });
});
document.querySelectorAll('button[data-draft-text]').forEach(button => {
  button.addEventListener('click', () => {
    const editor = document.querySelector('textarea[name="text"]');
    if (editor) {
      editor.value = button.dataset.draftText;
      editor.dispatchEvent(new Event('input', {bubbles: true}));
      editor.focus();
    }
  });
});
