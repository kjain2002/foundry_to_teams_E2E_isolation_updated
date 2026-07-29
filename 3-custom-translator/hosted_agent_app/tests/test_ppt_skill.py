"""Tests for PPT skill instructions (template vs no-template modes)."""

from __future__ import annotations

from ppt_skill import build_ppt_instructions, looks_like_ppt_request


def test_no_template_mode_uses_style():
    text = build_ppt_instructions(
        has_template=False, style_instructions="executive, Microsoft-style"
    )
    assert "NO-TEMPLATE MODE" in text
    assert "ORIGINAL design" in text
    assert "executive, Microsoft-style" in text
    # The template-mode-specific guidance must be absent.
    assert "a PowerPoint template was supplied" not in text


def test_template_mode_names_reference():
    text = build_ppt_instructions(
        has_template=True, template_ref="brand.potx"
    )
    assert "TEMPLATE MODE" in text
    assert "brand.potx" in text
    assert "preserve" in text.lower() or "preserved" in text.lower()


def test_no_template_without_style_still_valid():
    text = build_ppt_instructions(has_template=False)
    assert "NO-TEMPLATE MODE" in text
    assert "consistent" in text.lower()


def test_looks_like_ppt_request():
    assert looks_like_ppt_request("make me a PPTX deck")
    assert looks_like_ppt_request("build a slide presentation")
    assert not looks_like_ppt_request("what is the capital of France?")
