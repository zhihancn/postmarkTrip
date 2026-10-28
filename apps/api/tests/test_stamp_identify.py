"""M3C-5：候选归一化与应用（不连数据库）。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from postmarktrip.domain.stamp_identify import (
    IdentifyApplyError,
    _normalize_candidates,
    apply_candidate,
)


def _candidate(c_id: int = 659604, name: str = "Mu Dan Jiang (Snow Town)") -> dict[str, Any]:
    return {
        "c_id": c_id,
        "p_id": 1,
        "name": name,
        "similarity": 75.0,
        "url": "https://colnect.com/x",
        "image": "https://i.colnect.net/x.jpg",
        "query_sha256": "abc",
    }


class FakeDb:
    def __init__(self, existing: Any = None) -> None:
        self.existing = existing
        self.added: list[Any] = []
        self.commits = 0

    def scalar(self, stmt: Any) -> Any:
        return self.existing

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
        return None

    def commit(self) -> None:
        self.commits += 1

    def refresh(self, obj: Any) -> None:
        return None


def _stamp(**over: Any) -> Any:
    values = {
        "identify_candidates": [_candidate()],
        "catalog_id": None,
        "catalog_ref": None,
        "catalog_system": None,
        "name": None,
        "face_value": None,
        "currency": None,
        "issue_date": None,
    }
    values.update(over)
    return SimpleNamespace(**values)


def test_normalize_candidates_keeps_expected_fields() -> None:
    out = _normalize_candidates(
        {"image_sha256": "sha", "results": [_candidate(), {"c_id": None}, {"bad": 1}]}
    )
    assert len(out) == 1
    assert out[0]["c_id"] == 659604
    assert out[0]["query_sha256"] == "sha"


def test_apply_candidate_creates_draft_and_links() -> None:
    stamp = _stamp()
    db = FakeDb(existing=None)
    stamp, catalog, created = apply_candidate(db, stamp, 659604)
    assert created is True
    assert catalog.colnect_c_id == 659604
    assert catalog.source_state == "draft"
    assert stamp.catalog_id == catalog.id
    assert db.commits == 1


def test_apply_candidate_reuses_existing_catalog() -> None:
    existing = SimpleNamespace(
        id="cat-1",
        colnect_c_id=659604,
        colnect_url="u",
        design_name="Existing",
        catalog_system="china",
        catalog_ref="普32",
        face_value=None,
        currency=None,
        issue_date=None,
    )
    stamp = _stamp()
    db = FakeDb(existing=existing)
    stamp, catalog, created = apply_candidate(db, stamp, 659604)
    assert created is False
    assert catalog is existing
    assert stamp.catalog_id == "cat-1"
    assert stamp.catalog_ref == "普32"


def test_apply_candidate_requires_confirmation_when_linked() -> None:
    stamp = _stamp(catalog_id="old")
    with pytest.raises(IdentifyApplyError):
        apply_candidate(FakeDb(existing=None), stamp, 659604)


def test_apply_candidate_rejects_unknown_candidate() -> None:
    stamp = _stamp(identify_candidates=[])
    with pytest.raises(IdentifyApplyError):
        apply_candidate(FakeDb(existing=None), stamp, 1)
