/**
 * VeroRun — Dynamic Login (plugin-driven)
 * Fetches /auth/login-methods and renders login forms dynamically.
 * All user-facing strings use __() for i18n.
 */
var cd = 0, cdTimer = null, activeTab = '', capToken = '', capSolved = false;
var loginMethods = [], registerMethods = [];

// ── SVG Icons ──
var ICON_CHECK = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M20 6L9 17l-5-5"/></svg>';
var ICON_CROSS = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M18 6L6 18M6 6l12 12"/></svg>';
var ICON_EYE = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
var ICON_EYE_OFF = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

var OAUTH_ICONS = {
  douyin: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none"><path d="M19.59 6.69a4.83 4.83 0 0 1-3.77-4.69h-2.87v12.07a2.87 2.87 0 1 1-1.97-2.75v-2.87a5.73 5.73 0 1 0 5.73 5.73V10.1a4.83 4.83 0 0 0 2.88.99V8.16c-.12 0-.23-.01-.34-.03a4.85 4.85 0 0 1-3.48-1.68l.82 2.76z" fill="currentColor"/></svg>',
  wechat: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none"><path d="M8.5 11a1 1 0 1 1 0-2 1 1 0 0 1 0 2zm4 0a1 1 0 1 1 0-2 1 1 0 0 1 0 2z"/><path d="M17 3H7a5 5 0 0 0-5 5v4a5 5 0 0 0 5 5h1l2 3 3-3h4a5 5 0 0 0 5-5V8a5 5 0 0 0-5-5z" stroke="currentColor" stroke-width="1.5" fill="none"/></svg>',
  alipay: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.26 6.5h-4.5a.5.5 0 0 0-.5.5v.5h2.75c.41 0 .75.34.75.75s-.34.75-.75.75h-2.75V12h2.25c.41 0 .75.34.75.75s-.34.75-.75.75h-2.25v2.5c0 .41-.34.75-.75.75s-.75-.34-.75-.75v-2.5H9.5c-.41 0-.75-.34-.75-.75s.34-.75.75-.75h1.75v-1.5H8.74a.5.5 0 0 1-.5-.5v-1a.5.5 0 0 1 .5-.5h2.75V7.75c0-.41.34-.75.75-.75h3.77c.41 0 .75.34.75.75s-.34.75-.75.75z" fill="currentColor"/></svg>',
  google: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none"><path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4"/><path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/><path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/><path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/></svg>',
  github: '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/></svg>',
  facebook: '<svg width="18" height="18" viewBox="0 0 24 24" fill="#1877F2"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z"/></svg>',
  telegram: '<svg width="18" height="18" viewBox="0 0 24 24" fill="#0088cc"><path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/></svg>',
};

// ── CAPTCHA ──
document.addEventListener('captcha-success', function(e) {
  capToken = e.detail.token;
  capSolved = true;
});

// ── Init ──
document.addEventListener('DOMContentLoaded', function() {
  fetchLoginMethods();
});

function fetchLoginMethods() {
  fetch('/auth/login-methods')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (!d.success || !d.data) {
        // Fallback: show password form only (same as catch branch)
        loginMethods = [{
          type: 'password', name: 'Password Login', icon: 'key',
          fields: [
            {name: 'account', type: 'text', placeholder: 'Username / Email / Phone', autocomplete: 'username'},
            {name: 'password', type: 'password', placeholder: 'Enter password', autocomplete: 'current-password'}
          ]
        }];
        registerMethods = [];
        buildUI();
        return;
      }
      loginMethods = d.data.methods || [];
      registerMethods = d.data.register_methods || [];
      buildUI();
    })
    .catch(function() {
      // Fallback: show password form only
      loginMethods = [{
        type: 'password', name: 'Password Login', icon: 'key',
        tab_id: 'tabPwd', priority: 10, submit_url: '/user/password/login',
        submit_text: 'Log In', has_forgot_password: true,
        fields: [
          {name: 'account', type: 'text', placeholder: 'Username / Email / Phone', autocomplete: 'username'},
          {name: 'password', type: 'password', placeholder: 'Enter password', autocomplete: 'current-password'}
        ]
      }];
      registerMethods = [];
      buildUI();
    });
}

// ── Build Dynamic UI ──
function buildUI() {
  var primary = loginMethods.filter(function(m) { return !m.is_third_party; });
  var thirdParty = loginMethods.filter(function(m) { return m.is_third_party; });

  // ── Tabs (only if > 1 primary method) ──
  var tabsEl = document.getElementById('loginTabs');
  if (primary.length > 1) {
    var tabsHtml = '';
    primary.forEach(function(m, i) {
      tabsHtml += '<button type="button" class="login-tab' + (i === 0 ? ' active' : '') + '" data-tab="' + m.tab_id + '">' + __(m.name) + '</button>';
    });
    tabsEl.innerHTML = tabsHtml;
    // Bind tab clicks
    tabsEl.querySelectorAll('.login-tab').forEach(function(btn) {
      btn.addEventListener('click', function() {
        switchTab(this.dataset.tab);
      });
    });
  } else {
    tabsEl.innerHTML = '';
  }
  tabsEl.style.display = primary.length > 1 ? '' : 'none';

  // ── Forms ──
  var formsEl = document.getElementById('loginForms');
  var formsHtml = '';
  primary.forEach(function(m, i) {
    formsHtml += buildFormHTML(m, i === 0);
  });
  formsEl.innerHTML = formsHtml;

  // Set active tab to first primary method
  if (primary.length > 0) {
    activeTab = primary[0].tab_id;
  }

  // Bind form-specific events
  primary.forEach(function(m) {
    bindFormEvents(m);
  });

  // ── OAuth (third-party) ──
  var oauthDivider = document.getElementById('oauthDivider');
  var oauthProviders = document.getElementById('oauthProviders');
  oauthProviders.innerHTML = '';
  if (thirdParty.length > 0) {
    thirdParty.forEach(function(p) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'lf-btn-social';
      btn.innerHTML = (OAUTH_ICONS[p.icon] || '') + ' ' + p.name;
      btn.addEventListener('click', function() {
        window.location.href = p.login_url + '?redirect=' + encodeURIComponent(window.location.origin + '/');
      });
      oauthProviders.appendChild(btn);
    });
    oauthDivider.style.display = '';
  } else {
    oauthDivider.style.display = 'none';
  }

  // ── Register link ──
  var regLink = document.getElementById('registerLink');
  if (registerMethods.length > 0) {
    regLink.style.display = '';
  } else {
    regLink.style.display = 'none';
  }

  // ── Focus first input ──
  setTimeout(function() {
    var firstForm = document.getElementById('form-' + activeTab);
    if (firstForm) {
      var firstInput = firstForm.querySelector('input');
      if (firstInput) firstInput.focus();
    }
  }, 200);
}

// ── Build form HTML for a method ──
function buildFormHTML(method, isActive) {
  var fid = 'form-' + method.tab_id;
  var html = '<div id="' + fid + '" class="lf-form-wrap"' + (isActive ? '' : ' style="display:none"') + '>';

  method.fields.forEach(function(field) {
    html += '<div class="lf">';
    html += '<label class="lf-label" for="' + fid + '-' + field.name + '">' + __(field.placeholder) + '</label>';

    if (field.type === 'password') {
      // Password field with eye toggle
      html += '<div class="lf-pwd-row">';
      html += '<input class="lf-in" type="password" id="' + fid + '-' + field.name + '"';
      html += ' placeholder="' + __(field.placeholder) + '"';
      if (field.autocomplete) html += ' autocomplete="' + field.autocomplete + '"';
      html += '>';
      html += '<button type="button" class="lf-pwd-eye" data-target="' + fid + '-' + field.name + '">' + ICON_EYE + '</button>';
      html += '</div>';
    } else if (method.type === 'sms' && field.name === 'phone') {
      // Phone field with send-code button
      html += '<div class="lf-row">';
      html += '<div class="lf-wrap">';
      html += '<span class="lf-icon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="2" width="14" height="20" rx="2"/><line x1="12" y1="18" x2="12" y2="18"/></svg></span>';
      html += '<input class="lf-in" type="' + (field.type || 'tel') + '" id="' + fid + '-' + field.name + '"';
      html += ' placeholder="' + __(field.placeholder) + '"';
      if (field.maxlength) html += ' maxlength="' + field.maxlength + '"';
      if (field.autocomplete) html += ' autocomplete="' + field.autocomplete + '"';
      html += '>';
      html += '</div>';
      html += '<button type="button" class="lf-btn-send" id="' + fid + '-sendBtn">' + __('Get Code') + '</button>';
      html += '</div>';
    } else if (method.type === 'sms' && field.name === 'code') {
      // Code field
      html += '<div class="lf-wrap">';
      html += '<span class="lf-icon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></span>';
      html += '<input class="lf-in" type="' + (field.type || 'text') + '" id="' + fid + '-' + field.name + '"';
      html += ' placeholder="' + __(field.placeholder) + '"';
      if (field.maxlength) html += ' maxlength="' + field.maxlength + '"';
      if (field.inputmode) html += ' inputmode="' + field.inputmode + '"';
      if (field.autocomplete) html += ' autocomplete="' + field.autocomplete + '"';
      html += '>';
      html += '</div>';
    } else {
      // Regular text field with icon
      html += '<div class="lf-wrap">';
      html += '<span class="lf-icon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg></span>';
      html += '<input class="lf-in" type="' + (field.type || 'text') + '" id="' + fid + '-' + field.name + '"';
      html += ' placeholder="' + __(field.placeholder) + '"';
      if (field.autocomplete) html += ' autocomplete="' + field.autocomplete + '"';
      html += '>';
      html += '</div>';
    }

    html += '<div class="lf-msg" id="' + fid + '-' + field.name + 'Msg"></div>';
    html += '</div>';
  });

  // Agreement text
  html += '<div class="lf-agreement">';
  html += __('By logging in you agree to') + ' \u300a<a href="/terms" target="_blank">' + __('Service Agreement') + '</a>\u300b' + __('and') + '\u300a<a href="/privacy" target="_blank">' + __('Privacy Policy') + '</a>\u300b';
  html += '</div>';

  // Submit button
  html += '<button type="button" class="lf-btn-primary" id="' + fid + '-submitBtn">';
  html += '<span class="lf-btn-text">' + __(method.submit_text) + '</span>';
  html += '<div class="lf-spinner"></div>';
  html += '</button>';

  // Forgot password link
  if (method.has_forgot_password) {
    html += '<div class="lf-toggle"><a href="/reset-password">' + __('Forgot password?') + '</a></div>';
  }

  // Switch-to-other tab link (for SMS -> password)
  if (method.type === 'sms') {
    html += '<div class="lf-toggle"><button type="button" class="lf-switch-tab" data-target="tabPwd">' + __('Use password to login') + '</button></div>';
  }

  html += '</div>';
  return html;
}

// ── Bind form events ──
function bindFormEvents(method) {
  var fid = 'form-' + method.tab_id;

  // Password eye toggle
  var eyeBtn = document.querySelector('#' + fid + ' .lf-pwd-eye');
  if (eyeBtn) {
    eyeBtn.addEventListener('click', function() {
      var targetId = this.dataset.target;
      var el = document.getElementById(targetId);
      if (el.type === 'password') {
        el.type = 'text';
        this.innerHTML = ICON_EYE_OFF;
      } else {
        el.type = 'password';
        this.innerHTML = ICON_EYE;
      }
    });
  }

  // Phone formatting (SMS)
  if (method.type === 'sms') {
    var phoneEl = document.getElementById(fid + '-phone');
    if (phoneEl) {
      phoneEl.addEventListener('input', function() {
        var v = this.value.replace(/\D/g, '').slice(0, 11);
        if (v.length > 7) v = v.slice(0, 3) + ' ' + v.slice(3, 7) + ' ' + v.slice(7);
        else if (v.length > 3) v = v.slice(0, 3) + ' ' + v.slice(3);
        this.value = v;
      });
      phoneEl.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') sendCode(method);
      });
    }

    var codeEl = document.getElementById(fid + '-code');
    if (codeEl) {
      codeEl.addEventListener('input', function() {
        this.value = this.value.replace(/\D/g, '');
        if (this.value.length === 6 && rawPhone(method) && rawPhone(method).length === 11) {
          smsLogin(method);
        }
      });
      codeEl.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') smsLogin(method);
      });
    }

    var sendBtn = document.getElementById(fid + '-sendBtn');
    if (sendBtn) {
      sendBtn.addEventListener('click', function() { sendCode(method); });
    }
  }

  // Password login: Enter key
  if (method.type === 'password') {
    var accountEl = document.getElementById(fid + '-account');
    var passwordEl = document.getElementById(fid + '-password');
    if (accountEl) {
      accountEl.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && passwordEl) passwordEl.focus();
      });
    }
    if (passwordEl) {
      passwordEl.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') pwdLogin(method);
      });
    }
  }

  // Submit button
  var submitBtn = document.getElementById(fid + '-submitBtn');
  if (submitBtn) {
    submitBtn.addEventListener('click', function() {
      if (method.type === 'sms') {
        smsLogin(method);
      } else if (method.type === 'password') {
        pwdLogin(method);
      }
    });
  }

  // Switch tab links
  var switchLinks = document.querySelectorAll('#' + fid + ' .lf-switch-tab');
  switchLinks.forEach(function(link) {
    link.addEventListener('click', function() {
      switchTab(this.dataset.target);
    });
  });
}

// ── Tab switching ──
function switchTab(tabId) {
  activeTab = tabId;
  // Update tab buttons
  document.querySelectorAll('#loginTabs .login-tab').forEach(function(btn) {
    btn.classList.toggle('active', btn.dataset.tab === tabId);
  });
  // Show/hide forms
  document.querySelectorAll('#loginForms .lf-form-wrap').forEach(function(el) {
    el.style.display = el.id === 'form-' + tabId ? '' : 'none';
  });
  // Focus first input
  setTimeout(function() {
    var form = document.getElementById('form-' + tabId);
    if (form) {
      var firstInput = form.querySelector('input');
      if (firstInput) firstInput.focus();
    }
  }, 100);
}

// ── SMS: Send Code ──
function rawPhone(method) {
  var el = document.getElementById('form-' + method.tab_id + '-phone');
  return el ? el.value.replace(/\D/g, '') : '';
}

async function sendCode(method) {
  var p = rawPhone(method);
  if (p.length < 11) { showMsg('form-' + method.tab_id + '-phoneMsg', __('Please enter 11-digit phone number'), 'error'); return; }
  hideMsg('form-' + method.tab_id + '-phoneMsg');
  var btn = document.getElementById('form-' + method.tab_id + '-sendBtn');
  btn.disabled = true;
  btn.classList.add('countdown');
  try {
    var r = await fetch(method.send_code_url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: p, purpose: 'login', captcha_id: capToken })
    });
    var d = await r.json();
    if (d.success) {
      localStorage.setItem('tm_phone', p);
      toast(__('Verification code sent'), 'success');
      startCd(60, method);
      capSolved = false;
      capToken = '';
      hideCaptcha();
      setTimeout(function() {
        var codeEl = document.getElementById('form-' + method.tab_id + '-code');
        if (codeEl) codeEl.focus();
      }, 100);
    } else {
      if (d.error && d.error.indexOf('CAPTCHA') !== -1) {
        showCaptcha();
        capSolved = false;
        capToken = '';
      }
      showMsg('form-' + method.tab_id + '-phoneMsg', d.error || __('Send failed'), 'error');
      btn.disabled = false;
      btn.classList.remove('countdown');
    }
  } catch (e) {
    showMsg('form-' + method.tab_id + '-phoneMsg', __('Network error, please retry'), 'error');
    btn.disabled = false;
    btn.classList.remove('countdown');
  }
}

function startCd(s, method) {
  cd = s;
  var btn = document.getElementById('form-' + method.tab_id + '-sendBtn');
  function tick() {
    cd--;
    btn.textContent = cd + 's';
    if (cd <= 0) {
      clearInterval(cdTimer);
      btn.textContent = __('Resend');
      btn.disabled = false;
      btn.classList.remove('countdown');
    }
  }
  tick();
  clearInterval(cdTimer);
  cdTimer = setInterval(tick, 1000);
}

// ── SMS: Login ──
async function smsLogin(method) {
  var p = rawPhone(method);
  var codeEl = document.getElementById('form-' + method.tab_id + '-code');
  var c = codeEl ? codeEl.value.replace(/\D/g, '') : '';
  if (!p || p.length < 11) { showMsg('form-' + method.tab_id + '-codeMsg', __('Please enter 11-digit phone number'), 'error'); return; }
  if (!c || c.length < 6) { showMsg('form-' + method.tab_id + '-codeMsg', __('Please enter verification code'), 'error'); return; }
  hideMsg('form-' + method.tab_id + '-codeMsg');
  var btn = document.getElementById('form-' + method.tab_id + '-submitBtn');
  btn.disabled = true;
  btn.classList.add('loading');
  try {
    var r = await fetch(method.submit_url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: p, code: c, captcha_id: capToken })
    });
    var d = await r.json();
    if (d.success) {
      localStorage.setItem('tm_token', d.data.token);
      localStorage.setItem('sso_token', d.data.token);
      localStorage.setItem('token', d.data.token);
      localStorage.setItem('tm_user', JSON.stringify(d.data.user));
      toast(__('Login successful, redirecting...'), 'success');
      setTimeout(function() {
        var rd = new URLSearchParams(location.search).get('redirect') || window.location.origin;
        window.location.href = rd + (rd.indexOf('?') !== -1 ? '&' : '?') + 'token=' + d.data.token;
      }, 600);
    } else {
      if (d.error && d.error.indexOf('CAPTCHA') !== -1) {
        showCaptcha();
        capSolved = false;
        capToken = '';
      }
      showMsg('form-' + method.tab_id + '-codeMsg', d.error || __('Login failed'), 'error');
      btn.disabled = false;
      btn.classList.remove('loading');
    }
  } catch (e) {
    showMsg('form-' + method.tab_id + '-codeMsg', __('Network error, please retry'), 'error');
    btn.disabled = false;
    btn.classList.remove('loading');
  }
}

// ── Password: Login ──
async function pwdLogin(method) {
  var accountEl = document.getElementById('form-' + method.tab_id + '-account');
  var passwordEl = document.getElementById('form-' + method.tab_id + '-password');
  var a = accountEl ? accountEl.value.trim() : '';
  var pw = passwordEl ? passwordEl.value : '';
  if (!a || !pw) { showMsg('form-' + method.tab_id + '-passwordMsg', __('Please enter account and password'), 'error'); return; }
  hideMsg('form-' + method.tab_id + '-passwordMsg');
  var btn = document.getElementById('form-' + method.tab_id + '-submitBtn');
  btn.disabled = true;
  btn.classList.add('loading');
  try {
    var r = await fetch(method.submit_url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: a, password: pw, captcha_id: capToken })
    });
    var d = await r.json();
    if (d.success) {
      localStorage.setItem('tm_token', d.data.token);
      localStorage.setItem('sso_token', d.data.token);
      localStorage.setItem('token', d.data.token);
      localStorage.setItem('tm_user', JSON.stringify(d.data.user));
      toast(__('Login successful, redirecting...'), 'success');
      setTimeout(function() {
        var rd = new URLSearchParams(location.search).get('redirect') || window.location.origin;
        window.location.href = rd + (rd.indexOf('?') !== -1 ? '&' : '?') + 'token=' + d.data.token;
      }, 600);
    } else {
      if (d.error && d.error.indexOf('CAPTCHA') !== -1) {
        showCaptcha();
        capSolved = false;
        capToken = '';
      }
      showMsg('form-' + method.tab_id + '-passwordMsg', d.error || __('Login failed'), 'error');
      btn.disabled = false;
      btn.classList.remove('loading');
    }
  } catch (e) {
    showMsg('form-' + method.tab_id + '-passwordMsg', __('Network error, please retry'), 'error');
    btn.disabled = false;
    btn.classList.remove('loading');
  }
}

// ── CAPTCHA ──
function showCaptcha() {
  var wrap = document.getElementById('captchaWrap');
  if (wrap && wrap.style.display === 'none') {
    wrap.style.display = '';
    if (window.PuzzleCaptchaBuild) {
      try { window.PuzzleCaptchaBuild(); } catch (e) {}
    }
  }
}
function hideCaptcha() {
  var wrap = document.getElementById('captchaWrap');
  if (wrap) wrap.style.display = 'none';
}

// ── UI Helpers ──
function showMsg(id, msg, type) {
  var el = document.getElementById(id);
  if (!el) return;
  el.className = 'lf-msg ' + type;
  el.innerHTML = (type === 'error' ? ICON_CROSS : ICON_CHECK);
  el.appendChild(document.createTextNode(' ' + msg));
}
function hideMsg(id) {
  var el = document.getElementById(id);
  if (el) el.className = 'lf-msg';
}

function toast(msg, type) {
  var wrap = document.getElementById('toastWrap');
  if (!wrap) return;
  var el = document.createElement('div');
  el.className = 'toast-el ' + type;
  el.innerHTML = (type === 'success' ? ICON_CHECK : ICON_CROSS);
  var textSpan = document.createElement('span');
  textSpan.textContent = msg;
  el.appendChild(textSpan);
  wrap.appendChild(el);
  setTimeout(function() {
    el.classList.add('leave');
    setTimeout(function() { el.remove(); }, 250);
  }, 3000);
}