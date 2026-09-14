/* Site Builder — Commerce: data-driven products block renderer
 * 用法:区块 partial 输出 <div class="sb-products" data-sb-products data-category=.. data-limit=..>
 * 数据:GET /mall/api/products?category=&limit= (shop 插件);同域 SSO。
 * 渲染:商品卡(图/名/价/加购/收藏入口),链接跳 /mall/<pid>;空态/错误态/骨架。
 */
(function () {
  'use strict';
  if (!window.SBCommerce) window.SBCommerce = {};
  var API = '/mall/api';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function money(n) {
    var v = Number(n || 0);
    return Number.isFinite(v) ? '¥' + v.toFixed(2) : '';
  }
  function query(params) {
    var q = [];
    Object.keys(params).forEach(function (k) { if (params[k] !== '' && params[k] != null) q.push(encodeURIComponent(k) + '=' + encodeURIComponent(params[k])); });
    return q.length ? '?' + q.join('&') : '';
  }

  function renderCard(it) {
    var pid = it.id != null ? it.id : (it.product_id || '');
    var url = '/mall/' + pid;
    var title = it.title || it.name || it.product_name || '';
    var price = it.price != null ? it.price : (it.sale_price || 0);
    var img = it.image || it.cover || it.image_url || '';
    var card = document.createElement('a');
    card.href = url;
    card.style.cssText = 'display:flex;flex-direction:column;gap:10px;text-decoration:none;color:inherit;' +
      'background:var(--color-surface,#fff);border:1px solid var(--color-border,#e2e8f0);border-radius:var(--radius-md,12px);' +
      'overflow:hidden;transition:transform .18s,box-shadow .18s;position:relative';
    card.onmouseenter = function () { card.style.transform = 'translateY(-3px)'; card.style.boxShadow = '0 10px 24px rgba(0,0,0,.10)'; };
    card.onmouseleave = function () { card.style.transform = ''; card.style.boxShadow = ''; };
    card.innerHTML =
      (img ? '<img loading="lazy" src="' + esc(img) + '" alt="' + esc(title) + '" style="width:100%;aspect-ratio:1/1;object-fit:cover;background:#f1f5f9"/>'
        : '<div style="width:100%;aspect-ratio:1/1;display:flex;align-items:center;justify-content:center;font-size:38px;background:#f1f5f9">🛍️</div>') +
      '<div style="padding:0 12px 12px;display:flex;flex-direction:column;gap:6px">' +
      '<div style="font-size:13.5px;line-height:1.45;height:2.9em;overflow:hidden;color:var(--color-text-primary,#1a202c)">' + esc(title) + '</div>' +
      '<div style="display:flex;align-items:center;justify-content:space-between">' +
      '<b style="color:var(--color-accent,#f59e0b);font-size:16px">' + money(price) + '</b>' +
      '</div></div>';
    /* 加购按钮:置于卡片底部(阻止卡片跳转) */
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.setAttribute('data-add-cart', String(pid));
    btn.setAttribute('data-label', '加入购物车');
    btn.textContent = '加入购物车';
    btn.style.cssText = 'display:block;width:calc(100% - 24px);margin:0 12px 12px;padding:8px 0;border:none;border-radius:8px;' +
      'background:var(--color-primary,#f0a020);color:#fff;font-size:13px;font-weight:600;cursor:pointer;text-align:center';
    btn.addEventListener('click', function (e) { e.preventDefault(); e.stopPropagation(); });
    card.appendChild(btn);
    card.addEventListener('click', function (e) { if (e.target === btn) return; });
    return card;
  }

  function render(el) {
    var category = el.getAttribute('data-category') || '';
    var limit = Number(el.getAttribute('data-limit') || 8);
    var layout = el.getAttribute('data-layout') || 'grid';
    var wrap = document.createElement('div');
    if (layout === 'list') {
      wrap.style.cssText = 'display:grid;gap:12px;grid-template-columns:1fr';
    } else if (layout === 'scroll') {
      wrap.style.cssText = 'display:grid;gap:14px;grid-auto-flow:column;grid-auto-columns:minmax(200px,1fr);overflow-x:auto;padding-bottom:8px';
    } else {
      wrap.style.cssText = 'display:grid;gap:16px;grid-template-columns:repeat(auto-fill,minmax(200px,1fr))';
    }
    el.appendChild(wrap);

    var state = document.createElement('div');
    state.style.cssText = 'text-align:center;color:var(--color-text-secondary,#718096);font-size:13.5px;padding:36px 0';
    el.appendChild(state);
    state.textContent = '加载中…';

    fetch(API + '/products' + query({ category: category, limit: limit }), { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error(String(r.status))); })
      .then(function (d) {
        var list = d && (d.items || d.data || d.products);
        if (!Array.isArray(list)) list = [];
        state.style.display = 'none';
        if (!list.length) {
          state.style.display = '';
          state.textContent = '该分类暂时没有商品';
          return;
        }
        list.forEach(function (it) { wrap.appendChild(renderCard(it)); });
        if (window.SBCommerce && window.SBCommerce.refreshBadge) window.SBCommerce.refreshBadge();
      })
      .catch(function () {
        state.style.display = '';
        state.innerHTML = '商品加载失败 <a href="/mall" style="color:var(--color-primary,#f0a020)">去商城看看 →</a>';
      });
  }

  function init() {
    var els = document.querySelectorAll('[data-sb-products]');
    if (!els.length) return;
    [].forEach.call(els, render);
  }
  if (document.readyState === 'complete' || document.readyState === 'interactive') init();
  else document.addEventListener('DOMContentLoaded', init);

  window.SBCommerce.renderProducts = render;
})();
