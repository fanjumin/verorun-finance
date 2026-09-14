/* ════════════════════════════════════════════════════════════
   Site Builder — sb-blocks.js (2026-09 控件升级)
   控件运行时:图库灯箱/轮播/图表(ECharts 按需)/代码复制/FAQ/标签页/
   数字滚动/倒计时/打字机/受信域名嵌入/留资表单/商品评价拉取。
   - 零第三方依赖(图表除外,按需加载平台已 vendored 的 /static/lib/echarts.min.js)
   - 全部输出走 textContent / esc(),绝不 innerHTML 拼接不可信数据
   - 端点不可用一律静默降级(空态/隐藏),绝不抛错打断页面
   ════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function on(el, ev, fn) { if (el) el.addEventListener(ev, fn); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function num(v, dflt) { var n = Number(v); return isFinite(n) ? n : dflt; }
  function cssVar(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (e) { return fallback; }
  }
  /* 与服务器端 block_schemas 保持一致的受信嵌入域名白名单 */
  var EMBED_HOSTS = [
    'youtube.com', 'www.youtube.com', 'youtu.be', 'www.youtube-nocookie.com',
    'vimeo.com', 'player.vimeo.com',
    'bilibili.com', 'www.bilibili.com', 'player.bilibili.com',
    'maps.google.com', 'google.com', 'amap.com', 'www.amap.com', 'uri.amap.com',
    'ditu.amap.com', 'map.qq.com', 'openstreetmap.org', 'www.openstreetmap.org',
    'open.work.weixin.qq.com', 'docs.google.com', 'calendar.google.com',
    'tencentvideo.tv', 'www.tencentvideo.tv', 'v.qq.com'
  ];
  function embedAllowed(url) {
    var m = /^https?:\/\/([^/?#]+)/i.exec(String(url || '').trim());
    if (!m) return false;
    var host = m[1].toLowerCase();
    return EMBED_HOSTS.indexOf(host) !== -1;
  }
  function embedSrc(url) {
    /* 主流平台分享链接 → 可嵌入地址(尽力转换,失败则原样交给 iframe) */
    var u = String(url || '').trim();
    var m;
    m = /youtu\.be\/([\w-]+)/i.exec(u); if (m) return 'https://www.youtube.com/embed/' + m[1];
    m = /(?:youtube\.com\/watch\?v=|youtube\.com\/embed\/)([\w-]+)/i.exec(u); if (m) return 'https://www.youtube.com/embed/' + m[1];
    m = /player\.vimeo\.com\/video\/(\d+)/i.exec(u); if (m) return 'https://player.vimeo.com/video/' + m[1];
    m = /vimeo\.com\/(\d+)/i.exec(u); if (m) return 'https://player.vimeo.com/video/' + m[1];
    m = /bilibili\.com\/video\/(BV[\w]+)/i.exec(u); if (m) return 'https://player.bilibili.com/player.html?bvid=' + m[1] + '&page=1&high_quality=1&danmaku=0';
    return u;
  }

  /* ── 通用:进入视口显现(轻量;reduced-motion 由 CSS 接管) ── */
  function initReveal() {
    var els = $$('[data-sb-reveal]');
    if (!els.length) return;
    if (!('IntersectionObserver' in window)) {
      els.forEach(function (el) { el.classList.add('is-in'); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { en.target.classList.add('is-in'); io.unobserve(en.target); }
      });
    }, { threshold: 0.12 });
    els.forEach(function (el) { io.observe(el); });
  }

  /* ── 图库灯箱 ── */
  var lightbox = null;
  function ensureLightbox() {
    if (lightbox) return lightbox;
    lightbox = document.createElement('div');
    lightbox.className = 'sb-lightbox';
    lightbox.hidden = true;
    lightbox.innerHTML =
      '<img alt="大图预览"/>' +
      '<button type="button" class="sb-lightbox-btn sb-lightbox-close" aria-label="关闭">×</button>' +
      '<button type="button" class="sb-lightbox-btn sb-lightbox-prev" aria-label="上一张">‹</button>' +
      '<button type="button" class="sb-lightbox-btn sb-lightbox-next" aria-label="下一张">›</button>' +
      '<div class="sb-lightbox-count"></div>';
    document.body.appendChild(lightbox);
    var img = $('img', lightbox), count = $('.sb-lightbox-count', lightbox);
    var items = [], idx = 0;
    function show(i) {
      if (!items.length) return;
      idx = (i + items.length) % items.length;
      img.src = items[idx];
      img.alt = '图片 ' + (idx + 1) + ' / ' + items.length;
      count.textContent = (idx + 1) + ' / ' + items.length;
    }
    function close() { lightbox.hidden = true; document.body.style.overflow = ''; }
    on($('.sb-lightbox-close', lightbox), 'click', close);
    on($('.sb-lightbox-prev', lightbox), 'click', function () { show(idx - 1); });
    on($('.sb-lightbox-next', lightbox), 'click', function () { show(idx + 1); });
    on(lightbox, 'click', function (e) { if (e.target === lightbox) close(); });
    on(document, 'keydown', function (e) {
      if (lightbox.hidden) return;
      if (e.key === 'Escape') close();
      if (e.key === 'ArrowLeft') show(idx - 1);
      if (e.key === 'ArrowRight') show(idx + 1);
    });
    lightbox._show = function (urls, i) { items = urls; show(i); lightbox.hidden = false; document.body.style.overflow = 'hidden'; };
    return lightbox;
  }
  function initGallery() {
    $$('.sb-gallery').forEach(function (g) {
      var thumbs = $$('.sb-gallery-item', g);
      if (!thumbs.length) return;
      var urls = thumbs.map(function (t) { return t.getAttribute('data-sb-lightbox') || ''; });
      thumbs.forEach(function (t, i) {
        on(t, 'click', function () { var lb = ensureLightbox(); if (lb._show) lb._show(urls, i); });
      });
    });
  }

  /* ── 轮播 ── */
  function initCarousel() {
    $$('.sb-carousel').forEach(function (c) {
      var track = $('[data-sb-carousel-track]', c);
      var dots = $$('[data-sb-carousel-dot]', c);
      var slides = $$('.sb-carousel-slide', c);
      if (!track || !slides.length) return;
      var i = 0, timer = null, touchX = null;
      var autoplay = String(c.getAttribute('data-autoplay') || '1') !== '0';
      var interval = num(c.getAttribute('data-interval'), 4500);
      function go(n) {
        i = (n + slides.length) % slides.length;
        track.style.transform = 'translateX(-' + (i * 100) + '%)';
        dots.forEach(function (d, di) { d.classList.toggle('is-active', di === i); });
      }
      function play() { if (!autoplay) return; stop(); timer = setInterval(function () { go(i + 1); }, interval); }
      function stop() { if (timer) { clearInterval(timer); timer = null; } }
      on($('[data-sb-carousel-next]', c), 'click', function () { go(i + 1); play(); });
      on($('[data-sb-carousel-prev]', c), 'click', function () { go(i - 1); play(); });
      dots.forEach(function (d) { on(d, 'click', function () { go(num(d.getAttribute('data-sb-carousel-dot')), 0); play(); }); });
      /* 触摸滑动 */
      on(track, 'touchstart', function (e) { touchX = e.touches[0].clientX; stop(); }, { passive: true });
      on(track, 'touchend', function (e) {
        if (touchX === null) return;
        var dx = e.changedTouches[0].clientX - touchX;
        if (dx < -36) go(i + 1); else if (dx > 36) go(i - 1);
        touchX = null; play();
      });
      on(c, 'mouseenter', stop);
      on(c, 'mouseleave', play);
      play();
    });
  }

  /* ── 图表(ECharts 按需) ── */
  var echartsPromise = null;
  function loadECharts() {
    if (window.echarts) return Promise.resolve(window.echarts);
    if (echartsPromise) return echartsPromise;
    echartsPromise = new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = '/static/lib/echarts.min.js';
      s.async = true;
      s.onload = function () { resolve(window.echarts); };
      s.onerror = function () { reject(new Error('echarts load failed')); };
      document.head.appendChild(s);
    });
    return echartsPromise;
  }
  function chartColors(palette) {
    if (palette === 'custom') return null;
    var primary = cssVar('--color-primary', '#f0a020');
    var accent = cssVar('--color-accent', '#f97316');
    var blue = cssVar('--color-secondary', cssVar('--color-text-high', '#5b9dff'));
    return [primary, blue, accent, '#3ddc97', '#a78bfa', '#22d3ee', '#f472b6', '#fbbf24'];
  }
  function mountCharts() {
    $$('[data-sb-chart]').forEach(function (el) {
      var cfg = null;
      try { cfg = JSON.parse(el.getAttribute('data-config') || '{}'); } catch (e) { cfg = {}; }
      if (!cfg || !cfg.series) return;
      loadECharts().then(function () {
        var chart = window.echarts.init(el, null, { renderer: 'canvas' });
        var palette = chartColors(cfg.palette);
        var textColor = cssVar('--color-text-secondary', '#94a3b8');
        var splitColor = 'rgba(148,163,184,.14)';
        var labels = cfg.labels || [];
        var series = (cfg.series || []).map(function (it) {
          var base = {
            name: it.name || '',
            type: (cfg.kind === 'area' || cfg.kind === 'donut') ? 'line' : cfg.kind,
            data: it.data || [],
            smooth: cfg.smooth ? 0.35 : false
          };
          if (cfg.kind === 'pie' || cfg.kind === 'donut') {
            base.type = 'pie';
            base.radius = cfg.kind === 'donut' ? ['42%', '68%'] : '68%';
            base.data = (it.data || []).map(function (v, vi) {
              return { name: labels[vi] || it.name + ' ' + (vi + 1), value: v };
            });
            base.label = { color: textColor };
          }
          if (cfg.kind === 'area') base.areaStyle = { opacity: 0.25 };
          return base;
        });
        var opt = {
          backgroundColor: 'transparent',
          color: palette || undefined,
          textStyle: { color: textColor },
          tooltip: { trigger: cfg.kind === 'pie' || cfg.kind === 'donut' ? 'item' : 'axis' },
          legend: series.length > 1 ? { textStyle: { color: textColor }, top: 0 } : undefined,
          grid: { left: 10, right: 16, top: series.length > 1 ? 44 : 18, bottom: 6, containLabel: true },
          xAxis: (cfg.kind === 'pie' || cfg.kind === 'donut') ? undefined : {
            type: 'category', data: labels,
            axisLine: { lineStyle: { color: splitColor } },
            axisLabel: { color: textColor }
          },
          yAxis: (cfg.kind === 'pie' || cfg.kind === 'donut') ? undefined : {
            type: 'value', splitLine: { lineStyle: { color: splitColor } },
            axisLabel: { color: textColor }
          },
          series: series
        };
        chart.setOption(opt);
        var ro = new ResizeObserver(function () { chart.resize(); });
        ro.observe(el);
        el._sbChart = { chart: chart, ro: ro };
      }).catch(function () {
        el.innerHTML = '<p class="sb-empty" style="text-align:center;padding:30px 0">图表组件加载失败,请稍后刷新</p>';
      });
    });
  }

  /* ── 代码块 ── */
  function initCode() {
    $$('.sb-code').forEach(function (c) {
      var pre = $('.sb-code-pre', c);
      var body = $('[data-sb-code-body]', c);
      if (!pre || !body) return;
      if (pre.classList.contains('has-lines')) {
        var lines = body.textContent.split('\n');
        var html = '';
        for (var i = 0; i < lines.length; i++) {
          html += '<span class="sb-line">' + esc(lines[i].replace(/^(\s*)/, '$1')) + '</span>';
        }
        body.innerHTML = html;
      }
      var btn = $('[data-sb-code-copy]', c);
      if (btn) {
        on(btn, 'click', function () {
          var txt = body.textContent;
          function done() { btn.textContent = '已复制'; setTimeout(function () { btn.textContent = '复制'; }, 1600); }
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(txt).then(done).catch(function () { fallbackCopy(txt); done(); });
          } else { fallbackCopy(txt); done(); }
        });
      }
    });
  }
  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (e) { /* noop */ }
    ta.remove();
  }

  /* ── FAQ ── */
  function initFaq() {
    $$('[data-sb-faq]').forEach(function (faq) {
      $$('.sb-faq-q', faq).forEach(function (q) {
        on(q, 'click', function () {
          var item = q.parentNode;
          var ans = item ? $('.sb-faq-a', item) : null;
          var wasOpen = q.getAttribute('aria-expanded') === 'true';
          $$('.sb-faq-q', faq).forEach(function (o) {
            o.setAttribute('aria-expanded', 'false');
            var oi = o.parentNode;
            var oa = oi ? $('.sb-faq-a', oi) : null;
            if (oa) oa.hidden = true;
          });
          if (!wasOpen) {
            q.setAttribute('aria-expanded', 'true');
            if (ans) ans.hidden = false;
          }
        });
      });
    });
  }

  /* ── Tabs ── */
  function initTabs() {
    $$('[data-sb-tabs]').forEach(function (tabs) {
      var tabsEls = $$('[data-sb-tab]', tabs);
      var panes = $$('[data-sb-pane]', tabs);
      tabsEls.forEach(function (tb) {
        on(tb, 'click', function () {
          var idx = num(tb.getAttribute('data-sb-tab'), 0);
          tabsEls.forEach(function (t) { t.classList.toggle('is-active', t === tb); t.setAttribute('aria-selected', t === tb ? 'true' : 'false'); });
          panes.forEach(function (p) { p.classList.toggle('is-active', num(p.getAttribute('data-sb-pane'), -1) === idx); });
        });
      });
    });
  }

  /* ── 数字滚动 ── */
  function initCounters() {
    var groups = $$('[data-sb-counter-group]');
    if (!groups.length) return;
    var duration = num(groups[0].getAttribute('data-duration'), 1600);
    function run(group) {
      $$('[data-sb-counter-value]', group).forEach(function (el) {
        var target = num(el.getAttribute('data-sb-counter-value'), 0);
        var prefix = el.getAttribute('data-prefix') || '';
        var suffix = el.getAttribute('data-suffix') || '';
        var decimals = String(target).indexOf('.') >= 0 ? (String(target).split('.')[1] || '').length : 0;
        var t0 = null;
        function fmt(v) { el.textContent = prefix + v.toFixed(decimals) + suffix; }
        function step(ts) {
          if (!t0) t0 = ts;
          var p = Math.min((ts - t0) / duration, 1);
          var ease = 1 - Math.pow(1 - p, 3);
          fmt(target * ease);
          if (p < 1) requestAnimationFrame(step);
        }
        requestAnimationFrame(step);
      });
    }
    if (!('IntersectionObserver' in window)) { groups.forEach(run); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { run(en.target); io.unobserve(en.target); }
      });
    }, { threshold: 0.3 });
    groups.forEach(function (g) { io.observe(g); });
  }

  /* ── 倒计时 ── */
  function initCountdown() {
    $$('[data-sb-countdown]').forEach(function (el) {
      var target = new Date(String(el.getAttribute('data-sb-countdown') || '')).getTime();
      var labelEl = $('[data-sb-cd-label]', el);
      if (isNaN(target)) { el.hidden = true; return; }
      function pad(n) { return n < 10 ? '0' + n : String(n); }
      function tick() {
        var diff = target - Date.now();
        if (diff <= 0) {
          el.innerHTML = '<p class="sb-empty">' + esc(el.getAttribute('data-expired') || '活动已结束') + '</p>';
          return;
        }
        var d = Math.floor(diff / 86400000);
        var h = Math.floor(diff % 86400000 / 3600000);
        var m = Math.floor(diff % 3600000 / 60000);
        var s = Math.floor(diff % 60000 / 1000);
        var map = { d: pad(d), h: pad(h), m: pad(m), s: pad(s) };
        $$('[data-cd]', el).forEach(function (cell) { cell.textContent = map[cell.getAttribute('data-cd')]; });
      }
      tick();
      setInterval(tick, 1000);
    });
  }

  /* ── fx_hero 打字机 ── */
  function initTypewriter() {
    $$('[data-sb-fxhero]').forEach(function (hero) {
      var phrases = [];
      try { phrases = JSON.parse(hero.getAttribute('data-typing') || '[]'); } catch (e) { phrases = []; }
      var out = $('[data-sb-typewriter]', hero);
      if (!out) return;
      if (!phrases.length || hero.getAttribute('data-sb-fxhero') !== 'typewriter') {
        out.hidden = true;
        return;
      }
      var pi = 0, ci = 0, deleting = false;
      function type() {
        var word = phrases[pi] || '';
        if (!deleting) {
          ci++;
          out.textContent = word.slice(0, ci);
          if (ci >= word.length) { deleting = true; setTimeout(type, 1600); return; }
          setTimeout(type, 90);
        } else {
          ci--;
          out.textContent = word.slice(0, ci);
          if (ci <= 0) { deleting = false; pi = (pi + 1) % phrases.length; setTimeout(type, 350); return; }
          setTimeout(type, 45);
        }
      }
      setTimeout(type, 600);
    });
  }

  /* ── embed:受信域名 + 懒加载 ── */
  function initEmbeds() {
    $$('[data-sb-embed]').forEach(function (el) {
      var url = el.getAttribute('data-sb-embed') || '';
      if (!embedAllowed(url)) {
        el.innerHTML = '<div class="sb-embed-blocked">该嵌入来源不在受信域名白名单内,已阻止加载。</div>';
        return;
      }
      var src = embedSrc(url);
      var frame = document.createElement('iframe');
      frame.setAttribute('loading', 'lazy');
      frame.setAttribute('allowfullscreen', '');
      frame.setAttribute('referrerpolicy', 'strict-origin-when-cross-origin');
      frame.setAttribute('title', '嵌入内容');
      frame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-presentation allow-popups');
      function inject() {
        el.innerHTML = '';
        el.appendChild(frame);
        frame.src = src;
      }
      if (!('IntersectionObserver' in window)) { inject(); return; }
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          if (en.isIntersecting) { inject(); io.disconnect(); }
        });
      }, { rootMargin: '200px' });
      io.observe(el);
    });
  }

  /* ── 留资表单 ── */
  function initForms() {
    $$('[data-sb-form]').forEach(function (form) {
      var status = $('[data-sb-form-status]', form);
      function setStatus(msg, kind) {
        if (!status) return;
        status.textContent = msg || '';
        status.className = 'sb-form-status' + (kind ? ' ' + kind : '');
      }
      on(form, 'submit', function (e) {
        e.preventDefault();
        var valid = true;
        $$('input,textarea', form).forEach(function (f) {
          var err = f.parentNode.querySelector('.sb-field-err');
          f.classList.remove('invalid');
          if (f.name === 'company_website') return; /* honeypot */
          if (f.required && !f.value.trim()) { valid = false; f.classList.add('invalid'); }
          if (f.type === 'email' && f.value.trim() && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(f.value.trim())) { valid = false; f.classList.add('invalid'); }
          if (err) err.classList.toggle('show', !!f.classList.contains('invalid'));
        });
        var hasContact = false;
        var em = form.querySelector('[name="email"]'); var ph = form.querySelector('[name="phone"]');
        var nm = form.querySelector('[name="name"]');
        if (nm && nm.value.trim() && ((em && em.value.trim()) || (ph && ph.value.trim()))) hasContact = true;
        if (!hasContact) { valid = false; setStatus('请填写称呼与至少一种联系方式(邮箱/电话)', 'err'); }
        if (!valid) { setStatus('请填写必填项并检查格式', 'err'); return; }
        var payload = {};
        var hp = form.querySelector('[name="company_website"]');
        if (hp && hp.value) { setStatus('提交成功', 'ok'); form.reset(); return; }
        $$('input,textarea', form).forEach(function (f) { if (f.name) payload[f.name] = f.value.trim(); });
        var btn = form.querySelector('[type="submit"]');
        if (btn) { btn.disabled = true; }
        setStatus('提交中…', '');
        fetch('/page/api/contact', {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        }).then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { return { ok: r.ok, d: d }; }); })
          .then(function (res) {
            if (res.ok && res.d && res.d.success) {
              setStatus('提交成功,我们会尽快与您联系', 'ok');
              form.reset();
            } else {
              setStatus((res.d && res.d.error) || '提交失败,请稍后重试', 'err');
            }
          })
          .catch(function () { setStatus('网络错误,请稍后重试', 'err'); })
          .then(function () { if (btn) btn.disabled = false; });
      });
    });
  }

  /* ── 商品评价(reviews 插件公开 API) ── */
  function initLiveReviews() {
    $$('[data-sb-reviews]').forEach(function (box) {
      var pid = box.getAttribute('data-product-id');
      if (!pid) return;
      fetch('/plugin/reviews/api/' + encodeURIComponent(pid), { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error(String(r.status))); })
        .then(function (d) {
          var data = d && (d.data || d);
          var list = data && (data.reviews || data.items || data.list);
          if (!Array.isArray(list)) list = [];
          if (!list.length) {
            box.innerHTML = '<p class="sb-empty" style="text-align:center">该商品暂时还没有评价,来抢首评吧</p>';
            return;
          }
          var avg = 0, sum = 0, dist = { 5: 0, 4: 0, 3: 0, 2: 0, 1: 0 };
          list.forEach(function (r) {
            var star = Math.max(1, Math.min(5, num(r.rating || r.score || 5, 5)));
            sum += star; dist[star] = (dist[star] || 0) + 1;
          });
          avg = (sum / list.length).toFixed(1);
          var html = '<div class="sb-reviews-summary"><div><div class="sb-rv-avg">' + avg + '</div>' +
            '<div class="sb-rv-meta">' + esc((data.rating_count || data.count || list.length)) + ' 条评价</div></div><div class="sb-rv-bars">';
          for (var s = 5; s >= 1; s--) {
            var pct = Math.round((dist[s] / list.length) * 100);
            html += '<div class="sb-rv-bar">' + s + ' 星<i><em style="width:' + pct + '%"></em></i>' + dist[s] + '</div>';
          }
          html += '</div></div><div class="sb-reviews-grid">';
          list.forEach(function (r) {
            var name = String(r.user_name || r.nickname || r.author || '匿名用户') || '匿名用户';
            var text = String(r.content || r.text || '');
            var stars = '';
            var st = Math.max(1, Math.min(5, num(r.rating || r.score || 5, 5)));
            for (var k = 0; k < 5; k++) stars += k < st ? '★' : '☆';
            html += '<div class="sb-review-card"><div class="sb-review-head">' +
              '<span class="sb-review-avatar sb-review-avatar-text">' + esc(name.slice(0, 1)) + '</span>' +
              '<div><div class="sb-review-name">' + esc(name) + '</div><div class="sb-review-stars">' + stars + '</div></div></div>' +
              '<p class="sb-review-text">' + esc(text) + '</p></div>';
          });
          html += '</div>';
          box.innerHTML = html;
        })
        .catch(function () {
          box.innerHTML = '<p class="sb-empty" style="text-align:center">评价加载失败,请稍后刷新查看</p>';
        });
    });
  }

  /* ── 启动 ── */
  function boot() {
    initReveal();
    initGallery();
    initCarousel();
    initCode();
    initFaq();
    initTabs();
    initCounters();
    initCountdown();
    initTypewriter();
    initEmbeds();
    initForms();
    initLiveReviews();
    mountCharts();
  }
  if (document.readyState === 'complete' || document.readyState === 'interactive') boot();
  else document.addEventListener('DOMContentLoaded', boot);

  window.SBBlocks = {
    boot: boot,
    esc: esc,
    embedAllowed: embedAllowed
  };
})();
