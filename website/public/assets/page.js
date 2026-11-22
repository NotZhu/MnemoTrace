  // scroll reveal
  const io=new IntersectionObserver((es)=>es.forEach(e=>{if(e.isIntersecting){e.target.classList.add('in');io.unobserve(e.target);}}),{threshold:.16});
  document.querySelectorAll('.rv:not(.in)').forEach(el=>io.observe(el));
  // The HTTP demo can use the legacy copy path when the Clipboard API is unavailable.
  document.querySelectorAll('[data-open-example]').forEach(link => {
    link.addEventListener('click', () => {
      const example = document.getElementById('api-example');
      if (example) example.open = true;
    });
  });
  document.querySelectorAll('[data-copy-code]').forEach(button => {
    button.addEventListener('click', async () => {
      const snippet = document.getElementById(button.dataset.copyCode);
      if (!snippet) return;
      const status = button.parentElement.querySelector('.copy-status');
      const text = snippet.textContent;
      let copied = false;
      button.disabled = true;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
          copied = true;
        }
      } catch (_) {}
      if (!copied) {
        const field = document.createElement('textarea');
        const focused = document.activeElement;
        field.value = text;
        field.setAttribute('aria-hidden', 'true');
        field.tabIndex = -1;
        field.style.cssText = 'position:fixed;top:0;left:-9999px;opacity:0';
        document.body.appendChild(field);
        try {
          field.focus({ preventScroll: true });
          field.select();
          copied = document.execCommand('copy');
        } catch (_) {
          copied = false;
        } finally {
          field.remove();
          if (focused && typeof focused.focus === 'function') focused.focus({ preventScroll: true });
        }
      }
      button.disabled = false;
      button.textContent = copied ? '已复制' : '复制';
      if (copied) {
        if (status) status.textContent = '调用示例已复制。';
        window.clearTimeout(button._copyResetTimer);
        button._copyResetTimer = window.setTimeout(() => { button.textContent = '复制'; }, 2000);
      } else {
        const example = snippet.closest('details');
        if (example) example.open = true;
        snippet.focus();
        const selection = window.getSelection();
        if (selection) {
          const range = document.createRange();
          range.selectNodeContents(snippet);
          selection.removeAllRanges();
          selection.addRange(range);
        }
        if (status) status.textContent = '自动复制未成功，请在展开的示例中手动复制。';
      }
    });
  });
  // Close the mobile section menu after a jump, an outside click, or Escape.
  document.querySelectorAll('.mnav-panel a').forEach(link => link.addEventListener('click', () => { link.closest('details').open = false; }));
  document.addEventListener('click', e => document.querySelectorAll('.mnav[open]').forEach(menu => { if (!menu.contains(e.target)) menu.open = false; }));
  document.addEventListener('keydown', e => { if (e.key === 'Escape') document.querySelectorAll('.mnav[open]').forEach(menu => { menu.open = false; }); });
