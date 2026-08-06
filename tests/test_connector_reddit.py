"""Reddit saved-posts connector — mapping and auth-error behaviour."""
from __future__ import annotations

import pytest

from pka.connectors.reddit import RedditConnectorError, load_saved


def test_load_saved_maps_posts_and_comments(fake_reddit_client):
    items = load_saved(client=fake_reddit_client)

    assert [i.source_id for i in items] == ["t3_selfpost", "t3_linkpost", "t1_comment1"]

    self_post, link_post, comment = items

    # Self-post: content inline, no external target.
    assert self_post.kind == "post"
    assert self_post.external_url is None
    assert self_post.body and "Raft" in self_post.body
    assert self_post.url_or_path == self_post.permalink
    assert self_post.permalink.startswith("https://www.reddit.com/")
    assert self_post.collection == "r/compsci"

    # Link post: external URL present, body empty.
    assert link_post.kind == "post"
    assert link_post.external_url == "https://example.com/paxos.pdf"
    assert link_post.body is None
    assert link_post.url_or_path == "https://example.com/paxos.pdf"

    # Comment: titled by its thread, body carried inline.
    assert comment.kind == "comment"
    assert comment.title == 'Comment on "Understanding Raft"'
    assert comment.external_url is None
    assert comment.body and "leader election" in comment.body


def test_deleted_body_becomes_none(fake_reddit_client):
    me = fake_reddit_client.user.me.return_value
    from types import SimpleNamespace

    deleted = SimpleNamespace(
        name="t1_deleted",
        body="[deleted]",
        link_title="Gone",
        permalink="/r/x/comments/gone/c/",
        subreddit="x",
        created_utc=1700000300,
    )
    me.saved.side_effect = lambda *a, **k: iter([deleted])

    (item,) = load_saved(client=fake_reddit_client)
    assert item.body is None


def test_dedupes_by_fullname(fake_reddit_client):
    me = fake_reddit_client.user.me.return_value
    from types import SimpleNamespace

    dupe = SimpleNamespace(
        name="t3_dupe", title="Once", selftext="body", is_self=True,
        url="https://reddit.com/x", permalink="/r/x/comments/dupe/",
        subreddit="x", created_utc=1,
    )
    me.saved.side_effect = lambda *a, **k: iter([dupe, dupe])

    items = load_saved(client=fake_reddit_client)
    assert len(items) == 1


def test_missing_praw_raises_helpful_error(monkeypatch):
    """When no client is injected and praw is absent, error names the fix."""
    import builtins

    real_import = builtins.__import__

    def _no_praw(name, *args, **kwargs):
        if name == "praw":
            raise ImportError("No module named 'praw'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_praw)

    with pytest.raises(RedditConnectorError, match="pip install"):
        load_saved()
