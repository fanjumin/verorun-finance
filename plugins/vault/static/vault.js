/* ══════════════════════════════════════════════════════════════
   Vault 2.0 — Plugin JavaScript
   Dashboard + Restore + Schedules + Storage + Audit + Settings
   V20260819: inherit Admin three-theme (dark/light/slate) via
   localStorage 'verorun_admin_theme' + storage event sync.
   ══════════════════════════════════════════════════════════════ */

// ── Theme sync from Admin SPA (same-origin iframe) ──
(function () {
  try {
    var t = localStorage.getItem('verorun_admin_theme');
    if (t === 'light' || t === 'slate') document.documentElement.setAttribute('data-theme', t);
    window.addEventListener('storage', function (e) {
      if (e.key !== 'verorun_admin_theme') return;
      var v = e.newValue;
      if (v === 'light' || v === 'slate') document.documentElement.setAttribute('data-theme', v);
      else document.documentElement.removeAttribute('data-theme');
    });
  } catch (e) {}
})();

// ── Resolve CSS variables for canvas/ECharts colors ──
function vCssVar(name) {
  var v = getComputedStyle(document.documentElement).getPropertyValue(name);
  return v ? v.trim() : '';
}
function vHexToRgba(hex, alpha) {
  var m = /^#?([0-9a-f]{6})$/i.exec((hex || '').trim());
  if (!m) return hex;
  var n = parseInt(m[1], 16);
  return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + alpha + ')';
}

(function () {
  'use strict';

  // ── State ──
  var state = {
    backups: [],
    health: null,
    page: 1,
    perPage: 20,
    totalPages: 1,
    // restore
    restoreLabel: null,
    // audit
    auditPage: 0,
    auditLimit: 50,
  };

  // ── Page Detection ──
  var pageId = (function () {
    var path = window.location.pathname;
    if (/\/schedules/.test(path)) return 'schedules';
    if (/\/storage/.test(path)) return 'storage';
    if (/\/restore/.test(path)) return 'restore';
    if (/\/audit/.test(path)) return 'audit';
    if (/\/settings/.test(path)) return 'settings';
    return 'dashboard';
  })();

  // ── Toast ──
  function toast(msg, type) {
    type = type || 'info';
    // Embedded in admin shell → reuse the system toast for a unified style
    try {
      if (window.top !== window && window.top.showToast) {
        window.top.showToast(msg, type === 'warning' ? 'error' : type);
        return;
      }
    } catch (e) { /* cross-origin guard */ }
    // Standalone fallback (page opened directly): system-styled local toast
    var el = document.createElement('div');
    el.className = 'toast toast-' + type;
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 4000);
  }

  // Styled confirmation modal (aligned with admin shell .modal-overlay/.modal-box)
  function vaultConfirm(title, message, confirmLabel, onConfirm) {
    var overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML =
      '<div class="modal-box" onclick="event.stopPropagation()">' +
      '<h3>' + escHtml(title) + '</h3>' +
      '<div class="modal-message">' + escHtml(message) + '</div>' +
      '<div class="modal-actions">' +
      '<button class="btn btn-outline btn-sm" id="vaultConfirmCancel">Cancel</button>' +
      '<button class="btn btn-danger btn-sm" id="vaultConfirmOk">' + escHtml(confirmLabel || 'OK') + '</button>' +
      '</div></div>';
    document.body.appendChild(overlay);
    document.getElementById('vaultConfirmCancel').addEventListener('click', function () {
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    });
    document.getElementById('vaultConfirmOk').addEventListener('click', function () {
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
      onConfirm();
    });
    overlay.addEventListener('click', function (e) {
      if (e.target === overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
    });
  }

  // ── API helper ──
  function getCookie(name) {
    var m = document.cookie.match(new RegExp('(^|;\\s*)' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[2]) : '';
  }

  function api(url, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    // CSRF: 状态变更请求附加 X-CSRF-Token 头（双重提交 Cookie 模式）
    if (opts.method && opts.method.toUpperCase() !== 'GET') {
      opts.headers['X-CSRF-Token'] = getCookie('csrf_token');
    }
    if (opts.body && typeof opts.body !== 'string') {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }
    return fetch(url, opts).then(function (r) {
      if (!r.ok) {
        return r.json().then(function (d) {
          throw new Error(d.error || 'Request failed');
        });
      }
      return r.json();
    });
  }

  // ── XSS escape ──
  function escHtml(str) {
    var div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  function escAttr(str) {
    return String(str).replace(/&/g, '&amp;').replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // ══════════════════════════════════════════════════════════════
  // DASHBOARD
  // ══════════════════════════════════════════════════════════════

  function loadHealth() {
    api('/admin/vault/api/health')
      .then(function (data) {
        state.health = data.health;
        var score = document.getElementById('healthScore');
        if (score) {
          score.textContent = data.health.score + '/100';
          score.style.color =
            data.health.score >= 80 ? 'var(--green)' :
            data.health.score >= 50 ? 'var(--gold)' : 'var(--rose)';
        }
        var storage = document.getElementById('storageUsed');
        if (storage) storage.textContent = data.health.storage.used_percent + '%';
        var lastBackup = document.getElementById('lastBackup');
        if (lastBackup) lastBackup.textContent = data.health.last_backup || '--';
        var nextSchedule = document.getElementById('nextSchedule');
        if (nextSchedule) nextSchedule.textContent = data.health.next_schedule || '--';
      })
      .catch(function (e) { toast('Health check failed: ' + e.message, 'error'); });
  }

  function loadBackups(search, type, status) {
    var params = new URLSearchParams({ page: state.page, per_page: state.perPage });
    if (search) params.set('search', search);
    if (type) params.set('type', type);
    if (status) params.set('status', status);

    api('/admin/vault/api/backup/list?' + params.toString())
      .then(function (data) {
        state.backups = data.backups;
        renderBackupTable(data.backups);
      })
      .catch(function (e) { toast('Failed to load backups: ' + e.message, 'error'); });
  }

  function renderBackupTable(backups) {
    var tbody = document.getElementById('backupTbody');
    if (!tbody) return;
    if (!backups || backups.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty-state">No backups yet</td></tr>';
      return;
    }
    tbody.innerHTML = backups.map(function (b) {
      return '<tr>' +
        '<td><strong>' + escHtml(b.label) + '</strong></td>' +
        '<td><span class="badge badge-' + (b.backup_type === 'full' ? 'success' : 'pending') + '">' + escHtml(b.backup_type) + '</span></td>' +
        '<td>' + b.size_mb + ' MB</td>' +
        '<td><span class="badge badge-' + (b.status === 'success' ? 'success' : 'failed') + '">' + escHtml(b.status) + '</span></td>' +
        '<td>' + escHtml(b.created_at) + '</td>' +
        '<td class="actions-col">' +
        '<a class="btn btn-outline btn-sm" href="/admin/vault/api/backup/download/' + encodeURIComponent(b.label) + '" download>Download</a> ' +
        '<button class="btn btn-danger btn-sm" onclick="deleteBackup(\'' + escAttr(b.label) + '\')">Delete</button>' +
        '</td></tr>';
    }).join('');
  }

  function createBackup(type) {
    var btn = document.getElementById('btnBackupNow');
    if (btn) { btn.disabled = true; btn.textContent = 'Creating...'; }
    api('/admin/vault/api/backup/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: type || 'full', encrypt: false }),
    }).then(function (data) {
      if (data.success) {
        toast('Backup created: ' + data.label + ' (' + data.size_mb + ' MB)', 'success');
        loadBackups();
        loadHealth();
      } else {
        toast('Backup failed: ' + (data.error || 'unknown'), 'error');
      }
    }).catch(function (e) { toast('Backup error: ' + e.message, 'error'); })
      .finally(function () { if (btn) { btn.disabled = false; btn.textContent = '+ Backup Now'; } });
  }

  function deleteBackup(label) {
    vaultConfirm('Delete Backup', 'Delete backup ' + label + '? This cannot be undone.', 'Delete', function () {
      api('/admin/vault/api/backup/delete/' + encodeURIComponent(label), {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: true }),
      }).then(function () { toast('Backup deleted', 'success'); loadBackups(); loadHealth(); })
        .catch(function (e) { toast('Delete failed: ' + e.message, 'error'); });
    });
  }

  function cleanupBackups() {
    vaultConfirm('Cleanup Backups', 'Delete backups older than retention period?', 'Cleanup', function () {
      api('/admin/vault/api/cleanup', { method: 'DELETE' })
        .then(function (data) {
          if (data.success) { toast('Cleaned up ' + data.deleted + ' old backup(s)', 'success'); loadBackups(); loadHealth(); }
          else { toast('Cleanup failed: ' + (data.error || 'unknown'), 'error'); }
        }).catch(function (e) { toast('Cleanup error: ' + e.message, 'error'); });
    });
  }

  // ── ECharts: Trend & Storage Charts ──

  function loadCharts() {
    if (typeof echarts === 'undefined') { console.log('[Vault] echarts not loaded'); return; }

    api('/admin/vault/api/trend')
      .then(function (data) {
        if (!data.trend || data.trend.length === 0) return;

        var dates = data.trend.map(function (d) { return d.date; });
        var sizes = data.trend.map(function (d) { return d.size_mb; });
        var cumulative = [];
        var total = 0;
        sizes.forEach(function (s) { total += s; cumulative.push(parseFloat(total.toFixed(1))); });

        // Chart 1: Backup Size Trend
        var ct = document.getElementById('chartTrend');
        if (ct) {
          var cMuted = vCssVar('--muted') || '#94a3b8';
          var cBorder = vCssVar('--border') || 'rgba(255,255,255,0.05)';
          var cBar = vCssVar('--accent2') || '#6366f1';
          var chartTrend = echarts.init(ct);
          chartTrend.setOption({
            tooltip: { trigger: 'axis' },
            grid: { left: 50, right: 20, top: 20, bottom: 30 },
            xAxis: { type: 'category', data: dates, axisLabel: { color: cMuted, fontSize: 10, rotate: 30 } },
            yAxis: { type: 'value', name: 'MB', axisLabel: { color: cMuted }, splitLine: { lineStyle: { color: cBorder } } },
            series: [{
              data: sizes, type: 'bar',
              itemStyle: { color: cBar, borderRadius: [4, 4, 0, 0] },
            }],
          });
          window.addEventListener('resize', function () { chartTrend.resize(); });
        }

        // Chart 2: Storage Growth (cumulative)
        var cs = document.getElementById('chartStorage');
        if (cs) {
          var cLine = vCssVar('--accent') || '#00f5ff';
          var cMuted2 = vCssVar('--muted') || '#94a3b8';
          var cBorder2 = vCssVar('--border') || 'rgba(255,255,255,0.05)';
          var chartStorage = echarts.init(cs);
          chartStorage.setOption({
            tooltip: { trigger: 'axis' },
            grid: { left: 50, right: 20, top: 20, bottom: 30 },
            xAxis: { type: 'category', data: dates, axisLabel: { color: cMuted2, fontSize: 10, rotate: 30 } },
            yAxis: { type: 'value', name: 'MB', axisLabel: { color: cMuted2 }, splitLine: { lineStyle: { color: cBorder2 } } },
            series: [{
              data: cumulative, type: 'line', smooth: true,
              lineStyle: { color: cLine, width: 2 },
              itemStyle: { color: cLine },
              areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
                colorStops: [{ offset: 0, color: vHexToRgba(cLine, 0.2) }, { offset: 1, color: vHexToRgba(cLine, 0.01) }] } },
            }],
          });
          window.addEventListener('resize', function () { chartStorage.resize(); });
        }
      })
      .catch(function () { /* charts optional */ });
  }

  // ── Drill: Restore Drill ──

  function runDrill() {
    vaultConfirm('Restore Drill', 'Run a restore drill? This will restore the latest backup to a sandbox database, verify it, and clean up. No production data will be affected.', 'Run Drill', function () {
      var btns = document.querySelectorAll('#btnDrill, .js-drill');
      btns.forEach(function (b) { b.disabled = true; b.textContent = 'Drilling...'; });

      api('/admin/vault/api/restore/drill', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      }).then(function (data) {
        if (data.verified) {
          toast('Drill passed: backup is valid and restorable', 'success');
        } else {
          toast('Drill failed: ' + (data.report || data.error || 'verification failed'), 'error');
        }
      }).catch(function (e) { toast('Drill error: ' + e.message, 'error'); })
        .finally(function () { btns.forEach(function (b) { b.disabled = false; b.textContent = 'Drill'; }); });
    });
  }

  // ── PITR (available from Restore page) ──

  function runPitr(targetTime) {
    vaultConfirm('Point-in-Time Recovery', 'Run Point-in-Time Recovery to ' + targetTime + '? This will create a sandbox database.', 'Run PITR', function () {
      api('/admin/vault/api/restore/pitr', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_time: targetTime }),
      }).then(function (data) {
        if (data.success) {
          toast('PITR completed. Sandbox database: ' + data.sandbox_db, 'success');
        } else {
          toast('PITR failed: ' + (data.error || 'unknown'), 'error');
        }
      }).catch(function (e) { toast('PITR error: ' + e.message, 'error'); });
    });
  }

  // ══════════════════════════════════════════════════════════════
  // RESTORE WIZARD
  // ══════════════════════════════════════════════════════════════

  function initRestore() {
    loadRestoreBackups();
    document.getElementById('btnNextStep').addEventListener('click', function () {
      showRestoreStep(2);
    });
    document.getElementById('btnPrevStep2').addEventListener('click', function () {
      showRestoreStep(1);
    });
    document.getElementById('btnPrevStep3').addEventListener('click', function () {
      showRestoreStep(2);
    });
    document.getElementById('btnPreview').addEventListener('click', restorePreview);
    document.getElementById('btnConfirmRestore').addEventListener('click', function () {
      showRestoreStep(3);
      renderRestoreConfirm();
    });
    document.getElementById('btnExecuteRestore').addEventListener('click', executeRestore);
  }

  function loadRestoreBackups() {
    api('/admin/vault/api/backup/list?per_page=100')
      .then(function (data) {
        var tbody = document.getElementById('restoreBackupList');
        if (!tbody) return;
        if (!data.backups || data.backups.length === 0) {
          tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No backups available</td></tr>';
          return;
        }
        tbody.innerHTML = data.backups.map(function (b) {
          return '<tr>' +
            '<td><input type="radio" name="restoreLabel" value="' + escAttr(b.label) + '" onchange="vaultSetRestoreLabel(\'' + escAttr(b.label) + '\')"></td>' +
            '<td><strong>' + escHtml(b.label) + '</strong></td>' +
            '<td><span class="badge badge-' + (b.backup_type === 'full' ? 'success' : 'pending') + '">' + escHtml(b.backup_type) + '</span></td>' +
            '<td>' + b.size_mb + ' MB</td>' +
            '<td>' + escHtml(b.created_at) + '</td>' +
            '</tr>';
        }).join('');
      })
      .catch(function (e) { toast('Failed to load backups: ' + e.message, 'error'); });
  }

  window.vaultSetRestoreLabel = function (label) {
    state.restoreLabel = label;
    document.getElementById('btnNextStep').disabled = false;
  };

  function showRestoreStep(n) {
    for (var i = 1; i <= 3; i++) {
      var panel = document.getElementById('panelStep' + i);
      if (panel) panel.classList.toggle('hidden', i !== n);
    }
    var steps = document.querySelectorAll('#restoreSteps .step');
    steps.forEach(function (s) {
      var sn = parseInt(s.getAttribute('data-step'));
      s.classList.remove('active', 'done');
      if (sn === n) s.classList.add('active');
      else if (sn < n) s.classList.add('done');
    });
  }

  function restorePreview() {
    if (!state.restoreLabel) { toast('Please select a backup first', 'warning'); return; }
    var params = { label: state.restoreLabel };
    // Determine scope (preview always uses full scope to show all contents)
    api('/admin/vault/api/restore/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    }).then(function (data) {
      var previewDiv = document.getElementById('restorePreview');
      var tbody = document.getElementById('restorePreviewBody');
      if (!previewDiv || !tbody) return;
      previewDiv.classList.remove('hidden');

      // Collect file entries from steps
      var entries = [];
      if (data.steps) {
        data.steps.forEach(function (step) {
          if (step.preview) {
            (step.preview || []).forEach(function (f) { entries.push(f); });
          }
        });
      }
      if (entries.length === 0) {
        tbody.innerHTML = '<tr><td colspan="3" class="empty-state">No content found in backup</td></tr>';
        return;
      }
      tbody.innerHTML = entries.map(function (e) {
        var isDir = e.type === 'dir';
        return '<tr>' +
          '<td>' + escHtml(e.name || e) + '</td>' +
          '<td>' + (isDir ? 'Directory' : 'File') + '</td>' +
          '<td>' + (e.size ? e.size + ' B' : '--') + '</td>' +
          '</tr>';
      }).join('');
    }).catch(function (e) { toast('Preview failed: ' + e.message, 'error'); });
  }

  function renderRestoreConfirm() {
    var el = document.getElementById('restoreConfirmInfo');
    if (!el) return;
    var scopes = [];
    if (document.getElementById('scopeDb').checked) scopes.push('Database');
    if (document.getElementById('scopeFiles').checked) scopes.push('Files');
    if (document.getElementById('scopeDryRun').checked) scopes.push('(Preview Only)');
    el.innerHTML = '<p><strong>Backup:</strong> ' + escHtml(state.restoreLabel) + '</p>' +
      '<p><strong>Scope:</strong> ' + escHtml(scopes.join(', ') || 'All') + '</p>';
  }

  function executeRestore() {
    if (!state.restoreLabel) return;
    var scope = {
      restore_db: document.getElementById('scopeDb').checked,
      restore_files: document.getElementById('scopeFiles').checked,
    };
    var dryRun = document.getElementById('scopeDryRun').checked;
    var url = dryRun ? '/admin/vault/api/restore/preview' : '/admin/vault/api/restore';
    api(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: state.restoreLabel, scope: scope }),
    }).then(function (data) {
      if (data.success) {
        toast(dryRun ? 'Preview complete' : 'Restore completed successfully!', 'success');
      } else {
        toast('Operation failed: ' + (data.error || 'unknown'), 'error');
      }
    }).catch(function (e) { toast('Error: ' + e.message, 'error'); });
  }

  // ══════════════════════════════════════════════════════════════
  // SCHEDULES
  // ══════════════════════════════════════════════════════════════

  function initSchedules() {
    loadSchedules();
    document.getElementById('btnNewSchedule').addEventListener('click', function () { showScheduleForm(); });
    document.getElementById('btnCancelSchedule').addEventListener('click', hideScheduleForm);
    document.getElementById('scheduleForm').addEventListener('submit', saveSchedule);
  }

  function loadSchedules() {
    api('/admin/vault/api/schedule/list')
      .then(function (data) {
        var tbody = document.getElementById('scheduleTableBody');
        if (!tbody) return;
        if (!data.schedules || data.schedules.length === 0) {
          tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No schedules configured</td></tr>';
          return;
        }
        tbody.innerHTML = data.schedules.map(function (s) {
          var ret = '';
          if (s.retention_days) ret += s.retention_days + 'd';
          if (s.retention_count) ret += (ret ? ', ' : '') + s.retention_count + 'x';
          return '<tr>' +
            '<td><strong>' + escHtml(s.name) + '</strong></td>' +
            '<td><code>' + escHtml(s.cron_expression) + '</code></td>' +
            '<td><span class="badge badge-pending">' + escHtml(s.backup_type) + '</span></td>' +
            '<td>' + (ret || '--') + '</td>' +
            '<td><span class="badge ' + (s.enabled ? 'badge-success' : 'badge-failed') + '">' + (s.enabled ? 'Active' : 'Disabled') + '</span></td>' +
            '<td>' + (s.next_run_at ? escHtml(s.next_run_at) : '--') + '</td>' +
            '<td class="actions-col">' +
            '<button class="btn btn-outline btn-sm" onclick="vaultToggleSchedule(' + s.id + ', ' + !s.enabled + ')">' + (s.enabled ? 'Disable' : 'Enable') + '</button> ' +
            '<button class="btn btn-danger btn-sm" onclick="vaultDeleteSchedule(' + s.id + ')">Delete</button>' +
            '</td></tr>';
        }).join('');
      }).catch(function (e) { toast('Failed to load schedules: ' + e.message, 'error'); });
  }

  function showScheduleForm(sched) {
    var card = document.getElementById('scheduleFormCard');
    if (!card) return;
    card.classList.remove('hidden');
    document.getElementById('schedId').value = sched ? sched.id : '';
    document.getElementById('scheduleFormTitle').textContent = sched ? 'Edit Schedule' : 'Create Schedule';
    document.getElementById('schedName').value = sched ? sched.name : '';
    document.getElementById('schedCron').value = sched ? sched.cron_expression : '0 3 * * *';
    document.getElementById('schedType').value = sched ? sched.backup_type : 'full';
    document.getElementById('schedRetDays').value = sched ? (sched.retention_days || '') : 30;
    document.getElementById('schedRetCount').value = sched ? (sched.retention_count || '') : '';
  }

  function hideScheduleForm() {
    var card = document.getElementById('scheduleFormCard');
    if (card) card.classList.add('hidden');
  }

  function saveSchedule(e) {
    e.preventDefault();
    var id = document.getElementById('schedId').value;
    var payload = {
      name: document.getElementById('schedName').value,
      cron_expression: document.getElementById('schedCron').value,
      backup_type: document.getElementById('schedType').value,
      retention_days: parseInt(document.getElementById('schedRetDays').value) || null,
      retention_count: parseInt(document.getElementById('schedRetCount').value) || null,
    };

    var isNew = !id;
    var url = isNew ? '/admin/vault/api/schedule/create' : '/admin/vault/api/schedule/' + id;
    var method = isNew ? 'POST' : 'PUT';

    api(url, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then(function (data) {
      if (data.success) {
        toast(isNew ? 'Schedule created' : 'Schedule updated', 'success');
        hideScheduleForm();
        loadSchedules();
      } else {
        toast('Save failed: ' + (data.error || 'unknown'), 'error');
      }
    }).catch(function (e) { toast('Save error: ' + e.message, 'error'); });
  }

  window.vaultToggleSchedule = function (id, enable) {
    api('/admin/vault/api/schedule/' + id + '/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: enable }),
    }).then(function (data) {
      if (data.success) {
        toast('Schedule ' + (enable ? 'enabled' : 'disabled'), 'success');
        loadSchedules();
      } else {
        toast('Toggle failed: ' + (data.error || 'unknown'), 'error');
      }
    }).catch(function (e) { toast('Toggle error: ' + e.message, 'error'); });
  };

  window.vaultDeleteSchedule = function (id) {
    vaultConfirm('Delete Schedule', 'Delete this schedule?', 'Delete', function () {
      api('/admin/vault/api/schedule/' + id, { method: 'DELETE' })
        .then(function (data) {
          if (data.success) { toast('Schedule deleted', 'success'); loadSchedules(); }
          else { toast('Delete failed: ' + (data.error || 'unknown'), 'error'); }
        }).catch(function (e) { toast('Delete error: ' + e.message, 'error'); });
    });
  };

  // ══════════════════════════════════════════════════════════════
  // STORAGE
  // ══════════════════════════════════════════════════════════════

  function initStorage() {
    loadStorageTargets();
    document.getElementById('btnNewStorage').addEventListener('click', function () { showStorageForm(); });
    document.getElementById('btnCancelStorage').addEventListener('click', hideStorageForm);
    document.getElementById('storageForm').addEventListener('submit', saveStorageTarget);
    document.getElementById('btnTestStorage').addEventListener('click', testStorageConnection);
    document.getElementById('storageType').addEventListener('change', renderStorageConfigFields);
  }

  function loadStorageTargets() {
    api('/admin/vault/api/storage/list')
      .then(function (data) {
        var tbody = document.getElementById('storageTableBody');
        if (!tbody) return;
        if (!data.targets || data.targets.length === 0) {
          tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No storage targets configured</td></tr>';
          return;
        }
        tbody.innerHTML = data.targets.map(function (t) {
          return '<tr>' +
            '<td><strong>' + escHtml(t.name) + '</strong></td>' +
            '<td><span class="badge badge-pending">' + escHtml(t.storage_type) + '</span></td>' +
            '<td><span class="badge ' + (t.enabled ? 'badge-success' : 'badge-failed') + '">' + (t.enabled ? 'Active' : 'Disabled') + '</span></td>' +
            '<td>' + (t.last_test_at || '--') + (t.last_test_ok ? ' <span class="badge badge-success">OK</span>' : '') + '</td>' +
            '<td class="actions-col">' +
            '<button class="btn btn-outline btn-sm" onclick="vaultTestStorage(' + t.id + ')">Test</button> ' +
            '<button class="btn btn-danger btn-sm" onclick="vaultDeleteStorage(' + t.id + ')">Delete</button>' +
            '</td></tr>';
        }).join('');
      }).catch(function (e) { toast('Failed to load storage targets: ' + e.message, 'error'); });
  }

  function showStorageForm(target) {
    var card = document.getElementById('storageFormCard');
    if (!card) return;
    card.classList.remove('hidden');
    document.getElementById('storageId').value = target ? target.id : '';
    document.getElementById('storageFormTitle').textContent = target ? 'Edit Storage Target' : 'Add Storage Target';
    document.getElementById('storageName').value = target ? target.name : '';
    document.getElementById('storageType').value = target ? target.storage_type : 'local';
    renderStorageConfigFields();
  }

  function hideStorageForm() {
    var card = document.getElementById('storageFormCard');
    if (card) card.classList.add('hidden');
  }

  function renderStorageConfigFields() {
    var container = document.getElementById('storageConfigFields');
    if (!container) return;
    var stype = document.getElementById('storageType').value;

    var fields = '';
    if (stype === 'local') {
      fields =
        '<div class="form-group"><label>Local Path</label><input type="text" class="form-input" id="stLocalPath" placeholder="/backups"></div>';
    } else if (stype === 's3') {
      fields =
        '<div class="form-row">' +
        '<div class="form-group"><label>Bucket</label><input type="text" class="form-input" id="stS3Bucket" placeholder="my-backup-bucket"></div>' +
        '<div class="form-group"><label>Region</label><input type="text" class="form-input" id="stS3Region" placeholder="us-east-1"></div>' +
        '</div>' +
        '<div class="form-row">' +
        '<div class="form-group"><label>Access Key</label><input type="text" class="form-input" id="stS3Key"></div>' +
        '<div class="form-group"><label>Secret Key</label><input type="password" class="form-input" id="stS3Secret"></div>' +
        '</div>' +
        '<div class="form-group"><label>Endpoint (for S3-compatible)</label><input type="text" class="form-input" id="stS3Endpoint" placeholder="https://s3.amazonaws.com"></div>';
    } else if (stype === 'oss') {
      fields =
        '<div class="form-row">' +
        '<div class="form-group"><label>Bucket</label><input type="text" class="form-input" id="stOssBucket"></div>' +
        '<div class="form-group"><label>Endpoint</label><input type="text" class="form-input" id="stOssEndpoint" placeholder="https://oss-cn-hangzhou.aliyuncs.com"></div>' +
        '</div>' +
        '<div class="form-row">' +
        '<div class="form-group"><label>Access Key</label><input type="text" class="form-input" id="stOssKey"></div>' +
        '<div class="form-group"><label>Secret Key</label><input type="password" class="form-input" id="stOssSecret"></div>' +
        '</div>';
    } else if (stype === 'sftp') {
      fields =
        '<div class="form-row">' +
        '<div class="form-group"><label>Host</label><input type="text" class="form-input" id="stSftpHost" placeholder="backup.example.com"></div>' +
        '<div class="form-group"><label>Port</label><input type="number" class="form-input" id="stSftpPort" value="22"></div>' +
        '</div>' +
        '<div class="form-row">' +
        '<div class="form-group"><label>Username</label><input type="text" class="form-input" id="stSftpUser"></div>' +
        '<div class="form-group"><label>Password</label><input type="password" class="form-input" id="stSftpPass"></div>' +
        '</div>' +
        '<div class="form-group"><label>Remote Path</label><input type="text" class="form-input" id="stSftpPath" placeholder="/backups"></div>';
    } else if (stype === 'azure') {
      fields =
        '<div class="form-group"><label>Connection String</label><input type="text" class="form-input" id="stAzureConnStr" placeholder="DefaultEndpointsProtocol=https;AccountName=..."></div>' +
        '<div class="form-group"><label>Container Name</label><input type="text" class="form-input" id="stAzureContainer" placeholder="verorun-backups"></div>';
    } else if (stype === 'gcs') {
      fields =
        '<div class="form-group"><label>Bucket Name</label><input type="text" class="form-input" id="stGcsBucket" placeholder="verorun-backups"></div>' +
        '<div class="form-group"><label>Credentials Path (JSON)</label><input type="text" class="form-input" id="stGcsCreds" placeholder="/path/to/service-account.json"></div>' +
        '<div class="form-group"><label>Project ID</label><input type="text" class="form-input" id="stGcsProject" placeholder="my-project"></div>';
    } else if (stype === 'webdav') {
      fields =
        '<div class="form-group"><label>WebDAV URL</label><input type="text" class="form-input" id="stDavUrl" placeholder="https://nextcloud.example.com/remote.php/dav/files/user"></div>' +
        '<div class="form-row">' +
        '<div class="form-group"><label>Username</label><input type="text" class="form-input" id="stDavUser"></div>' +
        '<div class="form-group"><label>Password</label><input type="password" class="form-input" id="stDavPass"></div>' +
        '</div>' +
        '<div class="form-group"><label>Remote Path</label><input type="text" class="form-input" id="stDavPath" placeholder="/backups"></div>';
    }
    container.innerHTML = fields;
  }

  function saveStorageTarget(e) {
    e.preventDefault();
    var id = document.getElementById('storageId').value;
    var stype = document.getElementById('storageType').value;
    var config = {};

    if (stype === 'local') {
      config = { path: document.getElementById('stLocalPath') ? document.getElementById('stLocalPath').value : '/backups' };
    } else if (stype === 's3') {
      config = {
        bucket: document.getElementById('stS3Bucket') ? document.getElementById('stS3Bucket').value : '',
        region: document.getElementById('stS3Region') ? document.getElementById('stS3Region').value : '',
        access_key: document.getElementById('stS3Key') ? document.getElementById('stS3Key').value : '',
        secret_key: document.getElementById('stS3Secret') ? document.getElementById('stS3Secret').value : '',
        endpoint: document.getElementById('stS3Endpoint') ? document.getElementById('stS3Endpoint').value : '',
      };
    } else if (stype === 'oss') {
      config = {
        bucket: document.getElementById('stOssBucket') ? document.getElementById('stOssBucket').value : '',
        endpoint: document.getElementById('stOssEndpoint') ? document.getElementById('stOssEndpoint').value : '',
        access_key: document.getElementById('stOssKey') ? document.getElementById('stOssKey').value : '',
        secret_key: document.getElementById('stOssSecret') ? document.getElementById('stOssSecret').value : '',
      };
    } else if (stype === 'sftp') {
      config = {
        host: document.getElementById('stSftpHost') ? document.getElementById('stSftpHost').value : '',
        port: parseInt(document.getElementById('stSftpPort') ? document.getElementById('stSftpPort').value : '22'),
        username: document.getElementById('stSftpUser') ? document.getElementById('stSftpUser').value : '',
        password: document.getElementById('stSftpPass') ? document.getElementById('stSftpPass').value : '',
        remote_path: document.getElementById('stSftpPath') ? document.getElementById('stSftpPath').value : '/backups',
      };
    } else if (stype === 'azure') {
      config = {
        connection_string: document.getElementById('stAzureConnStr') ? document.getElementById('stAzureConnStr').value : '',
        container: document.getElementById('stAzureContainer') ? document.getElementById('stAzureContainer').value : 'verorun-backups',
      };
    } else if (stype === 'gcs') {
      config = {
        bucket: document.getElementById('stGcsBucket') ? document.getElementById('stGcsBucket').value : '',
        credentials_path: document.getElementById('stGcsCreds') ? document.getElementById('stGcsCreds').value : '',
        project: document.getElementById('stGcsProject') ? document.getElementById('stGcsProject').value : '',
      };
    } else if (stype === 'webdav') {
      config = {
        url: document.getElementById('stDavUrl') ? document.getElementById('stDavUrl').value : '',
        username: document.getElementById('stDavUser') ? document.getElementById('stDavUser').value : '',
        password: document.getElementById('stDavPass') ? document.getElementById('stDavPass').value : '',
        remote_path: document.getElementById('stDavPath') ? document.getElementById('stDavPath').value : '/backups',
      };
    }

    var payload = {
      name: document.getElementById('storageName').value,
      storage_type: stype,
      config: config,
    };

    var isNew = !id;
    var url = isNew ? '/admin/vault/api/storage/create' : '/admin/vault/api/storage/' + id;
    var method = isNew ? 'POST' : 'PUT';

    api(url, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then(function (data) {
      if (data.success) {
        toast(isNew ? 'Storage target created' : 'Storage target updated', 'success');
        hideStorageForm();
        loadStorageTargets();
      } else {
        toast('Save failed: ' + (data.error || 'unknown'), 'error');
      }
    }).catch(function (e) { toast('Save error: ' + e.message, 'error'); });
  }

  function testStorageConnection() {
    var id = document.getElementById('storageId').value;
    if (!id) return;

    api('/admin/vault/api/storage/' + id + '/test', { method: 'POST' })
      .then(function (data) {
        var el = document.getElementById('testResult');
        if (!el) return;
        if (data.ok) {
          el.textContent = 'Connection OK';
          el.className = 'form-test-result success';
        } else {
          el.textContent = 'Connection failed: ' + (data.error || 'unknown');
          el.className = 'form-test-result error';
        }
      }).catch(function (e) { toast('Test error: ' + e.message, 'error'); });
  }

  window.vaultTestStorage = function (id) {
    api('/admin/vault/api/storage/' + id + '/test', { method: 'POST' })
      .then(function (data) {
        if (data.ok) {
          toast('Connection OK', 'success');
        } else {
          toast('Connection failed: ' + (data.error || 'unknown'), 'error');
        }
        loadStorageTargets();
      }).catch(function (e) { toast('Test error: ' + e.message, 'error'); });
  };

  window.vaultDeleteStorage = function (id) {
    vaultConfirm('Delete Storage Target', 'Delete this storage target?', 'Delete', function () {
      api('/admin/vault/api/storage/' + id, { method: 'DELETE' })
        .then(function (data) {
          if (data.success) { toast('Storage target deleted', 'success'); loadStorageTargets(); }
          else { toast('Delete failed: ' + (data.error || 'unknown'), 'error'); }
        }).catch(function (e) { toast('Delete error: ' + e.message, 'error'); });
    });
  };

  // ══════════════════════════════════════════════════════════════
  // AUDIT LOG
  // ══════════════════════════════════════════════════════════════

  function initAudit() {
    loadAuditLogs();
    document.getElementById('auditActionFilter').addEventListener('change', function () {
      state.auditPage = 0;
      loadAuditLogs();
    });
    document.getElementById('auditSearch').addEventListener('input', function () {
      clearTimeout(this._timer);
      var self = this;
      this._timer = setTimeout(function () { state.auditPage = 0; loadAuditLogs(); }, 300);
    });
    document.getElementById('btnAuditPrev').addEventListener('click', function () {
      if (state.auditPage > 0) { state.auditPage--; loadAuditLogs(); }
    });
    document.getElementById('btnAuditNext').addEventListener('click', function () {
      state.auditPage++; loadAuditLogs();
    });
  }

  function loadAuditLogs() {
    var action = document.getElementById('auditActionFilter').value;
    var search = document.getElementById('auditSearch').value;
    var params = new URLSearchParams({
      limit: state.auditLimit,
      offset: state.auditPage * state.auditLimit,
    });
    if (action) params.set('action', action);

    api('/admin/vault/api/audit?' + params.toString())
      .then(function (data) {
        var tbody = document.getElementById('auditTableBody');
        if (!tbody) return;
        if (!data.logs || data.logs.length === 0) {
          tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No audit entries</td></tr>';
          document.getElementById('auditCount').textContent = 'No results';
          return;
        }
        if (search) {
          data.logs = data.logs.filter(function (l) {
            return (l.resource_id && l.resource_id.toLowerCase().indexOf(search.toLowerCase()) !== -1) ||
                   (l.operator && l.operator.toLowerCase().indexOf(search.toLowerCase()) !== -1);
          });
        }
        tbody.innerHTML = data.logs.map(function (log) {
          return '<tr>' +
            '<td><span class="badge badge-running">' + escHtml(log.action) + '</span></td>' +
            '<td>' + escHtml(log.resource_type || '') + ' / ' + escHtml(log.resource_id || '--') + '</td>' +
            '<td>' + escHtml(log.operator || 'system') + '</td>' +
            '<td>' + escHtml(log.ip_address || '--') + '</td>' +
            '<td>' + escHtml(log.created_at || '') + '</td>' +
            '</tr>';
        }).join('');
        document.getElementById('auditCount').textContent =
          'Showing ' + data.logs.length + ' entries (page ' + (state.auditPage + 1) + ')';
        document.getElementById('btnAuditPrev').disabled = state.auditPage === 0;
        document.getElementById('btnAuditNext').disabled = data.logs.length < state.auditLimit;
      }).catch(function (e) { toast('Failed to load audit logs: ' + e.message, 'error'); });
  }

  // ══════════════════════════════════════════════════════════════
  // SETTINGS
  // ══════════════════════════════════════════════════════════════

  function initSettings() {
    // 加载已保存的设置
    api('/admin/vault/api/settings').then(function (data) {
      var cfg = data.config || {};
      if (cfg.encryption) {
        var enc = cfg.encryption;
        if (enc.enabled !== undefined) document.getElementById('encEnabled').value = String(enc.enabled);
        if (enc.algorithm) document.getElementById('encAlgorithm').value = enc.algorithm;
        if (enc.key_source) document.getElementById('encKeySource').value = enc.key_source;
      }
      if (cfg.retention) {
        var ret = cfg.retention;
        if (ret.keep_days !== undefined) document.getElementById('keepDays').value = ret.keep_days;
        if (ret.compression) document.getElementById('compressionAlg').value = ret.compression;
      }
      if (cfg.notifications) {
        var notify = cfg.notifications;
        if (notify.email) {
          document.getElementById('notifyEmailEnabled').value = String(!!notify.email.enabled);
          if (notify.email.recipients && notify.email.recipients.length) {
            document.getElementById('notifyEmail').value = notify.email.recipients.join(',');
          }
        }
        if (notify.webhook && notify.webhook.url) {
          document.getElementById('notifyWebhook').value = notify.webhook.url;
        }
        if (notify.feishu && notify.feishu.webhook_url) {
          document.getElementById('notifyFeishu').value = notify.feishu.webhook_url;
        }
        if (notify.dingtalk && notify.dingtalk.webhook_url) {
          document.getElementById('notifyDingtalk').value = notify.dingtalk.webhook_url;
        }
      }
    }).catch(function (e) {
      toast('Failed to load settings: ' + e.message, 'error');
    });

    function saveSettings(section, payload) {
      var req = { };
      req[section] = payload;
      return api('/admin/vault/api/settings', {
        method: 'POST',
        body: JSON.stringify(req),
      });
    }

    document.getElementById('encryptionForm').addEventListener('submit', function (e) {
      e.preventDefault();
      var payload = {
        enabled: document.getElementById('encEnabled').value === 'true',
        algorithm: document.getElementById('encAlgorithm').value,
        key_source: document.getElementById('encKeySource').value,
      };
      saveSettings('encryption', payload)
        .then(function () { toast('Encryption settings saved', 'success'); })
        .catch(function (err) { toast('Failed to save: ' + err.message, 'error'); });
    });

    document.getElementById('retentionForm').addEventListener('submit', function (e) {
      e.preventDefault();
      var payload = {
        keep_days: parseInt(document.getElementById('keepDays').value, 10) || 30,
        compression: document.getElementById('compressionAlg').value,
      };
      saveSettings('retention', payload)
        .then(function () { toast('Retention settings saved', 'success'); })
        .catch(function (err) { toast('Failed to save: ' + err.message, 'error'); });
    });

    document.getElementById('notifyForm').addEventListener('submit', function (e) {
      e.preventDefault();
      var recipients = (document.getElementById('notifyEmail').value || '')
        .split(',').map(function (s) { return s.trim(); }).filter(Boolean);
      var payload = {
        email: {
          enabled: document.getElementById('notifyEmailEnabled').value === 'true',
          recipients: recipients,
        },
        webhook: {
          enabled: !!document.getElementById('notifyWebhook').value,
          url: document.getElementById('notifyWebhook').value,
        },
        feishu: {
          enabled: !!document.getElementById('notifyFeishu').value,
          webhook_url: document.getElementById('notifyFeishu').value,
        },
        dingtalk: {
          enabled: !!document.getElementById('notifyDingtalk').value,
          webhook_url: document.getElementById('notifyDingtalk').value,
        },
      };
      saveSettings('notifications', payload)
        .then(function () { toast('Notification settings saved', 'success'); })
        .catch(function (err) { toast('Failed to save: ' + err.message, 'error'); });
    });
  }

  // ══════════════════════════════════════════════════════════════
  // SHARED NAV
  // ══════════════════════════════════════════════════════════════

  function initVaultNav() {
    // 高亮当前页面对应的导航项
    document.querySelectorAll('.vault-nav-link[data-vault-page]').forEach(function (el) {
      if (el.getAttribute('data-vault-page') === pageId) {
        el.classList.add('active');
      }
    });
    // 导航中的 Drill 按钮在任意页面均可触发
    document.querySelectorAll('.js-drill').forEach(function (btn) {
      btn.addEventListener('click', runDrill);
    });
  }

  // ══════════════════════════════════════════════════════════════
  // GLOBAL EXPOSURE
  // ══════════════════════════════════════════════════════════════

  window.createBackup = createBackup;
  window.deleteBackup = deleteBackup;
  window.cleanupBackups = cleanupBackups;
  window.vaultSetRestoreLabel = window.vaultSetRestoreLabel;
  window.vaultToggleSchedule = window.vaultToggleSchedule;
  window.vaultDeleteSchedule = window.vaultDeleteSchedule;
  window.vaultTestStorage = window.vaultTestStorage;
  window.vaultDeleteStorage = window.vaultDeleteStorage;
  window.runDrill = runDrill;

  // ══════════════════════════════════════════════════════════════
  // INIT — Route to page-specific init
  // ══════════════════════════════════════════════════════════════

  document.addEventListener('DOMContentLoaded', function () {
    initVaultNav();
    if (pageId === 'dashboard') {
      loadHealth();
      loadBackups();
      loadCharts();
      setInterval(loadHealth, 30000);
      var searchInput = document.getElementById('backupSearch');
      if (searchInput) {
        var timer;
        searchInput.addEventListener('input', function () {
          clearTimeout(timer);
          timer = setTimeout(function () { state.page = 1; loadBackups(searchInput.value); }, 300);
        });
      }
      var btn = document.getElementById('btnBackupNow');
      if (btn) btn.addEventListener('click', function () { createBackup('full'); });
      var cleanupBtn = document.getElementById('btnCleanup');
      if (cleanupBtn) cleanupBtn.addEventListener('click', cleanupBackups);
    } else if (pageId === 'restore') {
      initRestore();
    } else if (pageId === 'schedules') {
      initSchedules();
    } else if (pageId === 'storage') {
      initStorage();
    } else if (pageId === 'audit') {
      initAudit();
    } else if (pageId === 'settings') {
      initSettings();
    }
  });
})();
