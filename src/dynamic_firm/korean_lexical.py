"""Small dependency-free Korean lexical normalization for local retrieval.

This module intentionally does not claim to be a morphological analyzer.  It
offers conservative *surface* variants for local recall: one trailing
postposition and one unambiguous connective inside a sufficiently long Hangul
run.  Thus ``가격전략의변경을`` can narrow toward raw Folder text such as
``가격 전략 변경`` without storing a lemma, POS tag, or rewritten query.
Candidate narrowing remains local, bounded, and is followed by the ordinary
hybrid evidence ranker.
"""

from __future__ import annotations

import re


_HANGUL_RUN = re.compile(r"[\uac00-\ud7af]+")

# Longest first prevents a shorter suffix from consuming a compound particle.
# This is deliberately a small transparent list, not a linguistic claim about
# stems, conjugation, or part-of-speech analysis.
_POSTPOSITIONS = (
    "으로부터",
    "에게서",
    "에서는",
    "에게는",
    "으로는",
    "으로도",
    "에서의",
    "까지는",
    "부터는",
    "한테서",
    "처럼",
    "보다",
    "에게",
    "한테",
    "에서",
    "에는",
    "으로",
    "까지",
    "부터",
    "라도",
    "마저",
    "조차",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "의",
    "에",
    "와",
    "과",
    "도",
    "만",
)

# These are deliberately narrower than the postposition list.  Removing a
# connective in the middle of an unspaced run can be useful for user-authored
# Knowledge names (``가격전략의변경``) but is not generally safe linguistic
# segmentation.  We therefore only offer one removal and require two Hangul
# syllables on both sides.  In particular, subject particles such as ``이``
# and ``가`` are excluded because they collide too easily with ordinary stems.
_COMPOUND_CONNECTORS = ("그리고", "및", "의", "와", "과")


def korean_postposition_variants(value: str) -> tuple[str, ...]:
    """Return a query run and at most one safe postposition-stripped variant.

    A two-syllable residual is required to avoid turning ordinary words such
    as ``회의`` into a single-syllable false lexical stem. The original form
    is always retained, so this expansion only widens candidate recall; it
    never rewrites the user query or Knowledge evidence.
    """

    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        return ()
    variants: list[str] = []
    for run in _HANGUL_RUN.findall(normalized):
        if len(run) < 3:
            continue
        variants.append(run)
        for suffix in _POSTPOSITIONS:
            if not run.endswith(suffix):
                continue
            stem = run[: -len(suffix)]
            if len(stem) >= 2:
                variants.append(stem)
            break
    return tuple(dict.fromkeys(variants))


def korean_retrieval_variants(value: str) -> tuple[str, ...]:
    """Return bounded transparent variants for Korean local retrieval.

    The source and user query are never rewritten.  This only supplies extra
    query/index probes, and can remove at most one connective from each
    postposition-normalized Hangul run.  It is explicitly not lemmatization,
    conjugation handling, POS tagging, or an assertion that a particular
    syllable is grammatically a particle.
    """

    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        return ()
    variants: list[str] = []
    for run in _HANGUL_RUN.findall(normalized):
        # Preserve existing safe postposition behavior first.  A two-syllable
        # run cannot safely expose a meaningful connector split.
        bases = korean_postposition_variants(run)
        if not bases:
            continue
        variants.extend(bases)
        for base in bases:
            for connector in _COMPOUND_CONNECTORS:
                position = base.find(connector)
                if position < 2 or position == len(base) - len(connector):
                    continue
                remainder = base[position + len(connector) :]
                if len(remainder) < 2:
                    continue
                variants.append(base[:position] + remainder)
                # One connector is enough: more than one would make this a
                # hidden parser and produce too many accidental candidates.
                break
    return tuple(dict.fromkeys(variants))
