"""neural_flow — 平台级 AI 数据流可视化基座（NeuralHub 后端，方案 v1.3 §6.2）。

定位（2026-09-26 平台化拍板）：
  - 神经中枢从金融版专用页升级为平台级能力；本插件为行业无关基座；
  - 行业差异全部由 Domain Profile 声明（domain.yaml / 内置档案），渲染引擎零行业代码；
  - 股票域（stock_analysis P1 埋点，commit 69da8cfb）是第一个数据源，不改动其代码。

通道设计（对齐 v1.3 §7.8 单连接铁律 / B3 修正）：
  - 实时：SDK 双写 sa_sse_events(topic='flow')——桌面端沿用现有单条 SSE 连接
    （stock_analysis /api/events 已放行 flow 主题），不新增并发闸占用；
    sa_sse_events 表归 stock_analysis 所有，本插件只写不建（跨插件直写同库，
    与 FusionPageV3 ontology 跨插件直读同范式）；若未来 stock_analysis 缺席的
    edition 需要独立实时通道，再评估自有出流表 + 单连接切换（列开放项）。
  - 归档：自有 nf_flow_spans（30 天，回放/钻取，跨域共表）。

权限门控（manager.py:84 CAPABILITY_PERMISSIONS）：第三方插件无官方豁免，
plugin.json 显式声明 routes/scheduler/events，缺一则对应 register_* 静默跳过。
"""
from plugin_manager.base import BasePlugin


def _lifecycle_binding(plugin, event):
    """把事件名闭包进 handler（EventBus 只传 kwargs，不传事件名）。"""
    def _handler(**kwargs):
        plugin._on_plugin_lifecycle(event, **kwargs)
    return _handler


class NeuralFlowPlugin(BasePlugin):
    name = "neural_flow"

    @property
    def version(self):
        # §13.1 版本号铁律：兜底值统一 '0.1.0'（对齐 BasePlugin 默认），禁硬编码版本
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'

    description = "Platform AI dataflow observability base"
    author = "fanjumin"

    # ── 能力注册（门控：plugin.json permissions 显式声明）──

    def register_routes(self):
        from .routes import neural_flow_bp
        return [neural_flow_bp]

    def register_jobs(self):
        # 调度器将 max_instances/coalesce 固定为 1/True（同 stock_analysis 注释口径）；
        # 采集器内部再自持 advisory lock，多 worker 场景仅一方执行（照抄
        # stock_analysis alert_engine.scheduled_scan 的 D1-c 互斥范式）。
        try:
            interval = max(1, int(self.get_config_value("collector_interval_seconds", 5) or 5))
        except (TypeError, ValueError):
            interval = 5
        return [
            {
                "id": "neural_flow_collector",
                "name": "NeuralFlow Platform Collector",
                "func": self._collector_job,
                "trigger": "interval",
                "seconds": interval,
            },
            {
                # 归档清理：保留 30 天（旁路，失败不影响主链路）
                "id": "neural_flow_span_prune",
                "name": "NeuralFlow Span Prune",
                "func": self._prune_job,
                "trigger": "cron",
                "day": "*",
                "hour": 3,
                "minute": 41,
            },
        ]

    def get_event_handlers(self):
        """保留 'events' 权限门控入口，但不在此注册（返回空映射）。

        管理器把返回字典注册进 HookRegistry，派发键是点号替换后的斜杠形式
        （manager.py:2704-2706 约定 plugin.installed → plugin/installed），
        而事件真实发射走 EventBus（点号键）——两条键空间永不相等，注册即死代码。
        真正的订阅在 activate() 内用 bus.on()（范式同 memory_engine）。
        """
        return {}

    # ── EventBus 订阅（activate/deactivate 成对，幂等）──

    def _event_bindings(self):
        """返回 [(event, handler)]；handler 实例缓存，保证 off() 能反注册。"""
        cached = getattr(self, '_bus_bindings', None)
        if cached:
            return cached
        from plugin_manager.event_bus import EventName
        bindings = [(EventName.AGENT_TASK_COMPLETED, self._on_task_completed)]
        for event in (EventName.PLUGIN_INSTALLED, EventName.PLUGIN_ENABLED,
                      EventName.PLUGIN_DISABLED):
            bindings.append((event, _lifecycle_binding(self, event)))
        self._bus_bindings = bindings
        return bindings

    def _subscribe_events(self):
        from plugin_manager.event_bus import get_event_bus
        bus = get_event_bus()
        for event, handler in self._event_bindings():
            bus.off(event, handler)   # 幂等：重复 activate 不叠加订阅
            bus.on(event, handler)

    def _unsubscribe_events(self):
        from plugin_manager.event_bus import get_event_bus
        bus = get_event_bus()
        for event, handler in self._event_bindings():
            bus.off(event, handler)

    # ── 生命周期 ──

    def setup(self):
        """[ENABLED] 建表 + 档案注册（新系统阶段钩子；不依赖 on_install 桥接语义）。"""
        reg = getattr(self, 'manager', None)
        try:
            from .models_nf import ensure_tables
            ensure_tables()
        except Exception as err:
            self.log("neural_flow ensure_tables failed: %s" % err, 'warning')
            return False
        try:
            from .profile_registry import refresh_registry
            n = refresh_registry()
            self.log("neural_flow setup: %d profile(s) registered" % n)
        except Exception as err:
            self.log("profile registry refresh failed: %s" % err, 'warning')
        return True

    def activate(self):
        """[ACTIVE] 运行时资源：采集器游标初始化 + EventBus 订阅。

        注意（base.py 生命周期勘误）：仅 _preload_routes 补调 activate() 的路径不走
        on_enable——所以运行时状态初始化放这里，不依赖 on_enable 桥接。
        """
        try:
            from .collectors import init_cursors
            init_cursors()
        except Exception as err:
            self.log("collector cursor init failed: %s" % err, 'warning')
        try:
            self._subscribe_events()
        except Exception as err:
            self.log("event subscribe failed: %s" % err, 'warning')
        return True

    def deactivate(self):
        try:
            self._unsubscribe_events()
        except Exception as err:
            self.log("event unsubscribe failed: %s" % err, 'warning')
        return True

    def on_uninstall(self, registry):
        """[UNINSTALL] 删除本插件自有 schema（零残留，标准 §12.5）。

        仅 DROP neural_flow schema（CASCADE），不触碰 public 与其他插件 schema；
        失败不阻塞卸载流程，记日志。
        """
        try:
            from .models_nf import drop_schema
            drop_schema()
            self.log("neural_flow schema dropped")
        except Exception as err:
            self.log("schema drop failed: %s" % err, 'warning')
        return True

    # ── 任务体 ──

    def _collector_job(self):
        """平台表增量采集（v1：agent_token_logs → llm 域 span）。

        失败静默降级：可视化是旁路，异常不阻塞调度、不计入主链路错误统计
        （与 _prune_job / setup / activate 同口径，仅落插件日志）。
        """
        try:
            from .collectors import run_once
            run_once()
        except Exception as err:
            self.log("collector failed: %s" % err, 'warning')

    def _prune_job(self):
        try:
            from .models_nf import prune_spans
            days = int(self.get_config_value("span_retention_days", 30) or 30)
            removed = prune_spans(retention_days=days)
            self.log("span prune removed=%s" % removed)
        except Exception as err:
            self.log("span prune failed: %s" % err)

    # ── EventBus handlers ──

    def _on_task_completed(self, **kwargs):
        """Agent 任务完成 → agent 域 span（平台级 Agent 活动流）。

        载荷契约 = agent_runner._emit_task_completed 实发：
          task(dict) / result(dict) / agent_id / agent_name（无顶层 task_id/session_id）。
        """
        try:
            from .sdk import emit_span
            task = kwargs.get("task") if isinstance(kwargs.get("task"), dict) else {}
            emit_span(
                trace_id=task.get("task_id") or kwargs.get("agent_id") or "unknown",
                domain="agent", stage="output", event="end",
                message="agent task completed",
                meta={k: v for k, v in (("agent_id", kwargs.get("agent_id")),
                                        ("agent_name", kwargs.get("agent_name")),
                                        ("session_id", task.get("session_id")))
                      if v is not None},
            )
        except Exception:
            pass  # 旁路，不上抛（EventBus handler 异常会进 guard 统计）

    def _on_plugin_lifecycle(self, event, **kwargs):
        """插件启停 → system 域 span + 触发 Profile 注册表刷新。

        event 由订阅处闭包注入；标识键为 plugin_id（manager._emit 实传键名）。
        """
        try:
            from .sdk import emit_span
            emit_span(
                trace_id="system",
                domain="system", stage="data", event="progress",
                message="plugin lifecycle: %s" % (kwargs.get("plugin_id") or "?"),
                meta={"event": event},
            )
        except Exception:
            pass
        try:
            from .profile_registry import refresh_registry
            refresh_registry()
        except Exception:
            pass


__all__ = ["NeuralFlowPlugin"]
