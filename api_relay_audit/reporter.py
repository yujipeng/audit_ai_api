"""Markdown report generator for audit results."""

from datetime import datetime


_PRICING_BADGE = {
    "pricing_compliant": ("\U0001f7e2", "green"),
    "token_drift_warn": ("\U0001f7e1", "yellow"),
    "inconclusive_balance": ("\U0001f7e1", "yellow"),
    "token_drift_critical": ("\U0001f534", "red"),
    "balance_drift_high": ("\U0001f534", "red"),
    "unit_price_mismatch": ("\U0001f534", "red"),
}


def _format_evidence(layer: str, details: dict) -> str:
    """One-line summary of the layer's evidence column."""
    if layer == "L0_character_ratio":
        return (
            f"expected={details.get('expected_total_tokens', '?')}, "
            f"reported={details.get('reported_total_tokens', '?')}, "
            f"drift_pct={details.get('token_drift_pct', '?')}%"
        )
    if layer == "L1_tokenizer":
        return (
            f"tokenizer={details.get('tokenizer', '?')}, "
            f"true={details.get('true_total_tokens', '?')}, "
            f"reported={details.get('reported_total_tokens', '?')}, "
            f"drift_pct={details.get('token_drift_pct', '?')}%"
        )
    if layer == "L2_balance_triangle":
        return (
            f"adapter={details.get('adapter', '?')}, "
            f"claimed={details.get('claimed_cost_usd', '?')} USD, "
            f"outflow={details.get('actual_outflow_usd', '?')} USD, "
            f"drift_pct={details.get('balance_drift_pct', '?')}%"
        )
    return ", ".join(f"{k}={v}" for k, v in sorted(details.items()))


class Reporter:
    """Builds a structured Markdown audit report with a risk summary header.

    Sections are accumulated via helper methods (``h1``, ``h2``, ``p``,
    ``code``, ``flag``, etc.) and rendered into a single Markdown string
    by ``render()``.

    Attributes:
        sections: Accumulated Markdown fragments (body of the report).
        summary: List of ``(level, message)`` tuples collected by ``flag()``.
    """

    def __init__(self):
        """Initialise an empty report."""
        self.sections = []
        self.summary = []

    def h1(self, t):
        """Append a level-1 heading.

        Args:
            t: Heading text.
        """
        self.sections.append(f"\n# {t}\n")

    def h2(self, t):
        """Append a level-2 heading.

        Args:
            t: Heading text.
        """
        self.sections.append(f"\n## {t}\n")

    def h3(self, t):
        """Append a level-3 heading.

        Args:
            t: Heading text.
        """
        self.sections.append(f"\n### {t}\n")

    def p(self, t):
        """Append a paragraph of text.

        Args:
            t: Paragraph content (plain text or inline Markdown).
        """
        self.sections.append(f"{t}\n")

    def code(self, t, lang=""):
        """Append a fenced code block.

        Args:
            t: Code content.
            lang: Optional language hint for syntax highlighting
                (e.g. ``"json"``). Defaults to ``""``.
        """
        self.sections.append(f"```{lang}\n{t}\n```\n")

    def flag(self, level, msg):
        """Record a risk finding and append a coloured flag line.

        The finding is added both to the ``summary`` list (used in the
        report header) and inline in the body.

        Args:
            level: Severity string -- ``"red"``, ``"yellow"``, or
                ``"green"``.
            msg: Human-readable description of the finding.
        """
        icon = {"red": "\U0001f534", "yellow": "\U0001f7e1", "green": "\U0001f7e2"}.get(level, "\u26aa")
        self.summary.append((level, msg))
        self.sections.append(f"{icon} **{msg}**\n")

    def pricing_section(self, aggregateds, *, standalone: bool = False) -> None:
        """Render a Pricing Compliance section (PRD \u00a76.1.4 / design \u00a74.5).

        ``aggregateds`` is a list of :class:`AggregatedPricingVerdict`.
        Three Markdown tables are emitted (token-drift / unit-price /
        balance) \u2014 each row carries ``confidence``, ``layer``, and
        ``evidence`` columns. ``standalone=True`` renders only L0 rows
        to preserve the standalone audit.py parity (PRD \u00a76.3.1).
        """
        if not aggregateds:
            return

        worst_severity = "pricing_compliant"
        rank = {
            "pricing_compliant": 0,
            "inconclusive_balance": 1,
            "token_drift_warn": 2,
            "unit_price_mismatch": 3,
            "balance_drift_high": 4,
            "token_drift_critical": 5,
        }
        for agg in aggregateds:
            if rank[agg.severity.value] > rank[worst_severity]:
                worst_severity = agg.severity.value

        badge, level = _PRICING_BADGE.get(worst_severity,
                                          ("\u26aa", "yellow"))
        self.sections.append(f"\n## {badge} Pricing Compliance\n")
        self.flag(level, f"Pricing verdict: {worst_severity}")

        layer_filter = (
            ("L0_character_ratio",) if standalone
            else ("L0_character_ratio", "L1_tokenizer", "L2_balance_triangle")
        )

        def _emit_table(title: str, layer: str, header: str) -> None:
            rows = []
            for agg in aggregateds:
                details = agg.per_layer.get(layer)
                if not details:
                    continue
                evidence = _format_evidence(layer, details)
                rows.append(
                    f"| {layer} | {agg.confidence:.2f} | {evidence} |"
                )
            if not rows:
                return
            self.sections.append(f"\n### {title}\n")
            self.sections.append(
                "| layer | confidence | evidence |\n"
                "|---|---|---|\n"
                + "\n".join(rows) + "\n"
            )

        for layer in layer_filter:
            if layer == "L0_character_ratio":
                _emit_table("Token drift (L0 character-ratio)", layer,
                            "expected vs reported")
            elif layer == "L1_tokenizer":
                _emit_table("Unit price / tokenizer truth (L1)", layer,
                            "tiktoken / claude count_tokens")
            elif layer == "L2_balance_triangle":
                _emit_table("Balance triangle (L2)", layer,
                            "claimed vs outflow")

    def render(self, target_url="", model=""):
        """Render the complete Markdown report.

        Produces a header block (title, metadata, risk summary) followed
        by all accumulated sections joined with newlines.

        Args:
            target_url: The relay URL under test. Shown in the report
                metadata when provided.
            model: The model identifier used for the audit. Shown in the
                report metadata when provided.

        Returns:
            A single Markdown string containing the full report.

        Examples:
            >>> rpt = Reporter()
            >>> rpt.h2("Authentication")
            >>> rpt.flag("green", "API key accepted")
            >>> print(rpt.render(target_url="https://relay.example.com"))
        """
        header = (
            f"# API Relay Security Audit Report\n\n"
            f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        )
        if target_url:
            header += f"**Target**: `{target_url}`\n"
        if model:
            header += f"**Model**: `{model}`\n"

        header += "\n## Risk Summary\n\n"
        for level, msg in self.summary:
            icon = {"red": "\U0001f534", "yellow": "\U0001f7e1", "green": "\U0001f7e2"}.get(level, "\u26aa")
            header += f"- {icon} {msg}\n"
        header += "\n---\n"
        return header + "\n".join(self.sections)
