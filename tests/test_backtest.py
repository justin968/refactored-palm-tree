from diy_growth.backtest import summarize_historical_opportunities
from diy_growth.models import ActionClass, Channel, Opportunity


def _op(id_, contribution):
    return Opportunity(id=id_, entity='x', channel=Channel.GOOGLE, opportunity_type='TEST', observed_problem='x', evidence={}, inferred_cause='x', recommended_action='x', expected_incremental_revenue=100, expected_incremental_contribution=contribution, confidence=0.8, action_class=ActionClass.APPROVAL_REQUIRED)


def test_backtest_flags_false_positive():
    summary = summarize_historical_opportunities([_op('a', 100), _op('b', 50)], {'a': 80, 'b': -10})
    assert summary.evaluated == 2
    assert summary.proposed == 2
    assert summary.false_positive_ids == ['b']
