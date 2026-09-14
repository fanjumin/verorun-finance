/* Site Builder — AI chatbot floating bubble (site-facing)
 * 依赖端点:POST /api/v1/site-chat {message, context:{product_id,order_id}, site_key}
 * 端点未就绪(404/网络错)→ 隐藏浮窗并仅提示一次;UI 消息/建议问题/快捷商品上下文。
 */
(function () {
  'use strict';
  var root = document.getElementById('sbChatBubble');
  if (!root) return;

  var API_EP = '/api/v1/site-chat';
  var COL = {
    primary: 'var(--color-primary,#f0a020)',
    bg: 'var(--color-surface,#ffffff)',
    border: 'var(--color-border,#e2e8f0)',
    text: 'var(--color-text-primary,#1a202c)',
    text2: 'var(--color-text-secondary,#718096)'
  };
  function css(el, s) { el.style.cssText = s; }

  /* 注入面板样式(与公开页令牌一致,带兜底) */
  var style = document.createElement('style');
  style.textContent =
    '.sb-chat-root{position:fixed;right:20px;bottom:24px;z-index:99970;font-family:inherit}' +
    '.sb-chat-fab{width:52px;height:52px;border-radius:50%;border:none;cursor:pointer;font-size:23px;background:' + COL.primary + ';color:#fff;box-shadow:0 8px 22px rgba(0,0,0,.26)}' +
    '.sb-chat-panel{position:fixed;right:20px;bottom:88px;width:min(360px,94vw);height:min(480px,70vh);background:' + COL.bg + ';border:1px solid ' + COL.border + ';border-radius:14px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 18px 48px rgba(0,0,0,.28);color:' + COL.text + '}' +
    '.sb-chat-head{padding:12px 16px;font-weight:700;font-size:14.5px;border-bottom:1px solid ' + COL.border + ';display:flex;justify-content:space-between;align-items:center}' +
    '.sb-chat-close{cursor:pointer;font-size:19px;color:' + COL.text2 + ';background:none;border:none;line-height:1}' +
    '.sb-chat-msgs{flex:1;overflow:auto;padding:12px 14px;display:flex;flex-direction:column;gap:9px;font-size:13.5px}' +
    '.sb-msg{max-width:86%;padding:8px 12px;border-radius:10px;line-height:1.55;white-space:pre-wrap;word-break:break-word}' +
    '.sb-msg.user{align-self:flex-end;background:' + COL.primary + ';color:#fff;border-bottom-right-radius:3px}' +
    '.sb-msg.ai{align-self:flex-start;background:#f1f5f9;color:' + COL.text + ';border-bottom-left-radius:3px}' +
    '.sb-chat-sugs{display:flex;gap:7px;flex-wrap:wrap;padding:0 14px 8px}' +
    '.sb-chat-sugs button{border:1px solid ' + COL.border + ';background:none;color:' + COL.text2 + ';border-radius:999px;padding:4px 12px;font-size:12px;cursor:pointer}' +
    '.sb-chat-form{display:flex;gap:8px;padding:10px 12px;border-top:1px solid ' + COL.border + '}' +
    '.sb-chat-form input{flex:1;border:1px solid ' + COL.border + ';border-radius:8px;padding:8px 11px;font-size:13.5px;outline:none;background:transparent;color:' + COL.text + '}' +
    '.sb-chat-form button{background:' + COL.primary + ';border:none;color:#fff;border-radius:8px;padding:0 16px;cursor:pointer;font-size:13.5px}';
  document.head.appendChild(style);

  var fab = root.querySelector('[data-chat-toggle]');
  var panel = root.querySelector('.sb-chat-panel');
  var msgs = root.querySelector('[data-chat-msgs]');
  var sugs = root.querySelector('[data-chat-sugs]');
  var form = root.querySelector('[data-chat-form]');
  var input = root.querySelector('[data-chat-input]');

  var online = false;
  var suggestions = ['你们有什么产品?', '怎么联系人工客服?', '下单后多久发货?'];

  function bubble(cls, text) {
    var d = document.createElement('div');
    d.className = 'sb-msg ' + cls;
    d.textContent = text;
    msgs.appendChild(d);
    msgs.scrollTop = msgs.scrollHeight;
    return d;
  }
  function welcome() {
    var meta = document.querySelector('meta[name="site-name"]');
    var name = meta ? meta.content : '本站';
    bubble('ai', '你好,我是 ' + name + ' 的 AI 客服助手,请问有什么可以帮您?');
    sugs.innerHTML = '';
    suggestions.forEach(function (s) {
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = s;
      b.addEventListener('click', function () { send(s); });
      sugs.appendChild(b);
    });
  }

  function context() {
    var c = {};
    /* 商品详情页自动携带上下文(页面注入 data-chat-ctx) */
    var ctxEl = document.querySelector('[data-chat-ctx]');
    if (ctxEl) {
      try { c = JSON.parse(ctxEl.getAttribute('data-chat-ctx')); } catch (e) { c = {}; }
    }
    return c;
  }

  function send(text) {
    if (!online) { bubble('ai', '客服暂时离线,请稍后再试或通过页面联系方式沟通。'); return; }
    var q = (text || '').trim();
    if (!q) return;
    bubble('user', q);
    input.value = '';
    var wait = bubble('ai', '正在思考…');
    fetch(API_EP, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: q, context: context(), site_key: 'platform' })
    }).then(function (r) {
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    }).then(function (d) {
      wait.remove();
      bubble('ai', (d && (d.reply || d.data && d.data.reply)) || '抱歉,我没有理解,换个问法试试?');
    }).catch(function () {
      wait.remove();
      bubble('ai', '网络开小差了,请稍后再试。');
    });
  }

  function toggle(open) {
    var willOpen = open != null ? open : panel.hidden;
    panel.hidden = !willOpen;
    fab.style.display = willOpen ? 'none' : '';
    if (willOpen && !msgs.children.length) welcome();
  }
  root.querySelectorAll('[data-chat-toggle]').forEach(function (el) {
    el.addEventListener('click', function () { toggle(panel.hidden); });
  });
  form.addEventListener('submit', function (e) { e.preventDefault(); send(input.value); });

  /* 端点健康探测:不可用 → 隐藏整个浮窗(不打扰),可用 → 显示 */
  fetch(API_EP, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: '__ping__', probe: true, site_key: 'platform' }) })
    .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
    .then(function () { online = true; root.hidden = false; })
    .catch(function (e) {
      root.hidden = true;
      if (window.console && console.info) console.info('[SB] site-chat endpoint not ready, bubble hidden:', String(e && e.message || e));
    });
})();
