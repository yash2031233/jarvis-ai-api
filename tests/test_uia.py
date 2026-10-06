"""Clicking by name: matching (pure logic - the Windows accessibility part is exercised by hand)."""

from jarvis.hands import uia
from jarvis.vision import pointing


def _el(i, role, name, off=False, value=""):
    return uia.El(i, role, name, (0, 0, 10, 10), off, value, None)


ELS = [_el(1, "Button", "Add to cart", off=True), _el(2, "Hyperlink", "Cart"), _el(3, "Image", "Orange cat photo"),
       _el(4, "Edit", "Search products"), _el(5, "Button", "Sign in"), _el(6, "Hyperlink", "Sign in help"),
       _el(7, "Text", "Add to cart to save 10%")]


def test_exact_name_wins_even_far_down_the_page():
    el, _ = uia.best(ELS, "Add to cart")
    assert el.id == 1


def test_role_words_steer_the_match():
    assert uia.best(ELS, "the cart link")[0].id == 2
    assert uia.best(ELS, "orange cat picture")[0].id == 3
    assert uia.best(ELS, "search box")[0].id == 4
    assert uia.best(ELS, "sign in button")[0].id == 5


def test_second_match_and_no_match():
    assert uia.best(ELS, "sign in", nth=2)[0].id == 6
    el, close = uia.best(ELS, "checkout now")
    assert el is None and close                 # nothing close enough - but the nearest are offered as hints


def test_grid_answers_are_parsed():
    assert pointing.parse_cell("The icon is in square D2.", 8, 6) == (3, 1)
    assert pointing.parse_cell("Looking at C1... no, it's E6", 8, 6) == (4, 5)
    assert pointing.parse_cell("NONE", 8, 6) is None
    assert pointing.parse_cell("Z9", 8, 6) is None
