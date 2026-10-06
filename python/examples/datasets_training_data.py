#!/usr/bin/env python3
"""Reproducible training data with a Lakekeeper Dataset.

Uploads a folder of images as a dataset version, tags the version a model is trained
on, lets the producer move on, and then reproduces the training set byte for byte.

Env:
  LAKEKEEPER    e.g. http://localhost:8181
  WAREHOUSE_ID  warehouse UUID (the URL path prefix, not the name)
  PROJECT_ID    x-project-id (optional)
  NAMESPACE     default ai          DATASET   default product_images
  IMG_DIR       default examples/assets/img
  TOKEN         a static token (any value against a no-auth dev server)
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from pylakekeeper import Client, NotFoundError, StaticToken

NAMESPACE = os.environ.get("NAMESPACE", "ai")
DATASET = os.environ.get("DATASET", "product_images")
IMG_DIR = Path(os.environ.get("IMG_DIR", Path(__file__).parent / "assets" / "img"))
WORK = Path(os.environ.get("WORK_DIR", "/tmp/pylakekeeper-datasets-example"))


def digest(root: Path) -> str:
    """One hash over every file below ``root``: equal means byte-for-byte the same set."""
    h = hashlib.sha256()
    for p in sorted(q for q in root.rglob("*") if q.is_file() and not q.name.startswith(".")):
        h.update(p.relative_to(root).as_posix().encode() + b"\0" + p.read_bytes())
    return h.hexdigest()[:16]


def main() -> None:
    with Client(
        base_url=os.environ["LAKEKEEPER"],
        warehouse=os.environ["WAREHOUSE_ID"],
        auth=StaticToken(os.environ.get("TOKEN", "dev")),
        project_id=os.environ.get("PROJECT_ID"),
    ) as lk:
        try:
            ds = lk.datasets.load(NAMESPACE, DATASET)
        except NotFoundError:
            ds = lk.datasets.create(NAMESPACE, DATASET)
        print(f"dataset   {ds}  at {ds.location}")

        # 1. The producer uploads a folder and commits it as one snapshot.
        main = ds.branch("main")
        with main.commit(summary={"source": str(IMG_DIR)}) as c:
            c.upload_dir(IMG_DIR, prefix="images/")
        print(f"commit    {c.snapshot.snapshot_id}  +{len(c.added)} files")

        # 2. Training reads a tag: a name for that snapshot that never moves.
        tag = f"train-{c.snapshot.created_at:%Y%m%d-%H%M%S}"
        ds.create_tag(tag)
        trained = ds.tag(tag).download(WORK / "trained-on")
        trained_digest = digest(trained.path)
        print(f"train     tag {tag} -> {trained.downloaded} files, digest {trained_digest}")

        # 3. The producer moves on: one image replaced in place, one removed, one added.
        first, second = sorted(f.logical_key for f in ds.tag(tag).files())[:2]
        with main.commit(summary={"change": "relabel"}) as c:
            c.put(first, b"replaced bytes")
            c.remove(second)
            c.put("images/new.png", b"a new image")
        for change in ds.diff(tag, "main"):
            print(f"diff      {change.change:<8} {change.logical_key}")

        # 4. Months later: the same tag gives back exactly what the model saw.
        again = ds.tag(tag).download(WORK / "reproduced")
        print(f"reproduce tag {tag} -> digest {digest(again.path)}")
        assert digest(again.path) == trained_digest, "the training set changed under its tag"
        print("ok        the tag reproduced the training set byte for byte")


if __name__ == "__main__":
    main()
