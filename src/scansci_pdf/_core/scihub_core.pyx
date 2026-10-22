# cython: language_level=3
"""Sci-Hub domain scoring, rotation, and racing strategy.

This module implements Sci-Hub download optimization:
- Domain health scoring with success rate and latency weighting
- Cooldown mechanics for failing domains
- Best-first-then-race domain selection strategy
"""

import time
from typing import Any


# Tuned constants - these represent empirical optimization
cdef double SCORE_SUCCESS_WEIGHT = 1000.0
cdef double SCORE_LATENCY_WEIGHT = 0.001  # 1/1000
cdef double SCORE_FLARESOLVERR_PENALTY = -5000.0
cdef int COOLDOWN_FAIL_STREAK = 10
cdef double COOLDOWN_SECONDS = 300.0
cdef int MAX_DOMAINS_TO_TRY = 3


def domain_score(dict stats, str domain, bint is_flaresolverr_domain) -> float:
    """Calculate a domain's score based on success rate and latency.

    Higher is better. Score = success_rate * 1000 - avg_latency / 1000
    FlareSolverr-dependent domains get a -5000 penalty.
    """
    cdef dict s = stats.get(domain, {})
    cdef int successes = s.get("success", 0)
    cdef int failures = s.get("fail", 0)
    cdef int total = successes + failures

    if total == 0:
        return 0.5

    cdef double success_rate = successes / total
    cdef double avg_latency = s.get("avg_latency_ms", 5000)
    cdef double score = success_rate * SCORE_SUCCESS_WEIGHT - avg_latency * SCORE_LATENCY_WEIGHT

    if is_flaresolverr_domain:
        score += SCORE_FLARESOLVERR_PENALTY

    return score


def filter_cooldown_domains(list domains, dict stats) -> list:
    """Filter out domains that are on cooldown (10+ consecutive failures within 300s).

    If all domains are on cooldown, reset and return all.
    """
    cdef double now = time.time()
    cdef list active = []
    cdef double last_fail
    cdef int fail_streak

    for d in domains:
        d_stats = stats.get(d, {})
        last_fail = d_stats.get("last_fail_time", 0)
        fail_streak = d_stats.get("fail_streak", 0)
        if fail_streak >= COOLDOWN_FAIL_STREAK and (now - last_fail) < COOLDOWN_SECONDS:
            continue
        active.append(d)

    if not active:
        # All domains on cooldown - reset and try again
        for d in domains:
            stats[d] = {"success": 0, "fail": 0, "last_fail_time": 0, "fail_streak": 0}
        return list(domains)

    return active


def rank_domains(list domains, dict stats, is_flaresolverr_fn) -> list:
    """Rank domains by score (best first), return top N."""
    cdef list scored = []
    for d in domains:
        is_flare = is_flaresolverr_fn(d)
        score = domain_score(stats, d, is_flare)
        scored.append((score, d))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in scored[:MAX_DOMAINS_TO_TRY]]


def record_domain_result(dict stats, str domain, bint success, double latency_ms) -> dict:
    """Record a domain attempt result and return updated stats.

    Manages success/fail counts, streaks, and average latency.
    """
    cdef double old_lat

    if domain not in stats:
        stats[domain] = {"success": 0, "fail": 0, "avg_latency_ms": 0, "fail_streak": 0, "last_fail_time": 0}

    cdef dict s = stats[domain]

    if success:
        s["success"] = s.get("success", 0) + 1
        s["fail_streak"] = 0
        # Exponential moving average for latency
        old_lat = s.get("avg_latency_ms", latency_ms)
        s["avg_latency_ms"] = old_lat * 0.7 + latency_ms * 0.3
    else:
        s["fail"] = s.get("fail", 0) + 1
        s["fail_streak"] = s.get("fail_streak", 0) + 1
        s["last_fail_time"] = time.time()

    return s


def select_domains_for_attempt(list all_domains, dict stats, is_flaresolverr_fn) -> list:
    """Full domain selection pipeline: filter cooldown -> rank -> return top N.

    Returns list of domains to try, best first.
    """
    active = filter_cooldown_domains(all_domains, stats)
    ranked = rank_domains(active, stats, is_flaresolverr_fn)
    return ranked
