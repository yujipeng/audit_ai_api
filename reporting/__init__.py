"""S5-C report rendering — three-way artifacts (Markdown, HTML, baseline diff).

All renderers share `reporting.schema.ReportView` and refuse `schema_version != 1`.
Schema design references `web/data-example.json` field semantics; runtime does
not read `web/`.
"""

from reporting.schema import ReportView, SUPPORTED_SCHEMA_VERSION

__all__ = ["ReportView", "SUPPORTED_SCHEMA_VERSION"]
