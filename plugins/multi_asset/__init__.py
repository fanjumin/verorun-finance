"""VeroRun multi-asset data plugin.

Positioning: **asset coverage + data-governance base**. It covers futures /
options / funds / bonds with CFI-aligned instrument typing (ISO 10962:2021) and
exchange double-field modelling (code + exchange), and it owns the storage layer
where every bar carries provenance and licensing tags (license id, security
level, origin, dataset lineage) per GB/T 42775-2023.

It extends stock_analysis rather than duplicating it: the provider contract
(BaseProviderV2) and the DataGateway are reused as-is.

Scope: finance edition only (same as stock_analysis); the finance desktop edition
claims the plugin through its role orchestration (``managed_modules``), see
docs/role-integration.md.
"""

from plugin_manager.base import BasePlugin

from .routes import multi_asset_bp

# Declared capabilities (namespaces must appear in the implementation, per the
# discovery-time capability check).
#
# Deliberately NOT declared yet: the governance capabilities
# (data.ingest / data.pit / data.quality / data.classification). This release only
# reserves the governance *structure* -- the tables exist but nothing reads or
# writes them (models.DDL, R-section). Declaring a capability whose implementation
# does not exist would make the manifest lie, which is exactly what the
# manifest-honesty assertions guard against. They get declared when P1-P4 land.
CAPABILITIES = (
    "asset.futures",
    "asset.options",
    "asset.funds",
    "asset.bonds",
    "asset.data_fetch",
    "asset.storage",      # ma_bars persistence + storage_stats
    "asset.provenance",   # ma_fetch_log fetch audit trail
)


class MultiAssetPlugin(BasePlugin):
    name = "multi_asset"
    description = ("Multi-asset market data (futures, options, funds, bonds) with "
                   "CFI-aligned instrument typing and a governance-ready storage layer")
    author = "VeroRun"
    dependencies = {"stock_analysis": ">=2.0.1"}

    @property
    def version(self):
        info = getattr(self, "plugin_info", None)
        return getattr(info, "version", None) or "0.1.0"

    # ── lifecycle ───────────────────────────────────────────────────────────

    def on_install(self, registry) -> bool:
        """Create the plugin schema/tables.

        Following the SA-N1 lesson (no install-time table hook -> 500 on first use)
        we build the schema here, and models.ensure_tables() re-verifies on the first
        request so a fresh deployment or wiped DB self-heals.
        """
        try:
            from . import models
            models.ensure_tables()
        except Exception as err:
            self.log("multi_asset ensure_tables failed: %s" % err, "warning")
        return True

    def on_enable(self, registry) -> bool:
        self.log("Multi-asset plugin enabled")
        return True

    def on_uninstall(self, registry) -> bool:
        try:
            from . import models
            models.drop_schema()
        except Exception as err:
            self.log("multi_asset drop schema failed: %s" % err, "warning")
        return True

    # ── registration ────────────────────────────────────────────────────────

    def register_routes(self):
        return [multi_asset_bp]

    def register_jobs(self):
        """Daily reference-data refresh (main futures contracts + ETF list)."""
        return [
            {
                "id": "multi_asset_ref_refresh",
                "name": "Multi-Asset Reference Refresh",
                "func": self._ref_refresh_job,
                "trigger": "cron",
                "day_of_week": "mon-fri",
                "hour": 17,
                "minute": 30,
            },
        ]

    def register_health_checks(self):
        """Storage / contract / chain health, mirroring the stock_analysis pattern.

        Cold start must not raise a false alarm: an empty reference table is healthy.
        """
        checks = []

        try:
            from .models import get_db

            def _db_check():
                try:
                    conn = get_db()
                except Exception:
                    return False
                try:
                    conn.execute("SELECT 1").fetchall()
                    return True
                except Exception:
                    return False
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass

            checks.append({"id": "multi_asset_db",
                           "name": "Multi-Asset DB",
                           "check": _db_check})
        except Exception:
            pass

        try:
            from .adapters import CHAINS, CONTRACT_AVAILABLE

            def _contract_check():
                if not CONTRACT_AVAILABLE:
                    return False
                return all(len(v) >= 2 for v in CHAINS.values())

            checks.append({"id": "multi_asset_sources",
                           "name": "Multi-Asset Data Sources",
                           "check": _contract_check})
        except Exception:
            pass

        return checks

    def get_event_handlers(self):
        """No hook subscriptions (plugin.json hooks.listens is empty)."""
        return {}

    # ── cross-plugin capability surface (plugin-standard-v1.8 §12.7) ────────

    def get_provider(self, asset_type: str):
        """Return the primary provider instance for an asset class."""
        from .adapters import provider_chain, build_provider, CHAINS
        at = str(asset_type or "").upper()
        chain = provider_chain(at)
        if chain:
            return chain[0]
        if at in CHAINS:
            return build_provider(at, CHAINS[at][0][0])
        raise ValueError("unknown asset type: %r" % (asset_type,))

    def chain_sources(self, asset_type: str) -> list:
        from .adapters import chain_sources as _sources
        return _sources(asset_type)

    def fetch_bars(self, symbol: str, asset_type: str = None, exchange: str = None,
                   freq: str = "daily", datalen: int = 120, persist: bool = True):
        """Programmatic bars access for other plugins / agents."""
        from . import data_link
        return data_link.fetch_bars(symbol, asset_type=asset_type, exchange=exchange,
                                    freq=freq, datalen=datalen, persist=persist)

    def resolve_symbol(self, raw: str, asset_type: str = None, exchange: str = None):
        """Normalize a raw instrument code into the canonical {key, asset_type, ...} dict."""
        from . import asset_symbol as sym
        return sym.normalize(raw, asset_type=asset_type, exchange=exchange)

    # ── jobs ────────────────────────────────────────────────────────────────

    def _ref_refresh_job(self):
        try:
            from .ref_sync import sync_all
            result = sync_all()
            self.log("reference sync: %s" % (result,))
        except Exception as err:
            self.log("reference sync job failed: %s" % err)


__all__ = ["MultiAssetPlugin", "multi_asset_bp", "CAPABILITIES"]
