"""Pick one speaker when several hear the same command.

With more than one Voice PE in the flat, a command spoken in the hallway wakes
two or three of them. Each used to run the whole command: every one of them
transcribed its own copy, heard it a little differently and acted on that
("zgaś światło przed pokojem" turned off the hallway, "...w szafie" from the
quietest speaker turned off the wardrobe), and every one of them answered.

Every speaker that wakes within GROUP_WINDOW of the first one joins a group.
While the user is still talking, each member is scored by how loud the speech
is on its microphone over the first SCORE_SPEECH_MS of speech. The loudest is
the speaker closest to the user, whose recording is the cleanest. It carries
on as if it were alone; the others are told to stop and go back to idle
without a sound. The decision is taken mid-utterance, so the winner never
waits for it.

Why loudness of the speech and not speech-over-noise: the command starts right
after the wake word, so there is no stretch of room noise to measure the floor
on, and the floor estimate is mostly its starting guess. On the three
recordings of 2026-11-02 12:59:27 speech-over-noise picked the wrong speaker,
while speech level put them in the right order (70.7 / 67.9 / 44.2 dB, the
loudest being the one that heard the command correctly).
"""
import asyncio
import math
import os
import time


def _option(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return default if not value or value.lower() == "null" else value


ENABLED = _option("SPEAKER_ARBITRATION", "true").lower() in ("1", "true", "yes", "on")
# Wake word detection and the reconnect for a new command differ between
# speakers by a few hundred ms; two people giving commands in different rooms
# within a second of each other is rare enough to accept.
GROUP_WINDOW_S = int(_option("ARBITER_GROUP_WINDOW_MS", "1000")) / 1000
SCORE_SPEECH_MS = int(_option("ARBITER_SCORE_SPEECH_MS", "300"))
# After the first member has a full score, how long the others get to catch up.
# Waiting costs nothing: the decision is forced anyway as soon as any member
# finishes its utterance, before any session can act.
DECIDE_WAIT_S = int(_option("ARBITER_DECIDE_WAIT_MS", "1000")) / 1000
# A member with less speech than this when the decision is due is not a contender.
MIN_SPEECH_MS = 96


class Participant:
    def __init__(self, name: str, group: "Group"):
        self.name = name
        self.group = group
        self.joined_at = time.monotonic()
        # (speech_ms after the chunk, chunk energy) for voiced chunks while scoring.
        self.voiced: list[tuple[int, float]] = []
        self.speech_ms = 0
        self.lost = asyncio.Event()
        self.finished = False

    def score_db(self, speech_ms: int) -> float:
        """RMS level of the speech over its first `speech_ms` milliseconds."""
        energies = [e for ms, e in self.voiced if ms <= speech_ms] or [e for _, e in self.voiced[:1]]
        if not energies:
            return float("-inf")
        return 10 * math.log10(max(sum(energies) / len(energies), 1.0))


class Group:
    def __init__(self):
        self.created_at = time.monotonic()
        self.members: list[Participant] = []
        self.decided = False
        self.winner: Participant | None = None
        self.timer: asyncio.TimerHandle | None = None


class Arbiter:
    def __init__(self):
        self._groups: list[Group] = []

    def join(self, name: str) -> Participant:
        now = time.monotonic()
        self._groups = [g for g in self._groups if now - g.created_at < 30]
        group = next((g for g in reversed(self._groups) if now - g.created_at <= GROUP_WINDOW_S), None)
        if group is None:
            group = Group()
            self._groups.append(group)
        p = Participant(name, group)
        group.members.append(p)
        if group.decided:
            # Woke after the decision: the user is already being served elsewhere.
            p.lost.set()
            print(f"  [arbiter] {name} woke {(now - group.created_at) * 1000:.0f}ms late, "
                  f"{group.winner.name if group.winner else '?'} already serving — standing down", flush=True)
        elif len(group.members) > 1:
            print(f"  [arbiter] {name} joined a group of {len(group.members)} "
                  f"(+{(now - group.created_at) * 1000:.0f}ms)", flush=True)
        return p

    def update(self, p: Participant, rms: float, is_voice: bool, speech_ms: int):
        p.speech_ms = speech_ms
        group = p.group
        if group.decided:
            return
        if is_voice and (not p.voiced or p.voiced[-1][0] < SCORE_SPEECH_MS):
            p.voiced.append((speech_ms, rms * rms))
        if speech_ms < SCORE_SPEECH_MS:
            return
        # A lone speaker decides at once; one that wakes later stands down on join.
        if all(m.speech_ms >= SCORE_SPEECH_MS or m.finished for m in group.members):
            self._decide(group, "all scored")
        elif group.timer is None:
            group.timer = asyncio.get_running_loop().call_later(
                DECIDE_WAIT_S, self._decide, group, "deadline")

    def finish(self, p: Participant):
        """The member's utterance ended. Decide now if it spoke: its session is about to act."""
        p.finished = True
        if not p.group.decided and p.speech_ms >= MIN_SPEECH_MS:
            self._decide(p.group, f"{p.name} finished")

    def _decide(self, group: Group, reason: str):
        if group.decided:
            return
        if group.timer is not None:
            group.timer.cancel()
        contenders = [m for m in group.members if m.speech_ms >= MIN_SPEECH_MS]
        if not contenders:
            # Called by the deadline before anyone really spoke; try again later.
            if all(m.finished for m in group.members):
                return
            group.timer = asyncio.get_running_loop().call_later(DECIDE_WAIT_S, self._decide, group, reason)
            return
        group.decided = True
        # Compare everyone over the same stretch of speech: the onset of a word is
        # quieter than its middle, so a member that is behind must not be judged
        # on less speech than the leader.
        window = min(SCORE_SPEECH_MS, *(m.speech_ms for m in contenders))
        scores = {id(m): m.score_db(window) for m in contenders}
        group.winner = max(contenders, key=lambda m: (scores[id(m)], -m.joined_at))
        for m in group.members:
            if m is not group.winner:
                m.lost.set()
        if len(group.members) > 1:
            ranking = " ".join(
                f"{m.name}({scores[id(m)]:.1f}dB)" if id(m) in scores else f"{m.name}(no speech)"
                for m in sorted(group.members, key=lambda m: scores.get(id(m), float("-inf")), reverse=True))
            print(f"  [arbiter] winner={group.winner.name} of {len(group.members)} "
                  f"over {window}ms of speech ({reason}): {ranking}", flush=True)


arbiter = Arbiter()


# Second line of defence: the same state-changing call from another speaker
# within a few seconds is a copy of the same command, e.g. when Wi-Fi delayed a
# speaker past the group window.
DEDUP_WINDOW_S = float(_option("ARBITER_DEDUP_SECONDS", "3"))
_READ_ONLY = ("get_", "list_", "search_")
_recent_calls: dict[str, tuple[float, str, asyncio.Future]] = {}


async def run_once(name: str, args: dict, speaker: str, call):
    """Run `call()` unless another speaker made the identical call moments ago.

    The call is registered before it runs, so two copies arriving together
    still run once; the copy gets the first one's result.
    """
    if not ENABLED or name.startswith(_READ_ONLY):
        return await call()
    key = name + repr(sorted(args.items(), key=lambda kv: kv[0]))
    now = time.monotonic()
    for k in [k for k, v in _recent_calls.items() if now - v[0] > DEDUP_WINDOW_S]:
        del _recent_calls[k]
    prev = _recent_calls.get(key)
    if prev and prev[1] != speaker:
        print(f"  [arbiter] {speaker}: {name} already done by {prev[1]} "
              f"{(now - prev[0]) * 1000:.0f}ms ago — not repeating", flush=True)
        return await asyncio.shield(prev[2])
    future = asyncio.get_running_loop().create_future()
    _recent_calls[key] = (now, speaker, future)
    try:
        result = await call()
    except BaseException as exc:
        future.set_result({"status": "error", "message": str(exc) or type(exc).__name__})
        raise
    future.set_result(result)
    return result


class ArbitrationLost(Exception):
    """This speaker's session was dropped because another one took the command."""
