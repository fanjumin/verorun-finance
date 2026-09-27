"""VeroRun self-contained stock analysis plugin."""

from plugin_manager.base import BasePlugin

from .routes import stock_analysis_bp


class StockAnalysisPlugin(BasePlugin):
    name = "stock_analysis"
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = "A-share multi-dimensional stock analysis"
    author = "easykai"

    def register_routes(self):
        return [stock_analysis_bp]

    def register_agents(self):
        # 注册能力到 finance 角色（05-finance.yaml），当前非独立版
        try:
            from agent_matrix.models import register_capability_to_role
            register_capability_to_role(
                domain='finance',
                name='Stock Analysis Agent',
                capabilities=[
                    "stock.technical_analysis",
                    "stock.fundamental_analysis",
                    "stock.sentiment_analysis",
                    "stock.signal_explanation",
                ],
                description='Explains technical, fundamental and sentiment signals for A-share research.',
            )
        except Exception as e:
            self.log(f'Register capabilities failed: {e}', 'warning')

        return [
            {
                "identifier": "stock_analysis_agent",
                "name": "Stock Analysis Agent",
                "description": "Explains technical, fundamental and sentiment signals for A-share research.",
                "role_type": "sub",
                "domain": "finance",
                "prompt_file": "agents/stock_analysis_agent_prompt.md",
                "capabilities": [
                    "stock.technical_analysis",
                    "stock.fundamental_analysis",
                    "stock.sentiment_analysis",
                    "stock.signal_explanation",
                ],
                "enabled_by_default": True,
            }
        ]

    def register_jobs(self):
        # 交易日收盘后（15:05）批量分析 watchlist 标的；调度器将 max_instances/coalesce 固定为 1/True
        from .batch import run_batch
        return [
            {
                "id": "stock_analysis_daily_batch",
                "name": "Stock Analysis Daily Batch",
                "func": run_batch,
                "trigger": "cron",
                "day_of_week": "mon-fri",
                "hour": 15,
                "minute": 5,
            },
            {
                # P0-2：交易日 16:00 增量回算信号兑现（失败不影响批量主链路）
                "id": "stock_analysis_signal_realize",
                "name": "Stock Analysis Signal Realize",
                "func": self._signal_realize_job,
                "trigger": "cron",
                "day_of_week": "mon-fri",
                "hour": 16,
                "minute": 0,
            },
            {
                # D1-c：告警规则周期扫描（60s）；advisory lock 在 scheduled_scan 内自持，
                # 多 worker 各自调度器触发时仅一方执行（见 alert_engine.scheduled_scan）
                "id": "stock_analysis_alert_scan",
                "name": "Stock Analysis Alert Scan",
                "func": self._alert_scan_job,
                "trigger": "interval",
                "seconds": 60,
            },
            {
                # P1：神经中枢 flow span 归档清理（保留 30 天，失败不影响主链路）
                "id": "stock_analysis_flow_prune",
                "name": "Stock Analysis Flow Span Prune",
                "func": self._flow_prune_job,
                "trigger": "cron",
                "day": "*",
                "hour": 3,
                "minute": 17,
            },
        ]

    def _flow_prune_job(self):
        """每日清理超期 flow span 归档（sa_flow_spans 保留 30 天，旁路）。"""
        try:
            from .models_sa import prune_flow_spans
            removed = prune_flow_spans(retention_days=30)
            self.log("flow span prune removed=%s" % removed)
        except Exception as err:
            self.log("flow span prune failed: %s" % err)

    def register_dag_nodes(self):
        """注册自定义工作流节点：stock_deep_research (v1) + stock.rs_* (v2 多智能体流水线)。"""
        from .deep_research import get_dag_nodes as _v1_nodes
        from .research_dag import get_dag_nodes as _v2_nodes
        nodes = {}
        nodes.update(_v1_nodes())
        nodes.update(_v2_nodes())
        return nodes

    def _signal_realize_job(self):
        """独立回算任务：增量兑现 sa_signal_log → sa_signal_realized。"""
        try:
            from .signal_quality import realize_signals
            result = realize_signals(days_back=90)
            self.log("signal realize: %s" % result)
        except Exception as err:
            self.log("signal realize job failed: %s" % err)

    def _alert_scan_job(self):
        """D1-c 告警周期扫描：scheduled_scan 内自持 advisory lock，多 worker 单跑。"""
        try:
            from .alert_engine import scheduled_scan
            result = scheduled_scan()
            if result.get("events"):
                self.log("alert scan: %d event(s) triggered" % result["events"])
        except Exception as err:
            self.log("alert scan job failed: %s" % err)

    def on_enable(self, registry):
        self.log("Stock analysis plugin enabled")
        try:
            from plugin_manager.event_bus import get_event_bus, EventName
            self._bus = get_event_bus()
            self._bus.on(EventName.SCHEDULER_JOB_COMPLETED, self._on_job_completed)
        except Exception as err:
            self.log("event subscribe failed: %s" % err)
            self._bus = None
        # 证券主数据后台预热：/api/search 依赖 sa_symbol_master（外部源单次约 17s），
        # 启动时异步拉取，避免用户首次搜索等待、也不阻塞插件启用。
        try:
            from .symbol_master import warm_async
            warm_async()
        except Exception as err:
            self.log("symbol master warm kick failed: %s" % err)
        return True

    def on_disable(self, registry):
        if getattr(self, "_bus", None):
            try:
                from plugin_manager.event_bus import EventName
                self._bus.off(EventName.SCHEDULER_JOB_COMPLETED, self._on_job_completed)
            except Exception:
                pass
        self.log("Stock analysis plugin disabled")
        return True

    def _on_job_completed(self, **kwargs):
        """批量完成 → 高置信标的自动深研（接线点③）。

        scheduler.job_completed 属同步前缀事件，由 batch.run_batch 收尾处自产发射
        （内核调度器不发射该事件）。处理器只做判断 + 入队（submit_job 仅插 sa_jobs 行），
        重活全在队列线程执行，满足同步事件处理器轻量要求。
        """
        if (kwargs.get("job_id") or "") != "stock_analysis_daily_batch":
            return
        if not self.get_config_value("auto_deep_research_on_batch", False):
            return
        try:
            from .batch import latest_high_confidence_symbols
            from .jobs_queue import submit_job
            for symbol in latest_high_confidence_symbols(limit=3):
                submit_job(symbol, scope="full", force=True)
        except Exception as err:
            self.log("auto deep research trigger failed: %s" % err)


__all__ = ["StockAnalysisPlugin", "stock_analysis_bp"]
