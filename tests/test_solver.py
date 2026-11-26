"""Solver logic (voting, required counts, memory, notes). No browser, no API. Run: uv run python tests/test_solver.py"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.solver import Solver, QuestionVote, MatchVote  # noqa: E402


class FakeAnalyzer:
    """parse() hands out scripted votes in call order."""
    def __init__(self, votes):
        self.votes = list(votes)
        self.calls = 0

    def system_role(self):
        return "system"

    async def parse(self, messages, schema, effort="low", temperature=0.1):
        self.calls += 1
        return self.votes.pop(0)


def qv(sel, conf=0.9):
    return QuestionVote(reasoning="r", selected=sel, confidence=conf)


def mk(votes):
    fake = FakeAnalyzer(votes)
    return Solver(fake, memory_path=Path(tempfile.mkdtemp()) / "m.json", votes=3), fake


OPTS = ["Alpha", "Beta", "Gamma", "Delta"]


async def main():
    # 1. majority wins, confidence reflects agreement
    s, _ = mk([qv([1]), qv([1]), qv([2])])
    r = await s.answer("Q one?", OPTS, required=1)
    assert r["indices"] == [0], r
    assert 0.5 < r["confidence"] < 0.7, r

    # 2. invalid votes (wrong count / out of range) are dropped
    s, _ = mk([qv([1]), qv([1, 2]), qv([1, 9])])
    r = await s.answer("Q two?", OPTS, required=2)
    assert r["indices"] == [0, 1], r

    # 3. an option set already marked WRONG is never re-picked, even if it wins the vote
    s, _ = mk([qv([1]), qv([1]), qv([2])])
    s.memory[s.key("Q three?", OPTS)] = {"q": "Q three?", "options": OPTS, "attempts": [{"chosen": ["Alpha"], "confidence": 0.9, "verdict": "wrong"}]}
    r = await s.answer("Q three?", OPTS, required=1)
    assert r["indices"] == [1], r

    # 4. an answer already marked CORRECT is reused without any model call; option order may be shuffled
    s, fake = mk([])
    s.memory[s.key("Q four?", OPTS)] = {"q": "Q four?", "options": OPTS, "attempts": [{"chosen": ["Gamma"], "confidence": 0.5, "verdict": "correct"}]}
    r = await s.answer("Q four?", list(reversed(OPTS)), required=1)
    assert r["indices"] == [1] and fake.calls == 0, (r, fake.calls)       # Gamma sits at index 1 of the reversed list

    # 5. retake: a shaky, unverified previous answer is avoided
    s, _ = mk([qv([1]), qv([1]), qv([2])])
    s.memory[s.key("Q five?", OPTS)] = {"q": "Q five?", "options": OPTS, "attempts": [{"chosen": ["Alpha"], "confidence": 0.4, "verdict": None}]}
    s.start_retake()
    r = await s.answer("Q five?", OPTS, required=1)
    assert r["indices"] == [1], r

    # 6. answers persist and verdicts attach to the last attempt
    s, _ = mk([qv([3]), qv([3]), qv([3])])
    r = await s.answer("Q six?", OPTS, required=1)
    s.set_verdict(r["key"], "correct")
    assert Solver(FakeAnalyzer([]), memory_path=s.memory_path, votes=3).memory[r["key"]]["attempts"][-1]["verdict"] == "correct"

    # 7. matching: majority permutation, invalid permutations ignored
    s, _ = mk([MatchVote(reasoning="r", right_for_left=[2, 1, 3], confidence=.9), MatchVote(reasoning="r", right_for_left=[2, 1, 3], confidence=.9),
               MatchVote(reasoning="r", right_for_left=[1, 1, 3], confidence=.9)])
    assert await s.match(["a", "b", "c"], ["x", "y", "z"]) == [1, 0, 2]

    # 8. notes: de-duplicated, capped
    s, _ = mk([])
    s.add_notes("lesson text " * 20); s.add_notes("lesson text " * 20)
    assert len(s.notes) == 1
    for i in range(40):
        s.add_notes(f"unique lesson {i} " + "x" * 900)
    assert sum(map(len, s.notes)) <= 14000 + 1000
    # 9-13. longest-option tie-breaker: practice only, only when unsure, never on graded tests / multi-select, self-disabling
    LONG = ["Short", "A medium length option", "The longest option by far with many more words in it", "Tiny"]
    split = lambda: [qv([1], .5), qv([2], .5), qv([4], .5)]          # three-way disagreement, low confidence

    s, _ = mk(split())
    r = await s.answer("Practice Q?", LONG, required=1, practice=True)
    assert r["indices"] == [2] and "longest" in r["source"], r        # unsure + practice -> longest

    s, _ = mk([qv([1], .95), qv([1], .95), qv([1], .95)])
    r = await s.answer("Practice Q2?", LONG, required=1, practice=True)
    assert r["indices"] == [0], r                                     # confident answer is never overridden

    s, _ = mk(split())
    r = await s.answer("Graded Q?", LONG, required=1, practice=False)
    assert r["indices"] != [2] and "longest" not in r["source"], r    # graded test: length is ignored

    s, _ = mk([qv([1, 2], .5), qv([1, 4], .5), qv([2, 4], .5)])
    r = await s.answer("Practice multi?", LONG, required=2, practice=True)
    assert 2 not in r["indices"], r                                   # multi-select: no length prior

    s, _ = mk(split())
    s.memory["old"] = {"attempts": [{"practice": True, "longest": True, "verdict": "wrong"}] * 5}
    r = await s.answer("Practice Q3?", LONG, required=1, practice=True)
    assert r["indices"] != [2], r                                     # feedback showed 'longest' fails -> prior switched off

    print("[solver] PASS")


asyncio.run(main())
