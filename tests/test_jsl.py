"""jsl clearance unit tests — no network."""

from __future__ import annotations

import hashlib

from zerg.jsl import (
    eval_js_string_expr,
    parse_cookie_pair,
    solve_go_clearance,
)


def test_eval_simple_concat():
    # mimics step1 style without needing node for pure strings
    expr = "('_')+('_')+('j')+('s')+('l')"
    assert eval_js_string_expr(expr) == "__jsl"


def test_solve_go_sha1():
    b0, b1 = "1628088065.4|0|rZGRv", "gwlQ6aBQ9ZCDZeup2IEk%3D"
    chars = "vMJCKTooRTjlkttqnCeuEL"
    # find a known pair by running solver against self-made ct
    a, b = chars[0], chars[1]
    cand = f"{b0}{a}{b}{b1}"
    ct = hashlib.sha1(cand.encode()).hexdigest()
    name, value = solve_go_clearance(
        {
            "bts": [b0, b1],
            "chars": chars,
            "ct": ct,
            "ha": "sha1",
            "tn": "__jsl_clearance_s",
        }
    )
    assert name == "__jsl_clearance_s"
    assert value == cand


def test_parse_cookie_pair():
    assert parse_cookie_pair("a=b; Max-age=1") == ("a", "b")


def test_process_jsl_html_step1_and_block():
    from zerg.jsl import process_jsl_html

    html = (
        "<script>document.cookie=('_')+('_')+('j')+('s')+('l')+('_')+('c')"
        "+('l')+('e')+('a')+('r')+('a')+('n')+('c')+('e')+('_')+('s')+('=')"
        "+('1')+('2')+('3');location.href=1</script>"
    )
    # may need node for full expr; pure strings path:
    html2 = "<script>document.cookie=('a')+('=')+('b');location</script>"
    r = process_jsl_html(html2)
    assert r["action"] == "retry"
    assert r["step"] == "step1"
    assert r["jar"].get("a") == "b"

    blocked = process_jsl_html("<title>Environment Checking</title>")
    assert blocked["action"] == "blocked"
