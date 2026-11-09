'use strict';
(async () => {
  const status = document.querySelector('#authBootstrap');
  function clearWorkspace() {
    document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
    document.querySelector('#content')?.replaceChildren();
    document.body.classList.add('auth-pending');
    if (status) { status.hidden = false; status.textContent = 'Returning to sign in…'; }
    window.SYSMAN_AUTH = null;
  }
  window.addEventListener('pageshow', event => { if (event.persisted) { clearWorkspace(); location.reload(); } });
  window.sysmanSessionExpired = () => { clearWorkspace(); location.replace('/login'); };
  try {
    const response = await fetch('/api/auth/session', { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw Error('Unable to check your session.');
    const session = await response.json();
    if (session.authenticated !== true || typeof session.csrf_token !== 'string' || !session.csrf_token) return window.sysmanSessionExpired();
    window.SYSMAN_AUTH = { csrf_token: session.csrf_token, user: session.user };
    document.querySelector('#sessionUsername').textContent = session.user?.username || 'Signed in';
    document.querySelector('#logout').onclick = async event => {
      const button = event.currentTarget;
      if (button.disabled) return;
      button.disabled = true;
      try {
        const result = await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window.SYSMAN_AUTH.csrf_token }, body: '{}' });
        if (!result.ok && result.status !== 401) throw Error('Sign out failed. Please try again.');
        clearWorkspace();
        location.replace('/login');
      } catch (error) {
        const alert = document.querySelector('#error');
        alert.textContent = error.message || 'Unable to sign out. Please try again.';
        alert.hidden = false;
        button.disabled = false;
      }
    };
    const script = document.createElement('script');
    script.src = '/app.js';
    script.onload = () => { document.body.classList.remove('auth-pending'); if (status) status.hidden = true; };
    script.onerror = () => { if (status) status.textContent = 'Unable to load your workspace. Refresh to try again.'; };
    document.body.append(script);
  } catch (_) {
    if (status) {
      status.textContent = 'Unable to check your session. ';
      const link = document.createElement('a'); link.href = '/login'; link.textContent = 'Return to sign in'; status.append(link);
    }
  }
})();
