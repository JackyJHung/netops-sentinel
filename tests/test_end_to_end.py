from sentinel.evaluation import evaluate
from sentinel.pipeline import PipelineConfig, run_pipeline
from sentinel.simulator import simulate


def test_pipeline_beats_static_baseline():
    sim = simulate(seed=0)
    ours = evaluate(sim, run_pipeline(sim))
    base = evaluate(sim, run_pipeline(sim, config=PipelineConfig.baseline()))
    assert ours.recall >= 0.85
    assert ours.precision >= 0.7
    assert ours.rca_top3 >= 0.8
    assert ours.recall >= base.recall
    assert ours.alert_compression > 1.5


def test_incidents_have_rca_runbook_summary():
    sim = simulate(seed=4, minutes=720)
    res = run_pipeline(sim)
    assert res.incidents
    for inc in res.incidents:
        assert inc.root_causes and inc.runbook and inc.summary
        d = inc.to_dict()
        assert d["root_cause"] == inc.root_causes[0]["node"]
