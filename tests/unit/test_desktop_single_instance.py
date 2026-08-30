from __future__ import annotations

from course_insight.desktop.single_instance import SingleInstance


class FakeMutexApi:
    def __init__(self, *, already_exists: bool = False) -> None:
        self.already_exists = already_exists
        self.created_names: list[str] = []
        self.closed_handles: list[int] = []

    def create_mutex(self, name: str) -> tuple[int, bool]:
        self.created_names.append(name)
        return 42, self.already_exists

    def close_handle(self, handle: int) -> None:
        self.closed_handles.append(handle)


def test_single_instance_owns_and_releases_the_windows_mutex() -> None:
    api = FakeMutexApi()
    instance = SingleInstance(api=api)

    assert instance.acquire() is True
    assert instance.acquire() is True
    instance.release()
    instance.release()

    assert api.created_names == ["Local\\KeYeZhiXi.Desktop"]
    assert api.closed_handles == [42]


def test_duplicate_instance_does_not_keep_the_foreign_mutex_handle() -> None:
    api = FakeMutexApi(already_exists=True)
    instance = SingleInstance(api=api)

    assert instance.acquire() is False
    instance.release()

    assert api.closed_handles == [42]

