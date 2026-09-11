"""Tests for the paper trade journal (tracking/journal.py) and the weekly
self-review generator (tracking/review.py)."""

import os

import pytest

from tracking import journal, review


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "test_journal.duckdb")


def _card(symbol="AAPL", **over):
    card = {
        "symbol": symbol,
        "side": "BUY",
        "entry_limit": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "quantity": 100,
        "conviction": 72,
        "entry_price_source": "premarket",
    }
    card.update(over)
    return card


def test_log_card_assigns_unique_ids(db):
    cid1 = journal.log_card(_card("AAPL"), "morning", "2026-09-11", db)
    cid2 = journal.log_card(_card("AAPL"), "morning", "2026-09-11", db)
    assert cid1 == "2026-09-11_morning_AAPL"
    assert cid2 == "2026-09-11_morning_AAPL-2"
    assert cid1 != cid2


def test_log_outcome_win_r_math(db):
    # entry 100, stop 95 -> risk 5. exit 110 -> +2R
    cid = journal.log_card(_card(), "morning", "2026-09-11", db)
    res = journal.log_outcome(cid, 110.0, "2026-09-15", db_path=db)
    assert res["outcome"] == "WIN"
    assert res["r_multiple"] == pytest.approx(2.0)


def test_log_outcome_loss_r_math(db):
    # exit 92.5 -> -1.5R
    cid = journal.log_card(_card(), "morning", "2026-09-11", db)
    res = journal.log_outcome(cid, 92.5, "2026-09-12", db_path=db)
    assert res["outcome"] == "LOSS"
    assert res["r_multiple"] == pytest.approx(-1.5)


def test_log_outcome_scratch_band(db):
    # exit 100.2 -> +0.04R -> SCRATCH
    cid = journal.log_card(_card(), "morning", "2026-09-11", db)
    res = journal.log_outcome(cid, 100.2, "2026-09-12", db_path=db)
    assert res["outcome"] == "SCRATCH"


def test_log_outcome_explicit_and_mfe_mae(db):
    cid = journal.log_card(_card(), "morning", "2026-09-11", db)
    res = journal.log_outcome(
        cid, 97.0, "2026-09-13", outcome="EXPIRED", notes="time stop",
        mfe_price=108.0, mae_price=94.0, db_path=db)
    assert res["outcome"] == "EXPIRED"
    got = journal.get_card(cid, db)
    assert got["mfe_r"] == pytest.approx(1.6)
    assert got["mae_r"] == pytest.approx(-1.2)
    assert "time stop" in got["notes"]


def test_log_outcome_unknown_card_raises(db):
    with pytest.raises(KeyError):
        journal.log_outcome("nope", 100.0, "2026-09-12", db_path=db)


def test_open_cards_only_unresolved(db):
    c1 = journal.log_card(_card("AAPL"), "morning", "2026-09-11", db)
    journal.log_card(_card("MSFT"), "midday", "2026-09-11", db)
    journal.log_outcome(c1, 110.0, "2026-09-15", db_path=db)
    opened = journal.open_cards(db)
    assert [c["symbol"] for c in opened] == ["MSFT"]


def _seed_review_db(db):
    # 6 resolved trades with known stats:
    # wins: +2R (80), +1R (75) ; losses: -1R (65), -1R (62) ; scratch counted separately
    specs = [
        ("AAPL", 80, 110.0),   # +2R win
        ("MSFT", 75, 105.0),   # +1R win
        ("TSLA", 65, 95.0),    # -1R loss
        ("NVDA", 62, 95.0),    # -1R loss
        ("AAPL", 72, 101.0),   # +0.2R win
        ("MSFT", 68, 99.0),    # -0.2R loss
    ]
    sessions = ["morning", "morning", "midday", "midday", "late", "late"]
    for (sym, conv, ex), sess in zip(specs, sessions):
        cid = journal.log_card(_card(sym, conviction=conv), sess,
                               "2026-09-07", db)
        journal.log_outcome(cid, ex, "2026-09-10", db_path=db)


def test_review_stats_and_proposals(db, tmp_path):
    _seed_review_db(db)
    reports = str(tmp_path / "reports")
    path = review.generate_review("2026-W37", db_path=db, reports_dir=reports)
    assert os.path.exists(path)
    text = open(path).read()
    # wins: +2R, +1R, +0.2R ; losses: -1R, -1R, -0.2R -> n=6, avg +0.17R
    assert "Resolved trades: **6**" in text
    assert "Win rate: **50.0%**" in text          # 3/6
    assert "Avg R-multiple: **+0.17R**" in text   # 1.0/6
    assert "Expectancy: **+0.17R per trade**" in text
    # conviction buckets present
    assert "80-89" in text and "60-69" in text
    # exactly 3 proposals, all pending approval
    assert text.count("### P") == 3
    assert text.count("pending approval") == 3
    assert "nothing auto-tunes" in text or "Proposals need human approval" in text


def test_review_empty_db_still_writes_three_proposals(db, tmp_path):
    reports = str(tmp_path / "reports")
    path = review.generate_review("2026-W37", db_path=db, reports_dir=reports)
    text = open(path).read()
    assert "Resolved trades: **0**" in text
    assert text.count("### P") == 3


def test_journal_default_db_is_not_the_sweep_db():
    # The whole-market sweep owns data/market.duckdb. The journal must never
    # write there — it gets its own file.
    path = journal._default_db_path()
    assert os.path.basename(path) == "paper_journal.duckdb"
    assert "market.duckdb" not in path
