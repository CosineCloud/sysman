'use strict';
(() => {
  const $ = selector => document.querySelector(selector);
  let mode = 'login', csrfToken = '', configured = false, busy = false;
  const form = $('#authForm');
  try { if (localStorage.getItem('sysman-theme') === 'dark') document.body.classList.add('dark'); } catch (_) {}
  function updateThemeButton() { $('#theme').textContent = document.body.classList.contains('dark') ? '☀' : '☾'; }
  updateThemeButton();
  $('#theme').onclick = () => {
    document.body.classList.toggle('dark');
    try { localStorage.setItem('sysman-theme', document.body.classList.contains('dark') ? 'dark' : 'light'); } catch (_) {}
    updateThemeButton();
  };
  function setMode(next) {
    if (busy) return;
    mode = next;
    document.querySelectorAll('[data-mode]').forEach(button => {
      const selected = button.dataset.mode === mode;
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    $('#authPanel').setAttribute('aria-labelledby', mode === 'login' ? 'signInTab' : 'linkTab');
    $('#loginTitle').textContent = mode === 'login' ? 'Welcome back.' : 'Connect your account.';
    $('#modeDescription').textContent = mode === 'login' ? 'Sign in to your agent workspace.' : 'Link your existing OuthMan account to SysMan.';
    $('#submitAuth').replaceChildren(document.createTextNode(mode === 'login' ? 'Sign in to SysMan →' : 'Link OuthMan account →'));
    $('#linkHelp').textContent = mode === 'login' ? 'First visit? Link your approved OuthMan account, then sign in with a new code.' : 'Your account must already exist and be approved in OuthMan. Linking does not sign you in.';
    $('#password').value = '';
    $('#code').value = '';
    $('#authError').hidden = true;
  }
  document.querySelectorAll('[data-mode]').forEach(button => {
    button.onclick = () => { $('#success').hidden = true; setMode(button.dataset.mode); };
    button.onkeydown = event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || busy) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 'login' : event.key === 'End' ? 'enroll' : mode === 'login' ? 'enroll' : 'login';
      $('#success').hidden = true;
      setMode(next);
      document.querySelector(`[data-mode="${next}"]`).focus();
    };
  });
  function showError(message) { $('#authError').textContent = message; $('#authError').hidden = false; }
  async function session() {
    const response = await fetch('/api/auth/session', { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw Error('Unable to check the sign-in service. Refresh to try again.');
    const data = await response.json();
    if (data.authenticated === true) { location.replace('/'); return false; }
    if (data.setup_required === true) { location.replace('/setup'); return false; }
    csrfToken = typeof data.csrf_token === 'string' ? data.csrf_token : '';
    configured = data.configured === true && Boolean(csrfToken);
    $('#setupNotice').hidden = data.configured === true;
    if (data.configured !== true) $('#setupNotice').textContent = 'Sign-in is unavailable. Ask the SysMan operator to check the saved OuthMan configuration and matching account database/encryption key.';
    $('#authFields').disabled = !configured;
    $('#connectionState').textContent = configured ? 'Use a new OuthMan code for each sign-in attempt.' : 'Waiting for OuthMan configuration.';
    return true;
  }
  form.onsubmit = async event => {
    event.preventDefault();
    if (busy || !configured || !form.reportValidity()) return;
    const action = mode;
    let linked = false;
    if (!$('#username').value.trim()) { showError('Enter your OuthMan username or email.'); $('#username').focus(); return; }
    const values = { username: $('#username').value.trim(), password: $('#password').value, code: $('#code').value };
    busy = true;
    $('#authFields').disabled = true;
    document.querySelectorAll('[data-mode]').forEach(button => button.disabled = true);
    $('#authError').hidden = true;
    $('#success').hidden = true;
    $('#connectionState').textContent = action === 'enroll' ? 'Linking your OuthMan account…' : 'Verifying your sign-in…';
    try {
      const response = await fetch(`/api/auth/${action}`, { method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify(values) });
      const result = await response.json();
      if (!response.ok) throw Error(result.error || 'Authentication failed. Generate a fresh OuthMan code and try again.');
      if (action === 'enroll') {
        if (result.linked !== true) throw Error('Account linking was not confirmed. Contact your SysMan operator before retrying.');
        busy = false;
        setMode('login');
        $('#success').textContent = 'Account linked. Generate a new six-digit code in OuthMan, then sign in below.';
        $('#success').hidden = false;
        linked = true;
      } else {
        if (result.authenticated !== true) throw Error('Sign-in was not confirmed. Generate a fresh code and try again.');
        location.replace('/');
      }
    } catch (error) {
      showError(error.message || 'Unable to reach the sign-in service. Try again with a fresh code.');
      // Refresh only the browser session/CSRF state; never retry credentials or a consumed code.
      try { await session(); } catch (_) { configured = false; }
    } finally {
      values.password = ''; values.code = '';
      $('#password').value = ''; $('#code').value = '';
      busy = false;
      $('#authFields').disabled = !configured;
      document.querySelectorAll('[data-mode]').forEach(button => button.disabled = false);
      $('#connectionState').textContent = configured ? 'Use a new OuthMan code for each sign-in attempt.' : 'Unable to sign in until OuthMan is configured and reachable.';
      if (linked && configured) $('#password').focus();
    }
  };
  if (new URLSearchParams(location.search).get('setup') === 'complete') { $('#success').textContent = 'Configuration saved. Select Link account to verify your OuthMan access, then sign in with a fresh code.'; $('#success').hidden = false; setMode('enroll'); }
  session().catch(error => { configured = false; $('#authFields').disabled = true; $('#connectionState').textContent = 'Sign-in service unavailable.'; showError(error.message); });
})();
