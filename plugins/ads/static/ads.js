/**
 * Ad Management Plugin — 前端广告渲染与埋点 (v0.2.0)
 *
 * 用法：
 *   1. 服务端渲染：直接由 Jinja2 宏注入 HTML
 *   2. 异步渲染：页面中放置 <div data-ad-position="sidebar" data-ad-page="plaza"></div>
 *      并调用 ads.autoRender() 或 ads.renderAds('[data-ad-position]')
 */
(function (window) {
  'use strict';

  var API_BASE = '/admin/ads/api/v1';

  /**
   * 获取当前页面标识（从 data-ad-page 或 body data-page）
   */
  function _detectPage(el) {
    return el.getAttribute('data-ad-page')
      || document.body.getAttribute('data-page')
      || window.location.pathname.split('/').pop()
      || '*';
  }

  /**
   * 构建带追踪参数的跳转链接
   */
  function _buildClickUrl(ad) {
    var url = ad.link_url || '';
    if (!url) return url;
    if (ad.utm_source || ad.click_tag) {
      var sep = url.indexOf('?') === -1 ? '?' : '&';
      var params = [];
      if (ad.utm_source) params.push('utm_source=' + encodeURIComponent(ad.utm_source));
      if (ad.click_tag) params.push('ad_tag=' + encodeURIComponent(ad.click_tag));
      url += sep + params.join('&');
    }
    return url;
  }

  /**
   * 上报一次展示
   */
  function trackImpression(adId, page, position) {
    if (!adId) return;
    try {
      navigator.sendBeacon && navigator.sendBeacon(
        API_BASE + '/stats/impression',
        JSON.stringify({ ad_id: adId, page: page || '', position: position || '' })
      );
    } catch (e) {}
  }

  /**
   * 上报一次点击
   */
  function trackClick(adId, page, position) {
    if (!adId) return;
    try {
      fetch(API_BASE + '/stats/click', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ad_id: adId, page: page || '', position: position || '' }),
        keepalive: true
      }).catch(function () {});
    } catch (e) {}
  }

  /**
   * 渲染单个广告元素
   */
  function _renderAdItem(ad) {
    var isImage = ad.ad_type === 'image';
    var clickUrl = _buildClickUrl(ad);
    var wrap = document.createElement('div');
    wrap.className = 'ad-item ad-type-' + ad.ad_type;
    wrap.setAttribute('data-ad-id', ad.id);
    wrap.style.marginBottom = '12px';

    if (isImage) {
      var style = '';
      if (ad.width) style += 'width:' + ad.width + 'px;max-width:100%;';
      if (ad.height) style += 'height:' + ad.height + 'px;';
      var a = document.createElement('a');
      a.href = clickUrl || 'javascript:void(0)';
      a.target = '_blank';
      a.rel = 'nofollow sponsored';
      a.className = 'ad-link';
      var img = document.createElement('img');
      img.src = ad.image_url || '';
      img.alt = ad.name || '';
      img.loading = 'lazy';
      img.style.cssText = style;
      a.appendChild(img);
      wrap.appendChild(a);

      // 绑定点击事件（图片类型才二次跳转与埋点）
      if (clickUrl) {
        a.addEventListener('click', function () {
          trackClick(ad.id, ad.page, ad.position);
        });
      }
    } else {
      // 代码类广告：在 sandbox iframe 中渲染（allow-scripts 但不含 allow-same-origin），
      // 第三方脚本运行于不透明源中，无法访问父页面 DOM / Cookie，防存储型 XSS 外溢。
      // 注：部分依赖同源上下文的广告联盟（如 AdSense）可能因此不渲染。
      var iframe = document.createElement('iframe');
      iframe.className = 'ad-code';
      iframe.setAttribute('sandbox', 'allow-scripts allow-popups allow-forms allow-popups-to-escape-sandbox');
      iframe.setAttribute('loading', 'lazy');
      iframe.style.border = '0';
      iframe.style.width = '100%';
      if (ad.width) iframe.style.width = ad.width + 'px';
      if (ad.height) iframe.style.height = ad.height + 'px';
      wrap.appendChild(iframe);
      // 通过 DOM 属性设置 srcdoc，ad_code 在 iframe 独立文档中解析，不进入父页面
      iframe.srcdoc = '<!DOCTYPE html><html><head></head><body>' + (ad.ad_code || '') + '</body></html>';
    }

    return wrap;
  }

  /**
   * 在指定容器内渲染广告
   */
  function renderContainer(container) {
    if (!container) return;
    var position = container.getAttribute('data-ad-position') || '';
    var page = _detectPage(container);
    var siteKey = container.getAttribute('data-ad-site-key')
      || document.body.getAttribute('data-site-key')
      || 'default';
    var zoneId = container.getAttribute('data-ad-zone-id');

    var url = API_BASE + '/ads?page=' + encodeURIComponent(page)
            + '&position=' + encodeURIComponent(position)
            + '&site_key=' + encodeURIComponent(siteKey);
    if (zoneId) url += '&zone_id=' + encodeURIComponent(zoneId);

    fetch(url)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.success) return;
        var ads = d.data || [];
        container.innerHTML = '';
        if (!ads.length) {
          container.style.display = 'none';
          return;
        }
        container.style.display = '';
        ads.forEach(function (ad) {
          container.appendChild(_renderAdItem(ad));
          trackImpression(ad.id, page, position);
        });
      })
      .catch(function (e) {
        console.warn('[Ads] render failed:', e);
      });
  }

  /**
   * 批量渲染广告容器
   */
  function renderAds(selector) {
    var containers = document.querySelectorAll(selector || '[data-ad-position]');
    if (!containers.length) return;  // 页面无广告位时提前退出，避免无谓扫描
    containers.forEach(renderContainer);
  }

  /**
   * 自动渲染页面中所有广告位
   */
  function autoRender() {
    renderAds('[data-ad-position]');
  }

  // DOMReady 自动渲染
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autoRender);
  } else {
    autoRender();
  }

  window.ads = {
    renderAds: renderAds,
    renderContainer: renderContainer,
    autoRender: autoRender,
    trackImpression: trackImpression,
    trackClick: trackClick
  };
})(window);
