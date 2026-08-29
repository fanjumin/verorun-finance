/* ══════════════════════════════════════════════════════════════
   workflow_editor.js — 编辑器工具函数
   非 React 逻辑：API 调用、节点默认值、配置面板、序列化等
   ══════════════════════════════════════════════════════════════ */

window.editor = (function() {
  'use strict';

  // P2-1: 捕获 SSO Token 到闭包，避免暴露在全局作用域
  var __capturedToken = window.__SSO_TOKEN || '';
  // 立即清理全局变量，防止第三方脚本读取
  try { delete window.__SSO_TOKEN; } catch(e) { window.__SSO_TOKEN = undefined; }

  function getToken() {
    if (__capturedToken) return __capturedToken;
    var m = document.cookie.match(/(?:^|;\s*)sso_token=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : '';
  }
  var API_BASE = '/admin/automation/workflows';
  var CURRENT_WORKFLOW_ID = null;  // 编辑模式时非 null
  var EDITOR_INSTANCE = null;      // 保存时获取画布状态

  // ── 国际化辅助 ──
  function _t(key) {
    return (window.__t && typeof window.__t._ === 'function' && window.__t._(key)) || key;
  }

  // ── HTML 转义 ──
  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  function escAttr(s) {
    if (s == null) return '';
    return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  // ── 节点类型配置 ──
  var NODE_CONFIGS = {
    data_collect: { icon: '📡', color: '#2563eb', label: 'node.data_collect', description: 'Get source data', showInput: false },
    ai_agent:     { icon: '🤖', color: '#7c3aed', label: 'node.ai_agent', description: 'AI agent call' },
    ai_process:   { icon: '🧠', color: '#7c3aed', label: 'node.ai_process', description: 'Data processing' },
    condition:    { icon: '🔀', color: '#eab308', label: 'node.condition', description: 'Branch logic' },
    wait:         { icon: '⏱️', color: '#64748b', label: 'node.wait', description: 'Time delay' },
    publish:      { icon: '📤', color: '#16a34a', label: 'node.publish', description: 'Auto publish' },
    notify:       { icon: '🔔', color: '#ec4899', label: 'node.notify', description: 'Send notification', showOutput: false },
    approval:     { icon: '✅', color: '#ef4444', label: 'node.approval', description: 'Human approval' },
    script:       { icon: '📜', color: '#a16207', label: 'node.script', description: 'Custom script' },
    http_request: { icon: '🌐', color: '#0891b2', label: 'node.http_request', description: 'HTTP call' },
    market_check: { icon: '📊', color: '#ea580c', label: 'node.market_check', description: 'Market data check' },
    sub_workflow: { icon: '🔗', color: '#15803d', label: 'node.sub_workflow', description: 'Nested workflow' }
  };

  // ── 配置字段定义 ──
  var CONFIG_FIELDS = {
    ai_agent: [
      { key: 'agent_type', type: 'select', label: 'Agent Type', options: ['system','user'], default: 'system' },
      { key: 'agent_id', type: 'number', label: 'Agent ID', default: 0 },
      { key: 'prompt', type: 'textarea', label: 'Prompt Template', default: '', placeholder: 'Enter prompt template...' },
      { key: 'model', type: 'text', label: 'Model', default: '', placeholder: 'e.g. qwen-turbo' }
    ],
    data_collect: [
      { key: 'source_ids', type: 'tags', label: 'Data Sources', default: [], placeholder: 'Enter source IDs, comma-separated' },
      { key: 'max_per_source', type: 'number', label: 'Max Per Source', default: 10 },
      { key: 'keywords', type: 'text', label: 'Keywords', default: '', placeholder: 'comma-separated' }
    ],
    ai_process: [
      { key: 'instruction', type: 'textarea', label: 'Processing Instruction', default: '', placeholder: 'Describe what to do...' },
      { key: 'fields', type: 'tags', label: 'Output Fields', default: [], placeholder: 'Field names, comma-separated' },
      { key: 'model', type: 'text', label: 'Model', default: '', placeholder: 'e.g. qwen-turbo' }
    ],
    condition: [
      { key: 'expression', type: 'textarea', label: 'Condition Expression', default: '', placeholder: 'e.g. context.source_count > 5' }
    ],
    wait: [
      { key: 'seconds', type: 'number', label: 'Wait Time (seconds)', default: 60 }
    ],
    publish: [
      { key: 'platforms', type: 'tags', label: 'Publish Platforms', default: [], placeholder: 'Platform names, comma-separated' },
      { key: 'title', type: 'text', label: 'Title Template', default: '', placeholder: 'e.g. Daily Report - ${date}' },
      { key: 'category', type: 'text', label: 'Category', default: '', placeholder: 'e.g. tech' }
    ],
    notify: [
      { key: 'channels', type: 'tags', label: 'Notification Channels', default: [], placeholder: 'webhook, email, sms...' },
      { key: 'title', type: 'text', label: 'Notification Title', default: '' },
      { key: 'message', type: 'textarea', label: 'Message Template', default: '', placeholder: 'Message content...' },
      { key: 'webhook_url', type: 'text', label: 'Webhook URL', default: '', placeholder: 'https://...' },
      { key: 'email_to', type: 'text', label: 'Email To', default: '', placeholder: 'user@example.com' }
    ],
    approval: [
      { key: 'approver_role', type: 'select', label: 'Approver Role', options: ['admin','super_admin','operator'], default: 'admin' },
      { key: 'approver_ids', type: 'tags', label: 'Approver User IDs', default: [], placeholder: 'User IDs, comma-separated (empty = any by role)' },
      { key: 'timeout_minutes', type: 'number', label: 'Approval Timeout (minutes, 0=global 72h)', default: 0 },
      { key: 'message', type: 'text', label: 'Approval Message', default: '', placeholder: 'Describe why approval is needed' },
      { key: 'require_approval_on_error', type: 'checkbox', label: 'Require Approval on Error', default: false }
    ],
    script: [
      { key: 'script', type: 'text', label: 'Script Name', default: '', placeholder: 'builtin: check_new_posts / generate_static_incremental, or file name in scripts/' },
      { key: 'lang', type: 'select', label: 'Language', options: ['builtin','python','shell'], default: 'builtin' },
      { key: 'args', type: 'tags', label: 'Script Args', default: [], placeholder: 'Arguments, comma-separated' },
      { key: 'timeout_seconds', type: 'number', label: 'Timeout (seconds)', default: 120 }
    ],
    http_request: [
      { key: 'url', type: 'text', label: 'URL', default: '', placeholder: 'https://api.example.com/endpoint' },
      { key: 'method', type: 'select', label: 'Method', options: ['GET','POST','PUT','DELETE'], default: 'GET' },
      { key: 'headers', type: 'textarea', label: 'Headers (JSON)', default: '{}', placeholder: '{"Authorization": "Bearer ..."}' },
      { key: 'body', type: 'textarea', label: 'Request Body', default: '', placeholder: 'JSON body template...' }
    ],
    market_check: [
      { key: 'symbol', type: 'text', label: 'Stock Symbol', default: '', placeholder: 'e.g. sh000001, AAPL' },
      { key: 'metric', type: 'select', label: 'Metric', options: ['price','change_pct','volume'], default: 'price' },
      { key: 'operator', type: 'select', label: 'Operator', options: ['>','<','>=','<=','=='], default: '>' },
      { key: 'threshold', type: 'number', label: 'Threshold', default: 0 }
    ],
    sub_workflow: [
      { key: 'workflow_id', type: 'number', label: 'Target Workflow ID', default: 0, placeholder: 'Enter workflow ID from the Workflows list' }
    ]
  };

  // 按分类组织
  var NODE_CATEGORIES = [
    { name: 'AI Processing',    key: 'panel.category.ai',      nodes: ['ai_agent','data_collect','ai_process'] },
    { name: 'Flow Control',     key: 'panel.category.flow',    nodes: ['condition','wait'] },
    { name: 'Output Actions',   key: 'panel.category.output',  nodes: ['publish','notify'] },
    { name: 'Human Interaction',key: 'panel.category.human',   nodes: ['approval'] },
    { name: 'Advanced',         key: 'panel.category.advanced', nodes: ['script','http_request','market_check','sub_workflow'] }
  ];

  // ── 获取节点默认 data ──
  function getNodeDefaults(type) {
    var cfg = NODE_CONFIGS[type];
    if (!cfg) return { label: type, color: '#6366f1', description: '' };
    var fields = CONFIG_FIELDS[type] || [];
    var defaultConfig = {};
    fields.forEach(function(f) { defaultConfig[f.key] = f.default; });
    return {
      type: type,
      label: _t(cfg.label) || cfg.label,
      description: cfg.description,
      color: cfg.color,
      icon: cfg.icon,
      showInput: cfg.showInput !== false,
      showOutput: cfg.showOutput !== false,
      incomplete: true,
      config: defaultConfig
    };
  }

  // ── 渲染节点面板 ──
  function renderNodePanel(container) {
    if (!container) container = document.getElementById('node-panel-list');
    if (!container) return;
    var html = '';
    NODE_CATEGORIES.forEach(function(cat) {
      var catLabel = _t(cat.key) || cat.name;
      html += '<div class="panel-title" style="margin-top:12px">' + catLabel + '</div>';
      cat.nodes.forEach(function(type) {
        var cfg = NODE_CONFIGS[type];
        html += '<div class="node-panel-item" draggable="true" data-node-type="' + type + '"';
        html += ' ondragstart="editor.onNodeDragStart(event)">';
        html += '<span class="node-icon" style="background:' + (cfg.color + '22') + ';color:' + cfg.color + '">' + cfg.icon + '</span>';
        html += _t(cfg.label) || cfg.label;
        html += '</div>';
      });
    });
    container.innerHTML = html;
  }

  // ── 拖拽开始 ──
  function onNodeDragStart(event) {
    if (!event.dataTransfer) return;
    var type = event.target.closest('[data-node-type]').getAttribute('data-node-type');
    event.dataTransfer.setData('application/reactflow', type);
    event.dataTransfer.effectAllowed = 'move';
  }

  // ═══════════════════════════════════════════════════════════
  //  配置面板渲染
  // ═══════════════════════════════════════════════════════════

  // 渲染配置表单
  function renderConfigPanel(node) {
    var panel = document.getElementById('node-config-panel');
    if (!panel) return;
    if (!node) {
      panel.innerHTML = '<div class="panel-empty">'
        + '<div style="font-size:13px;font-weight:600;margin-bottom:6px">' + _t('editor.select_node') + '</div>'
        + '<div style="font-size:12px;line-height:1.7;opacity:.85">' + _t('editor.select_node_hint') + '</div>'
        + '</div>';
      return;
    }
    var data = node.data;
    var fields = CONFIG_FIELDS[data.type];
    if (!fields) {
      panel.innerHTML = '<div class="panel-title" style="margin-bottom:8px">' + (data.label || '') + '</div>';
      panel.innerHTML += '<div style="font-size:11px;color:var(--text-dim)">' + _t('misc.no_config') + '</div>';
      return;
    }

    var config = data.config || {};
    var html = '';
    html += '<div class="cp-header">' + (data.icon || '') + ' ' + (data.label || '') + '</div>';
    html += '<div class="cp-type">' + (data.type || '') + '</div>';
    html += '<div class="cp-fields">';

    fields.forEach(function(f) {
      var val = (config[f.key] !== undefined && config[f.key] !== null) ? config[f.key] : f.default;
      html += '<div class="cp-field">';
      html += '<label class="cp-label">' + f.label + '</label>';

      if (f.type === 'select') {
        html += '<select class="cp-input cp-select" data-key="' + f.key + '" data-type="' + f.type + '">';
        (f.options || []).forEach(function(o) {
          html += '<option value="' + escAttr(o) + '"' + (val === o ? ' selected' : '') + '>' + o + '</option>';
        });
        html += '</select>';
      } else if (f.type === 'checkbox') {
        html += '<input type="checkbox" class="cp-checkbox" data-key="' + f.key + '" data-type="checkbox"' + (val ? ' checked' : '') + '>';
      } else if (f.type === 'number') {
        html += '<input type="number" class="cp-input" data-key="' + f.key + '" data-type="number" value="' + val + '"' + (f.placeholder ? ' placeholder="' + escAttr(f.placeholder) + '"' : '') + '>';
      } else if (f.type === 'textarea') {
        html += '<textarea class="cp-input cp-textarea" data-key="' + f.key + '" data-type="textarea" rows="3"' + (f.placeholder ? ' placeholder="' + escAttr(f.placeholder) + '"' : '') + '>' + esc(String(val)) + '</textarea>';
      } else if (f.type === 'tags') {
        var tagStr = Array.isArray(val) ? val.join(', ') : String(val);
        html += '<input class="cp-input" data-key="' + f.key + '" data-type="tags" value="' + escAttr(tagStr) + '"' + (f.placeholder ? ' placeholder="' + escAttr(f.placeholder) + '"' : '') + '>';
      } else {
        html += '<input class="cp-input" data-key="' + f.key + '" data-type="text" value="' + escAttr(String(val)) + '"' + (f.placeholder ? ' placeholder="' + escAttr(f.placeholder) + '"' : '') + '>';
      }

      html += '</div>';
    });

    html += '</div>';

    // 保存/取消按钮
    html += '<div class="cp-actions">';
    html += '<button class="btn bp bs" data-action="save-config">' + _t('editor.save') + '</button>';
    html += '<button class="btn bo bs" data-action="cancel-config" style="margin-left:6px">' + _t('editor.cancel') + '</button>';
    html += '</div>';

    panel.innerHTML = html;

    // 绑定事件
    panel.querySelector('[data-action="save-config"]').onclick = function() {
      saveNodeConfig(node.id, data.type);
    };
    panel.querySelector('[data-action="cancel-config"]').onclick = function() {
      renderConfigPanel(null);
    };
  }

  // 读取表单数据
  function readFormData(nodeType) {
    var fields = CONFIG_FIELDS[nodeType];
    if (!fields) return {};
    var data = {};
    fields.forEach(function(f) {
      var el = document.querySelector('[data-key="' + f.key + '"]');
      if (!el) { data[f.key] = f.default; return; }
      var tag = el.tagName.toLowerCase();
      if (f.type === 'checkbox') {
        data[f.key] = el.checked;
      } else if (f.type === 'number') {
        data[f.key] = parseFloat(el.value) || 0;
      } else if (f.type === 'tags') {
        data[f.key] = el.value.split(',').map(function(s) { return s.trim(); }).filter(function(s) { return s.length > 0; });
      } else {
        data[f.key] = el.value;
      }
    });
    return data;
  }

  // 统一的节点配置完整性校验（P1-5: 统一 saveNodeConfig 和 deserialize 的检测逻辑）
  function validateNode(type, config) {
    var fields = CONFIG_FIELDS[type] || [];
    var incomplete = false;
    fields.forEach(function(f) {
      var val = config[f.key];
      if (f.type === 'text' || f.type === 'textarea') {
        if (f.key === 'prompt' || f.key === 'instruction' || f.key === 'expression' || f.key === 'url') {
          if (!val || val.toString().trim() === '') incomplete = true;
        }
      }
    });
    return incomplete;
  }

  // 保存节点配置
  function saveNodeConfig(nodeId, nodeType) {
    // P1-4: 保存前推送撤销栈
    var flowState = window.editor.__flowState;
    if (flowState && flowState.pushUndo) {
      flowState.pushUndo();
    }

    var config = readFormData(nodeType);
    var incomplete = validateNode(nodeType, config);

    if (flowState) {
      flowState.updateNodeConfig(nodeId, config, incomplete);
    }
    renderConfigPanel(null);
  }

  // ═══════════════════════════════════════════════════════════
  //  序列化 / 反序列化
  // ═══════════════════════════════════════════════════════════

  // 将 React Flow 状态 → 后端 definition JSON
  function serializeToDefinition(nodes, edges) {
    var defNodes = (nodes || []).map(function(n) {
      return {
        id: n.id,
        type: n.data.type || 'unknown',
        name: n.data.label || '',
        config: n.data.config || {},
        position: { x: n.position.x, y: n.position.y }
      };
    });
    var defEdges = (edges || []).map(function(e) {
      return {
        id: e.id,
        from: e.source,
        to: e.target,
        sourceHandle: e.sourceHandle || null,
        targetHandle: e.targetHandle || null,
        animated: e.animated !== undefined ? e.animated : true,
        style: e.style || { stroke: '#6366f1', strokeWidth: 2 },
        label: e.label || '',
        condition: e.sourceHandle || 'success'
      };
    });
    return { nodes: defNodes, edges: defEdges };
  }

  // 将后端 definition JSON → React Flow state
  function deserializeFromDefinition(definition) {
    if (!definition) return { nodes: [], edges: [] };
    var defNodes = definition.nodes || [];
    var defEdges = definition.edges || [];

    var nodes = defNodes.map(function(n) {
      var cfg = NODE_CONFIGS[n.type];
      var defaults = getNodeDefaults(n.type);
      // 合并默认配置与已保存配置，确保反序列化后节点不缺少必要字段
      var config = Object.assign({}, defaults.config, n.config || {});
      var incomplete = validateNode(n.type, config);

      return {
        id: n.id,
        type: (n.type === 'condition' || n.type === 'market_check') ? n.type : 'default',
        position: n.position || { x: 100, y: 100 },
        data: {
          type: n.type,
          label: n.name || defaults.label,
          description: cfg ? (_t(cfg.description) || '') : '',
          color: cfg ? cfg.color : '#6366f1',
          icon: cfg ? cfg.icon : '',
          showInput: cfg ? cfg.showInput !== false : true,
          showOutput: cfg ? cfg.showOutput !== false : true,
          config: config,
          incomplete: incomplete
        }
      };
    });

    var edges = defEdges.map(function(e) {
      return {
        id: e.id || ('edge_' + e.from + '_' + (e.sourceHandle || 'default') + '_' + e.to),
        source: e.from,
        target: e.to,
        sourceHandle: e.sourceHandle || null,
        targetHandle: e.targetHandle || null,
        animated: e.animated !== undefined ? e.animated : true,
        style: e.style || { stroke: '#6366f1', strokeWidth: 2 },
        label: e.label || ''
      };
    });

    return { nodes: nodes, edges: edges };
  }

  // ═══════════════════════════════════════════════════════════
  //  DAG 循环检测（DFS）
  // ═══════════════════════════════════════════════════════════

  function wouldCreateCycle(edges, source, target) {
    var adj = {};
    edges.forEach(function(e) {
      if (!adj[e.source]) adj[e.source] = [];
      adj[e.source].push(e.target);
    });
    var visited = {};
    var stack = [target];
    while (stack.length > 0) {
      var node = stack.pop();
      if (node === source) return true;
      if (visited[node]) continue;
      visited[node] = true;
      (adj[node] || []).forEach(function(next) { stack.push(next); });
    }
    return false;
  }

  function validateConnection(nodes, edges, source, target, sourceHandle) {
    if (source === target) {
      return { ok: false, reason: _t('toast.self_connect') };
    }
    var dup = edges.some(function(e) {
      return e.source === source && e.target === target && e.sourceHandle === sourceHandle;
    });
    if (dup) {
      return { ok: false, reason: _t('toast.duplicate_edge') };
    }
    if (wouldCreateCycle(edges, source, target)) {
      return { ok: false, reason: _t('toast.cycle_detected') };
    }
    return { ok: true };
  }

  var _getEdges = function() { return []; };
  var _getNodes = function() { return []; };
  function setEdgeAccessor(getNodes, getEdges) {
    _getNodes = getNodes;
    _getEdges = getEdges;
  }

  // ═══════════════════════════════════════════════════════════
  //  API 操作
  // ═══════════════════════════════════════════════════════════

  // 保存工作流
  var __stateVersion = 0;  // P1-2: 竞态条件防护
  function save() {
    var flowState = window.editor.__flowState;
    if (!flowState) { toast(_t('misc.editor_not_ready'), 'error'); return; }
    var nodes = flowState.getNodes();
    var edges = flowState.getEdges();
    if (!nodes || nodes.length === 0) {
      toast(_t('toast.empty_workflow'), 'error');
      return;
    }

    // P1-2: 递增版本号，防止异步操作覆盖最新状态
    var version = ++__stateVersion;

    // 检查未配置的节点
    var incompleteNodes = nodes.filter(function(n) { return n.data && n.data.incomplete; });
    if (incompleteNodes.length > 0) {
      var msg = _t('toast.config.incomplete') + ': ' + incompleteNodes.map(function(n) { return n.data.label || n.id; }).join(', ');
      var proceed = function() { submitSave(nodes, edges, version); };
      if (window.showConfirm) {
        // 系统统一确认弹窗（独立页无 showConfirm 时回退原生 confirm）
        showConfirm(_t('toast.config.incomplete'), esc(msg), proceed, _t('editor.save_anyway_confirm') || 'Save anyway?');
        return;
      }
      if (!confirm(msg + '\n\n' + (_t('editor.save_anyway_confirm') || 'Save anyway?'))) return;
    }
    submitSave(nodes, edges, version);
  }

  // 保存主体（未配置节点确认后继续执行）
  function submitSave(nodes, edges, version) {
    var definition = serializeToDefinition(nodes, edges);
    var nameInput = document.getElementById('workflow-name');
    var name = nameInput ? nameInput.value.trim() : '';
    if (!name) {
      toast(_t('toast.name_required'), 'error');
      return;
    }

    var payload = { name: name, definition: definition };

    var url = API_BASE;
    var method = 'POST';
    if (CURRENT_WORKFLOW_ID) {
      url += '/' + CURRENT_WORKFLOW_ID;
      method = 'PUT';
    }

    document.getElementById('btn-save').disabled = true;
    document.getElementById('btn-save').textContent = _t('misc.saving');

    doFetch(url, method, payload)
      .then(function(d) {
        // P1-2: 版本号不匹配则放弃处理（后续操作已覆盖此状态）
        if (version !== __stateVersion) return;
        document.getElementById('btn-save').disabled = false;
        document.getElementById('btn-save').textContent = _t('editor.save') || 'Save';
        if (d.success) {
          var newId = d.data && (d.data.id || d.data.workflow_id);
          if (newId && !CURRENT_WORKFLOW_ID) {
            CURRENT_WORKFLOW_ID = newId;
            // 内嵌模式不污染后台 URL，仅独立页同步地址栏
            if (!window.__WF_EMBEDDED && history.pushState) {
              var u = new URL(window.location);
              u.searchParams.set('id', newId);
              history.pushState({}, '', u);
            }
          }
          var fs = document.getElementById('footer-status');
          if (fs) { fs.textContent = _t('statusbar.saved') || 'Saved'; fs.style.color = 'var(--green)'; }
          toast(_t('toast.save.success') || 'Saved');
        } else {
          toast(_t('toast.save.failed') + ': ' + (d.error || ''));
        }
      })
      .catch(function() {
        document.getElementById('btn-save').disabled = false;
        document.getElementById('btn-save').textContent = _t('editor.save') || 'Save';
        toast(_t('toast.save.failed') || 'Network error');
      });
  }

  // 带重试的 fetch
  function doFetch(url, method, body, retries) {
    retries = retries || 2;
    return fetch(url, {
      method: method,
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + getToken() },
      body: body ? JSON.stringify(body) : undefined
    }).then(function(r) {
      if (!r.ok && r.status === 409) {
        throw new Error('version_conflict');
      }
      return r.text().then(function(t) {
        try { return JSON.parse(t); } catch (e) {
          throw new Error('parse_error: ' + t.substring(0, 100));
        }
      });
    }).catch(function(err) {
      if (retries > 0 && (err.message === 'version_conflict' || err.message.indexOf('parse_error') === 0)) {
        // 版本冲突或解析错误，不重试
        throw err;
      }
      if (retries > 0) {
        return doFetch(url, method, body, retries - 1);
      }
      throw err;
    });
  }

  // 加载工作流
  function load(id) {
    doFetch(API_BASE + '/' + id, 'GET', null)
    .then(function(d) {
      if (!d.success) {
        toast(_t('toast.load.failed'));
        return;
      }
      var wf = d.data;
      CURRENT_WORKFLOW_ID = wf.id;
      var nameInput = document.getElementById('workflow-name');
      if (nameInput) nameInput.value = wf.name || '';

      var definition = wf.definition;
      if (typeof definition === 'string') {
        try { definition = JSON.parse(definition); } catch(e) {
          toast(_t('toast.load.failed') + ': ' + (e.message || 'Invalid JSON'), 'error');
          console.error('[WorkflowEditor] JSON parse error:', e, 'raw:', definition.substring(0, 200));
          definition = null;
        }
      }
      var state = definition ? deserializeFromDefinition(definition) : { nodes: [], edges: [] };
      var flowState = window.editor.__flowState;
      if (flowState) {
        flowState.setNodes(state.nodes);
        flowState.setEdges(state.edges);
      }
      if (state.nodes.length > 0) {
        var fs = document.getElementById('footer-status');
        if (fs) { fs.textContent = _t('statusbar.saved') || 'Loaded'; fs.style.color = 'var(--green)'; }
      }
      toast(_t('toast.load.success') || 'Workflow loaded: ' + (wf.name || '#') + wf.id);
    })
    .catch(function() {
      toast(_t('toast.load.failed'));
    });
  }

  function run() {
    if (!CURRENT_WORKFLOW_ID) { save(); return; }
    doFetch(API_BASE + '/' + CURRENT_WORKFLOW_ID + '/run', 'POST', null)
    .then(function(d) {
      if (d.success) {
        toast(_t('toast.run.success') || 'Workflow started');
      } else {
        toast(_t('toast.run.failed') + ': ' + (d.error || ''));
      }
    })
    .catch(function() {
      toast(_t('toast.run.failed') || 'Network error');
    });
  }

  // ── Toast（统一系统风格：复用 head.html #adminToast，独立页兜底自建） ──
  function toast(message, type) {
    type = type || 'info';
    var t = document.getElementById('adminToast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'adminToast';
      t.style.cssText = 'position:fixed;bottom:30px;left:50%;transform:translateX(-50%);background:#111118;color:#e0e0e0;padding:10px 20px;border-radius:8px;font-size:13px;z-index:2000;opacity:0;transition:opacity .3s;pointer-events:none;border:1px solid rgba(255,255,255,0.06);max-width:80vw;word-break:break-word';
      document.body.appendChild(t);
    }
    t.textContent = message;
    if (type === 'success') { t.style.borderColor = '#00ff9f'; }
    else if (type === 'error') { t.style.borderColor = '#f85149'; }
    else { t.style.borderColor = ''; }
    t.style.opacity = '1';
    clearTimeout(t._toastTimer);
    t._toastTimer = setTimeout(function() { t.style.opacity = '0'; }, 3000);
  }

  // ── 初始化 ──
  function init() {
    renderNodePanel();

    // 内嵌模式优先读 __wfEditId；独立页回退解析 URL ?id 参数
    var loadId = null;
    if (typeof window.__wfEditId !== 'undefined' && window.__wfEditId) {
      loadId = window.__wfEditId;
    } else {
      var params = new URLSearchParams(window.location.search);
      loadId = params.get('id');
    }
    if (loadId) {
      CURRENT_WORKFLOW_ID = parseInt(loadId);
      load(CURRENT_WORKFLOW_ID);
    } else {
      // P0 修复：新建工作流时清除上次编辑残留的 ID，防止保存误走 PUT 覆盖旧工作流
      CURRENT_WORKFLOW_ID = null;
    }
  }

  return {
    NODE_CONFIGS: NODE_CONFIGS,
    CONFIG_FIELDS: CONFIG_FIELDS,
    NODE_CATEGORIES: NODE_CATEGORIES,
    getNodeDefaults: getNodeDefaults,
    renderNodePanel: renderNodePanel,
    onNodeDragStart: onNodeDragStart,
    renderConfigPanel: renderConfigPanel,
    saveNodeConfig: saveNodeConfig,
    serializeToDefinition: serializeToDefinition,
    deserializeFromDefinition: deserializeFromDefinition,
    save: save,
    run: run,
    load: load,
    toast: toast,
    validateConnection: validateConnection,
    wouldCreateCycle: wouldCreateCycle,
    setEdgeAccessor: setEdgeAccessor,
    init: init,
    _t: _t,
    esc: esc,
    escAttr: escAttr
  };
})();

// ── 页面加载后初始化 ──
document.addEventListener('DOMContentLoaded', function() {
  window.editor.init();
});
