import subprocess
import sys

from beehive.collector.jobs_lock import JobsLock


def test_one_holder_at_a_time_until_it_releases(tmp_path):
    db_path = str(tmp_path / "jobs.db")
    first, second = JobsLock(db_path), JobsLock(db_path)

    assert first.try_acquire() is True
    assert second.try_acquire() is False
    first.release()
    assert second.try_acquire() is True
    second.release()


def test_the_lock_goes_with_a_process_that_dies_without_releasing(tmp_path):
    db_path = str(tmp_path / "jobs.db")
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; from beehive.collector.jobs_lock import JobsLock; "
         f"assert JobsLock({db_path!r}).try_acquire(); print('held', flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    lock = JobsLock(db_path)
    assert lock.try_acquire() is False

    holder.kill()
    holder.wait(timeout=10)

    assert lock.try_acquire() is True
    lock.release()
