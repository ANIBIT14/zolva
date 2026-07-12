from zolva.bus import Bus, Step, Verdict


def step() -> Step:
    return Step(type="response", session_id="s1", agent="a", data={"text": "hi"})


async def test_no_hooks_allows() -> None:
    assert (await Bus().emit(step())).allow is True


async def test_blocking_hook_short_circuits() -> None:
    bus = Bus()
    seen: list[str] = []

    async def blocker(s: Step) -> Verdict:
        seen.append("blocker")
        return Verdict(allow=False, reason="policy")

    async def never_runs(s: Step) -> None:
        seen.append("never")
        return None

    bus.on(blocker)
    bus.on(never_runs)
    v = await bus.emit(step())
    assert v.allow is False and v.reason == "policy"
    assert seen == ["blocker"]


async def test_observing_hook_sees_all_steps() -> None:
    bus = Bus()
    log: list[Step] = []

    async def observer(s: Step) -> None:
        log.append(s)
        return None

    bus.on(observer)
    await bus.emit(step())
    await bus.emit(step())
    assert len(log) == 2
