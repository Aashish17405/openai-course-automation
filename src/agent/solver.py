"""Dedicated question solver: course notes + high-effort reasoning + majority vote + answer memory.

The page-analysis model decides WHAT is on screen; this module decides what is CORRECT.
"""
import asyncio
import hashlib
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from rich.console import Console

from src.config import settings
from .analyzer import PageAnalyzer, PRINCIPLES

console = Console()

NOTES_CHARS = 14000
MEMORY_FILE = settings.debug_dir.parent / "answers.json"

SYSTEM = (
    "You are an expert taking a graded professional-training assessment. Your goal is the single most defensible answer.\n"
    "Method: (1) The COURSE MATERIAL (if present) is the source of truth; prefer it over general knowledge. "
    "(2) Otherwise apply the principles below. (3) Judge EVERY option; eliminate those with absolutes ('always', 'never', "
    "'fully autonomous') the material does not support, and those that skip human review, privacy, or source-checking. "
    "(4) 'Best' means the most complete AND responsible option, not merely a true one. (5) If a count is required, "
    "pick exactly that many. Be decisive; report honest confidence.\n\n" + PRINCIPLES
)


class QuestionVote(BaseModel):
    reasoning: str = Field(description="Brief elimination reasoning, option by option")
    selected: List[int] = Field(description="1-based numbers of the correct option(s)")
    confidence: float = Field(description="0.0 to 1.0")


class MatchVote(BaseModel):
    reasoning: str = Field(description="Brief reasoning for each pairing")
    right_for_left: List[int] = Field(description="For each LEFT item in order, the 1-based number of its RIGHT partner; each right item used exactly once")
    confidence: float = Field(description="0.0 to 1.0")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


class Solver:
    def __init__(self, analyzer: PageAnalyzer, memory_path: Path = MEMORY_FILE, votes: Optional[int] = None):
        self.an = analyzer
        self.memory_path = memory_path
        self.votes = settings.answer_votes if votes is None else votes
        self.notes: List[str] = []
        self._seen: set = set()
        self.retake = False
        try:
            self.memory: Dict[str, Any] = json.loads(self.memory_path.read_text(encoding="utf-8"))
        except Exception:
            self.memory = {}

    # ---------------------------------------------------------------- course notes
    def add_notes(self, text: str) -> None:
        """Remember lesson text the agent has read; it becomes reference material for later questions."""
        t = re.sub(r"\s+", " ", text or "").strip()
        h = hashlib.sha1(t[:300].encode()).hexdigest()
        if len(t) < 80 or h in self._seen:
            return
        self._seen.add(h)
        self.notes.append(t)
        while sum(map(len, self.notes)) > NOTES_CHARS and len(self.notes) > 1:
            self.notes.pop(0)

    def start_retake(self) -> None:
        self.retake = True

    # ---------------------------------------------------------------- memory
    def _save(self) -> None:
        try:
            self.memory_path.parent.mkdir(parents=True, exist_ok=True)
            self.memory_path.write_text(json.dumps(self.memory, indent=1), encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def key(question: str, options: List[str]) -> str:
        return hashlib.sha1((_norm(question) + "|" + "|".join(sorted(_norm(o) for o in options))).encode()).hexdigest()[:16]

    def set_verdict(self, key: str, verdict: str) -> None:
        attempts = self.memory.get(key, {}).get("attempts")
        if attempts:
            attempts[-1]["verdict"] = verdict
            self._save()

    # ---------------------------------------------------------------- one LLM vote
    async def _ask(self, user: str, schema: type) -> Optional[Any]:
        try:
            msgs = [{"role": self.an.system_role(), "content": SYSTEM}, {"role": "user", "content": user}]
            return await self.an.parse(msgs, schema, effort=settings.solve_effort, temperature=0.4)
        except Exception as e:
            console.print(f"[dim]Solver vote failed: {e}[/dim]")
            return None

    def _material(self) -> str:
        return "COURSE MATERIAL (text the learner has read so far):\n" + ("\n---\n".join(self.notes) or "(none yet)")

    # ---------------------------------------------------------------- quiz question
    def _length_prior_ok(self) -> bool:
        """Has picking the longest option actually worked on practice questions? Needs >= 4 verdicts before it can say no."""
        won = lost = 0
        for rec in self.memory.values():
            for a in rec.get("attempts", []):
                if a.get("practice") and a.get("longest") and a.get("verdict") in ("correct", "wrong"):
                    won, lost = won + (a["verdict"] == "correct"), lost + (a["verdict"] == "wrong")
        return won + lost < 4 or won / (won + lost) >= 0.5

    async def answer(self, question: str, options: List[str], required: int = 0, practice: bool = False) -> Optional[dict]:
        """Best option set for a question. practice=True (mid-course, ungraded) enables the longest-option tie-breaker.
        -> {'indices': [0-based], 'confidence', 'source', 'key'} or None."""
        n = len(options)
        if n < 2:
            return None
        key = self.key(question, options)
        rec = self.memory.setdefault(key, {"q": question[:200], "options": options, "attempts": []})
        names = lambda idx: frozenset(_norm(options[i]) for i in idx)

        for att in rec["attempts"]:      # feedback told us this exact answer was right: just reuse it
            if att.get("verdict") == "correct":
                idx = [i for i, o in enumerate(options) if _norm(o) in {_norm(c) for c in att["chosen"]}]
                if idx:
                    return {"indices": idx, "confidence": 1.0, "source": "memory (marked correct before)", "key": key}

        banned = [frozenset(_norm(c) for c in a["chosen"]) for a in rec["attempts"] if a.get("verdict") == "wrong"]
        if self.retake and rec["attempts"] and rec["attempts"][-1].get("verdict") is None and rec["attempts"][-1].get("confidence", 1) < 0.75:
            banned.append(frozenset(_norm(c) for c in rec["attempts"][-1]["chosen"]))   # shaky last time and the quiz was failed

        use_len = practice and required == 1 and settings.length_prior and self._length_prior_ok()
        tip = ("\n\nObservation from this course's ungraded practice questions: the correct option is usually the longest, most complete one. "
               "Use that ONLY as a tie-breaker when the content leaves you undecided; content always comes first." if use_len
               else "" if practice else "\n\nThis is a GRADED test: judge strictly on content. Option length is NOT a signal.")
        user = (f"{self._material()}\n\nQUESTION:\n{question}\n\nOPTIONS:\n" + "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
                + (f"\n\nSelect exactly {required} option(s)." if required else "\n\nSelect every option that is correct (one if it is single-choice).")
                + tip
                + ("\n\nThese option sets were already marked WRONG or are doubtful. Do NOT choose them:\n"
                   + "\n".join("- " + " | ".join(sorted(b)) for b in banned) if banned else ""))
        votes = [v for v in await asyncio.gather(*[self._ask(user, QuestionVote) for _ in range(max(1, self.votes))]) if v]
        valid = []
        for v in votes:
            sel = sorted(set(v.selected))
            if sel and all(1 <= i <= n for i in sel) and (not required or len(sel) == required):
                valid.append((tuple(i - 1 for i in sel), max(0.0, min(1.0, v.confidence))))
        if not valid:
            return None

        tally: Dict[tuple, List[float]] = {}
        for sel, conf in valid:
            tally.setdefault(sel, []).append(conf)
        ranked = sorted(tally.items(), key=lambda kv: (len(kv[1]), sum(kv[1]), sum(len(options[i]) for i in kv[0]) if use_len else 0), reverse=True)
        pick = next((sel for sel, _ in ranked if names(sel) not in banned), ranked[0][0])
        confs = tally[pick]
        confidence = round(sum(confs) / len(confs) * len(confs) / len(valid), 2)    # mean confidence x agreement
        source = f"{len(confs)}/{len(valid)} votes"
        longest = max(range(n), key=lambda i: len(options[i]))
        if use_len and pick != (longest,) and confidence < 0.6 and names((longest,)) not in banned:
            pick, source = (longest,), source + " unsure -> longest-option tie-break"
        rec["attempts"].append({"chosen": [options[i] for i in pick], "confidence": confidence, "verdict": None, "ts": int(time.time()),
                                "practice": practice, "longest": pick == (longest,)})
        self._save()
        return {"indices": list(pick), "confidence": confidence, "source": source, "key": key}

    # ---------------------------------------------------------------- matching exercise
    async def match(self, left: List[str], right: List[str]) -> Optional[List[int]]:
        """perm[i] = index of the right item that pairs with left item i (majority over independent solves)."""
        n = len(left)
        if n < 2 or n != len(right):
            return None
        user = (f"{self._material()}\n\nMatch every LEFT item to its single best RIGHT partner. Each right item is used exactly once.\n\n"
                "LEFT:\n" + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(left))
                + "\n\nRIGHT:\n" + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(right)))
        votes = [v for v in await asyncio.gather(*[self._ask(user, MatchVote) for _ in range(max(1, self.votes))]) if v]
        perms = Counter(tuple(i - 1 for i in v.right_for_left) for v in votes if sorted(v.right_for_left) == list(range(1, n + 1)))
        if not perms:
            return None
        perm, count = perms.most_common(1)[0]
        console.print(f"[dim]Matching solved by {count}/{len(votes)} votes.[/dim]")
        return list(perm)
