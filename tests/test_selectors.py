from __future__ import annotations

from pyestro.selectors import (
    Selector,
    find_element,
    parse_elements,
    selector_from_value,
)

from .conftest import hierarchy, node


def screen():
    return parse_elements(
        hierarchy(
            node("Email", bounds="[0,0][400,100]"),
            node("", rid="com.app:id/email_input", bounds="[0,100][400,200]"),
            node("Password", bounds="[0,200][400,300]"),
            node("", rid="com.app:id/pass_input", bounds="[0,300][400,400]"),
            node(
                "",
                desc="Sign in",
                bounds="[0,400][400,500]",
                enabled="false",
            ),
            node("Item", bounds="[0,500][400,600]"),
            node("Item", bounds="[0,600][400,700]"),
        )
    )


def test_text_is_full_match_regex_case_insensitive():
    els = screen()
    assert find_element(els, Selector(text="email")) is not None
    assert find_element(els, Selector(text="Ema")) is None
    assert find_element(els, Selector(text="Ema.*")) is not None


def test_invalid_regex_falls_back_to_literal():
    els = parse_elements(hierarchy(node("a(b", bounds="[0,0][10,10]")))
    assert find_element(els, Selector(text="a(b")) is not None


def test_text_matches_content_desc():
    assert find_element(screen(), Selector(text="Sign in")) is not None


def test_id_matches_with_or_without_package():
    els = screen()
    assert find_element(els, Selector(id="email_input")) is not None
    assert find_element(els, Selector(id="com.app:id/email_input")) is not None
    assert find_element(els, Selector(id="email")) is None


def test_state_filters():
    els = screen()
    assert find_element(els, Selector(text="Sign in", enabled=True)) is None
    assert find_element(els, Selector(text="Sign in", enabled=False)) is not None


def test_index_picks_among_matches():
    els = screen()
    first = find_element(els, Selector(text="Item", index=0))
    second = find_element(els, Selector(text="Item", index=1))
    assert first.bounds[1] == 500
    assert second.bounds[1] == 600
    assert find_element(els, Selector(text="Item", index=2)) is None


def test_relative_selectors_pick_nearest():
    els = screen()
    sel = selector_from_value({"id": ".*input", "below": "Password"})
    assert find_element(els, sel).resource_id.endswith("pass_input")
    sel = selector_from_value({"id": ".*input", "above": "Password"})
    assert find_element(els, sel).resource_id.endswith("email_input")


def test_deepest_match_wins():
    els = parse_elements(
        hierarchy(
            node(
                "Save",
                bounds="[0,0][400,100]",
                children=node("Save", bounds="[10,10][100,50]"),
            )
        )
    )
    assert find_element(els, Selector(text="Save")).bounds == (10, 10, 100, 50)


def test_contains_child_and_child_of():
    els = parse_elements(
        hierarchy(
            node(
                "",
                rid="card",
                bounds="[0,0][400,100]",
                children=node("Title", bounds="[0,0][100,50]"),
            ),
            node("", rid="card", bounds="[0,100][400,200]"),
        )
    )
    sel = selector_from_value({"id": "card", "containsChild": "Title"})
    assert find_element(els, sel).bounds[1] == 0
    sel = selector_from_value({"text": "Title", "childOf": {"id": "card"}})
    assert find_element(els, sel) is not None


def test_invisible_elements_are_ignored():
    els = parse_elements(hierarchy(node("Hidden", bounds="[0,0][0,0]")))
    assert find_element(els, Selector(text="Hidden")) is None
