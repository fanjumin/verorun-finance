#!/usr/bin/env python3
"""V-09 / V-13 — i18n integrity and plugin.json <-> implementation consistency.

V-09: the i18n standard requires English source strings as keys, identical key sets
      in ``en.yml`` / ``zh-CN.yml``, and a ``plugin.name`` entry.
V-13: the manifest must describe the plugin that actually exists -- capabilities
      matching ``CAPABILITIES``, ``hooks.provides`` matching the event constants,
      permissions covering the registration hooks that are implemented, and a
      settings schema that only exposes providers that are really wired.

Also enforces the i18n *hard* rules on the embedded page (no CJK literals, and no
translation call wrapping a Jinja expression), which ``scripts/i18n_check.py``
checks repo-wide.
"""
import json
import pathlib
import re
import unittest

from plugins.multi_asset import __file__ as pkg_init
from plugins.multi_asset import CAPABILITIES, MultiAssetPlugin, events
from plugins.multi_asset.adapters import CHAINS, REGISTRY
from plugins.multi_asset.routes import multi_asset_bp

ROOT = pathlib.Path(pkg_init).resolve().parent

CN_RE = re.compile(r"[\u4e00-\u9fff]")


def _load_yaml(path):
    import yaml
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


class I18nTest(unittest.TestCase):

    def setUp(self):
        self.en = _load_yaml(ROOT / "i18n" / "en.yml")
        self.zh = _load_yaml(ROOT / "i18n" / "zh-CN.yml")

    def test_key_sets_are_identical(self):
        self.assertEqual(set(self.en), set(self.zh),
                         "en-only=%s zh-only=%s" % (sorted(set(self.en) - set(self.zh)),
                                                    sorted(set(self.zh) - set(self.en))))

    def test_plugin_name_key_present(self):
        self.assertIn("plugin.name", self.en)
        self.assertIn("plugin.name", self.zh)

    def test_menu_key_present(self):
        self.assertIn("menu.multi_asset", self.en)

    def test_english_values_are_the_keys_themselves(self):
        """The i18n standard: the English source string *is* the key."""
        for key in ("Multi-Asset Workbench", "Symbol", "Load Bars", "No data."):
            with self.subTest(key=key):
                self.assertEqual(self.en[key], key)

    def test_no_empty_translations(self):
        for key, value in list(self.en.items()) + list(self.zh.items()):
            with self.subTest(key=key):
                self.assertTrue(str(value).strip())

    def test_every_label_rendered_by_the_page_is_translated(self):
        """The route builds its label dict from a literal key tuple; keep them in sync."""
        src = (ROOT / "routes.py").read_text(encoding="utf-8")
        block = re.search(r"labels = \{key: _t\(key\) for key in \((.*?)\)\}",
                          src, re.S)
        self.assertIsNotNone(block, "label tuple not found in routes.py")
        keys = re.findall(r'"([^"]+)"', block.group(1))
        self.assertGreater(len(keys), 10)
        for key in keys:
            with self.subTest(key=key):
                self.assertIn(key, self.en)
                self.assertIn(key, self.zh)

    def test_risk_disclosure_key_is_translated(self):
        self.assertIn("Derivative risk notice", self.en)
        self.assertIn("Derivative risk notice", self.zh)


class TemplateTest(unittest.TestCase):

    def setUp(self):
        self.path = ROOT / "templates" / "multi_asset.html"
        self.text = self.path.read_text(encoding="utf-8")

    def test_template_exists(self):
        self.assertTrue(self.path.is_file())

    def test_no_hardcoded_chinese_in_the_template(self):
        offenders = [(i, line) for i, line in enumerate(self.text.splitlines(), 1)
                     if CN_RE.search(line)]
        self.assertEqual(offenders, [], "hardcoded CJK in template: %s" % offenders[:3])

    def test_no_nested_translation_in_jinja_expressions(self):
        # Built by concatenation so this file does not itself trip the repo's
        # zero-growth "no `_(` inside a string literal" gate (scripts/i18n_check.py).
        pattern = re.compile(r"_" + r"\(\s*[\"']?(\{\{|\{%)")
        self.assertIsNone(pattern.search(self.text))

    def test_token_is_read_from_the_embed_url(self):
        """iframe plugins receive the JWT as ``?token=`` (plugin-standard §15.7)."""
        self.assertIn("token", self.text)
        self.assertIn("Authorization", self.text)

    def test_api_base_matches_the_blueprint_prefix(self):
        self.assertIn(multi_asset_bp.url_prefix, self.text)


class ManifestTest(unittest.TestCase):

    def setUp(self):
        self.meta = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))

    def test_identifier_matches_directory(self):
        self.assertEqual(self.meta["identifier"], ROOT.name)

    def test_name_i18n_key_present(self):
        self.assertEqual(self.meta["name_i18n_key"], "plugin.name")

    def test_capabilities_match_the_code(self):
        self.assertEqual(tuple(self.meta["capabilities"]), tuple(CAPABILITIES))

    def test_declared_capability_namespaces_exist_in_the_implementation(self):
        """Mirrors the discovery-time namespace check -- but strengthened.

        The platform check scans every ``.py`` including ``__init__.py``, which is
        the very file that declares ``CAPABILITIES``. That makes it trivially true:
        a capability list always contains its own namespaces. We exclude the
        declaration file, so the namespace has to appear in real implementation
        code -- otherwise the guard proves nothing.
        """
        blob = "\n".join(p.read_text(encoding="utf-8")
                         for p in ROOT.rglob("*.py")
                         if "tests" not in p.parts and p.name != "__init__.py")
        self.assertTrue(blob.strip(), "no implementation files were scanned")
        for cap in self.meta["capabilities"]:
            namespace = cap.split(".")[0]
            with self.subTest(cap=cap):
                self.assertIn(namespace, blob)

    def test_storage_and_provenance_capabilities_have_real_implementations(self):
        """A declared capability must map to code that actually exists."""
        models_src = (ROOT / "models.py").read_text(encoding="utf-8")
        self.assertIn("asset.storage", self.meta["capabilities"])
        self.assertIn("def upsert_bars", models_src)
        self.assertIn("asset.provenance", self.meta["capabilities"])
        self.assertIn("def record_fetch", models_src)

    def test_governance_capabilities_are_not_claimed_while_unimplemented(self):
        """The R-section built structure only -- nothing reads or writes those tables.

        Declaring ``data.*`` capabilities now would make the manifest lie: the same
        honesty rule that ``test_unimplemented_privileges_are_not_claimed`` enforces.
        """
        caps = set(self.meta["capabilities"])
        for undeclared in ("data.ingest", "data.pit", "data.quality",
                           "data.classification"):
            with self.subTest(cap=undeclared):
                self.assertNotIn(undeclared, caps)

    def test_provides_hooks_match_the_event_constants(self):
        self.assertEqual(self.meta["hooks"]["provides"], [events.EVENT_DATA_READY])

    def test_listens_is_empty_and_matches_get_event_handlers(self):
        """hooks.listens and get_event_handlers() must agree: no subscriptions."""
        self.assertEqual(self.meta["hooks"]["listens"], [])
        plugin = MultiAssetPlugin.__new__(MultiAssetPlugin)
        self.assertEqual(plugin.get_event_handlers(), {})

    def test_permissions_cover_the_registration_hooks(self):
        perms = set(self.meta["permissions"])
        # register_routes / register_jobs / register_health_checks / get_event_handlers
        for needed in ("routes", "scheduler", "health", "events"):
            with self.subTest(permission=needed):
                self.assertIn(needed, perms)

    def test_network_permission_declared_because_the_code_makes_requests(self):
        self.assertIn("network:request", set(self.meta["permissions"]))

    def test_unimplemented_privileges_are_not_claimed(self):
        """The plugin implements no DAG nodes -> it must not claim the 'dag' permission."""
        perms = set(self.meta["permissions"])
        self.assertNotIn("dag", perms)
        # BasePlugin ships a no-op register_dag_nodes; this class must not override it.
        self.assertNotIn("register_dag_nodes", MultiAssetPlugin.__dict__)
        self.assertNotIn("register_dag_nodes",
                         (ROOT / "__init__.py").read_text(encoding="utf-8"))

    def test_settings_only_expose_real_providers(self):
        schema = self.meta["settings_schema"]["properties"]["data_provider"]
        offered = set(schema["enum"]) - {""}
        known = {name for name, _at in REGISTRY}
        self.assertTrue(offered)
        self.assertTrue(offered.issubset(known),
                        "settings offers providers that do not exist: %s"
                        % sorted(offered - known))

    def test_no_dead_settings_keys(self):
        """Config keys must be read somewhere in the implementation."""
        blob = "\n".join(p.read_text(encoding="utf-8")
                         for p in ROOT.rglob("*.py") if "tests" not in p.parts)
        for key in self.meta.get("config", {}):
            with self.subTest(key=key):
                self.assertIn(key, blob)

    def test_menu_embed_url_matches_the_blueprint(self):
        url = self.meta["menu"]["items"][0]["embed_url"]
        self.assertTrue(url.startswith(multi_asset_bp.url_prefix))

    def test_edition_and_role_declarations(self):
        self.assertEqual(self.meta["compatible_editions"], ["finance"])
        self.assertEqual(self.meta["agent_role"], "stock_analyst")
        self.assertIn("stock_analysis", self.meta["dependencies"])

    def test_dependency_spec_matches_the_code_expectation(self):
        self.assertTrue(self.meta["dependencies"]["stock_analysis"].startswith(">="))

    def test_chains_are_declared_for_every_covered_asset_class(self):
        self.assertEqual(set(CHAINS), {"FUTURE", "OPTION", "FUND", "BOND"})

    def test_role_integration_note_is_shipped(self):
        self.assertTrue((ROOT / "docs" / "role-integration.md").is_file())

    def test_skill_and_readme_are_shipped(self):
        for name in ("SKILL.md", "README.md"):
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).is_file())


class PluginClassTest(unittest.TestCase):

    def setUp(self):
        self.plugin = MultiAssetPlugin.__new__(MultiAssetPlugin)

    def test_register_routes_returns_the_blueprint(self):
        self.assertEqual([multi_asset_bp], self.plugin.register_routes())

    def test_register_jobs_declares_one_cron_job(self):
        jobs = self.plugin.register_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["trigger"], "cron")
        self.assertTrue(callable(jobs[0]["func"]))

    def test_register_health_checks_reports_db_and_sources(self):
        ids = {c["id"] for c in self.plugin.register_health_checks()}
        self.assertEqual(ids, {"multi_asset_db", "multi_asset_sources"})

    def test_dependencies_declared_on_the_class(self):
        self.assertEqual(MultiAssetPlugin.dependencies, {"stock_analysis": ">=2.0.1"})

    def test_resolve_symbol_has_no_placeholder_left(self):
        """The shipped helper must call the real normalizer."""
        src = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("if False else", src)
        self.assertEqual(self.plugin.resolve_symbol("SA605")["key"],
                         "FUTURE:CZCE:SA605")


if __name__ == "__main__":
    unittest.main(verbosity=2)
