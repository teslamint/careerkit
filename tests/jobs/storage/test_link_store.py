"""Tests for LinkStore file-backed storage."""

from __future__ import annotations

from pathlib import Path

import pytest

from careerkit.jobs.adapters.storage.link_store import LinkStore
from careerkit.jobs.domain.link_model import LinkGroup, LinkSchemaError
from careerkit.jobs.domain.model import JobKey


@pytest.fixture
def store(tmp_path: Path) -> LinkStore:
    return LinkStore(tmp_path / "links")


def test_create_and_get(store: LinkStore):
    k1 = JobKey("saramin", "111")
    k2 = JobKey("wanted", "222")
    group = store.create([k1, k2], note="same company")
    assert len(group.group_id) == 32
    assert len(group.members) == 2
    assert group.note == "same company"

    by_k1 = store.get_by_key(k1)
    assert by_k1 is not None
    assert by_k1.group_id == group.group_id

    by_k2 = store.get_by_key(k2)
    assert by_k2 is not None
    assert by_k2.group_id == group.group_id

    by_id = store.get_by_group_id(group.group_id)
    assert by_id is not None
    assert by_id.group_id == group.group_id


def test_list_all(store: LinkStore):
    store.create([JobKey("a", "1"), JobKey("b", "2")])
    store.create([JobKey("c", "3"), JobKey("d", "4")])
    groups = store.list_all()
    assert len(groups) == 2


def test_remove_last_member_deletes_file(store: LinkStore):
    k1 = JobKey("a", "1")
    k2 = JobKey("b", "2")
    group = store.create([k1, k2])
    group_file = store.root / f"{group.group_id}.json"

    result1 = store.remove_member(k1)
    assert result1 is None
    assert not group_file.exists()

    assert store.get_by_key(k1) is None
    assert store.get_by_key(k2) is None


def test_remove_from_three_members(store: LinkStore):
    k1 = JobKey("a", "1")
    k2 = JobKey("b", "2")
    k3 = JobKey("c", "3")
    store.create([k1, k2, k3])

    updated = store.remove_member(k1)
    assert updated is not None
    assert len(updated.members) == 2
    assert k1 not in updated.members
    assert store.get_by_key(k1) is None
    assert store.get_by_key(k2) is not None


def test_reject_already_linked_key(store: LinkStore):
    k1 = JobKey("a", "1")
    k2 = JobKey("b", "2")
    k3 = JobKey("c", "3")
    store.create([k1, k2])
    with pytest.raises(ValueError, match="이미.*그룹"):
        store.create([k1, k3])


def test_get_nonexistent(store: LinkStore):
    assert store.get_by_key(JobKey("x", "99")) is None
    assert store.get_by_group_id("nonexist") is None


def test_remove_nonexistent(store: LinkStore):
    assert store.remove_member(JobKey("x", "99")) is None


def test_create_writes_json_file(store: LinkStore):
    group = store.create([JobKey("a", "1"), JobKey("b", "2")])
    path = store.root / f"{group.group_id}.json"
    assert path.exists()
    import json
    data = json.loads(path.read_text())
    assert data["group_id"] == group.group_id


def test_create_replace_failure_leaves_no_group(store: LinkStore, monkeypatch):
    original_replace = Path.replace

    def failing_replace(self, target):
        raise OSError("injected replace failure")

    monkeypatch.setattr(Path, "replace", failing_replace)
    with pytest.raises(OSError, match="injected"):
        store.create([JobKey("a", "1"), JobKey("b", "2")])
    monkeypatch.setattr(Path, "replace", original_replace)
    assert store.list_all() == []


def test_remove_unlink_failure_preserves_file(store: LinkStore, monkeypatch):
    k1 = JobKey("a", "1")
    k2 = JobKey("b", "2")
    group = store.create([k1, k2])
    group_file = store.root / f"{group.group_id}.json"
    assert group_file.exists()

    original_unlink = Path.unlink

    def failing_unlink(self, **kwargs):
        raise OSError("injected unlink failure")

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    with pytest.raises(OSError, match="injected"):
        store.remove_member(k1)
    monkeypatch.setattr(Path, "unlink", original_unlink)
    assert group_file.exists()


def test_create_rejects_id_collision_without_clobbering(store: LinkStore, monkeypatch):
    """A pre-existing file with the ID that generate_group_id returns must
    raise, not be silently overwritten (review finding: blind tmp_path.replace)."""
    g2 = LinkGroup(
        group_id="f" * 32,
        members=(JobKey("c", "1"), JobKey("d", "1")),
        created_at="2026-08-25T01:00:00+09:00",
    )
    store.root.mkdir(parents=True, exist_ok=True)
    store._save_group(g2)
    monkeypatch.setattr(
        "careerkit.jobs.adapters.storage.link_store.generate_group_id",
        lambda: "f" * 32,
    )
    with pytest.raises(ValueError):
        store.create([JobKey("e", "1"), JobKey("f", "1")])
    survivor = store.get_by_group_id("f" * 32)
    assert survivor is not None
    assert survivor == g2


def test_get_by_group_id_rejects_path_traversal(store: LinkStore):
    """get_by_group_id must not read files outside the link root
    (review finding: unvalidated group_id fed to Path)."""
    import json

    outside_dir = store.root.parent / "outside"
    store.root.mkdir(parents=True, exist_ok=True)
    outside_dir.mkdir(parents=True)
    group = LinkGroup(
        group_id="a" * 32,
        members=(JobKey("c", "1"), JobKey("d", "1")),
        created_at="2026-08-25T01:00:00+09:00",
    )
    (outside_dir / "x.json").write_text(
        json.dumps(group.to_dict()) + "\n", encoding="utf-8"
    )

    assert store.get_by_group_id("../outside/x") is None
    assert store.get_by_group_id("/tmp/x") is None


def _write_raw_group(store: LinkStore, filename: str, payload: dict) -> None:
    import json

    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / filename).write_text(
        json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def test_store_rejects_wellformed_unknown_schema_version(store: LinkStore):
    """A well-formed v2 group file must fail loudly on every store entry
    point, not load as a partial group (review finding: schema version swallowed)."""
    _write_raw_group(
        store,
        "e" * 32 + ".json",
        {
            "group_id": "e" * 32,
            "members": [
                {"platform": "a", "job_id": "1"},
                {"platform": "b", "job_id": "2"},
            ],
            "created_at": "2026-08-25T01:00:00+09:00",
            "note": None,
            "schema_version": 2,
        },
    )
    with pytest.raises(LinkSchemaError):
        store.get_by_group_id("e" * 32)
    with pytest.raises(LinkSchemaError):
        store.list_all()
    with pytest.raises(LinkSchemaError):
        store.create([JobKey("a", "1"), JobKey("b", "2")])


def test_store_rejects_broken_shape_unknown_version_before_member_access(store: LinkStore):
    """A v2 file missing 'members' must fail on the version check, not be
    swallowed as a missing file and let create() build a duplicate
    (review finding: version error masked by KeyError)."""
    _write_raw_group(
        store, "d" * 32 + ".json", {"group_id": "d" * 32, "schema_version": 2}
    )
    with pytest.raises(LinkSchemaError):
        store.create([JobKey("a", "1"), JobKey("b", "2")])
