import embroidery_progress as emb_progress


def test_planned_quantity_made_is_not_treated_as_finished():
    assert emb_progress.completed_qty_for_scheduler(0, 48, 48, "NEEDS WORK") == 0
    assert emb_progress.completed_qty_for_scheduler(0, 48, 48, "NOT STARTED") == 0


def test_partial_quantity_made_counts_as_progress():
    assert emb_progress.completed_qty_for_scheduler(0, 12, 48, "NEEDS WORK") == 12


def test_floor_progress_wins_over_planned_list_qty():
    assert emb_progress.completed_qty_for_scheduler(18, 48, 48, "NEEDS WORK") == 18


def test_complete_status_keeps_list_qty():
    assert emb_progress.completed_qty_for_scheduler(0, 48, 48, "COMPLETE") == 48
