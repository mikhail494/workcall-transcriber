from workcall_transcriber.autostart import AutostartManager


class FakeRegistry:
    def __init__(self) -> None:
        self.values = {}

    def set_value(self, name: str, value: str) -> None:
        self.values[name] = value

    def remove_value(self, name: str) -> None:
        self.values.pop(name, None)

    def get_value(self, name: str) -> str | None:
        return self.values.get(name)


def test_autostart_uses_a_per_user_run_entry() -> None:
    registry = FakeRegistry()
    manager = AutostartManager('"C:\\Program Files\\WorkCall Transcriber.exe" --minimized', registry)

    manager.set_enabled(True)

    assert manager.is_enabled() is True
    assert "--minimized" in registry.values["WorkCallTranscriber"]
    manager.set_enabled(False)
    assert manager.is_enabled() is False
