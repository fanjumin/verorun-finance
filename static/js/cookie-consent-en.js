/**
 * Cookie Consent Banner — (International / English)
 * Self-contained GDPR-compliant consent management.
 *
 * Usage: include <script src="/static/js/cookie-consent-en.js"></script> before </body>.
 * No dependencies.
 *
 * Exposes: window.cookieConsent — same API as the Chinese version.
 */

;(function () {
  'use strict';

  var CONFIG = {
    cookieName: 'cookie_consent_prefs',
    expiryDays: 30,
    policyVersion: 1,
    bannerDelayMs: 300,
    categories: [
      {
        id: 'necessary',
        label: 'Necessary Cookies',
        desc: 'Login sessions, security checks, session management. These cookies are essential for the website to function and cannot be disabled.',
        required: true
      },
      {
        id: 'analytics',
        label: 'Analytics & Performance',
        desc: 'Help us understand how visitors use the website so we can improve the experience. All data is anonymized.',
        required: false
      },
      {
        id: 'marketing',
        label: 'Marketing Cookies',
        desc: 'Used to deliver relevant advertisements and measure ad effectiveness. May be shared with third-party advertisers.',
        required: false
      }
    ],
    privacyUrl: '/cookie-policy',
    bannerTitle: 'Cookie Consent',
    bannerText: 'This website uses cookies to ensure basic functionality, analyze traffic, and enhance your experience. For more details, please read our',
    privacyLinkText: 'Cookie Policy',
  };

  var _prefs = null;
  var _listeners = [];
  var _overlayEl = null;
  var _settingsOpen = false;
  var _floatBtnEl = null;

  function loadPrefs() {
    try {
      var raw = localStorage.getItem(CONFIG.cookieName);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object') return null;
      if (!parsed.accepted || !parsed.version) return null;
      if (parsed.version !== CONFIG.policyVersion) return null;
      var acceptedDate = new Date(parsed.accepted);
      var expiryDate = new Date(acceptedDate.getTime() + CONFIG.expiryDays * 86400000);
      if (new Date() > expiryDate) {
        localStorage.removeItem(CONFIG.cookieName);
        return null;
      }
      CONFIG.categories.forEach(function (cat) {
        if (!(cat.id in parsed)) parsed[cat.id] = cat.required;
      });
      return parsed;
    } catch (e) {
      return null;
    }
  }

  function savePrefs(prefs) {
    prefs.accepted = new Date().toISOString();
    prefs.version = CONFIG.policyVersion;
    try {
      localStorage.setItem(CONFIG.cookieName, JSON.stringify(prefs));
    } catch (e) {}
    _prefs = prefs;
    notifyListeners(prefs);
    try {
      var d = new Date();
      d.setTime(d.getTime() + CONFIG.expiryDays * 86400000);
      document.cookie = 'cookie_consent=' + encodeURIComponent(JSON.stringify({
        necessary: prefs.necessary,
        analytics: prefs.analytics,
        marketing: prefs.marketing,
        v: CONFIG.policyVersion
      })) + ';expires=' + d.toUTCString() + ';path=/;SameSite=Lax';
    } catch (e) {}
  }

  function notifyListeners(prefs) {
    _listeners.forEach(function (fn) {
      try { fn(prefs); } catch (e) {}
    });
  }

  window.__CC_LOADERS__ = window.__CC_LOADERS__ || {};

  function runLoaders(prefs) {
    Object.keys(window.__CC_LOADERS__).forEach(function (cat) {
      if (prefs[cat] && typeof window.__CC_LOADERS__[cat] === 'function') {
        try { window.__CC_LOADERS__[cat](); } catch (e) {}
      }
    });
  }

  function buildToggle(catId, checked, disabled) {
    return (
      '<label class="cc-toggle">' +
        '<input type="checkbox" ' +
          'data-cc-cat="' + catId + '" ' +
          (checked ? 'checked ' : '') +
          (disabled ? 'disabled ' : '') +
        '/>' +
        '<span class="cc-toggle-slider"></span>' +
      '</label>'
    );
  }

  function buildSettingsHTML(prefs) {
    var rows = '';
    CONFIG.categories.forEach(function (cat) {
      var checked = prefs ? !!prefs[cat.id] : cat.required;
      rows += (
        '<div class="cc-setting-row">' +
          '<div class="cc-setting-info">' +
            '<div class="cc-setting-label">' + cat.label + '</div>' +
            '<div class="cc-setting-desc">' + cat.desc + '</div>' +
            (cat.required ? '<div class="cc-setting-always">Always On</div>' : '') +
          '</div>' +
          buildToggle(cat.id, checked, cat.required) +
        '</div>'
      );
    });
    return rows;
  }

  function buildBannerHTML() {
    var prefs = _prefs;
    return (
      '<div class="cc-root">' +
        '<div class="cc-overlay" role="dialog" aria-label="Cookie Consent Banner" aria-live="polite">' +
          '<div class="cc-banner">' +
            '<div class="cc-banner-title">' + CONFIG.bannerTitle + '</div>' +
            '<div class="cc-banner-text">' +
              CONFIG.bannerText + ' ' +
              '<a href="' + CONFIG.privacyUrl + '" target="_blank" rel="noopener">' + CONFIG.privacyLinkText + '</a>.' +
            '</div>' +
            '<div class="cc-btn-row">' +
              '<button class="cc-btn cc-btn-settings" id="cc-btn-settings" aria-expanded="false">' +
                'Customize' +
              '</button>' +
              '<span class="cc-spacer"></span>' +
              '<button class="cc-btn cc-btn-decline" id="cc-btn-decline">Decline All</button>' +
              '<button class="cc-btn cc-btn-accept" id="cc-btn-accept">Accept All</button>' +
            '</div>' +
            '<div class="cc-settings" id="cc-settings">' +
              buildSettingsHTML(prefs) +
              '<div class="cc-btn-row" style="margin-top:16px;justify-content:flex-end">' +
                '<button class="cc-btn cc-btn-accept" id="cc-btn-save">Save Settings</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</div>'
    );
  }

  function buildFloatButton() {
    return (
      '<button class="cc-float" id="cc-float-btn" aria-label="Cookie Settings" title="Cookie Settings">' +
        '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
          '<circle cx="12" cy="12" r="3"/>' +
          '<path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>' +
        '</svg>' +
      '</button>'
    );
  }

  function showBanner() {
    if (_overlayEl) return;
    var container = document.createElement('div');
    container.innerHTML = buildBannerHTML();
    document.body.appendChild(container.firstElementChild);
    _overlayEl = document.querySelector('.cc-overlay');
    bindEvents();
  }

  function hideBanner() {
    if (_overlayEl) {
      _overlayEl.style.opacity = '0';
      _overlayEl.style.transition = 'opacity 0.2s';
      setTimeout(function () {
        if (_overlayEl && _overlayEl.parentNode) {
          _overlayEl.parentNode.remove();
        }
        _overlayEl = null;
        _settingsOpen = false;
        showFloatButton();
      }, 200);
    }
  }

  function showFloatButton() {
    if (_floatBtnEl) return;
    if (!_prefs) return;
    var container = document.createElement('div');
    container.innerHTML = buildFloatButton();
    document.body.appendChild(container.firstElementChild);
    _floatBtnEl = document.getElementById('cc-float-btn');
    if (_floatBtnEl) {
      _floatBtnEl.addEventListener('click', function () {
        removeFloatButton();
        showBanner();
        setTimeout(function () {
          var settingsEl = document.getElementById('cc-settings');
          var btn = document.getElementById('cc-btn-settings');
          if (settingsEl) settingsEl.classList.add('open');
          if (btn) btn.setAttribute('aria-expanded', 'true');
          _settingsOpen = true;
        }, 50);
      });
    }
  }

  function removeFloatButton() {
    if (_floatBtnEl && _floatBtnEl.parentNode) {
      _floatBtnEl.parentNode.remove();
    }
    _floatBtnEl = null;
  }

  function bindEvents() {
    var acceptBtn = document.getElementById('cc-btn-accept');
    if (acceptBtn) {
      acceptBtn.addEventListener('click', function () {
        var prefs = {};
        CONFIG.categories.forEach(function (cat) { prefs[cat.id] = true; });
        savePrefs(prefs);
        runLoaders(prefs);
        hideBanner();
      });
    }

    var declineBtn = document.getElementById('cc-btn-decline');
    if (declineBtn) {
      declineBtn.addEventListener('click', function () {
        var prefs = {};
        CONFIG.categories.forEach(function (cat) { prefs[cat.id] = cat.required; });
        savePrefs(prefs);
        hideBanner();
      });
    }

    var settingsBtn = document.getElementById('cc-btn-settings');
    if (settingsBtn) {
      settingsBtn.addEventListener('click', function () {
        var panel = document.getElementById('cc-settings');
        if (!panel) return;
        _settingsOpen = !_settingsOpen;
        if (_settingsOpen) {
          panel.classList.add('open');
          settingsBtn.setAttribute('aria-expanded', 'true');
        } else {
          panel.classList.remove('open');
          settingsBtn.setAttribute('aria-expanded', 'false');
        }
      });
    }

    var saveBtn = document.getElementById('cc-btn-save');
    if (saveBtn) {
      saveBtn.addEventListener('click', function () {
        var prefs = {};
        CONFIG.categories.forEach(function (cat) {
          var cb = document.querySelector('input[data-cc-cat="' + cat.id + '"]');
          prefs[cat.id] = cb ? cb.checked : cat.required;
        });
        savePrefs(prefs);
        runLoaders(prefs);
        hideBanner();
      });
    }

    document.addEventListener('keydown', function ccKeyHandler(e) {
      if (e.key === 'Escape' && _overlayEl && !_settingsOpen && _prefs) {
        hideBanner();
      }
    });
  }

  window.cookieConsent = {
    getPreferences: function () {
      return _prefs ? Object.assign({}, _prefs) : null;
    },
    hasConsented: function (category) {
      if (!_prefs) return false;
      return !!_prefs[category];
    },
    showBanner: function () {
      removeFloatButton();
      showBanner();
      setTimeout(function () {
        var settingsEl = document.getElementById('cc-settings');
        var btn = document.getElementById('cc-btn-settings');
        if (settingsEl) settingsEl.classList.add('open');
        if (btn) btn.setAttribute('aria-expanded', 'true');
        _settingsOpen = true;
      }, 50);
    },
    onChange: function (callback) {
      if (typeof callback === 'function') {
        _listeners.push(callback);
      }
    }
  };

  function init() {
    _prefs = loadPrefs();
    if (_prefs) {
      runLoaders(_prefs);
      showFloatButton();
    } else {
      setTimeout(function () {
        _prefs = _prefs || loadPrefs();
        if (!_prefs) showBanner();
      }, CONFIG.bannerDelayMs);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
