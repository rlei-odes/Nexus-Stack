"""Tests for nexus_deploy.r2_tokens.

Covers the orphan-R2-API-token bug from PR #530 R2:

- Pagination invariant: list_user_tokens walks ``result_info.total_pages``
  so the matching-name lookup doesn't miss tokens past page 1.
- Per-name + per-prefix filtering returns ALL matches (not just first).
- cleanup_orphan_tokens refuses prefixes outside ``nexus-r2-`` (defence
  against wiping the protected ``Nexus-Stack`` / ``Nexus2`` tokens).
- Dry-run is the default; --apply path actually deletes.
- Inventory's ``near_cap`` flag fires before the 50-token hard cap.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus_deploy.r2_tokens import (
    ACCOUNT_TOKEN_HARD_CAP,
    DEFAULT_NEXUS_R2_PREFIX,
    CleanupResult,
    DeleteResult,
    TokenInfo,
    build_inventory,
    cleanup_orphan_tokens,
    delete_token,
    find_tokens_by_name,
    find_tokens_by_prefix,
    list_user_tokens,
)


def _mock_response(json_body: dict[str, Any], *, status_code: int = 200) -> MagicMock:
    """Stand-in for a `requests.Response` object."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# TokenInfo.from_api — lenient parse
# ---------------------------------------------------------------------------


def test_token_info_from_api_minimal_fields() -> None:
    info = TokenInfo.from_api({"id": "abc", "name": "nexus-r2-foo"})
    assert info.id == "abc"
    assert info.name == "nexus-r2-foo"
    assert info.issued_on == ""


def test_token_info_from_api_with_issued_on() -> None:
    info = TokenInfo.from_api(
        {"id": "abc", "name": "x", "issued_on": "2026-04-19T00:00:00Z"},
    )
    assert info.issued_on == "2026-04-19T00:00:00Z"


def test_token_info_from_api_missing_fields_returns_empty_strings() -> None:
    """Future Cloudflare API additions / removals shouldn't break parsing."""
    info = TokenInfo.from_api({})
    assert info.id == ""
    assert info.name == ""
    assert info.issued_on == ""


# ---------------------------------------------------------------------------
# list_user_tokens — pagination
# ---------------------------------------------------------------------------


def test_list_user_tokens_single_page() -> None:
    client = MagicMock()
    client.get.return_value = _mock_response(
        {
            "success": True,
            "result": [
                {"id": "a", "name": "nexus-r2-foo"},
                {"id": "b", "name": "nexus-r2-bar"},
            ],
            "result_info": {"total_pages": 1},
        },
    )
    tokens = list_user_tokens(api_token="t", client=client)
    assert len(tokens) == 2
    assert tokens[0].id == "a"
    assert tokens[1].name == "nexus-r2-bar"
    # Used per_page=100 by default
    call = client.get.call_args
    assert call.kwargs["params"]["per_page"] == 100
    assert call.kwargs["params"]["page"] == 1


def test_list_user_tokens_walks_multiple_pages() -> None:
    """R-pagination: even if Cloudflare's per_page=100 already covers
    the 50-token cap, we walk total_pages defensively. This was the
    bug class: the legacy unpaginated lookup missed tokens past page 1
    when default per_page=25 truncated the response."""
    client = MagicMock()
    client.get.side_effect = [
        _mock_response(
            {
                "success": True,
                "result": [{"id": "a", "name": "n1"}, {"id": "b", "name": "n2"}],
                "result_info": {"total_pages": 2},
            },
        ),
        _mock_response(
            {
                "success": True,
                "result": [{"id": "c", "name": "n3"}],
                "result_info": {"total_pages": 2},
            },
        ),
    ]
    tokens = list_user_tokens(api_token="t", client=client)
    assert len(tokens) == 3
    assert [t.id for t in tokens] == ["a", "b", "c"]
    # Page 1 then page 2
    assert client.get.call_args_list[0].kwargs["params"]["page"] == 1
    assert client.get.call_args_list[1].kwargs["params"]["page"] == 2


def test_list_user_tokens_unsuccessful_response_raises() -> None:
    client = MagicMock()
    client.get.return_value = _mock_response(
        {"success": False, "errors": [{"message": "Auth invalid"}]},
    )
    with pytest.raises(RuntimeError, match="Auth invalid"):
        list_user_tokens(api_token="t", client=client)


def test_list_user_tokens_skips_non_dict_results() -> None:
    """Lenient parse: a string / int / None in the result array
    is skipped instead of crashing."""
    client = MagicMock()
    client.get.return_value = _mock_response(
        {
            "success": True,
            "result": [
                {"id": "a", "name": "valid"},
                "garbage",
                None,
                42,
            ],
            "result_info": {"total_pages": 1},
        },
    )
    tokens = list_user_tokens(api_token="t", client=client)
    assert len(tokens) == 1
    assert tokens[0].id == "a"


# ---------------------------------------------------------------------------
# find_tokens_by_name / find_tokens_by_prefix
# ---------------------------------------------------------------------------


def test_find_tokens_by_name_returns_all_duplicates() -> None:
    """If the account ended up with multiple same-name tokens (e.g.
    from an earlier API behavior change or race during re-setup),
    we return ALL of them so caller can clean them up uniformly."""
    tokens = [
        TokenInfo(id="a", name="nexus-r2-foo"),
        TokenInfo(id="b", name="nexus-r2-bar"),
        TokenInfo(id="c", name="nexus-r2-foo"),  # duplicate name
    ]
    matched = find_tokens_by_name(tokens, "nexus-r2-foo")
    assert {t.id for t in matched} == {"a", "c"}


def test_find_tokens_by_prefix_excludes_non_matches() -> None:
    tokens = [
        TokenInfo(id="a", name="nexus-r2-foo"),
        TokenInfo(id="b", name="Nexus-Stack"),
        TokenInfo(id="c", name="nexus-r2-bar"),
        TokenInfo(id="d", name="nexus2-something"),
    ]
    matched = find_tokens_by_prefix(tokens, "nexus-r2-")
    assert {t.id for t in matched} == {"a", "c"}


# ---------------------------------------------------------------------------
# delete_token
# ---------------------------------------------------------------------------


def test_delete_token_success() -> None:
    client = MagicMock()
    client.delete.return_value = _mock_response({"success": True, "result": {"id": "abc"}})
    result = delete_token("abc", api_token="t", name="x", client=client)
    assert result.id == "abc"
    assert result.name == "x"
    assert result.deleted is True
    assert result.error == ""


def test_delete_token_failure_extracts_error_message() -> None:
    client = MagicMock()
    client.delete.return_value = _mock_response(
        {"success": False, "errors": [{"message": "Not allowed"}]},
        status_code=403,
    )
    result = delete_token("abc", api_token="t", client=client)
    assert result.deleted is False
    assert "Not allowed" in result.error


def test_delete_token_non_json_response_falls_back_to_status_code() -> None:
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 502
    resp.json.side_effect = ValueError("not json")
    client.delete.return_value = resp
    result = delete_token("abc", api_token="t", client=client)
    assert result.deleted is False
    assert "HTTP 502" in result.error


# ---------------------------------------------------------------------------
# cleanup_orphan_tokens — name + prefix paths, dry-run, safety
# ---------------------------------------------------------------------------


def test_cleanup_requires_exactly_one_filter() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        cleanup_orphan_tokens(api_token="t")
    with pytest.raises(ValueError, match="exactly one"):
        cleanup_orphan_tokens(api_token="t", name="x", prefix="nexus-r2-x")


def test_cleanup_refuses_prefix_outside_nexus_r2() -> None:
    """R-safety: prefix MUST start with `nexus-r2-`. Without this guard,
    an operator typo could wipe the protected Nexus-Stack / Nexus2 /
    build tokens documented in CLAUDE.md."""
    for bad_prefix in ("Nexus-Stack", "nexus", "nexus-r", "n3xus-r2-foo"):
        with pytest.raises(ValueError, match="must start with"):
            cleanup_orphan_tokens(api_token="t", prefix=bad_prefix, dry_run=True)


def test_cleanup_dry_run_lists_but_doesnt_delete() -> None:
    client = MagicMock()
    client.get.return_value = _mock_response(
        {
            "success": True,
            "result": [
                {"id": "a", "name": "nexus-r2-foo"},
                {"id": "b", "name": "nexus-r2-bar"},
            ],
            "result_info": {"total_pages": 1},
        },
    )
    result = cleanup_orphan_tokens(
        api_token="t",
        prefix="nexus-r2-",
        dry_run=True,
        client=client,
    )
    assert result.dry_run is True
    assert result.total_tokens_before == 2
    assert len(result.matched) == 2
    assert result.deletions == ()
    # Delete was NOT called
    client.delete.assert_not_called()


def test_cleanup_apply_deletes_each_matched_token() -> None:
    client = MagicMock()
    client.get.return_value = _mock_response(
        {
            "success": True,
            "result": [
                {"id": "a", "name": "nexus-r2-foo"},
                {"id": "b", "name": "nexus-r2-foo"},  # duplicate name
                {"id": "c", "name": "nexus-r2-other"},
            ],
            "result_info": {"total_pages": 1},
        },
    )
    client.delete.return_value = _mock_response({"success": True})
    result = cleanup_orphan_tokens(
        api_token="t",
        name="nexus-r2-foo",
        dry_run=False,
        client=client,
    )
    assert result.dry_run is False
    # Both duplicate-named tokens deleted; the third (different name) untouched
    assert len(result.matched) == 2
    assert len(result.deletions) == 2
    assert result.deleted_count == 2
    assert result.failed_count == 0
    assert result.is_success is True


def test_cleanup_apply_partial_failure_records_per_token() -> None:
    client = MagicMock()
    client.get.return_value = _mock_response(
        {
            "success": True,
            "result": [
                {"id": "a", "name": "nexus-r2-x"},
                {"id": "b", "name": "nexus-r2-x"},
            ],
            "result_info": {"total_pages": 1},
        },
    )
    # First delete succeeds, second fails
    client.delete.side_effect = [
        _mock_response({"success": True}),
        _mock_response(
            {"success": False, "errors": [{"message": "Locked"}]},
            status_code=423,
        ),
    ]
    result = cleanup_orphan_tokens(
        api_token="t",
        name="nexus-r2-x",
        dry_run=False,
        client=client,
    )
    assert result.deleted_count == 1
    assert result.failed_count == 1
    assert result.is_success is False
    failures = [d for d in result.deletions if not d.deleted]
    assert "Locked" in failures[0].error


# ---------------------------------------------------------------------------
# build_inventory — audit shape
# ---------------------------------------------------------------------------


def test_build_inventory_remaining_slots() -> None:
    client = MagicMock()
    # 3 tokens total, 2 of them nexus-r2-*
    client.get.return_value = _mock_response(
        {
            "success": True,
            "result": [
                {"id": "a", "name": "nexus-r2-foo"},
                {"id": "b", "name": "nexus-r2-bar"},
                {"id": "c", "name": "Nexus-Stack"},  # protected, NOT matched
            ],
            "result_info": {"total_pages": 1},
        },
    )
    inv = build_inventory(api_token="t", client=client)
    assert inv.total == 3
    assert inv.prefix == DEFAULT_NEXUS_R2_PREFIX
    assert len(inv.matched) == 2
    assert inv.remaining_slots == ACCOUNT_TOKEN_HARD_CAP - 3
    assert inv.near_cap is False  # 47 slots free


def test_build_inventory_near_cap_threshold() -> None:
    """near_cap fires when fewer than 5 slots remain (matches the
    bug-report's 'Should' criterion: cron warns before the wall)."""
    client = MagicMock()
    # 46 tokens → 4 free slots → near_cap=True
    client.get.return_value = _mock_response(
        {
            "success": True,
            "result": [{"id": f"id-{i}", "name": f"nexus-r2-x{i}"} for i in range(46)],
            "result_info": {"total_pages": 1},
        },
    )
    inv = build_inventory(api_token="t", client=client)
    assert inv.total == 46
    assert inv.remaining_slots == 4
    assert inv.near_cap is True


def test_build_inventory_excludes_non_matching_prefix() -> None:
    """Token total counts ALL tokens; matched only counts the prefix."""
    client = MagicMock()
    client.get.return_value = _mock_response(
        {
            "success": True,
            "result": [
                {"id": "a", "name": "nexus-r2-foo"},
                {"id": "b", "name": "Nexus-Stack"},
                {"id": "c", "name": "Nexus2"},
                {"id": "d", "name": "nexus-stack-ch build token"},
            ],
            "result_info": {"total_pages": 1},
        },
    )
    inv = build_inventory(api_token="t", client=client, prefix="nexus-r2-")
    assert inv.total == 4
    assert len(inv.matched) == 1
    assert inv.matched[0].id == "a"


# ---------------------------------------------------------------------------
# Aggregate result fields
# ---------------------------------------------------------------------------


def test_cleanup_result_field_consistency() -> None:
    """deletion counts match what's in the deletions tuple."""
    deletions = (
        DeleteResult(id="a", name="x", deleted=True),
        DeleteResult(id="b", name="y", deleted=False, error="boom"),
        DeleteResult(id="c", name="z", deleted=True),
    )
    result = CleanupResult(
        total_tokens_before=10,
        matched=(
            TokenInfo(id="a", name="x"),
            TokenInfo(id="b", name="y"),
            TokenInfo(id="c", name="z"),
        ),
        deletions=deletions,
    )
    assert result.deleted_count == 2
    assert result.failed_count == 1
    assert result.is_success is False
