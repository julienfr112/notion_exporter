# SPDX-License-Identifier: Apache-2.0
import pytest

from notion_exporter.uuid_strategy import (
    derive,
    derive_block,
    extract_from_filename,
    normalize,
)


def test_normalize_hyphenates_32_hex():
    out = normalize("11111111111111111111111111111111")
    assert out == "11111111-1111-1111-1111-111111111111"


def test_normalize_rejects_non_hex():
    with pytest.raises(ValueError):
        normalize("not-a-real-uuid")


def test_extract_from_filename_finds_hex_suffix():
    assert (
        extract_from_filename("Page Name 11111111111111111111111111111111")
        == "11111111-1111-1111-1111-111111111111"
    )


def test_extract_from_filename_returns_none_when_absent():
    assert extract_from_filename("README") is None


def test_derive_is_deterministic():
    a = derive("a", "b", "c")
    b = derive("a", "b", "c")
    assert a == b


def test_derive_block_rotates_on_content_change():
    page = "11111111-1111-1111-1111-111111111111"
    a = derive_block(page, "0", "hello")
    b = derive_block(page, "0", "hello world")
    assert a != b
