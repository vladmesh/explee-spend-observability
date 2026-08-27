import time

from explee_test.observability.cache import ProjectionCache


def _counting_cache(**kwargs) -> tuple[ProjectionCache, list[int]]:
    builds: list[int] = []

    def build(value: int) -> str:
        builds.append(value)
        return f"built {value} #{len(builds)}"

    return ProjectionCache(build, **kwargs), builds


def test_a_projection_is_built_once_and_handed_to_everyone() -> None:
    cache, builds = _counting_cache()
    assert cache.get((1,)) == "built 1 #1"
    assert cache.get((1,)) == "built 1 #1"
    assert cache.get((2,)) == "built 2 #2"
    assert builds == [1, 2]


def test_an_expensive_projection_is_refreshed_less_often() -> None:
    cache, _ = _counting_cache(minimum_age=0.0, refresh_factor=1000.0)
    cache.get((1,))
    # The build itself took some measurable time, and a thousand times that has not
    # passed, so nothing is due yet.
    assert cache.refresh_due() == []


def test_what_nobody_looks_at_stops_being_refreshed() -> None:
    cache, _ = _counting_cache(minimum_age=0.0, refresh_factor=0.0, demand_seconds=0.0)
    cache.get((1,))
    time.sleep(0.01)
    assert cache.refresh_due() == []
    # ... and a window named as warm keeps being refreshed regardless.
    cache.keep_warm(((2,),))
    assert cache.refresh_due() == [(2,)]


def test_a_stale_answer_is_not_served() -> None:
    cache, builds = _counting_cache(stale_after=0.0)
    cache.get((1,))
    assert cache.get((1,)) == "built 1 #2"
    assert builds == [1, 1]
