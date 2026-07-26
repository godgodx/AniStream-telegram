from __future__ import annotations

import re


_PACKED = re.compile(
    r"eval\(function\(p,a,c,k,e,(?:d|r)\).*?\}\(\s*(['\"])(.*?)\1\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(['\"])(.*?)\5\.split\(['\"]\|['\"]\)",
    re.DOTALL,
)


def _base_token(number: int, radix: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if number == 0:
        return "0"
    output = ""
    while number:
        output = alphabet[number % radix] + output
        number //= radix
    return output


def _unescape_javascript(value: str) -> str:
    return re.sub(
        r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.|$)",
        lambda match: (
            chr(int(match.group(1)[1:], 16))
            if match.group(1).startswith(("x", "u"))
            else {"n": "\n", "r": "\r", "t": "\t"}.get(match.group(1), match.group(1))
        ),
        value,
    )


def unpack_packer(source: str) -> str | None:
    """Unpack the common Dean Edwards JavaScript packer format."""
    match = _PACKED.search(source)
    if not match:
        return None
    payload = _unescape_javascript(match.group(2))
    radix = int(match.group(3))
    count = int(match.group(4))
    words = match.group(6).split("|")
    if not 2 <= radix <= 62:
        return None
    for index in range(count - 1, -1, -1):
        if index >= len(words) or not words[index]:
            continue
        payload = re.sub(rf"\b{re.escape(_base_token(index, radix))}\b", words[index], payload)
    return payload
