"""Does this look like a real person's data?

The existing customer-data guard keys on `assistant_uuid` — it refuses a bot
that holds real accounts. That is necessary and not sufficient: on 2026-08-24 a
run used a *correct* assistant and still leaked, because the scenario's own
`vars` carried a real application number, which the bot POSTed onward as
`applicationNumber`. Nothing inspected the values.

This inspects the values. It is a heuristic and it says so: the honest output is
"this looks like it identifies a real person, confirm or mark it synthetic",
never a confident verdict. Two rules keep it useful rather than noisy:

* **Known-fictional patterns are silent.** NANP `555-01XX` is reserved for
  fiction, and repdigit/sequential numbers are obviously invented. A set built
  from those produces no findings at all.
* **Findings never echo the value.** A lint message gets pasted into tickets and
  chat. Naming the field and masking the value keeps the guard from becoming the
  leak it exists to prevent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# NANP: 555-0100..555-0199 is reserved for fiction. Also treat any 555 exchange
# as intentional, since that is the convention people actually reach for.
_FICTIONAL_PHONE = re.compile(r"^\+?1?\s*\(?[2-9]\d{2}\)?[\s.-]*555[\s.-]*\d{4}$")
_DIGITS_ONLY = re.compile(r"[^0-9]")

# Field names that carry identity. Checked by name because a bare 10-digit
# string is ambiguous, but `phone: <10 digits>` is not.
_IDENTITY_FIELDS = {
    "phone", "phone_number", "mobile", "cell", "rmn", "recipient_id",
    "ssn", "ssn_last4", "last_four", "national_id", "nationalidlast4",
    "account", "account_number", "acct", "application", "application_number",
    "dob", "date_of_birth", "email", "email_id", "address", "zip", "postal",
    "first_name", "last_name", "full_name", "name",
}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
# Domains that exist to be examples. RFC 2606 plus the obvious test ones.
_SAFE_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "test", "invalid", "localhost")


# Scenario vars carry identifiers as SPOKEN WORDS, because the caller has to say
# them aloud: `phone_number: "Seven One Nine Seven Two Three Zero Four One Five"`.
# The digits usually appear only in a trailing YAML comment, which the parser
# discards — so a digit-only guard misses essentially the whole corpus. Normalise
# spoken runs back to digits before checking.
_SPOKEN_DIGIT = {
    "zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3",
    "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    # Spanish, since the corpus has bilingual scenarios
    "cero": "0", "uno": "1", "dos": "2", "tres": "3", "cuatro": "4",
    "cinco": "5", "seis": "6", "siete": "7", "ocho": "8", "nueve": "9",
}
_WORD = re.compile(r"[a-záéíóúñ]+", re.I)


def spoken_to_digits(text: str) -> str:
    """Longest run of consecutive spoken digit-words, as digits.

    Returns "" when the text is not a spoken number. Requires a run of at least
    four so ordinary prose ("one moment", "for two of them") cannot trip it.
    """
    words = _WORD.findall(str(text).lower())
    best: list[str] = []
    run: list[str] = []
    for w in words:
        mapped = _SPOKEN_DIGIT.get(w)
        if mapped is not None:
            run.append(mapped)
        else:
            if len(run) > len(best):
                best = run
            run = []
    if len(run) > len(best):
        best = run
    return "".join(best) if len(best) >= 4 else ""


@dataclass(frozen=True)
class Finding:
    field: str
    reason: str
    masked: str
    # A lone 4-digit SSN last-4 is indistinguishable from an invented one unless
    # it happens to be patterned — `7545` on a wholly fictional persona looks
    # exactly as real as `3279` on a customer. So findings carry confidence, and
    # `is_significant` requires either one strong signal or two weak ones. A real
    # identity leaks CORRELATED identifiers; a fictional one rarely trips twice.
    strong: bool = True

    def describe(self) -> str:
        return f"{self.field} ({self.reason}: {self.masked})"


def _mask(value: str) -> str:
    """Enough to recognise the value you meant, not enough to reuse it."""
    s = str(value)
    if len(s) <= 4:
        return "*" * len(s)
    return f"{s[:2]}{'*' * (len(s) - 4)}{s[-2:]}"


def _mask_email(value: str) -> str:
    """Harder than `_mask`: an address is directly contactable, and the domain
    plus a length hint is often enough to identify someone on its own."""
    local, _, domain = str(value).partition("@")
    tld = domain.rsplit(".", 1)[-1] if "." in domain else "?"
    return f"{local[:1]}***@***.{tld}"


def _is_sequential(digits: str) -> bool:
    """An ascending or descending run: 1234, 4321, 456789. Nobody's real SSN."""
    if len(digits) < 3:
        return False
    steps = {ord(b) - ord(a) for a, b in zip(digits, digits[1:])}
    return steps in ({1}, {-1})


def _looks_invented(digits: str) -> bool:
    """Repdigits, sequential runs, and the well-known test accounts.

    `1234` matters specifically: it is the Veros test account's SSN last-4, and
    a guard that cries wolf on the deliberately-seeded test identity is a guard
    people switch off.
    """
    if len(set(digits)) <= 2:
        return True                                   # 1111111111, 1212121212
    if _is_sequential(digits):
        return True                                   # 1234, 123456789
    if digits.startswith(("1234567", "1234568", "0000")):
        return True                                   # the Veros test account
    return False


def _check_value(field_name: str, value: Any) -> list[Finding]:
    key = field_name.strip().lower()
    findings: list[Finding] = []
    if not isinstance(value, (str, int)):
        return findings
    text = str(value).strip()
    if not text:
        return findings

    if _EMAIL.match(text):
        domain = text.rsplit("@", 1)[-1].lower()
        if not any(domain == d or domain.endswith("." + d) for d in _SAFE_EMAIL_DOMAINS):
            findings.append(Finding(field_name, "real-looking email address", _mask_email(text)))
        return findings

    if key not in _IDENTITY_FIELDS and not any(k in key for k in ("phone", "ssn", "account", "dob")):
        return findings

    digits = _DIGITS_ONLY.sub("", text)
    if not digits:
        # "Seven One Nine..." — spoken form, which is how the corpus writes them.
        digits = spoken_to_digits(text)

    if "phone" in key or key in ("mobile", "cell", "rmn", "recipient_id"):
        if _FICTIONAL_PHONE.match(text) or (len(digits) >= 10 and digits[-7:-4] == "555"):
            return findings                            # reserved for fiction
        if len(digits) >= 10 and not _looks_invented(digits):
            findings.append(Finding(field_name, "real-looking phone number", _mask(digits)))
        return findings

    if "ssn" in key or "national" in key or "last_four" in key or key == "last_four":
        if len(digits) in (4, 9) and not _looks_invented(digits):
            findings.append(Finding(field_name, "possible real SSN digits", _mask(digits),
                                    strong=len(digits) == 9))
        return findings

    if "account" in key or "application" in key:
        if len(digits) >= 6 and not _looks_invented(digits):
            findings.append(Finding(field_name, "real-looking account/application number", _mask(digits)))
        return findings

    return findings


def inspect(values: dict[str, Any], *, _prefix: str = "") -> list[Finding]:
    """Walk a resolved variable mapping and report what looks real.

    Recurses into nested mappings, because `card: {last_4: ...}` is a shape the
    existing scenarios already use.
    """
    findings: list[Finding] = []
    for key, value in (values or {}).items():
        path = f"{_prefix}{key}"
        if isinstance(value, dict):
            findings.extend(inspect(value, _prefix=f"{path}."))
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    findings.extend(inspect(item, _prefix=f"{path}[{i}]."))
                else:
                    findings.extend(_check_value(f"{path}[{i}]", item))
        else:
            findings.extend(_check_value(path, value))
    return findings


def is_significant(findings: list[Finding]) -> bool:
    """Worth telling the author about: one strong signal, or two weak ones."""
    if any(f.strong for f in findings):
        return True
    return len(findings) >= 2
