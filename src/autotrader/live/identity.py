"""The account pin: proof that this runtime reached the account it was authorized for.

The paper runtime proves its environment twice - the client must reach the
paper host, and the account that answers must carry the paper namespace prefix.
Both checks work because "paper" is a *category*: there is one paper host, and
every paper account number starts with the same two letters.

Real money has no such category to check. Every live account reaches the same
host and every live account number looks like every other one, so proving "this
is a live account" proves nothing an operator cares about. The question worth
asking is narrower and much more useful:

    is this the *specific* account I was authorized to operate?

That is what this module answers, and it answers it against a pin the operator
sets once. A credential rotated to the wrong key, a secrets file copied from
another host, a second account opened for something else, an environment file
edited by the wrong hand - every one of them shows up here as a fingerprint
that does not match, and every one of them stops the runtime before it reads a
bar.

**A fingerprint, not the number.** The pin is a SHA-256 digest of the account
number, not the number itself. It is stored in a world-readable configuration
file next to a path and a port, and an account number does not need to be
there for the check to work: a digest compares exactly as well and reveals
nothing if the file is shared, pasted into an issue, or read over a shoulder.

**Absent means refuse.** There is no "unpinned" mode in which the runtime
operates whatever account answers. A missing pin is not a permissive default,
it is a runtime that has not been told what it is for.
"""

from __future__ import annotations

import hashlib
import os

#: Where the pin is read from. A digest, so it belongs in the non-secret
#: configuration file rather than beside the credentials.
LIVE_ACCOUNT_FINGERPRINT_ENV = "AUTOTRADER_LIVE_ACCOUNT_FINGERPRINT"

#: How many hex characters of the digest the pin carries. Sixteen bytes of a
#: SHA-256 is far past any collision an operator could produce by accident, and
#: it stays short enough to read aloud when checking a deployment.
FINGERPRINT_LENGTH = 32


class AccountIdentityError(Exception):
    """The account that answered is not the one this runtime is pinned to.

    Also raised when no pin is configured at all. The two are deliberately the
    same class: from a caller's point of view "the wrong account answered" and
    "nobody said which account is right" are both states in which continuing
    would mean operating an account on nobody's authority.
    """


def account_fingerprint(account_number: str) -> str:
    """The pin for one account number. Deterministic, one-way, and short.

    Whitespace is stripped and the value uppercased before hashing so that a
    pin generated from a number copied with a trailing newline still matches
    the number the broker reports. Nothing else is normalized: an account
    number is an opaque token and reshaping it further would risk two genuinely
    different accounts fingerprinting the same.
    """
    normalized = str(account_number).strip().upper()
    if not normalized:
        raise AccountIdentityError(
            "An empty account number has no fingerprint. Nothing was verified."
        )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def configured_account_fingerprint() -> str | None:
    """The pin this host was provisioned with, or None when it has none."""
    value = os.environ.get(LIVE_ACCOUNT_FINGERPRINT_ENV, "").strip().lower()
    return value or None


def require_expected_account(client: object) -> str:
    """Read the account and prove it is the pinned one, or refuse. Read-only.

    One `GET`. It submits nothing, cancels nothing, and changes nothing at the
    broker or on disk, which is what lets it run as the very first thing a
    live process does - before a bar is fetched, before a store is opened for
    writing, before any decision exists to act on.

    Returns the fingerprint that matched, so a caller can log *which* account
    it opened without logging the account number.
    """
    expected = configured_account_fingerprint()
    if expected is None:
        raise AccountIdentityError(
            f"Refusing to start: {LIVE_ACCOUNT_FINGERPRINT_ENV} is not set, so nothing has "
            "declared which real-money account this runtime is authorized to operate. "
            "There is no unpinned mode. Nothing was fetched and no order was submitted."
        )
    if len(expected) != FINGERPRINT_LENGTH or any(
        character not in "0123456789abcdef" for character in expected
    ):
        raise AccountIdentityError(
            f"Refusing to start: {LIVE_ACCOUNT_FINGERPRINT_ENV} is not a "
            f"{FINGERPRINT_LENGTH}-character hexadecimal fingerprint. A pin this build "
            "cannot interpret is not an authorization. Nothing was fetched."
        )

    try:
        account = client.get_account()  # type: ignore[attr-defined]
    except Exception as error:  # noqa: BLE001 - an unreadable account must fail closed
        raise AccountIdentityError(
            f"Refusing to start: the account could not be read ({type(error).__name__}), so "
            "its identity could not be confirmed. Nothing was fetched and no order was "
            "submitted."
        ) from None

    number = str(getattr(account, "account_number", "") or "")
    if not number:
        raise AccountIdentityError(
            "Refusing to start: the broker returned an account with no account number, so "
            "there is nothing to compare the pin against. Nothing was submitted."
        )

    actual = account_fingerprint(number)
    if actual != expected:
        raise AccountIdentityError(
            "Refusing to start: the account that answered fingerprints to "
            f"{actual} but this runtime is pinned to {expected}. The credentials in this "
            "environment reach an account nobody authorized this runtime to operate. "
            "Nothing was fetched and no order was submitted."
        )
    return actual


__all__ = [
    "FINGERPRINT_LENGTH",
    "LIVE_ACCOUNT_FINGERPRINT_ENV",
    "AccountIdentityError",
    "account_fingerprint",
    "configured_account_fingerprint",
    "require_expected_account",
]
