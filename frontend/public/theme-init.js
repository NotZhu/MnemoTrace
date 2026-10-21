// Synchronous head script: restore appearance before styles and React can paint.
(() => {
  let theme = 'dark'
  try {
    const saved = JSON.parse(localStorage.getItem('mnemotrace.theme') || 'null')
    if (saved?.state?.theme === 'light') theme = 'light'
  } catch { /* Restricted or damaged storage uses the same dark default as the store. */ }
  document.documentElement.dataset.theme = theme
  document.documentElement.style.colorScheme = theme
})()
