"""分段合成与整批计算的等价性、以及大测时小差值的数值稳定性。"""
import json
import math

import numpy as np

from src.services import business


def _params(team='全部', confidence='0.95'):
    return {'team': team, 'confidence': confidence}


def _segmented(records, params, size):
    team, confidence, clean = business.prepare(records, params)
    partials = [business.with_rows(business.summarize(chunk), chunk) for chunk in business.chunks(clean, size)]
    return business.build_result(partials, team, confidence)


def _same(a, b):
    return json.dumps(a, sort_keys=True, ensure_ascii=False, allow_nan=False) == json.dumps(b, sort_keys=True, ensure_ascii=False, allow_nan=False)


def test_segmented_matches_whole_batch_for_every_segment_size():
    records = business.seed_records()
    for team in ('全部', '一组', '三组'):
        for confidence in ('0.90', '0.95', '0.99'):
            params = _params(team, confidence)
            whole = business.analyze(records, params)
            for size in (1, 2, 3, 5, 7, 8, 13, 40, 80, 500):
                assert _same(_segmented(records, params, size), whole), (team, confidence, size)


def test_segment_merge_reports_same_nac():
    records = business.seed_records()
    team, confidence, clean = business.prepare(records, _params())
    whole = business.analyze(records, _params())
    n, mean, m2 = business.combine([business.with_rows(business.summarize(c), c) for c in business.chunks(clean, 9)])
    assert n == len(clean) == whole['table']['rows'].__len__()
    assert math.isclose(mean, np.mean([r['difference'] for r in clean]), rel_tol=0, abs_tol=1e-12)
    assert math.isclose(m2, np.sum((np.array([r['difference'] for r in clean]) - mean) ** 2), rel_tol=1e-12)


def test_large_timings_small_differences_are_not_distorted():
    """测时 1e9 秒量级、差值毫秒量级时，逐人差值先做减法再累计。"""
    rng = np.random.default_rng(20261117)
    base = 1e9
    records = [{'operator': f'OP-{i:04}', 'team': '一组',
                'before': float(base + rng.normal(0, 1e-3)),
                'after': float(base + rng.normal(0, 1e-3))} for i in range(240)]
    params = _params()
    whole = business.analyze(records, params)
    diffs = np.array([r['before'] - r['after'] for r in records])
    assert math.isclose(whole['metrics'][0]['value'], round(float(diffs.mean()), 5), abs_tol=1e-9)
    assert math.isclose(whole['metrics'][2]['value'], round(float(diffs.mean() / diffs.std(ddof=1)), 5), abs_tol=1e-9)
    for size in (1, 3, 11, 37, 240):
        assert _same(_segmented(records, params, size), whole)


def test_out_of_order_segment_arrival_produces_same_result():
    """段完成顺序不同（并发重试/迟到写回）不影响合成结果。"""
    records = business.seed_records()
    params = _params()
    whole = business.analyze(records, params)
    team, confidence, clean = business.prepare(records, params)
    pieces = business.chunks(clean, 7)
    partials = [business.with_rows(business.summarize(p), p) for p in pieces]
    for order in (partials, list(reversed(partials)), partials[3:] + partials[:3]):
        assert _same(business.build_result(order, team, confidence), whole)


def test_prepare_rejects_invalid_rows_like_before():
    bad = [{'operator': 'A', 'team': '一组', 'before': 10, 'after': 9}] * 2
    try:
        business.prepare(bad, _params())
    except ValueError as exc:
        assert '每人仅能有一条' in str(exc)
    else:
        raise AssertionError('重复作业员应被拒绝')
    try:
        business.prepare([{'operator': f'O{i}', 'team': '一组', 'before': float('nan'), 'after': 1} for i in range(4)], _params())
    except ValueError:
        pass
    else:
        raise AssertionError('非有限测时应被拒绝')
