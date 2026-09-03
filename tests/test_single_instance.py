import uuid

from workcall_transcriber.single_instance import SingleInstance


def test_second_instance_is_rejected_and_first_can_clean_up(qtbot) -> None:
    name = f"workcall-test-{uuid.uuid4()}"
    activated = []
    first = SingleInstance(name)
    second = SingleInstance(name)

    assert first.acquire(lambda: activated.append(True)) is True
    assert second.acquire(lambda: None) is False

    second.close()
    first.close()
