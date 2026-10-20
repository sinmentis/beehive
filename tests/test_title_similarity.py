# tests/test_title_similarity.py
"""The neutral title-similarity primitives extracted out of research/clustering.py.

These are exercised directly here -- domain-free, no database -- while
tests/research/test_clustering.py continues to prove that Research's Evidence Cluster behavior is
unchanged by the extraction.
"""
import pytest

from beehive.title_similarity import (DEFAULT_SIMILARITY_THRESHOLD, group_by_title_similarity,
                                       jaccard_similarity, tokenize_title)

# ============================================================================
# tokenize_title
# ============================================================================


def test_tokenizes_lowercased_alphanumeric_runs():
    assert tokenize_title("RBNZ raises OCR to 5.5%") == frozenset(
        {"rbnz", "raises", "ocr", "to", "5", "5"})


def test_punctuation_and_case_do_not_change_tokens():
    assert tokenize_title("Cash-Rate Decision!") == tokenize_title("cash rate decision")


def test_title_with_no_ascii_tokens_yields_no_tokens():
    assert tokenize_title("政策公告") == frozenset()


# ============================================================================
# jaccard_similarity
# ============================================================================


def test_identical_token_sets_score_one():
    tokens = tokenize_title("central bank raises rates")
    assert jaccard_similarity(tokens, tokens) == 1.0


def test_disjoint_token_sets_score_zero():
    assert jaccard_similarity(tokenize_title("alpha"), tokenize_title("beta")) == 0.0


def test_empty_side_scores_zero_rather_than_matching_everything():
    assert jaccard_similarity(frozenset(), tokenize_title("anything")) == 0.0
    assert jaccard_similarity(tokenize_title("anything"), frozenset()) == 0.0
    assert jaccard_similarity(frozenset(), frozenset()) == 0.0


def test_partial_overlap_is_intersection_over_union():
    left, right = frozenset({"a", "b"}), frozenset({"b", "c"})
    assert jaccard_similarity(left, right) == pytest.approx(1 / 3)


# ============================================================================
# group_by_title_similarity
# ============================================================================


def test_near_duplicate_titles_group_together():
    groups = group_by_title_similarity([
        ("a", "RBNZ raises official cash rate to 5.5%"),
        ("b", "RBNZ raises official cash rate 5.5 percent"),
    ])
    assert groups == [["a", "b"]]


def test_dissimilar_titles_do_not_group():
    assert group_by_title_similarity([
        ("a", "RBNZ raises official cash rate"),
        ("b", "Completely unrelated sports headline"),
    ]) == []


def test_singleton_group_is_dropped_by_default():
    assert group_by_title_similarity([("a", "A totally unique headline")]) == []


def test_min_group_size_one_keeps_singletons():
    assert group_by_title_similarity(
        [("a", "A totally unique headline")], min_group_size=1) == [["a"]]


def test_min_group_size_below_one_is_rejected():
    with pytest.raises(ValueError, match="min_group_size"):
        group_by_title_similarity([("a", "x")], min_group_size=0)


def test_empty_input_produces_no_groups():
    assert group_by_title_similarity([]) == []


def test_group_absorbs_member_tokens_so_a_later_title_can_match_through_any_member():
    groups = group_by_title_similarity([
        ("a", "central bank raises interest rates sharply"),
        ("b", "central bank raises interest rates today"),
        ("c", "central bank raises interest rates again"),
    ])
    assert len(groups) == 1
    assert set(groups[0]) == {"a", "b", "c"}


def test_keys_are_returned_in_first_seen_order():
    groups = group_by_title_similarity([
        ("second", "shared headline text here"),
        ("first", "shared headline text here"),
    ])
    assert groups == [["second", "first"]]


def test_threshold_is_configurable():
    entries = [("a", "cash rate decision today"), ("b", "cash rate decision announced")]
    assert group_by_title_similarity(entries, similarity_threshold=0.3) == [["a", "b"]]
    assert group_by_title_similarity(entries, similarity_threshold=0.99) == []


def test_default_threshold_is_the_documented_conservative_value():
    assert DEFAULT_SIMILARITY_THRESHOLD == 0.6


def test_grouping_is_deterministic_across_repeated_calls():
    entries = [
        ("a", "council votes on rates rise"),
        ("b", "council votes on rates rise proposal"),
        ("c", "unrelated transport announcement"),
    ]
    assert group_by_title_similarity(entries) == group_by_title_similarity(entries)


def test_accepts_any_hashable_key_type_including_ints():
    assert group_by_title_similarity([(1, "same words here"), (2, "same words here")]) == [[1, 2]]


def test_input_iterable_is_never_mutated():
    entries = [("a", "x y z"), ("b", "x y z")]
    original = list(entries)
    group_by_title_similarity(entries)
    assert entries == original


def test_generator_input_is_accepted():
    entries = ((key, "shared title words") for key in ("a", "b"))
    assert group_by_title_similarity(entries) == [["a", "b"]]


def test_untokenizable_titles_never_group_with_each_other():
    """Two titles that produce no tokens score 0.0, so they stay separate rather than collapsing
    into one bogus 'everything matches everything' group."""
    assert group_by_title_similarity([("a", "政策公告"), ("b", "選挙結果")]) == []
