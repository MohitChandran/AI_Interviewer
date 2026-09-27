import time


class InterviewTimer:
    def __init__(self, duration_minutes: int):
        self.duration_seconds = duration_minutes * 60
        self.start_time: float | None = None

    def start(self) -> None:
        self.start_time = time.time()

    def elapsed_seconds(self) -> float:
        if self.start_time is None:
            return 0.0
        return time.time() - self.start_time

    def is_expired(self) -> bool:
        return self.start_time is not None and self.elapsed_seconds() >= self.duration_seconds

    def remaining_seconds(self) -> int:
        return int(max(0, self.duration_seconds - self.elapsed_seconds()))
