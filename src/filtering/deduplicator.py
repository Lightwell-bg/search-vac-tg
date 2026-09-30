"""Duplicate detection: (channel, message_id) -> sha256 of dedup key -> RapidFuzz similarity."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from rapidfuzz import fuzz

# very short texts give meaningless fuzzy scores
MIN_FUZZY_LEN = 60


@dataclass
class DedupMatch:
    job_id: int
    kind: str                  # hash | fuzzy
    similarity: float | None = None


def best_fuzzy_match(key: str, candidates: list[tuple[int, str]], threshold: int) -> DedupMatch | None:
    """Most similar earlier job whose dedup key scores >= threshold.

    ``token_set_ratio`` tolerates reordered/added lines (repost with a footer), and the
    length ratio guard stops a short post from matching a long one it is a subset of.
    """
    if len(key) < MIN_FUZZY_LEN:
        return None
    best: DedupMatch | None = None
    for job_id, other in candidates:
        if not other or len(other) < MIN_FUZZY_LEN:
            continue
        ratio = min(len(key), len(other)) / max(len(key), len(other))
        if ratio < 0.6:
            continue
        score = fuzz.token_set_ratio(key, other, score_cutoff=threshold)
        if score and (best is None or score > (best.similarity or 0)):
            best = DedupMatch(job_id, "fuzzy", round(float(score), 1))
    return best


class Deduplicator:
    def __init__(self, repo, fuzzy_threshold: int = 90, window_days: int = 14, max_candidates: int = 1000):
        self.repo = repo
        self.threshold = fuzzy_threshold
        self.window = timedelta(days=window_days)
        self.max_candidates = max_candidates

    async def find(self, text_hash: str, key: str) -> DedupMatch | None:
        job_id = await self.repo.find_job_by_hash(text_hash)
        if job_id is not None:
            return DedupMatch(job_id, "hash", 100.0)
        since = datetime.now(timezone.utc) - self.window
        candidates = await self.repo.recent_job_keys(since, self.max_candidates)
        return best_fuzzy_match(key, candidates, self.threshold)
