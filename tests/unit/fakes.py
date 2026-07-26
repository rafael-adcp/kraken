#!/usr/bin/env python3
"""Test doubles for the transport boundary.

`Api` is the one object the program talks to GitHub through, so a unit test
supplies its own instead of rebinding module attributes. Two consequences worth
the file: a fake cannot leak into the next test (there is nothing global to
restore), and overriding a name the real `Api` does not have is an error rather
than a silently dead stub.

Stdlib only, like everything else here.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(HERE, "..", "..", "skills", "unleash")
sys.path.insert(0, os.path.abspath(SKILL_DIR))

import kraken  # noqa: E402

DEFAULT_REPO = "OWNER/tasks"


class FakeApi(kraken.Api):
    """An `Api` whose methods the test supplies by keyword.

    Anything not supplied keeps the REAL implementation, which is the point:
    a test that fakes only `request` still exercises the real path building,
    pagination, JSON decoding and status branching above it — the parts most
    worth testing. A test that wants to skip all that fakes the higher method
    (`graphql`, `swap_labels`, …) directly.

        FakeApi(request=lambda m, p, body=None: (200, "[]"))
        FakeApi(swap_labels=lambda issue, remove=None, add=None: True)

    Note the arity: methods lose the leading `repo` the free functions used to
    take, and gain nothing — `self` is bound at assignment, so the lambdas here
    take exactly the arguments a caller passes.
    """

    def __init__(self, repo: str = DEFAULT_REPO, **methods):
        super().__init__(repo)
        for name, fn in methods.items():
            if not callable(getattr(kraken.Api, name, None)):
                raise AttributeError(
                    f"Api has no method {name!r} to fake — check the spelling")
            setattr(self, name, fn)


def recording_api(repo: str = DEFAULT_REPO, **methods):
    """A FakeApi plus the list its writes are appended to.

    Returns `(api, writes)`. Every write method not explicitly overridden
    records `(verb, *args)` and answers True, which is what the reconciler and
    transition tests assert on."""
    writes: list[tuple] = []
    defaults = {
        "post_comment": lambda issue, body: (
            writes.append(("comment", issue, body)) or True),
        "swap_labels": lambda issue, remove=None, add=None: (
            writes.append(("labels", issue, remove, add)) or True),
    }
    defaults.update(methods)
    return FakeApi(repo, **defaults), writes


def paged(items, per_page=None):
    """A `request` stand-in that serves `items` as a paginated REST list, so a
    test exercises the real pagination loop instead of asserting against a
    single hand-fed page."""
    size = per_page or kraken.PER_PAGE

    def request(method, path, body=None):
        page = 1
        for part in path.split("&"):
            if part.startswith("page="):
                page = int(part.split("=", 1)[1])
        start = (page - 1) * size
        chunk = items[start:start + size]
        import json
        return (200, json.dumps(chunk))

    return request
