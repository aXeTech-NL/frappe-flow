# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from unittest import TestCase
from unittest.mock import call, patch

import flow.utils.lancedb as lancedb_loader


class TestLanceDBCompatibilityLoader(TestCase):
	def test_standard_distribution_imports_without_metadata_shim(self):
		module = object()
		with (
			patch.object(lancedb_loader.metadata, "version", return_value="0.37.1") as version,
			patch.object(lancedb_loader.importlib, "import_module", return_value=module) as import_module,
		):
			self.assertIs(lancedb_loader._load_lancedb(), module)

		version.assert_called_once_with("lancedb")
		import_module.assert_called_once_with("lancedb")

	def test_compat_distribution_supplies_lancedb_version_during_import(self):
		module = object()

		def distribution_version(name):
			if name == "lancedb":
				raise lancedb_loader.metadata.PackageNotFoundError(name)
			if name == "lancedb-compat":
				return "0.37.1"
			raise AssertionError(f"unexpected distribution lookup: {name}")

		def import_module(name):
			self.assertEqual(name, "lancedb")
			self.assertEqual(lancedb_loader.metadata.version("lancedb"), "0.37.1")
			return module

		with patch.object(lancedb_loader.metadata, "version", side_effect=distribution_version) as version:
			original_version = lancedb_loader.metadata.version
			with patch.object(lancedb_loader.importlib, "import_module", side_effect=import_module):
				self.assertIs(lancedb_loader._load_lancedb(), module)
			self.assertIs(lancedb_loader.metadata.version, original_version)

		self.assertEqual(version.call_args_list, [call("lancedb"), call("lancedb-compat")])
