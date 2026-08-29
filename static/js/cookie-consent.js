/**
 * Cookie Consent Banner — VeroRun
 * Self-contained GDPR/CCPA compliant consent management.
 *
 * Usage: include <script src="/static/js/cookie-consent.js"></script> before </body>.
 * No dependencies. Aligns with design-system.css v2.0.
 *
 * Exposes: window.cookieConsent
 *   .getPreferences()      → { necessary, analytics, marketing, accepted, version }
 *   .hasConsented(cat)     → boolean
 *   .showBanner()          → reopen settings panel (re-consent)
 *   .onChange(callback)    → register listener for preference changes
 */

;(function () {
  'use strict';
  if(typeof window.__==='undefined'){window.__=function(k){return k;};}

  /* ═══════════════════════════════════════════════
     Configuration
     ═══════════════════════════════════════════════ */
  var CONFIG = {
    cookieName: 'cookie_consent_prefs',
    expiryDays: 30,                  // re-prompt after 30 days
    policyVersion: 1,                // increment to force re-consent
    bannerDelayMs: 300,              // delay before showing banner (avoid flicker)
    categories: [
      {
        id: 'necessary',
        label: __('必要 Cookie'),
        desc: __('登录态、安全校验、会话管理。网站核心功能依赖这些 Cookie，无法关闭。'),
        required: true
      },
      {
        id: 'analytics',
        label: __('分析与性能 Cookie'),
        desc: __('帮助我们了解访客如何使用网站，以便改进体验。数据匿名化处理。'),
        required: false
      },
      {
        id: 'marketing',
        label: __('营销 Cookie'),
        desc: __('用于投放相关广告和衡量广告效果。可能与第三方广告商共享。'),
        required: false
      }
    ],
    privacyUrl: '/cookie-policy',
    bannerTitle: __('Cookie 使用说明'),
    bannerText: __('本站使用 Cookie 来保障基本功能、分析访问数据以及提升您的体验。详细信息请阅读我们的'),
    privacyLinkText: __('隐私政策'),
  };

  /* ═══════════════════════════════════════════════
     State
     ═══════════════════════════════════════════════ */
  var _prefs = null;            // current preferences object (or null if unset)
  var _listeners = [];          // onChange callbacks
  var _overlayEl = null;        // .cc-overlay DOM ref
  var _settingsOpen = false;    // is the settings panel expanded?
  var _floatBtnEl = null;       // floating reopen button

  /* ═══════════════════════════════════════════════
     Storage helpers
     ═══════════════════════════════════════════════ */
  function loadPrefs() {
    try {
      var raw = localStorage.getItem(CONFIG.cookieName);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      // Validate structure
      if (!parsed || typeof parsed !== 'object') return null;
      if (!parsed.accepted || !parsed.version) return null;
      // Version mismatch → treat as unset
      if (parsed.version !== CONFIG.policyVersion) return null;
      // Expiry check
      var acceptedDate = new Date(parsed.accepted);
      var expiryDate = new Date(acceptedDate.getTime() + CONFIG.expiryDays * 86400000);
      if (new Date() > expiryDate) {
        localStorage.removeItem(CONFIG.cookieName);
        return null;
      }
      // Ensure all categories exist
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
    } catch (e) {
      // localStorage unavailable (private mode) → keep in memory only
    }
    _prefs = prefs;
    notifyListeners(prefs);
    // Also set a non-HttpOnly cookie as a signal for server-side checks
    try {
      var d = new Date();
      d.setTime(d.getTime() + CONFIG.expiryDays * 86400000);
      document.cookie = 'cookie_consent=' + encodeURIComponent(JSON.stringify({
        necessary: prefs.necessary,
        analytics: prefs.analytics,
        marketing: prefs.marketing,
        v: CONFIG.policyVersion
      })) + ';expires=' + d.toUTCString() + ';path=/;SameSite=Lax';
    } catch (e) { /* ignore */ }
  }

  function notifyListeners(prefs) {
    _listeners.forEach(function (fn) {
      try { fn(prefs); } catch (e) { /* listener must not break consent flow */ }
    });
  }

  /* ═══════════════════════════════════════════════
     Dynamic script loaders
     ═══════════════════════════════════════════════ */
  // Register loaders here: { category: function }
  window.__CC_LOADERS__ = window.__CC_LOADERS__ || {};

  function runLoaders(prefs) {
    Object.keys(window.__CC_LOADERS__).forEach(function (cat) {
      if (prefs[cat] && typeof window.__CC_LOADERS__[cat] === 'function') {
        try { window.__CC_LOADERS__[cat](); } catch (e) {
          console.warn('[CookieConsent] loader failed for ' + cat, e);
        }
      }
    });
  }

  /* ═══════════════════════════════════════════════
     DOM builders
     ═══════════════════════════════════════════════ */

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
            (cat.required ? '<div class="cc-setting-always">'+__('始终启用')+'</div>' : '') +
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
        '<div class="cc-overlay" role="dialog" aria-label="'+__('Cookie 同意提示')+'" aria-live="polite">' +
          '<div class="cc-banner">' +
            '<div class="cc-banner-title">' + CONFIG.bannerTitle + '</div>' +
            '<div class="cc-banner-text">' +
              CONFIG.bannerText + ' ' +
              '<a href="' + CONFIG.privacyUrl + '" target="_blank" rel="noopener">' + CONFIG.privacyLinkText + '</a>。' +
            '</div>' +

            // Main buttons row
            '<div class="cc-btn-row">' +
              '<button class="cc-btn cc-btn-settings" id="cc-btn-settings" aria-expanded="false">' +
                __('个性化设置') +
              '</button>' +
              '<span class="cc-spacer"></span>' +
              '<button class="cc-btn cc-btn-decline" id="cc-btn-decline">'+__('拒绝全部')+'</button>' +
              '<button class="cc-btn cc-btn-accept" id="cc-btn-accept">'+__('同意全部')+'</button>' +
            '</div>' +

            // Settings panel (hidden)
            '<div class="cc-settings" id="cc-settings">' +
              buildSettingsHTML(prefs) +
              '<div class="cc-btn-row" style="margin-top:16px;justify-content:flex-end">' +
                '<button class="cc-btn cc-btn-accept" id="cc-btn-save">'+__('保存设置')+'</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</div>'
    );
  }

  function buildFloatButton() {
    return (
      '<button class="cc-float" id="cc-float-btn" aria-label="'+__('Cookie 设置')+'" title="'+__('Cookie 设置')+'">' +
        '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
          '<circle cx="12" cy="12" r="3"/>' +
          '<path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>' +
        '</svg>' +
      '</button>'
    );
  }

  /* ═══════════════════════════════════════════════
     Banner lifecycle
     ═══════════════════════════════════════════════ */

  function showBanner() {
    if (_overlayEl) return; // already showing
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
        // Show floating reopen button
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
        // Auto-expand settings
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

  /* ═══════════════════════════════════════════════
     Event handling
     ═══════════════════════════════════════════════ */

  function bindEvents() {
    // Accept all
    var acceptBtn = document.getElementById('cc-btn-accept');
    if (acceptBtn) {
      acceptBtn.addEventListener('click', function () {
        var prefs = {};
        CONFIG.categories.forEach(function (cat) {
          prefs[cat.id] = true;
        });
        savePrefs(prefs);
        runLoaders(prefs);
        hideBanner();
      });
    }

    // Decline all (only necessary)
    var declineBtn = document.getElementById('cc-btn-decline');
    if (declineBtn) {
      declineBtn.addEventListener('click', function () {
        var prefs = {};
        CONFIG.categories.forEach(function (cat) {
          prefs[cat.id] = cat.required;
        });
        savePrefs(prefs);
        hideBanner();
      });
    }

    // Toggle settings panel
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

    // Save settings (from settings panel)
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

    // Keyboard: Escape to close (when settings is closed)
    document.addEventListener('keydown', function ccKeyHandler(e) {
      if (e.key === 'Escape' && _overlayEl && !_settingsOpen && _prefs) {
        // Already saved prefs → just close
        hideBanner();
      }
    });
  }

  /* ═══════════════════════════════════════════════
     Public API — window.cookieConsent
     ═══════════════════════════════════════════════ */

  window.cookieConsent = {
    /**
     * Get current consent preferences.
     * @returns {Object|null} { necessary, analytics, marketing, accepted, version } or null if unset.
     */
    getPreferences: function () {
      return _prefs ? Object.assign({}, _prefs) : null;
    },

    /**
     * Check if user has consented to a specific category.
     * @param {string} category - 'necessary' | 'analytics' | 'marketing'
     * @returns {boolean}
     */
    hasConsented: function (category) {
      if (!_prefs) return false;
      // necessary is always required, but only "true" if user has made a choice
      return !!_prefs[category];
    },

    /**
     * Re-open the consent banner (settings panel expanded).
     * Useful for "Cookie Settings" links in footer / privacy page.
     */
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

    /**
     * Register a callback that fires when preferences change.
     * @param {function} callback - receives the new preferences object.
     */
    onChange: function (callback) {
      if (typeof callback === 'function') {
        _listeners.push(callback);
      }
    }
  };

  /* ═══════════════════════════════════════════════
     Initialization
     ═══════════════════════════════════════════════ */

  function init() {
    _prefs = loadPrefs();

    if (_prefs) {
      // User has already made a choice → run loaders, show float button
      runLoaders(_prefs);
      showFloatButton();
    } else {
      // No consent yet → show banner after short delay
      setTimeout(function () {
        // Double-check nothing was set during the delay (edge case)
        _prefs = _prefs || loadPrefs();
        if (!_prefs) {
          showBanner();
        }
      }, CONFIG.bannerDelayMs);
    }
  }

  // Wait for DOM to be ready, then init
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
