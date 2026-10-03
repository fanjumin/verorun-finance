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
            {
                # LLM 响应缓存清理（sa_llm_cache 保留 7 天；报告 JSONB 约 2-4KB/行，
                # 不清理会持续膨胀。与 flow_prune 错开 3 分钟避免同刻争抢连接）
                "id": "stock_analysis_llm_cache_prune",
                "name": "Stock Analysis LLM Cache Prune",
                "func": self._llm_cache_prune_job,
                "trigger": "cron",
                "day": "*",
                "hour": 3,
                "minute": 20,
            },
            {
                # O6：交易日 08:30 晨报（汇总上一交易日信号 → 钩子派发 → email 推送）。
                # 与 15:05 批量错开；无信号时 build_morning_brief 返回 None，不派发。
                "id": "stock_analysis_morning_brief",
                "name": "Stock Analysis Morning Brief",
                "func": self._morning_brief_job,
                "trigger": "cron",
                "day_of_week": "mon-fri",
                "hour": 8,
                "minute": 30,
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

    def _llm_cache_prune_job(self):
        """每日清理超期 LLM 响应缓存（sa_llm_cache 保留 7 天，旁路）。"""
        try:
            from .models_sa import purge_llm_cache
            removed = purge_llm_cache(retain_days=7)
            self.log("llm cache prune removed=%s" % removed)
        except Exception as err:
            self.log("llm cache prune failed: %s" % err)

    def register_dag_nodes(self):
        """注册自定义工作流节点：stock_deep_research (v1) + stock.rs_* (v2 多智能体流水线)。"""
        from .deep_research import get_dag_nodes as _v1_nodes
        from .research_dag import get_dag_nodes as _v2_nodes
        nodes = {}
        nodes.update(_v1_nodes())
        nodes.update(_v2_nodes())
        return nodes

    def register_health_checks(self):
        """数据源与存储健康检查（D7），范式取自 project_workspace/__init__.py:178。

        背景：此前数据源健康只有进程内滑窗（metrics_collector），重启即清零、
        不落库、管理页不可见 —— 免费源断供时无人知晓。这里把它接到平台 health 缝上，
        与 net_proxy / project_workspace / veroscholar 等一致地出现在管理页。
        """
        checks = []

        # 1) 数据库：sa_* schema 连通性
        try:
            from .models_sa import get_db
            conn = get_db()
            try:
                conn.execute("SELECT 1").fetchall()
                checks.append({'id': 'stock_analysis_db',
                               'name': 'Stock Analysis DB',
                               'check': lambda: True})
            except Exception:
                checks.append({'id': 'stock_analysis_db',
                               'name': 'Stock Analysis DB',
                               'check': lambda: False})
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            pass

        # 2) 数据源取数命中率：窗口内失败率 ≥ 50% 判为不健康。
        #    无调用时判为健康 —— 冷启动/盘后未取数 ≠ 数据源故障，避免恒定假告警。
        try:
            from .metrics_collector import snapshot

            def _provider_check():
                try:
                    rows = snapshot()
                except Exception:
                    return False
                calls = sum(r.get('calls') or 0 for r in rows)
                if calls <= 0:
                    return True
                ok = sum(r.get('ok') or 0 for r in rows)
                return (ok / calls) >= 0.5

            checks.append({'id': 'stock_analysis_providers',
                           'name': 'Stock Data Providers',
                           'check': _provider_check})
        except Exception:
            pass
        return checks

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

    def _morning_brief_job(self):
        """O6 晨报：交易日 08:30 汇总上一交易日信号并以钩子派发（旁路）。"""
        try:
            from .morning_brief import dispatch_morning_brief
            result = dispatch_morning_brief()
            if result.get("skipped"):
                self.log("morning brief skipped: %s" % result.get("reason"))
            else:
                self.log("morning brief sent: trade_date=%s total=%s"
                         % (result.get("trade_date"), result.get("total")))
        except Exception as err:
            self.log("morning brief job failed: %s" % err)

    def on_enable(self, registry):
        self.log("Stock analysis plugin enabled")
        # 事件订阅走 event_bus 而非 get_event_handlers()，原因（勿改，改了会静默失效）：
        #   scheduler.job_completed 由 batch.py 以 get_event_bus().emit() **自产发射**
        #   （内核调度器不发射该事件，见 batch.py:163 注释）；而 get_event_handlers()
        #   返回的处理器只会被 PluginManager 注册进 hook_registry（manager.py:1141），
        #   收不到 event_bus 发射的事件。二者是两条独立通道，须按事件源选择订阅方式。
        #   误改为 get_event_handlers() 会让「批量完成 → 自动深研」联动彻底失效。
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
