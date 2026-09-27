from backend.interview.timer import InterviewTimer


def test_timer_not_started_is_not_expired():
    timer = InterviewTimer(1)
    assert not timer.is_expired()
    assert timer.remaining_seconds() == 60


def test_timer_expires():
    timer = InterviewTimer(0)
    timer.start()
    assert timer.is_expired()
    assert timer.remaining_seconds() == 0
