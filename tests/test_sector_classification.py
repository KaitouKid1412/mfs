"""Sector classification of Sectoral/Thematic schemes.

Verifies that scheme names land in the right sector bucket via SECTOR_RULES,
including the priority ordering (FMCG before Consumption, ESG before generic,
Healthcare-suffix beats Pharma-prefix, etc.) and the residual Thematic fallback.
"""

from __future__ import annotations

import pytest

from mfs.master.scheme_master import _classify_sector


@pytest.mark.parametrize(
    "scheme_name,expected_sector",
    [
        # Clear single-sector matches
        ("ICICI Prudential Banking and Financial Services Fund", "Banking & Financial Services"),
        ("HDFC Banking ETF", "Banking & Financial Services"),
        ("Aditya Birla Sun Life Banking & Financial Services Fund - Direct - Growth",
         "Banking & Financial Services"),
        ("SBI Technology Opportunities Fund - Direct - Growth", "IT"),
        ("ICICI Prudential Technology Fund", "IT"),
        ("Tata Digital India Fund", "IT"),
        ("DSP Pharma Healthcare and Diagnostics Fund", "Pharma & Healthcare"),
        ("Nippon India Pharma Fund", "Pharma & Healthcare"),
        ("UTI Healthcare Fund", "Pharma & Healthcare"),
        ("ICICI Prudential FMCG Fund", "FMCG"),
        ("HDFC FMCG Fund", "FMCG"),
        ("Mirae Asset Auto Fund", "Auto"),
        ("Nippon India Power & Infra Fund", "Energy"),  # Energy beats Infra by order
        ("DSP Energy Fund", "Energy"),
        ("ICICI Prudential Infrastructure Fund", "Infrastructure"),
        ("SBI PSU Fund", "PSU"),
        ("Aditya Birla Sun Life PSU Equity Fund", "PSU"),
        ("HDFC Consumption Fund", "Consumption"),
        ("Aditya Birla Sun Life MNC Fund", "MNC"),
        ("Quant ESG Equity Fund", "ESG"),
        ("ICICI Prudential Manufacturing Fund", "Manufacturing"),
        # Residual thematic falls through to "Thematic"
        ("ICICI Prudential India Opportunities Fund", "Thematic"),
        ("Aditya Birla Sun Life Special Opportunities Fund", "Thematic"),
        ("Kotak Innovation Opportunities Fund", "Thematic"),
        # Empty / whitespace
        ("", "Thematic"),
        ("   ", "Thematic"),
    ],
)
def test_classify_sector(scheme_name: str, expected_sector: str) -> None:
    assert _classify_sector(scheme_name) == expected_sector


def test_sector_priority_ordering() -> None:
    """FMCG-named fund should NOT be classified as Consumption even if 'Consumer' appears."""
    # Hypothetical: "FMCG and Consumer Goods Fund" — FMCG rule runs first.
    assert _classify_sector("FMCG and Consumer Goods Fund") == "FMCG"
    # Pharma+Healthcare combined → Pharma & Healthcare (single bucket)
    assert _classify_sector("Pharma and Healthcare Fund") == "Pharma & Healthcare"
