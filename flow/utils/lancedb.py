# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""Load LanceDB across its standard and CPU-compatible distributions.

``lancedb-compat`` deliberately installs the same ``lancedb`` import package as
``lancedb``. Version 0.37.1 still asks importlib for the standard distribution's
metadata during import, though, so a clean compat-only installation raises
``PackageNotFoundError``. Supply that one metadata lookup from the compat
distribution while the package initializes.
"""

from __future__ import annotations

import importlib
from importlib import metadata
from types import ModuleType


def _load_lancedb() -> ModuleType:
	try:
		metadata.version("lancedb")
	except metadata.PackageNotFoundError:
		compat_version = metadata.version("lancedb-compat")
	else:
		return importlib.import_module("lancedb")

	original_version = metadata.version

	def compatible_version(distribution_name: str) -> str:
		if distribution_name.lower().replace("_", "-") == "lancedb":
			return compat_version
		return original_version(distribution_name)

	metadata.version = compatible_version
	try:
		return importlib.import_module("lancedb")
	finally:
		metadata.version = original_version


lancedb = _load_lancedb()
