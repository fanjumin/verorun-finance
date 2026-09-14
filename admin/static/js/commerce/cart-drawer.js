/* Site Builder — Commerce frontend (cart drawer + badge + toast)
 * 全局商务交互件:事件委托 [data-add-cart],角标实时刷新,悬浮抽屉预览。
 * 依赖:/mall/api/cart* (shop 插件,同域 SSO);零第三方库;样式令牌化(带兜底)。
 * 挂载:模板级 <script src="/static/js/commerce/cart-drawer.js" defer></script>
 */
(function () {
  'use strict';
  if (window.SBCommerce) return;

  var API = '/mall/api';
  var COLORS = {
    primary: 'var(--color-primary, #f0a020)',
    bg: 'var(--color-surface, #ffffff)',
    border: 'var(--color-border, #e2e8f0)',
    text: 'var(--color-text-primary, #1a202c)',
    text2: 'var(--color-text-secondary, #718096)',
    danger: '#ef4444', ok: '#10b981'
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function money(n) {
    var v = Number(n || 0);
    return Number.isFinite(v) ? '¥' + v.toFixed(2) : '';
  }
  function toast(msg, kind) {
    var t = document.createElement('div');
    t.textContent = msg;
    t.style.cssText = 'position:fixed;left:50%;bottom:84px;transform:translateX(-50%);z-index:99999;' +
      'background:' + (kind === 'err' ? COLORS.danger : '#111827') + ';color:#fff;padding:9px 18px;' +
      'border-radius:999px;font-size:13px;box-shadow:0 6px 20px rgba(0,0,0,.25);opacity:0;transition:opacity .25s';
    document.body.appendChild(t);
    requestAnimationFrame(function () { t.style.opacity = '1'; });
    setTimeout(function () { t.style.opacity = '0'; setTimeout(function () { t.remove(); }, 300); }, 2200);
  }

  /* ── 角标:在导航中指向购物车的链接上挂数字徽标 ── */
  function placeBadge() {
    var links = document.querySelectorAll('a[href*="/mall/cart"]');
    if (!links.length) return null;
    var a = links[links.length - 1];
    var b = a.querySelector('.sb-cart-badge');
    if (!b) {
      b = document.createElement('span');
      b.className = 'sb-cart-badge';
      b.style.cssText = 'display:inline-block;min-width:17px;height:17px;line-height:17px;margin-left:5px;' +
        'border-radius:999px;background:' + COLORS.primary + ';color:#fff;font-size:11px;text-align:center;' +
        'padding:0 5px;vertical-align:2px';
      a.appendChild(b);
    }
    return b;
  }
  var badgeEl = null;
  function setBadge(n) {
    if (!badgeEl) badgeEl = placeBadge();
    if (badgeEl) { badgeEl.textContent = n > 99 ? '99+' : String(n); badgeEl.style.display = n > 0 ? '' : 'none'; }
  }
  function refreshBadge() {
    fetch(API + '/cart', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var items = (d && (d.items || d.data || d.cart)) || [];
        var n = 0;
        items.forEach(function (it) { n += Number(it.quantity || it.qty || 0); });
        setBadge(n);
      })
      .catch(function () { setBadge(0); });
  }

  /* ── 抽屉 ── */
  var drawer = null, overlay = null;
  function ensureDrawer() {
    if (drawer) return;
    overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(8,10,16,.45);z-index:99990;opacity:0;transition:opacity .2s';
    drawer = document.createElement('aside');
    drawer.setAttribute('role', 'dialog');
    drawer.setAttribute('aria-label', '购物车');
    drawer.style.cssText = 'position:fixed;top:0;right:0;bottom:0;width:min(420px,94vw);background:' + COLORS.bg +
      ';z-index:99991;box-shadow:-8px 0 32px rgba(0,0,0,.22);transform:translateX(100%);transition:transform .25s ease;' +
      'display:flex;flex-direction:column;color:' + COLORS.text;
    drawer.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:space-between;padding:16px 18px;border-bottom:1px solid ' + COLORS.border + '">' +
      '<strong style="font-size:16px">🛒 购物车</strong>' +
      '<button type="button" data-cart-close style="background:none;border:none;font-size:20px;cursor:pointer;color:' + COLORS.text2 + '">×</button></div>' +
      '<div data-cart-body style="flex:1;overflow:auto;padding:6px 0"></div>' +
      '<div style="padding:14px 18px;border-top:1px solid ' + COLORS.border + ';display:flex;justify-content:space-between;align-items:center">' +
      '<span style="font-size:13px;color:' + COLORS.text2 + '">合计 <b data-cart-total style="color:' + COLORS.primary + ';font-size:16px">¥0.00</b></span>' +
      '<a href="/mall/cart" style="background:' + COLORS.primary + ';color:#fff;text-decoration:none;padding:9px 22px;border-radius:8px;font-size:14px;font-weight:600">去结算</a></div>';
    document.body.appendChild(overlay);
    document.body.appendChild(drawer);
    drawer.querySelector('[data-cart-close]').addEventListener('click', closeDrawer);
    overlay.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeDrawer(); });
  }
  function openDrawer() {
    ensureDrawer();
    overlay.style.opacity = '1'; drawer.style.transform = 'translateX(0)';
    loadCart();
  }
  function closeDrawer() {
    if (!drawer) return;
    overlay.style.opacity = '0'; drawer.style.transform = 'translateX(100%)';
  }

  function normItems(d) {
    var items = (d && (d.items || d.data || d.cart)) || [];
    if (!Array.isArray(items)) items = [];
    return items.map(function (it) {
      return {
        id: it.product_id || it.productId || it.id,
        title: it.title || it.name || it.product_name || '商品',
        price: it.price || it.sale_price || 0,
        qty: it.quantity || it.qty || 1,
        image: it.image || it.cover || it.image_url || '',
        url: '/mall/' + (it.product_id || it.id || '')
      };
    });
  }
  function loadCart() {
    var body = drawer.querySelector('[data-cart-body]');
    body.innerHTML = '<div style="text-align:center;color:' + COLORS.text2 + ';font-size:13px;padding:40px 0">加载中…</div>';
    fetch(API + '/cart', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error(r.status)); })
      .then(function (d) {
        var items = normItems(d);
        if (!items.length) {
          body.innerHTML = '<div style="text-align:center;padding:60px 20px;color:' + COLORS.text2 + '">' +
            '<div style="font-size:40px;margin-bottom:10px">🛒</div><div style="font-size:14px">购物车还是空的</div>' +
            '<a href="/mall" style="display:inline-block;margin-top:16px;color:' + COLORS.primary + ';font-size:13px">去逛逛 →</a></div>';
          setTotal(0); return;
        }
        var html = '';
        var total = 0;
        items.forEach(function (it) {
          total += Number(it.price) * Number(it.qty);
          html += '<div data-cart-row="' + it.id + '" style="display:flex;gap:12px;padding:12px 18px;border-bottom:1px solid ' + COLORS.border + '">' +
            (it.image ? '<img loading="lazy" src="' + esc(it.image) + '" alt="" style="width:64px;height:64px;object-fit:cover;border-radius:8px;background:#f1f5f9"/>'
              : '<div style="width:64px;height:64px;border-radius:8px;background:#f1f5f9;display:flex;align-items:center;justify-content:center;font-size:22px">🛍️</div>') +
            '<div style="flex:1;min-width:0"><a href="' + esc(it.url) + '" style="color:' + COLORS.text + ';text-decoration:none;font-size:13.5px;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(it.title) + '</a>' +
            '<div style="font-size:12.5px;color:' + COLORS.text2 + ';margin-top:3px">' + money(it.price) + '</div>' +
            '<div style="display:flex;align-items:center;gap:8px;margin-top:6px">' +
            '<button type="button" data-cart-op="minus" data-id="' + it.id + '" style="width:22px;height:22px;border-radius:5px;border:1px solid ' + COLORS.border + ';background:none;cursor:pointer">−</button>' +
            '<span style="font-size:13px;min-width:18px;text-align:center">' + it.qty + '</span>' +
            '<button type="button" data-cart-op="plus" data-id="' + it.id + '" style="width:22px;height:22px;border-radius:5px;border:1px solid ' + COLORS.border + ';background:none;cursor:pointer">+</button>' +
            '<button type="button" data-cart-op="remove" data-id="' + it.id + '" style="margin-left:auto;background:none;border:none;cursor:pointer;color:' + COLORS.text2 + ';font-size:12px">删除</button>' +
            '</div></div></div>';
        });
        body.innerHTML = html;
        setTotal(total);
      })
      .catch(function () {
        body.innerHTML = '<div style="text-align:center;padding:50px 20px;color:' + COLORS.text2 + ';font-size:13.5px">' +
          '购物车加载失败,请稍后重试<br/><span style="font-size:12px;margin-top:6px;display:inline-block">或前往 <a href="/mall/cart" style="color:' + COLORS.primary + '">购物车页面</a></span></div>';
      });
  }
  function setTotal(v) {
    var el = drawer && drawer.querySelector('[data-cart-total]');
    if (el) el.textContent = money(v);
  }
  function cartOp(id, op) {
    var url = op === 'remove' ? API + '/cart/remove' : API + '/cart/update';
    var body = op === 'remove' ? { product_id: id } : { product_id: id, quantity: op === 'plus' ? 999 : 0 };
    fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.success === false) { toast(d.error || '操作失败', 'err'); return; }
        loadCart(); refreshBadge();
      })
      .catch(function () { toast('网络错误,请重试', 'err'); });
  }

  /* ── 加购事件委托(公开页任意 [data-add-cart]) ── */
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-add-cart]');
    if (btn) {
      e.preventDefault();
      var pid = btn.getAttribute('data-add-cart');
      var qty = Number(btn.getAttribute('data-qty') || 1);
      var label = btn.getAttribute('data-label') || btn.textContent || '加入购物车';
      btn.disabled = true;
      var old = btn.textContent;
      btn.textContent = '加入中…';
      fetch(API + '/cart/add', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ product_id: pid, quantity: qty }) })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d && d.success === false) { toast(d.error || '加购失败', 'err'); return; }
          toast('已加入购物车'); refreshBadge(); openDrawer();
        })
        .catch(function () { toast('网络错误,请重试', 'err'); })
        .finally(function () { btn.disabled = false; btn.textContent = old; });
      return;
    }
    var opBtn = e.target.closest('[data-cart-op]');
    if (opBtn) { cartOp(opBtn.getAttribute('data-id'), opBtn.getAttribute('data-cart-op')); return; }
    var fab = e.target.closest('[data-cart-fab]');
    if (fab) { openDrawer(); }
  });

  /* ── 悬浮购物车按钮(导航无购物车链接时兜底) ── */
  function ensureFab() {
    if (document.querySelector('.sb-cart-fab')) return;
    var fab = document.createElement('button');
    fab.type = 'button';
    fab.className = 'sb-cart-fab';
    fab.setAttribute('data-cart-fab', '');
    fab.setAttribute('aria-label', '打开购物车');
    fab.innerHTML = '🛒<span data-cart-fab-n style="position:absolute;top:-4px;right:-4px;background:' + COLORS.danger + ';color:#fff;font-size:10.5px;min-width:16px;height:16px;line-height:16px;border-radius:999px;display:none;padding:0 4px"></span>';
    fab.style.cssText = 'position:fixed;right:18px;bottom:88px;width:48px;height:48px;border-radius:50%;border:none;cursor:pointer;background:' + COLORS.primary + ';color:#fff;font-size:20px;z-index:99980;box-shadow:0 8px 22px rgba(0,0,0,.28);display:none';
    document.body.appendChild(fab);
  }
  function maybeShowFab() {
    var hasNavCart = !!document.querySelector('a[href*="/mall/cart"]');
    var fab = document.querySelector('.sb-cart-fab');
    if (fab) { fab.style.display = hasNavCart ? 'none' : 'block'; }
  }

  window.SBCommerce = {
    refreshBadge: refreshBadge,
    openDrawer: openDrawer,
    closeDrawer: closeDrawer,
    toast: toast,
    addToCart: function (pid, qty) {
      var t = document.createElement('button'); t.setAttribute('data-add-cart', pid);
      if (qty) t.setAttribute('data-qty', qty);
      document.body.appendChild(t); t.click(); t.remove();
    }
  };

  function init() {
    ensureFab(); maybeShowFab();
    if (document.readyState === 'complete' || document.readyState === 'interactive') refreshBadge();
    else document.addEventListener('DOMContentLoaded', refreshBadge);
    setInterval(refreshBadge, 60000);
  }
  init();
})();
