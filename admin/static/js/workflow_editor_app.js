"use strict";

function _typeof(o) { "@babel/helpers - typeof"; return _typeof = "function" == typeof Symbol && "symbol" == typeof Symbol.iterator ? function (o) { return typeof o; } : function (o) { return o && "function" == typeof Symbol && o.constructor === Symbol && o !== Symbol.prototype ? "symbol" : typeof o; }, _typeof(o); }
function ownKeys(e, r) { var t = Object.keys(e); if (Object.getOwnPropertySymbols) { var o = Object.getOwnPropertySymbols(e); r && (o = o.filter(function (r) { return Object.getOwnPropertyDescriptor(e, r).enumerable; })), t.push.apply(t, o); } return t; }
function _objectSpread(e) { for (var r = 1; r < arguments.length; r++) { var t = null != arguments[r] ? arguments[r] : {}; r % 2 ? ownKeys(Object(t), !0).forEach(function (r) { _defineProperty(e, r, t[r]); }) : Object.getOwnPropertyDescriptors ? Object.defineProperties(e, Object.getOwnPropertyDescriptors(t)) : ownKeys(Object(t)).forEach(function (r) { Object.defineProperty(e, r, Object.getOwnPropertyDescriptor(t, r)); }); } return e; }
function _defineProperty(e, r, t) { return (r = _toPropertyKey(r)) in e ? Object.defineProperty(e, r, { value: t, enumerable: !0, configurable: !0, writable: !0 }) : e[r] = t, e; }
function _toPropertyKey(t) { var i = _toPrimitive(t, "string"); return "symbol" == _typeof(i) ? i : i + ""; }
function _toPrimitive(t, r) { if ("object" != _typeof(t) || !t) return t; var e = t[Symbol.toPrimitive]; if (void 0 !== e) { var i = e.call(t, r || "default"); if ("object" != _typeof(i)) return i; throw new TypeError("@@toPrimitive must return a primitive value."); } return ("string" === r ? String : Number)(t); }
function _slicedToArray(r, e) { return _arrayWithHoles(r) || _iterableToArrayLimit(r, e) || _unsupportedIterableToArray(r, e) || _nonIterableRest(); }
function _nonIterableRest() { throw new TypeError("Invalid attempt to destructure non-iterable instance.\nIn order to be iterable, non-array objects must have a [Symbol.iterator]() method."); }
function _unsupportedIterableToArray(r, a) { if (r) { if ("string" == typeof r) return _arrayLikeToArray(r, a); var t = {}.toString.call(r).slice(8, -1); return "Object" === t && r.constructor && (t = r.constructor.name), "Map" === t || "Set" === t ? Array.from(r) : "Arguments" === t || /^(?:Ui|I)nt(?:8|16|32)(?:Clamped)?Array$/.test(t) ? _arrayLikeToArray(r, a) : void 0; } }
function _arrayLikeToArray(r, a) { (null == a || a > r.length) && (a = r.length); for (var e = 0, n = Array(a); e < a; e++) n[e] = r[e]; return n; }
function _iterableToArrayLimit(r, l) { var t = null == r ? null : "undefined" != typeof Symbol && r[Symbol.iterator] || r["@@iterator"]; if (null != t) { var e, n, i, u, a = [], f = !0, o = !1; try { if (i = (t = t.call(r)).next, 0 === l) { if (Object(t) !== t) return; f = !1; } else for (; !(f = (e = i.call(t)).done) && (a.push(e.value), a.length !== l); f = !0); } catch (r) { o = !0, n = r; } finally { try { if (!f && null != t["return"] && (u = t["return"](), Object(u) !== u)) return; } finally { if (o) throw n; } } return a; } }
function _arrayWithHoles(r) { if (Array.isArray(r)) return r; }
var _React = React,
  E = _React.createElement,
  useState = _React.useState,
  useCallback = _React.useCallback,
  useRef = _React.useRef,
  useEffect = _React.useEffect,
  useMemo = _React.useMemo;
var lib = window.ReactFlow;
if (!lib) {
  throw new Error('ReactFlow not loaded');
}
var RF = lib["default"];
var Provider = lib.ReactFlowProvider;
var Handle = lib.Handle;
var Position = lib.Position;
var Controls = lib.Controls;
var Background = lib.Background;
var MiniMap = lib.MiniMap;
var addEdge = lib.addEdge;

// ── Helper: 更新底部状态栏 ──
function updateFooter(nodes, edges) {
  var fn = document.getElementById('footer-nodes');
  var fe = document.getElementById('footer-edges');
  var fs = document.getElementById('footer-status');
  if (fn) fn.textContent = window.__t._('statusbar.nodes').replace('{count}', nodes.length);
  if (fe) fe.textContent = window.__t._('statusbar.edges').replace('{count}', edges.length);
  if (fs) {
    fs.textContent = window.__t._('statusbar.modified');
    fs.style.color = 'var(--gold)';
  }
}

// ── Helper: 更新工作流名称输入 ──
function syncWorkflowName(nodes) {
  // 将来从 API 加载时使用
}

// ── 自定义节点组件 ──
// 所有 12 种节点类型通过 data 属性 + 少数变体组件覆盖

// 基础节点（单入单出，覆盖 8 种：ai_agent/ai_process/wait/publish/approval/script/http_request/sub_workflow）
function BaseNode(_ref) {
  var data = _ref.data,
    selected = _ref.selected;
  var borderColor = data.color || '#6366f1';
  return E('div', {
    className: 'rf-node' + (selected ? ' rf-node-selected' : '') + (data.incomplete ? ' rf-node-incomplete' : ''),
    style: {
      borderColor: borderColor
    }
  }, data.icon ? E('span', {
    className: 'rf-node-icon'
  }, data.icon) : null, E('div', {
    className: 'rf-node-body'
  }, E('div', {
    className: 'rf-node-label'
  }, data.label || ''), data.description ? E('div', {
    className: 'rf-node-desc'
  }, data.description) : null),
  // 输入端口（showInput=false 时为起始节点，如 data_collect）
  data.showInput !== false && E(Handle, {
    type: 'target',
    position: Position.Left,
    className: 'rf-handle',
    id: 'in'
  }),
  // 输出端口（showOutput=false 时为终止节点，如 notify）
  data.showOutput !== false && E(Handle, {
    type: 'source',
    position: Position.Right,
    className: 'rf-handle',
    id: 'out'
  }));
}

// 条件节点：1 入 2 出（true / false）
function ConditionNode(p) {
  return DualPortNode(p, '#eab308', 'true', '#16a34a', 'false', '#ef4444');
}
// 市场检查节点：1 入 2 出（pass / fail）
function MarketCheckNode(p) {
  return DualPortNode(p, '#ea580c', 'pass', '#16a34a', 'fail', '#ef4444');
}

// 双输出端口节点（通用实现）
function DualPortNode(_ref2, color, out1Id, out1Color, out2Id, out2Color) {
  var data = _ref2.data,
    selected = _ref2.selected;
  var borderColor = data.color || color || '#eab308';
  return E('div', {
    className: 'rf-node' + (selected ? ' rf-node-selected' : '') + (data.incomplete ? ' rf-node-incomplete' : ''),
    style: {
      borderColor: borderColor
    }
  }, data.icon ? E('span', {
    className: 'rf-node-icon'
  }, data.icon) : null, E('div', {
    className: 'rf-node-body'
  }, E('div', {
    className: 'rf-node-label'
  }, data.label || ''), data.description ? E('div', {
    className: 'rf-node-desc'
  }, data.description) : null), E(Handle, {
    type: 'target',
    position: Position.Left,
    className: 'rf-handle',
    id: 'in'
  }), E(Handle, {
    type: 'source',
    position: Position.Top,
    className: 'rf-handle rf-handle-condition',
    id: out1Id,
    style: {
      background: out1Color
    }
  }), E(Handle, {
    type: 'source',
    position: Position.Bottom,
    className: 'rf-handle rf-handle-condition',
    id: out2Id,
    style: {
      background: out2Color
    }
  }));
}

// 注：data_collect (start) 和 notify (end) 由 BaseNode 通过 data.showInput/showOutput 控制
// 12 种节点类型映射表
var nodeTypes = {
  "default": BaseNode,
  // 通用单入/单出
  condition: ConditionNode,
  // 双输出（true/false）
  market_check: MarketCheckNode // 双输出（pass/fail）
};

// ── ReactFlow 应用组件 ──
function FlowEditor() {
  var _lib$useNodesState = lib.useNodesState([]),
    _lib$useNodesState2 = _slicedToArray(_lib$useNodesState, 3),
    nodes = _lib$useNodesState2[0],
    setNodes = _lib$useNodesState2[1],
    _onNodesChange = _lib$useNodesState2[2];
  var _lib$useEdgesState = lib.useEdgesState([]),
    _lib$useEdgesState2 = _slicedToArray(_lib$useEdgesState, 3),
    edges = _lib$useEdgesState2[0],
    setEdges = _lib$useEdgesState2[1],
    _onEdgesChange = _lib$useEdgesState2[2];
  var _useState = useState(null),
    _useState2 = _slicedToArray(_useState, 2),
    rfInstance = _useState2[0],
    setRfInstance = _useState2[1];
  var _useState3 = useState(null),
    _useState4 = _slicedToArray(_useState3, 2),
    selectedNode = _useState4[0],
    setSelectedNode = _useState4[1];
  // 用于快捷键撤销/重做
  var _useState5 = useState({
      undo: [],
      redo: []
    }),
    _useState6 = _slicedToArray(_useState5, 2),
    history = _useState6[0],
    setHistory = _useState6[1];
  var historyRef = useRef({
    undo: [],
    redo: []
  });
  var nodesRef = useRef(nodes);
  var edgesRef = useRef(edges);
  nodesRef.current = nodes;
  edgesRef.current = edges;
  var dragPosRef = useRef(null);

  // 保存快照到撤销栈
  var pushUndo = useCallback(function () {
    var hs = historyRef.current;
    // 记录当前状态
    hs.undo.push({
      nodes: JSON.parse(JSON.stringify(nodesRef.current)),
      edges: JSON.parse(JSON.stringify(edgesRef.current))
    });
    // 上限 50
    if (hs.undo.length > 50) hs.undo.shift();
    hs.redo = [];
  }, []);

  // 撤销
  var undo = useCallback(function () {
    var hs = historyRef.current;
    if (hs.undo.length === 0) return;
    var snap = hs.undo.pop();
    hs.redo.push({
      nodes: JSON.parse(JSON.stringify(nodesRef.current)),
      edges: JSON.parse(JSON.stringify(edgesRef.current))
    });
    setNodes(snap.nodes);
    setEdges(snap.edges);
  }, []);

  // 重做
  var redo = useCallback(function () {
    var hs = historyRef.current;
    if (hs.redo.length === 0) return;
    var snap = hs.redo.pop();
    hs.undo.push({
      nodes: JSON.parse(JSON.stringify(nodesRef.current)),
      edges: JSON.parse(JSON.stringify(edgesRef.current))
    });
    setNodes(snap.nodes);
    setEdges(snap.edges);
  }, []);

  // 连线完成（含验证）
  var onConnect = useCallback(function (params) {
    // 校验
    var result = editor.validateConnection(nodesRef.current, edgesRef.current, params.source, params.target, params.sourceHandle);
    if (!result.ok) {
      // 显示非法连线（红色闪烁）— 临时添加再删除
      var tempId = 'invalid_' + Date.now();
      setEdges(function (eds) {
        return eds.concat({
          id: tempId,
          source: params.source,
          target: params.target,
          sourceHandle: params.sourceHandle,
          targetHandle: params.targetHandle,
          style: {
            stroke: '#f85149',
            strokeWidth: 3,
            strokeDasharray: '6 3'
          },
          animated: false,
          className: 'invalid-edge'
        });
      });
      // 600ms 后移除
      setTimeout(function () {
        setEdges(function (eds) {
          return eds.filter(function (e) {
            return e.id !== tempId;
          });
        });
      }, 600);
      // Toast
      console.log('[warn]', result.reason);
      return;
    }
    pushUndo();
    setEdges(function (eds) {
      return addEdge(_objectSpread(_objectSpread({}, params), {}, {
        animated: true,
        style: {
          stroke: '#6366f1',
          strokeWidth: 2
        }
      }), eds);
    });
  }, []);

  // 选中节点 → 更新右侧配置面板
  var onNodeClick = useCallback(function (event, node) {
    setSelectedNode(node);
    window.editor.renderConfigPanel(node);
  }, []);

  // 点击空白 → 清空右侧面板
  var onPaneClick = useCallback(function () {
    setSelectedNode(null);
    window.editor.renderConfigPanel(null);
  }, []);

  // 更新节点配置（由 editor.saveNodeConfig 调用）
  var updateNodeConfig = useCallback(function (nodeId, config, incomplete) {
    setNodes(function (nds) {
      return nds.map(function (n) {
        if (n.id !== nodeId) return n;
        return _objectSpread(_objectSpread({}, n), {}, {
          data: _objectSpread(_objectSpread({}, n.data), {}, {
            config: _objectSpread(_objectSpread({}, n.data.config), config),
            incomplete: incomplete
          })
        });
      });
    });
  }, []);

  // 从面板拖入画布
  var onDragOver = useCallback(function (event) {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    dragPosRef.current = { x: event.clientX, y: event.clientY };
  }, []);
  var onDrop = useCallback(function (event) {
    event.preventDefault();
    var type = event.dataTransfer.getData('application/reactflow');
    if (!type || !rfInstance) return;
    var pos = dragPosRef.current || { x: event.clientX, y: event.clientY };
    var reactFlowBounds = document.getElementById('react-flow-root').getBoundingClientRect();
    var viewport = rfInstance.getViewport ? rfInstance.getViewport() : { x: 0, y: 0, zoom: 1 };
    var position = {
      x: (pos.x - reactFlowBounds.left - viewport.x) / viewport.zoom,
      y: (pos.y - reactFlowBounds.top - viewport.y) / viewport.zoom
    };
    var defaults = editor.getNodeDefaults(type);
    var typeMap = {
      condition: 'condition',
      market_check: 'market_check'
    };
    var nodeType = typeMap[type] || 'default';
    var newNode = {
      id: 'node_' + Date.now(),
      type: nodeType,
      position: position,
      data: defaults,
      selected: true
    };
    pushUndo();
    setNodes(function (nds) {
      return nds.concat(newNode);
    });
    // P3: 拖入后自动选中，立即在右侧打开该节点的配置面板
    setSelectedNode(newNode);
    window.editor.renderConfigPanel(newNode);
  }, [rfInstance]);

  // 节点/边变化时更新状态栏
  useEffect(function () {
    updateFooter(nodes, edges);
  }, [nodes, edges]);

  // 暴露 state 给全局 editor
  useEffect(function () {
    window.editor.__flowState = {
      setNodes: setNodes,
      setEdges: setEdges,
      rfInstance: rfInstance,
      getNodes: function getNodes() {
        return nodes;
      },
      getEdges: function getEdges() {
        return edges;
      },
      updateNodeConfig: updateNodeConfig,
      pushUndo: pushUndo
    };
    // 注册 accessor
    window.editor.setEdgeAccessor(function () {
      return nodes;
    }, function () {
      return edges;
    });
    // 通知内嵌加载器：__flowState 已就绪，可安全初始化（回调一次性消费）
    if (window.onFlowReady) {
      window.onFlowReady();
      window.onFlowReady = null;
    }
  }, [nodes, edges, rfInstance]);

  // 快捷键
  useEffect(function () {
    function handler(e) {
      // Ctrl+Z 撤销
      if (e.ctrlKey && e.key === 'z' && !e.shiftKey) {
        e.preventDefault();
        undo();
      }
      // Ctrl+Shift+Z 重做
      else if (e.ctrlKey && e.key === 'z' && e.shiftKey) {
        e.preventDefault();
        redo();
      }
      // Ctrl+S 保存
      else if (e.ctrlKey && e.key === 's') {
        e.preventDefault();
        if (window.editor) window.editor.save();
      }
      // Ctrl+A 全选
      else if (e.ctrlKey && e.key === 'a') {
        e.preventDefault();
        if (rfInstance) rfInstance.fitView();
      }
      // Ctrl+0 fit view
      else if (e.ctrlKey && e.key === '0') {
        e.preventDefault();
        if (rfInstance) rfInstance.fitView();
      }
    }
    window.addEventListener('keydown', handler);
    return function () {
      window.removeEventListener('keydown', handler);
    };
  }, [undo, redo]);
  return E(Provider, null, E(RF, {
    nodes: nodes,
    edges: edges,
    onNodesChange: function onNodesChange(changes) {
      _onNodesChange(changes);
    },
    onEdgesChange: function onEdgesChange(changes) {
      _onEdgesChange(changes);
    },
    onConnect: onConnect,
    nodeTypes: nodeTypes,
    fitView: nodes.length > 0,
    defaultViewport: { x: 0, y: 0, zoom: 1 },
    onInit: setRfInstance,
    onDragOver: onDragOver,
    onDrop: onDrop,
    onNodeClick: onNodeClick,
    onPaneClick: onPaneClick,
    deleteKeyCode: ['Backspace', 'Delete'],
    attributionPosition: 'bottom-left',
    className: 'react-flow-custom'
  }, E(Controls, {
    className: 'rf-controls'
  })));
}

// ── 挂载 / 卸载 React 应用（独立页首屏自动挂载；SPA 内嵌由加载器按需重挂载）──
window.mountFlowEditor = function(){
  var root = document.getElementById('react-flow-root');
  if (root && window.ReactDOM) ReactDOM.render(E(FlowEditor), root);
};
window.unmountWorkflowEditor = function(){
  var root = document.getElementById('react-flow-root');
  if (root && window.ReactDOM) ReactDOM.unmountComponentAtNode(root);
};
window.mountFlowEditor();
