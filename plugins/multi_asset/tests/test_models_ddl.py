#!/usr/bin/env python3
"""V-06 / V-07 — DDL idempotency and single-table naming.

V-06 (audit P1-5): the draft used ``CREATE TYPE ... AS ENUM``, which has no
    ``IF NOT EXISTS`` -- a second run always dies with ``duplicate_object``. The
    shipped DDL must be re-runnable, and asset types are expressed as
    ``VARCHAR(8) + CHECK`` instead.
V-07 (audit P1-6): the document contradicted itself between ``bars`` and
    ``ma_bars``; exactly one time-series table name may exist.

The static checks always run; the live round-trip is skipped when PostgreSQL is
unreachable so the suite stays green on a workstation without a local DB.
"""
import inspect
import re
import unittest

from plugins.multi_asset import models


def _pg_available() -> bool:
    try:
        from plugins._base.db import get_pooled_connection
        conn = get_pooled_connection()
        try:
            conn.execute("SELECT 1").fetchall()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return True
    except Exception:
        return False


class DdlStaticTest(unittest.TestCase):
    """Everything checkable without a database."""

    def setUp(self):
        self.sql = " ; ".join(models.DDL)

    def test_every_create_is_idempotent(self):
        heads = re.findall(r"CREATE\s+(?:TABLE|INDEX|UNIQUE\s+INDEX)\s+[^(\n]*",
                           self.sql, flags=re.IGNORECASE)
        self.assertTrue(heads)
        for head in heads:
            with self.subTest(head=head.strip()):
                self.assertIn("IF NOT EXISTS", head.upper())

    def test_no_enum_type_creation(self):
        """P1-5: ``CREATE TYPE`` is not idempotent -- must not appear at all."""
        self.assertNotIn("CREATE TYPE", self.sql.upper())
        self.assertNotIn("AS ENUM", self.sql.upper())

    def test_alter_column_is_idempotent_if_present(self):
        # A2/A3-a put constraint changes inside DO $$ ... $$ blocks whose
        # pg_constraint existence checks are the idempotency guard; strip those
        # blocks and evaluate only bare ALTER statements here.
        sql_no_do = re.sub(r"DO\s+\$\$.*?\$\$\s*;", " ", self.sql,
                           flags=re.IGNORECASE | re.DOTALL)
        for stmt in re.findall(r"ALTER TABLE[^;]*", sql_no_do, flags=re.IGNORECASE):
            with self.subTest(stmt=stmt):
                # DROP DEFAULT on a column already without one is NOTICE-level.
                if "DROP DEFAULT" in stmt.upper():
                    continue
                self.assertIn("IF NOT EXISTS", stmt.upper())

    # ── R-section: irreversible structure reservation ───────────────────────

    def _table_ddl(self, table):
        """Return the DDL statement that creates ``table``."""
        for stmt in models.DDL:
            if re.search(r"CREATE TABLE IF NOT EXISTS\s+%s\b" % table, stmt,
                         flags=re.IGNORECASE):
                return stmt
        self.fail("table DDL not found: %s" % table)
        return ""                                   # pragma: no cover

    def test_governance_tag_columns_exist_on_ma_bars(self):
        """R1: every bar must be able to carry its licence and level.

        GB/T 42775-2023 says externally sourced data must not be downgraded, so
        provenance and licence have to travel with the row itself.
        """
        for column in ("license_id", "security_level", "origin",
                       "dataset_id", "ingested_at"):
            with self.subTest(column=column):
                self.assertIn(column, self.sql)

    def test_security_level_is_bounded_to_the_standard_range(self):
        """R1: GB/T 42775-2023 grades 1..4; the column must reject anything else."""
        self.assertRegex(self.sql, r"security_level\s+BETWEEN\s+1\s+AND\s+4")

    def test_governance_skeleton_tables_are_declared_and_created(self):
        """R2-R5: licence ledger, dataset registry, PIT tables, quality reports."""
        expected = ("ma_license", "ma_dataset_registry", "ma_pit_fundamentals",
                    "ma_pit_consensus", "ma_index_membership", "ma_quality_report")
        for table in expected:
            with self.subTest(table=table):
                self.assertIn(table, models.TABLES)
                self.assertIn("CREATE TABLE IF NOT EXISTS %s" % table, self.sql)

    def test_pit_tables_carry_both_time_axes(self):
        """The point of PIT is valid-time x system-time; both axes must be present."""
        for table in ("ma_pit_fundamentals", "ma_pit_consensus"):
            with self.subTest(table=table):
                block = self._table_ddl(table)
                for column in ("valid_from", "system_from", "system_to"):
                    self.assertIn(column, block)

    def test_index_membership_keeps_a_valid_to_edge(self):
        """Survivorship bias is avoided only if delisted members keep their edge."""
        self.assertIn("valid_to", self._table_ddl("ma_index_membership"))

    def test_licence_defaults_are_most_restrictive(self):
        """Q5: imported licences cannot be verified after the fact, so default to no."""
        block = self._table_ddl("ma_license")
        for column in ("allow_export", "allow_forward", "allow_llm"):
            with self.subTest(column=column):
                self.assertRegex(block, r"%s\s+SMALLINT\s+DEFAULT\s+0" % column)

    # ── R-section patch (A1-A5): governance gap closure ─────────────────────

    def test_license_records_provider_assigned_level(self):
        """A1: GB/T 42775 §8.4 needs the provider's grade as the comparison LHS."""
        block = self._table_ddl("ma_license") + self.sql
        self.assertIn("level_as_provided", block)
        self.assertRegex(
            self.sql,
            r"level_as_provided\s+SMALLINT[^)]*BETWEEN\s+1\s+AND\s+4")

    def test_security_level_is_bounded_on_every_governance_table(self):
        """A2: R-section only bounded ma_bars; registry/PIT columns were bare."""
        for constraint in ("ck_ma_dataset_registry_level",
                           "ck_ma_pit_fundamentals_level",
                           "ck_ma_pit_consensus_level",
                           "ck_ma_index_membership_level"):
            with self.subTest(constraint=constraint):
                self.assertIn(constraint, self.sql)

    def test_pit_fundamentals_distinguishes_statement_types(self):
        """A3-a: balance/income/cashflow share metric names; key needs statement_type."""
        block = self._table_ddl("ma_pit_fundamentals")
        self.assertIn("statement_type", block)
        # New-shape unique key in the CREATE block ...
        self.assertRegex(
            block,
            r"UNIQUE\s*\(\s*symbol,\s*exchange,\s*report_period,\s*"
            r"statement_type,\s*metric,\s*valid_from,\s*system_from")
        # ... and the idempotent migration guard for already-created databases.
        self.assertIn("uq_ma_pit_fundamentals", self.sql)
        self.assertRegex(self.sql, r"DROP CONSTRAINT uq_ma_pit_fundamentals")

    def test_pit_system_time_has_no_insert_default(self):
        """A4: system_from must come from publication facts, never silently now()."""
        for table in ("ma_pit_fundamentals", "ma_pit_consensus",
                      "ma_index_membership"):
            with self.subTest(table=table):
                block = self._table_ddl(table)
                self.assertNotRegex(
                    block,
                    r"system_from\s+TIMESTAMPTZ\s+NOT NULL\s+DEFAULT\s+now\(\)",
                    "%s.system_from still defaults to insertion time" % table)
        # Existing databases lose the default through explicit DROP DEFAULT.
        self.assertEqual(
            len(re.findall(r"ALTER COLUMN system_from DROP DEFAULT", self.sql,
                           flags=re.IGNORECASE)),
            3)

    def test_index_membership_is_valid_axis_only_by_design(self):
        """A3-b(i): half-temporal membership is intentional, not an oversight."""
        block = self._table_ddl("ma_index_membership")
        self.assertNotIn("system_to", block)
        self.assertNotIn("announced_date", block)

    def test_upsert_preserves_tags_on_untagged_refresh(self):
        """A5: free-feed re-fetch must not wipe a later-attached licence/level."""
        source = inspect.getsource(models.upsert_bars)
        for column in ("license_id", "security_level", "origin", "dataset_id"):
            with self.subTest(column=column):
                self.assertRegex(
                    source,
                    r"%s\s*=\s*COALESCE\(EXCLUDED\.%s,\s*ma_bars\.%s\)"
                    % (column, column, column))
        self.assertIn("ingested_at = ma_bars.ingested_at", source)
        self.assertIn("COALESCE(?, now())", source)

    def test_get_bars_returns_governance_tags(self):
        """A5: every returned row must answer licence/level/origin/dataset/when."""
        source = inspect.getsource(models.get_bars)
        for column in ("license_id", "security_level", "origin",
                       "dataset_id", "ingested_at"):
            with self.subTest(column=column):
                self.assertIn(column, source)

    def test_backfill_function_is_scope_guarded_and_parameterized(self):
        """A5: tag_dataset_rows exists and never accepts raw SQL fragments."""
        self.assertTrue(callable(models.tag_dataset_rows))
        source = inspect.getsource(models.tag_dataset_rows)
        self.assertIn("all_rows", source)
        self.assertIn("raise ValueError", source)
        # The IN-list is expanded from ? placeholders, not interpolated values.
        self.assertIn('",".join(["?"]', source.replace(" ", ""))
        self.assertIn('sets = ["dataset_id = ?"]', source)
        # No f-strings: every externally supplied value must travel as a param.
        self.assertNotIn('f"', source)
        self.assertNotIn("f'", source)

    def test_asset_type_is_constrained_by_check(self):
        self.assertIn("CHECK", self.sql.upper())
        for asset_type in models.ASSET_TYPES:
            with self.subTest(asset_type=asset_type):
                self.assertIn("'%s'" % asset_type, self.sql)

    def test_declared_tables_match_the_ddl(self):
        created = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)",
                                 self.sql, flags=re.IGNORECASE))
        self.assertEqual(created, set(models.TABLES))

    def test_single_time_series_table_name(self):
        """P1-6: one and only one bar table, and it is ``ma_bars``."""
        created = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)",
                                 self.sql, flags=re.IGNORECASE))
        bar_tables = {t for t in created if t.endswith("bars")}
        self.assertEqual(bar_tables, {"ma_bars"})
        self.assertNotIn("sa_bars", self.sql)

    def test_all_tables_are_schema_prefixed(self):
        for table in models.TABLES:
            with self.subTest(table=table):
                self.assertTrue(table.startswith("ma_"))

    def test_bars_unique_key_covers_business_identity(self):
        for column in ("asset_type", "symbol", "exchange", "freq",
                       "trade_date", "bar_time"):
            with self.subTest(column=column):
                self.assertIn(column, self.sql)
        self.assertIn("UNIQUE", self.sql.upper())

    def test_upsert_is_conflict_based_and_not_a_plain_insert(self):
        """Idempotent writes come from ON CONFLICT, not from clearing the table."""
        source = inspect.getsource(models.upsert_bars)
        self.assertIn("ON CONFLICT", source)
        self.assertNotIn("TRUNCATE", source.upper())
        self.assertNotIn("DELETE FROM", source.upper())

    def test_schema_is_dedicated_and_not_the_equity_one(self):
        self.assertEqual(models.SCHEMA, "multi_asset")
        self.assertNotEqual(models.SCHEMA, "stock_analysis")

    def test_required_public_functions_exist(self):
        for name in ("ensure_tables", "drop_schema", "get_db", "upsert_bars",
                     "get_bars", "tag_dataset_rows",
                     "record_fetch", "list_fetch_log",
                     "storage_stats", "search_ref_instruments",
                     "upsert_fund_ref", "upsert_bond_ref",
                     "upsert_future_contracts", "upsert_option_contracts",
                     "upsert_option_greeks"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(models, name, None)),
                                "missing model function: %s" % name)

    def test_ensure_tables_is_guarded_by_a_module_flag(self):
        """Repeated calls must not re-run DDL on every request."""
        self.assertIn("_tables_ready", dir(models))
        self.assertIn("_ensure_lock", dir(models))


@unittest.skipUnless(_pg_available(), "PostgreSQL is not reachable from this host")
class DdlLiveTest(unittest.TestCase):
    """V-06 live proof: build the schema twice and read the bars table back."""

    def test_ensure_tables_is_re_runnable(self):
        models._tables_ready = False
        self.assertTrue(models.ensure_tables())
        models._tables_ready = False          # force a second, real DDL pass
        try:
            self.assertTrue(models.ensure_tables())
        finally:
            models._tables_ready = True

    def test_bars_round_trip_is_idempotent(self):
        import datetime
        models.ensure_tables()
        row = {"asset_type": "FUTURE", "symbol": "rb2610", "exchange": "SHFE",
               "freq": "daily", "trade_date": datetime.date(2026, 3, 6),
               "bar_time": "00:00", "open": 3000, "high": 3050, "low": 2980,
               "close": 3020, "volume": 1000, "amount": None, "value": None,
               "source": "test"}
        models.upsert_bars([row])
        first = models.get_bars("FUTURE", "rb2610", exchange="SHFE", limit=10)
        models.upsert_bars([dict(row, close=3100)])       # same key -> update
        second = models.get_bars("FUTURE", "rb2610", exchange="SHFE", limit=10)
        self.assertEqual(len(first), len(second))
        self.assertEqual(float(second[-1]["close"]), 3100.0)

    def test_governance_tags_survive_untagged_refresh(self):
        """A5 live: tagged row refreshed by a tag-less free feed keeps its tags."""
        import datetime
        models.ensure_tables()
        trade_date = datetime.date(2026, 3, 9)
        tagged = {"asset_type": "FUTURE", "symbol": "rb2611", "exchange": "SHFE",
                  "freq": "daily", "trade_date": trade_date,
                  "close": 3200, "source": "vendor-file",
                  "license_id": "lic-1", "security_level": 2,
                  "origin": "upload", "dataset_id": "ds-1"}
        models.upsert_bars([tagged])
        models.upsert_bars([{"asset_type": "FUTURE", "symbol": "rb2611",
                             "exchange": "SHFE", "freq": "daily",
                             "trade_date": trade_date, "close": 3210,
                             "source": "akshare"}])
        got = models.get_bars("FUTURE", "rb2611", exchange="SHFE",
                              limit=10)[-1]
        self.assertEqual(float(got["close"]), 3210.0)        # market refreshed
        self.assertEqual(got["source"], "akshare")
        self.assertEqual(got["license_id"], "lic-1")         # tags retained
        self.assertEqual(int(got["security_level"]), 2)
        self.assertEqual(got["origin"], "upload")
        self.assertEqual(got["dataset_id"], "ds-1")
        self.assertTrue(got["ingested_at"])

    def test_backfill_attaches_dataset_to_free_feed_rows(self):
        """A5 live: rows enter tag-less, then get tagged by an explicit scope."""
        import datetime
        models.ensure_tables()
        # Scope guard: no filter and no all_rows -> refuse.
        with self.assertRaises(ValueError):
            models.tag_dataset_rows(dataset_id="ds-x")
        trade_date = datetime.date(2026, 3, 10)
        models.upsert_bars([{"asset_type": "FUTURE", "symbol": "cu2611",
                             "exchange": "SHFE", "freq": "daily",
                             "trade_date": trade_date, "close": 78000,
                             "source": "akshare"}])
        affected = models.tag_dataset_rows(
            dataset_id="ds-2", origin="upload", asset_type="FUTURE",
            symbols=["cu2611"])
        self.assertEqual(affected, 1)
        got = models.get_bars("FUTURE", "cu2611", exchange="SHFE",
                              limit=10)[-1]
        self.assertEqual(got["dataset_id"], "ds-2")
        self.assertEqual(got["origin"], "upload")
        # Columns not passed to the partial update stay untouched.
        self.assertIsNone(got["license_id"])
        self.assertIsNone(got["security_level"])

    def test_security_level_check_rejects_out_of_range_grade(self):
        """A2 live: grade 9 must be rejected by the table constraint."""
        import datetime
        models.ensure_tables()
        with self.assertRaises(Exception):
            models.upsert_bars([{"asset_type": "FUTURE", "symbol": "au2612",
                                 "exchange": "SHFE", "freq": "daily",
                                 "trade_date": datetime.date(2026, 3, 11),
                                 "close": 600, "security_level": 9}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
