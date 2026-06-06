// Bootstrap loader for the full BEACON web UI script.
// The previous app.js was out of sync with the current backend routes.
(async function loadBeaconUi() {
  try {
    const res = await fetch('/static/app.js.bak_step4', { cache: 'no-store' });
    if (!res.ok) {
      throw new Error('HTTP ' + res.status);
    }
    const source = await res.text();
    // Execute in global scope so inline onclick handlers resolve correctly.
    (0, eval)(source);
  } catch (err) {
    console.error('[BEACON] Failed to load UI script:', err);
    alert('Failed to load UI script. Please hard refresh and try again.');
  }
})();
