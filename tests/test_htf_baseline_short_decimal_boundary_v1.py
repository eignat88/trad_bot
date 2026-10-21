from tools.research.htf_baseline_short_asof_atomic_cluster_v1 import (
    AtomicAsofClusters,
    within_price_bps,
)


def event(event_id, asof, price):
    return {
        "event_id": event_id,
        "episode_id": event_id,
        "known_at_ms": asof,
        "level_price_at_touch": price,
    }


def test_exact_20bps_boundary_is_scale_invariant():
    for a, b in ((100.0, 100.2), (1000.0, 1002.0), (2500.0, 2505.0)):
        assert within_price_bps(a, b, 20)

    assert not within_price_bps(100.0, 100.201, 20)


def test_same_asof_exact_boundary_clusters_across_scales():
    for scale in (1, 10, 25):
        clusters = AtomicAsofClusters("TEST")
        results = clusters.observe([
            event("a", 300000, 100.0 * scale),
            event("b", 300000, 100.2 * scale),
        ], price_bps=20)

        assert len(results) == 2
        assert len(clusters.clusters) == 1
        assert len({r["reaction_cluster_id"] for r in results}) == 1


def test_prior_anchor_exact_boundary_attaches_across_scales():
    for scale in (1, 10, 25):
        clusters = AtomicAsofClusters("TEST")
        first = clusters.observe(
            [event("a", 300000, 100.0 * scale)],
            price_bps=20,
        )
        second = clusters.observe(
            [event("b", 600000, 100.2 * scale)],
            price_bps=20,
        )

        assert second[0]["status"] == "PROVISIONAL_ATTACH"
        assert second[0]["reaction_cluster_id"] == first[0]["reaction_cluster_id"]
        assert len(clusters.clusters) == 1


def test_just_outside_boundary_remains_separate():
    clusters = AtomicAsofClusters("TEST")
    clusters.observe(
        [event("a", 300000, 100.0)],
        price_bps=20,
    )
    result = clusters.observe(
        [event("b", 600000, 100.201)],
        price_bps=20,
    )

    assert result[0]["status"] == "PROVISIONAL_NEW"
    assert len(clusters.clusters) == 2
