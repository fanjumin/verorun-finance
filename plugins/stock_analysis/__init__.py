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

    def on_enable(self, registry):
        self.log("Stock analysis plugin enabled")
        return True

    def on_disable(self, registry):
        self.log("Stock analysis plugin disabled")
        return True


__all__ = ["StockAnalysisPlugin", "stock_analysis_bp"]
