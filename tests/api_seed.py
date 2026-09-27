"""Row builders shared by the ``test_api_*`` modules.

Split out of the old 2,400-line ``tests/test_api.py``, whose
section banners became one module per router. These build the SQLite rows an
endpoint test needs; the ``client`` fixture that serves them lives in
``conftest.py`` so every module gets it without an import.
"""

import time

import sqlalchemy as sa

from pka.db.schema import cluster_assignments, cluster_runs, clusters
from tests.conftest import make_document


def seed_docs(n: int = 3) -> list[int]:
    ids = []
    for i in range(n):
        src = ["zotero", "firefox", "calibre"][i % 3]
        ids.append(
            make_document(
                src,
                f"K{i:03d}",
                f"Document {i}",
                f"https://example.com/{i}",
                int(time.time()) - i * 86400,
            )
        )
    return ids


def seed_run(
    doc_ids: list[int],
    n_clusters: int = 2,
    *,
    with_l2: bool = False,
    noise_doc_ids: list[int] | None = None,
) -> int:
    from pka.db.queries import get_engine

    eng = get_engine()
    now = int(time.time())
    with eng.begin() as con:
        run_res = con.execute(
            cluster_runs.insert().values(
                timestamp=now,
                algorithm="HDBSCAN-hierarchical",
                parameters="{}",
                accepted=True,
                status="finished",
            )
        )
        run_id = run_res.inserted_primary_key[0]
        l1_ids: list[int] = []
        for i in range(n_clusters):
            cl_res = con.execute(
                clusters.insert().values(
                    label=f"Cluster {i}",
                    description="",
                    created_at=now,
                    run_id=run_id,
                    level=1,
                    parent_cluster_id=None,
                )
            )
            cid = cl_res.inserted_primary_key[0]
            l1_ids.append(cid)
            for did in doc_ids[i::n_clusters]:
                con.execute(
                    cluster_assignments.insert().values(
                        document_id=did,
                        cluster_id=cid,
                        run_id=run_id,
                        score=0.9,
                        assigned_at=now,
                        level=1,
                    )
                )
        if with_l2 and l1_ids:
            parent = l1_ids[0]
            parent_docs = doc_ids[0::n_clusters]
            for sub_idx in range(2):
                l2_res = con.execute(
                    clusters.insert().values(
                        label=f"Subcluster {sub_idx}",
                        description="",
                        created_at=now,
                        run_id=run_id,
                        level=2,
                        parent_cluster_id=parent,
                    )
                )
                l2_id = l2_res.inserted_primary_key[0]
                for did in parent_docs[sub_idx::2]:
                    con.execute(
                        cluster_assignments.insert().values(
                            document_id=did,
                            cluster_id=l2_id,
                            run_id=run_id,
                            score=0.8,
                            assigned_at=now,
                            level=2,
                        )
                    )
        if noise_doc_ids:
            noise_res = con.execute(
                clusters.insert().values(
                    label="Unclustered",
                    description="",
                    created_at=now,
                    run_id=run_id,
                    level=1,
                    parent_cluster_id=None,
                    is_noise=True,
                )
            )
            noise_cid = noise_res.inserted_primary_key[0]
            for did in noise_doc_ids:
                con.execute(
                    cluster_assignments.insert().values(
                        document_id=did,
                        cluster_id=noise_cid,
                        run_id=run_id,
                        score=0.1,
                        assigned_at=now,
                        level=1,
                    )
                )
    return run_id


def seed_image(client=None) -> int:
    """Insert a test image (unified document row + images sidecar); return image id."""
    from pka.db.queries import get_engine
    from pka.db.schema import image_tags
    from pka.db.schema import images as images_tbl

    now = int(time.time())
    doc_id = make_document(
        "image",
        "/tmp/slide.png",
        "slide.png",
        "/tmp/slide.png",
        now,
        fetch_status="available",
    )
    with get_engine().begin() as con:
        res = con.execute(
            images_tbl.insert().values(
                document_id=doc_id,
                path="/tmp/slide.png",
                filename="slide.png",
                image_type="slide",
                width=800,
                height=600,
                file_size=12345,
                date_taken=now,
                ocr_text="Neural networks overview",
                description="A slide about neural networks",
                clip_vector_id="clip-1",
                text_vector_id="text-1",
                indexed_at=now,
            )
        )
        image_id = res.inserted_primary_key[0]
        con.execute(
            image_tags.insert().values(
                image_id=image_id,
                tag="ml",
                origin="auto",
            )
        )
    return image_id


def image_document_id(image_id: int) -> int:
    from pka.db.queries import get_engine
    from pka.db.schema import images as images_tbl

    with get_engine().connect() as con:
        return con.execute(
            sa.select(images_tbl.c.document_id).where(images_tbl.c.id == image_id)
        ).scalar()
