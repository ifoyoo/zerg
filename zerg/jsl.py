"""Generic ``__jsl_clearance`` helpers (RUJIA / CDN jsl style).

Used by multiple Chinese sites (CNVD, Mafengwo, …). Algorithm is public;
site-specific captcha after clearance is still the spider's problem.

Step-1 cookie expressions are tiny JS; we eval with ``node`` when present,
else a narrow pure-Python subset.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import shutil
import subprocess
from typing import Any

_COOKIE_ASSIGN_RE = re.compile(
    r"document\.cookie\s*=\s*(.*?);\s*location",
    re.I | re.S,
)
_GO_RE = re.compile(r";?go\((\{.*?\})\)\s*</script>", re.I | re.S)
_GO_RE_LOOSE = re.compile(r"\bgo\((\{.*?\})\)", re.I | re.S)


def extract_cookie_assign_expr(html: str) -> str | None:
    m = _COOKIE_ASSIGN_RE.search(html)
    return m.group(1).strip() if m else None


def extract_go_payload(html: str) -> dict[str, Any] | None:
    for cre in (_GO_RE, _GO_RE_LOOSE):
        m = cre.search(html)
        if not m:
            continue
        raw = m.group(1)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            continue
    return None


def merge_set_cookie_header(jar: dict[str, str], headers: dict[str, str]) -> None:
    """Merge a single Set-Cookie style header value into jar."""
    for k, v in headers.items():
        if k.lower() != "set-cookie":
            continue
        first = v.split(";", 1)[0].strip()
        if "=" in first:
            name, val = first.split("=", 1)
            jar[name.strip()] = val.strip()


def cookie_header(jar: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def process_jsl_html(
    html: str,
    jar: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Advance one jsl step.

    Returns dict:
      action: ``retry`` | ``pass`` | ``blocked`` | ``unknown``
      jar: updated cookie map
      step: ``step1`` | ``step2`` | ``done`` | ``blocked`` | ``none``
      detail: short reason
      cookie: last set name (if any)
    """
    jar = dict(jar or {})
    text = html or ""

    if (
        "验证码保护" in text
        or "本站开启了验证码" in text
        or "Environment Checking" in text
        or "cdn-cgi/challenge" in text.lower()
    ):
        return {
            "action": "blocked",
            "jar": jar,
            "step": "blocked",
            "detail": "captcha_or_env_check",
            "cookie": None,
        }

    if extract_cookie_assign_expr(text):
        try:
            name, value = clearance_from_step1_html(text)
            jar[name] = value
            return {
                "action": "retry",
                "jar": jar,
                "step": "step1",
                "detail": "clearance_assign",
                "cookie": name,
            }
        except Exception as e:
            return {
                "action": "unknown",
                "jar": jar,
                "step": "step1",
                "detail": f"step1_fail:{e}"[:120],
                "cookie": None,
            }

    if extract_go_payload(text):
        try:
            name, value = clearance_from_step2_html(text)
            jar[name] = value
            return {
                "action": "retry",
                "jar": jar,
                "step": "step2",
                "detail": "go_brute",
                "cookie": name,
            }
        except Exception as e:
            return {
                "action": "unknown",
                "jar": jar,
                "step": "step2",
                "detail": f"step2_fail:{e}"[:120],
                "cookie": None,
            }

    # looks like real content
    if len(text) > 800 and "<html" in text.lower():
        return {
            "action": "pass",
            "jar": jar,
            "step": "done",
            "detail": "html",
            "cookie": None,
        }

    return {
        "action": "unknown",
        "jar": jar,
        "step": "none",
        "detail": "unrecognized",
        "cookie": None,
    }


def eval_js_string_expr(expr: str) -> str:
    """Evaluate a JS expression that returns a string (cookie assignment RHS)."""
    if shutil.which("node"):
        out = subprocess.check_output(
            ["node", "-e", f"console.log({expr})"],
            text=True,
            timeout=5,
        )
        return out.strip()
    return _eval_js_expr_subset(expr)


def _eval_js_expr_subset(expr: str) -> str:
    """Very small subset: string literals, + concat, and a few numeric idioms."""
    # tokenize roughly by top-level + outside parens
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    s = expr
    while i < len(s):
        ch = s[i]
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "+" and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf).strip())

    out = []
    for p in parts:
        out.append(str(_eval_atom(p)))
    return "".join(out)


def _eval_atom(atom: str) -> Any:
    atom = atom.strip()
    if (atom.startswith("'") and atom.endswith("'")) or (
        atom.startswith('"') and atom.endswith('"')
    ):
        return atom[1:-1]
    # common jsl idioms
    table = {
        "-~false": 1,
        "-~true": 2,
        "+!+[]": 1,
        "+!![]": 1,
        "~~{}": 0,
        "+[]": 0,
        "false": 0,
        "true": 1,
    }
    if atom in table:
        return table[atom]
    # (n+'')  or  (expr+'')
    m = re.fullmatch(r"\((.*)\)", atom)
    if m:
        inner = m.group(1).strip()
        if inner.endswith("+'')") or inner.endswith("+''"):
            # rare
            pass
        if inner.endswith("+''"):
            return str(_eval_atom(inner[:-3].strip()))
        if re.fullmatch(r".+\+''", inner):
            return str(_eval_atom(inner[:-3].strip()))
        # arithmetic-ish: try python with replacements
        py = inner
        py = py.replace("-~false", "1").replace("-~true", "2")
        py = py.replace("+!+[]", "1").replace("~~{}", "0")
        py = re.sub(r"-~\[(\d+)\]", r"(1+\1)", py)
        py = re.sub(r"\[(\d+)\]", r"\1", py)
        try:
            return eval(py, {"__builtins__": {}}, {})  # noqa: S307 — constrained
        except Exception as e:
            raise ValueError(f"cannot eval atom {atom!r}: {e}") from e
    if re.fullmatch(r"-?\d+", atom):
        return int(atom)
    raise ValueError(f"unsupported JS atom: {atom!r}")


def parse_cookie_pair(cookie_assign_result: str) -> tuple[str, str]:
    """``name=value; Max-age=...`` → (name, value)."""
    first = cookie_assign_result.split(";", 1)[0].strip()
    if "=" not in first:
        raise ValueError(f"bad cookie assign: {cookie_assign_result!r}")
    name, value = first.split("=", 1)
    return name.strip(), value.strip()


def solve_go_clearance(payload: dict[str, Any]) -> tuple[str, str]:
    """Brute 2-char clearance. Returns ``(cookie_name, cookie_value)``."""
    bts = payload["bts"]
    chars = payload["chars"]
    ct = payload["ct"]
    ha = payload.get("ha") or "sha1"
    tn = payload.get("tn") or "__jsl_clearance_s"

    def digest(s: str) -> str:
        raw = s.encode()
        if ha == "sha1":
            return hashlib.sha1(raw).hexdigest()
        if ha == "sha256":
            return hashlib.sha256(raw).hexdigest()
        if ha == "md5":
            return hashlib.md5(raw).hexdigest()
        raise ValueError(f"unsupported ha={ha!r}")

    for a, b in itertools.product(chars, chars):
        candidate = f"{bts[0]}{a}{b}{bts[1]}"
        if digest(candidate) == ct:
            return tn, candidate
    raise ValueError("jsl go() clearance not found")


def clearance_from_step1_html(html: str) -> tuple[str, str]:
    expr = extract_cookie_assign_expr(html)
    if not expr:
        raise ValueError("no document.cookie assignment in step1 html")
    return parse_cookie_pair(eval_js_string_expr(expr))


def clearance_from_step2_html(html: str) -> tuple[str, str]:
    payload = extract_go_payload(html)
    if not payload:
        raise ValueError("no go({...}) payload in step2 html")
    return solve_go_clearance(payload)
