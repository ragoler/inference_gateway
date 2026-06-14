"""Unit tests for the dependency-free load-test helpers (app/loadtest_util.py).

These run locally with no cluster and no FastAPI/httpx — just pytest.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from loadtest_util import (  # noqa: E402
    make_documents, build_prompts, order_prompts, percentile, summarize, compare,
)


def test_make_documents_count_and_uniqueness():
    docs = make_documents(8, "abc")
    assert len(docs) == 8
    texts = [d["text"] for d in docs]
    # Distinct documents, all carrying the nonce so they are fresh prefixes.
    assert len(set(texts)) == 8
    assert all("abc" in t for t in texts)
    # Long enough to span several KV blocks (well over a block at any block size).
    assert all(len(t.split()) > 50 for t in texts)


def test_make_documents_nonce_changes_prefix():
    a = make_documents(3, "run1")[0]["text"]
    b = make_documents(3, "run2")[0]["text"]
    assert a != b  # different nonce -> different (cold) prefix


def test_build_prompts_shape():
    docs = make_documents(3, "n")
    prompts = build_prompts(docs, 4)
    assert len(prompts) == 12  # 3 docs * 4 queries
    # Each prompt shares its document's prefix (the doc text leads the prompt).
    for doc_id, prompt in prompts:
        assert docs[doc_id]["text"] in prompt
        assert prompt.rstrip().endswith("Answer:")


def _flat(waves):
    return [it for w in waves for it in w]


def test_order_grouped_default():
    prompts = build_prompts(make_documents(3, "n"), 2)  # 6 prompts, doc-grouped
    waves = order_prompts(prompts, "grouped")
    assert len(waves) == 1
    assert [d for d, _ in waves[0]] == [0, 0, 1, 1, 2, 2]


def test_order_interleave_round_robins_docs():
    prompts = build_prompts(make_documents(3, "n"), 2)
    waves = order_prompts(prompts, "interleave")
    assert len(waves) == 1
    # First each doc's q1, then each doc's q2.
    assert [d for d, _ in waves[0]] == [0, 1, 2, 0, 1, 2]


def test_order_stagger_two_waves_prime_then_repeats():
    prompts = build_prompts(make_documents(3, "n"), 3)
    waves = order_prompts(prompts, "stagger")
    assert len(waves) == 2
    assert [d for d, _ in waves[0]] == [0, 1, 2]            # one primer per doc
    assert sorted(d for d, _ in waves[1]) == [0, 0, 1, 1, 2, 2]  # the repeats
    assert len(_flat(waves)) == 9


def test_order_stagger_single_query_has_no_repeat_wave():
    prompts = build_prompts(make_documents(3, "n"), 1)
    waves = order_prompts(prompts, "stagger")
    assert len(waves) == 1
    assert len(waves[0]) == 3


def test_order_shuffle_deterministic_per_seed_and_complete():
    prompts = build_prompts(make_documents(4, "n"), 3)
    a = order_prompts(prompts, "shuffle", seed=7)
    b = order_prompts(prompts, "shuffle", seed=7)
    c = order_prompts(prompts, "shuffle", seed=8)
    assert a == b                      # same seed -> identical (apples-to-apples)
    assert a != c                      # different seed -> different order
    assert sorted(_flat(a)) == sorted(prompts)  # no requests dropped/added


def test_order_preserves_request_count_all_patterns():
    prompts = build_prompts(make_documents(5, "n"), 4)  # 20 prompts
    for pat in ("grouped", "shuffle", "stagger", "interleave"):
        assert len(_flat(order_prompts(prompts, pat))) == 20


def test_percentile_basic():
    assert percentile([], 95) == 0.0
    assert percentile([42], 50) == 42.0
    assert percentile([0, 10], 50) == 5.0
    data = [10, 20, 30, 40, 50]
    assert percentile(data, 0) == 10.0
    assert percentile(data, 100) == 50.0
    assert percentile(data, 50) == 30.0


def test_summarize_metrics():
    records = [
        {"ok": True, "ttft_ms": 100.0, "out_tokens": 16},
        {"ok": True, "ttft_ms": 300.0, "out_tokens": 16},
        {"ok": False, "ttft_ms": None, "out_tokens": 0},
    ]
    m = summarize(records, wall_s=2.0, hit_rate=0.5,
                  pod_spread={"vllm-server-a": 100, "vllm-server-b": 20})
    assert m["requests_total"] == 3
    assert m["requests_ok"] == 2
    assert m["requests_failed"] == 1
    assert m["ttft_mean_ms"] == 200.0
    assert m["ttft_p50_ms"] == 200.0
    assert m["throughput_tok_s"] == 16.0  # 32 tokens / 2 s
    assert m["hit_rate"] == 0.5
    assert m["pod_spread"]["vllm-server-a"] == 100


def test_summarize_empty_is_safe():
    m = summarize([], wall_s=0.0, hit_rate=0.0, pod_spread={})
    assert m["requests_total"] == 0
    assert m["ttft_p95_ms"] == 0.0
    assert m["throughput_tok_s"] == 0.0


def test_compare_headline():
    llmd = {"ttft_p95_ms": 200.0, "throughput_tok_s": 120.0, "hit_rate": 0.85}
    direct = {"ttft_p95_ms": 800.0, "throughput_tok_s": 60.0, "hit_rate": 0.20}
    c = compare(llmd, direct)
    assert c["ttft_p95_speedup"] == 4.0      # 800 / 200
    assert c["throughput_ratio"] == 2.0      # 120 / 60
    assert c["hit_rate_gain"] == 0.65        # 0.85 - 0.20


def test_compare_handles_missing():
    assert compare(None, {"ttft_p95_ms": 1}) == {}
    assert compare({"ttft_p95_ms": 1}, None) == {}
