"""Storage / gc tests — no network."""

from __future__ import annotations

from pathlib import Path

from zerg.store import GCPolicy, dir_size, gc, human_bytes, usage


def test_human_bytes():
    assert human_bytes(500) == "500B"
    assert "KB" in human_bytes(2048)


def test_gc_media_and_age(tmp_path: Path):
    root = tmp_path / "data"
    spider = root / "site_a"
    images = spider / "images" / "foo"
    images.mkdir(parents=True)
    (images / "001.jpg").write_bytes(b"x" * 1000)
    (spider / "items.jsonl").write_text("{}\n")
    u = usage(root)
    assert u.bytes >= 1000

    r = gc(root, GCPolicy(media_only=True, dry_run=False))
    assert not images.exists() or not any(images.rglob("*"))
    assert (spider / "items.jsonl").exists()
    assert r.freed > 0


def test_gc_keep_spiders(tmp_path: Path):
    root = tmp_path / "data"
    for i, name in enumerate(("old", "mid", "new")):
        d = root / name
        d.mkdir(parents=True)
        f = d / "items.jsonl"
        f.write_text("x" * (i + 1))
        # bump mtime order
        import os
        import time

        os.utime(d, (time.time() - 100 + i, time.time() - 100 + i))

    protected = root / "protected"
    protected.mkdir()
    r = gc(
        root,
        GCPolicy(
            keep_spiders=1,
            max_spider_bytes=None,
            max_data_bytes=None,
            protect=("protected",),
        ),
    )
    assert (root / "new").exists()
    assert not (root / "old").exists()
    assert protected.exists()
    assert r.freed >= 0
