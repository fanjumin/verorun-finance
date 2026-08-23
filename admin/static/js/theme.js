/* ════════════════════════════════════════════════════════
   VeroRun Admin — 三主题实时切换 (theme.js)
   通过 <html data-theme> + localStorage 实现即时换肤
   顶栏并列 3 个图标按钮，点选即切换（深/中/亮）
   ════════════════════════════════════════════════════════ */
(function () {
  var KEY = 'verorun_admin_theme';
  var THEMES = ['dark', 'slate', 'light'];

  // 读取当前主题，非法值回退到 slate（默认灰色）
  function current() {
    var t = localStorage.getItem(KEY);
    return THEMES.indexOf(t) >= 0 ? t : 'slate';
  }

  // 应用主题：设置 data-theme + 记忆 + 高亮当前按钮
  function apply(t) {
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem(KEY, t); } catch (e) {}
    var btns = document.querySelectorAll('.ts-btn');
    for (var i = 0; i < btns.length; i++) {
      btns[i].classList.toggle('active', btns[i].getAttribute('data-theme') === t);
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    apply(current());
    var btns = document.querySelectorAll('.ts-btn');
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener('click', function () {
        apply(this.getAttribute('data-theme'));
      });
    }
  });
})();
