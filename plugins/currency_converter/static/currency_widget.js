/**
 * Currency Converter Widget v0.1.0
 * ==================================
 * 全站货币换算组件 — 价格标签自动替换 + 用户币种选择器。
 *
 * 用法:
 *   1. 在需要换算的价格元素上加上 class="ez-price" 和 data 属性：
 *      <span class="ez-price" data-amount="1000" data-currency="CNY">¥1,000</span>
 *   2. 插件会自动将其替换为用户选中的币种显示。
 *   3. 该文件由 admin 模板 include 或通过 <script> 标签加载。
 */

(function () {
  'use strict';

  // ── 配置 ─────────────────────────────────────────────
  var BASE_CURRENCY = 'CNY';
  var RATES = {};           // {USD: 0.14, EUR: 0.13, ...}
  var CURRENCIES = [];      // [{code, symbol, name, decimals}, ...]
  var SELECTED = '';        // 当前选中的币种代码

  // ── Cookie 工具 ──────────────────────────────────────

  function getCookie(name) {
    var match = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return match ? decodeURIComponent(match[2]) : null;
  }

  function setCookie(name, value, days) {
    var expires = '';
    if (days) {
      var date = new Date();
      date.setTime(date.getTime() + days * 24 * 60 * 60 * 1000);
      expires = '; expires=' + date.toUTCString();
    }
    document.cookie = name + '=' + encodeURIComponent(value) + expires + '; path=/';
  }

  // ── 加载汇率数据 ─────────────────────────────────────

  function loadRates() {
    // 尝试从本地存储读取缓存
    var cached = localStorage.getItem('ez_currency_rates');
    var cacheTime = localStorage.getItem('ez_currency_rates_time');
    var now = Date.now();

    if (cached && cacheTime && (now - parseInt(cacheTime)) < 3600000) {
      // 缓存未过期 (1h)
      try {
        var data = JSON.parse(cached);
        RATES = data.rates || {};
        CURRENCIES = data.currencies || [];
        BASE_CURRENCY = data.base_currency || 'CNY';
        return;
      } catch (e) {
        // 解析失败，重新请求
      }
    }

    // 从 API 加载
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/admin/currency/rates', true);
    xhr.onload = function () {
      if (xhr.status === 200) {
        try {
          var resp = JSON.parse(xhr.responseText);
          if (resp.success && resp.data) {
            RATES = resp.data.rates || {};
            CURRENCIES = resp.data.currencies || [];
            BASE_CURRENCY = resp.data.base_currency || 'CNY';
            // 缓存到本地存储
            localStorage.setItem('ez_currency_rates', JSON.stringify(resp.data));
            localStorage.setItem('ez_currency_rates_time', String(Date.now()));
            // 加载完成后执行换算
            convertAll();
          }
        } catch (e) {
          // 忽略
        }
      }
    };
    xhr.send();
  }

  // ── 换算并替换 DOM 中的价格 ─────────────────────────

  function convertPrice(amount, fromCurrency, toCurrency) {
    fromCurrency = fromCurrency.toUpperCase();
    toCurrency = toCurrency.toUpperCase();
    if (fromCurrency === toCurrency) return amount;
    var fromRate = RATES[fromCurrency];
    var toRate = RATES[toCurrency];
    if (!fromRate || !toRate) return amount;
    var baseAmount = amount / fromRate;
    return baseAmount * toRate;
  }

  function getCurrencyInfo(code) {
    for (var i = 0; i < CURRENCIES.length; i++) {
      if (CURRENCIES[i].code === code) return CURRENCIES[i];
    }
    return { code: code, symbol: '', decimals: 2 };
  }

  function formatPrice(amount, currency) {
    var info = getCurrencyInfo(currency);
    var fixed = amount.toFixed(info.decimals);
    // 千分位
    var parts = fixed.split('.');
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return info.symbol + parts.join('.');
  }

  function convertAll() {
    if (!SELECTED || !RATES || Object.keys(RATES).length === 0) return;

    var elements = document.querySelectorAll('.ez-price');
    for (var i = 0; i < elements.length; i++) {
      var el = elements[i];
      var amount = parseFloat(el.getAttribute('data-amount'));
      var currency = (el.getAttribute('data-currency') || BASE_CURRENCY).toUpperCase();

      if (isNaN(amount)) continue;

      var converted = convertPrice(amount, currency, SELECTED);
      el.textContent = formatPrice(converted, SELECTED);
      el.setAttribute('data-display-currency', SELECTED);
    }
  }

  // ── 货币选择器 UI ────────────────────────────────────

  function createCurrencySelector(container) {
    // 如果已有选择器，不重复创建
    if (document.getElementById('ez-currency-selector')) return;

    var wrapper = container || document.body;
    var select = document.createElement('select');
    select.id = 'ez-currency-selector';
    select.className = 'ez-currency-select';

    // 填充选项
    for (var i = 0; i < CURRENCIES.length; i++) {
      var opt = document.createElement('option');
      opt.value = CURRENCIES[i].code;
      opt.textContent = CURRENCIES[i].code + ' (' + CURRENCIES[i].symbol + ')';
      if (CURRENCIES[i].code === SELECTED) opt.selected = true;
      select.appendChild(opt);
    }

    // 切换事件
    select.addEventListener('change', function () {
      SELECTED = this.value;
      setCookie('ez_preferred_currency', SELECTED, 30);
      convertAll();
      // 尝试保存到后端（如果用户已登录）
      savePreferenceToServer(SELECTED);
    });

    wrapper.appendChild(select);
  }

  function savePreferenceToServer(currency) {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/admin/currency/preference', true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.send(JSON.stringify({ currency: currency }));
  }

  // ── GeoIP 自动检测 ──────────────────────────────────

  function detectByGeoIP(callback) {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/admin/currency/geoip', true);
    xhr.onload = function () {
      if (xhr.status === 200) {
        try {
          var resp = JSON.parse(xhr.responseText);
          if (resp.success && resp.data && resp.data.source === 'geoip') {
            callback(resp.data.currency);
            return;
          }
        } catch (e) { /* ignore */ }
      }
      callback(null);  // GeoIP 失败，用默认
    };
    xhr.onerror = function () { callback(null); };
    xhr.send();
  }

  // ── 初始化 ────────────────────────────────────────────

  function init() {
    // 1. 读取用户偏好（Cookie > GeoIP > 默认）
    var cookieVal = getCookie('ez_preferred_currency');

    if (cookieVal) {
      SELECTED = cookieVal;
      // 2. 加载汇率（同步）
      loadRates();

      // 3. 等 DOM 就绪后换算
      scheduleConvert();

      // 4. 监听动态 DOM
      observeDOM();
    } else {
      // 首次访问 → GeoIP 检测
      detectByGeoIP(function (geoCurrency) {
        SELECTED = geoCurrency || BASE_CURRENCY;
        // 写回 Cookie 作为偏好
        setCookie('ez_preferred_currency', SELECTED, 30);

        // 加载汇率
        loadRates();

        // 换算
        scheduleConvert();
        observeDOM();
      });
    }
  }

  function scheduleConvert() {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function () { convertAll(); });
    } else {
      convertAll();
    }
  }

  function observeDOM() {
    var observer = new MutationObserver(function () { convertAll(); });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  // ── 暴露全局接口（供其他 JS 调用） ────────────────────

  window.EZCurrency = {
    convert: convertPrice,
    format: formatPrice,
    getRates: function () { return RATES; },
    getSelected: function () { return SELECTED; },
    setCurrency: function (code) {
      SELECTED = code;
      setCookie('ez_preferred_currency', code, 30);
      convertAll();
    },
    refresh: function () {
      localStorage.removeItem('ez_currency_rates');
      loadRates();
    },
    getCurrencyInfo: getCurrencyInfo,
    createSelector: createCurrencySelector,
  };

  // 自动初始化
  init();

})();
