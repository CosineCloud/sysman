'use strict';
(() => {
  const $ = selector => document.querySelector(selector);
  let csrf = '', ready = false, busy = false;
  const form = $('#setupForm');
  try { if (localStorage.getItem('sysman-theme') === 'dark') document.body.classList.add('dark'); } catch (_) {}
  const themeButton = $('#theme');
  function themeLabel() { themeButton.textContent = document.body.classList.contains('dark') ? '☀' : '☾'; }
  themeLabel();
  themeButton.onclick = () => { document.body.classList.toggle('dark'); themeLabel(); try { localStorage.setItem('sysman-theme',document.body.classList.contains('dark')?'dark':'light'); } catch (_) {} };
  function error(message) { $('#setupError').textContent = message; $('#setupError').hidden = false; }
  async function status() {
    const response = await fetch('/api/auth/setup', {credentials:'same-origin',cache:'no-store'});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || 'Unable to check setup. Refresh to try again.');
    csrf = typeof data.csrf_token === 'string' ? data.csrf_token : '';
    ready = data.setup_required === true && Boolean(csrf);
    $('#setupFields').disabled = !ready;
    if (!ready) {
      form.hidden = true;
      $('#setupUnavailable').textContent = data.configured === true ? 'OuthMan is already configured. Continue to sign in with your linked account.' : 'First-time setup is unavailable. Ask the SysMan operator to check the existing configuration and restore its matching account database and encryption key.';
      $('#setupUnavailable').hidden = false;
      $('#continueLogin').hidden = false;
      $('#setupState').textContent = data.configured === true ? 'Configuration is complete.' : 'Operator recovery is required.';
      return;
    }
    const defaults = data.defaults || {};
    $('#baseUrl').value = typeof defaults.base_url === 'string' ? defaults.base_url : '';
    $('#appId').value = typeof defaults.app_id === 'string' ? defaults.app_id : '';
    $('#allowedUsers').value = Array.isArray(defaults.allowed_users) ? defaults.allowed_users.join('\n') : '';
    $('#allowHttp').checked = defaults.allow_insecure_http === true;
    $('#setupState').textContent = 'Ready for your OuthMan connection details.';
  }
  form.onsubmit = async event => {
    event.preventDefault();
    if (!ready || busy || !form.reportValidity()) return;
    const allowedUsers = [...new Set($('#allowedUsers').value.split(/[\n,]+/).map(v=>v.trim()).filter(Boolean))];
    if (!allowedUsers.length || allowedUsers.length > 100) { error('Enter between 1 and 100 allowed operator usernames.'); $('#allowedUsers').focus(); return; }
    const values = {setup_token:$('#setupToken').value,base_url:$('#baseUrl').value.trim(),app_id:$('#appId').value.trim(),app_secret:$('#appSecret').value,allowed_users:allowedUsers,allow_insecure_http:$('#allowHttp').checked};
    busy = true; $('#setupFields').disabled = true; $('#setupError').hidden = true; $('#setupState').textContent = 'Saving encrypted configuration…';
    try {
      const response = await fetch('/api/auth/setup',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(values)});
      const result = await response.json();
      if (!response.ok) throw Error(result.error || 'Unable to save the configuration. Check your entries and try again.');
      $('#setupToken').value = ''; $('#appSecret').value = '';
      $('#setupState').textContent = 'Saved. Link your account to verify access.';
      location.replace('/login?setup=complete');
    } catch (failure) {
      error(failure.message || 'Unable to save the configuration. Try again.');
      $('#setupState').textContent = 'Configuration was not confirmed. Check the message above.';
      // Never retry a secret-bearing submission automatically.
    } finally {
      values.setup_token = ''; values.app_secret = '';
      busy = false; $('#setupFields').disabled = !ready;
    }
  };
  status().catch(failure=>{ready=false;$('#setupFields').disabled=true;$('#setupState').textContent='Setup service unavailable.';error(failure.message || 'Unable to load setup. Refresh to try again.');});
})();
