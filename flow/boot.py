# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from flow.knowledge.extract import FILE_EXTENSIONS
from flow.permissions import has_flow_access


def boot_session(bootinfo):
	# Single source of truth for file types the ingest pipeline can extract
	bootinfo.flow_supported_file_types = sorted(FILE_EXTENSIONS)
	# Presentation only: the API role check remains the security boundary.
	bootinfo.flow_enabled = has_flow_access()
