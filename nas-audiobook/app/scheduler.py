"""Who may use a machine right now, so several books can be converted at once without two of them sharing a machine.

Every machine has a Gate. A book's lane for that machine takes a Ticket and keeps the machine for as long as the book
has chapters to hand it, so a book that was queued first keeps its machines busy. When the book has nothing left for
that machine (its last chapters are on slower machines) the ticket is given back and the next book's lane takes over.
Between two chapters a lane also steps aside if an older book is waiting for the machine, so a chapter that comes back
to an older book (another machine failed) is not stuck behind a long newer one."""
from __future__ import annotations

import itertools
import threading
from typing import Callable

Priority = tuple  # (created, job id): smaller is older, and older books are served first

WAITING, GRANTED, GONE = "waiting", "granted", "gone"


class Ticket:
    """A place in line for one machine, then the machine itself. Giving it back is always safe, even twice."""

    def __init__(self, gate: "Gate", priority: Priority, seq: int):
        self._gate, self.priority, self._seq = gate, priority, seq
        self._state = WAITING

    @property
    def order(self) -> tuple:
        return (self.priority, self._seq)

    def wait(self, should_stop: Callable[[], bool] = lambda: False, poll: float = 0.2) -> bool:
        """Block until this ticket holds the machine. False if it was given up or `should_stop()` turned true."""
        gate = self._gate
        with gate._cv:
            while True:
                if self._state == GRANTED:
                    return True
                if self._state == GONE:
                    return False
                if gate._holder is None and min(gate._line, key=Ticket._order_of) is self:
                    gate._line.remove(self)
                    gate._holder, self._state = self, GRANTED
                    return True
                if should_stop():
                    self._leave_locked()
                    return False
                gate._cv.wait(poll)

    def release(self) -> None:
        with self._gate._cv:
            self._leave_locked()

    def _leave_locked(self) -> None:
        gate = self._gate
        if self._state == GRANTED:
            gate._holder = None
        elif self._state == WAITING:
            gate._line.remove(self)
        self._state = GONE
        gate._cv.notify_all()

    @staticmethod
    def _order_of(ticket: "Ticket") -> tuple:
        return ticket.order


class Gate:
    def __init__(self):
        self._cv = threading.Condition()
        self._holder: Ticket | None = None
        self._line: list[Ticket] = []
        self._seq = itertools.count()

    def queue(self, priority: Priority) -> Ticket:
        with self._cv:
            ticket = Ticket(self, priority, next(self._seq))
            self._line.append(ticket)
            self._cv.notify_all()
            return ticket

    @property
    def free(self) -> bool:
        """Nobody has the machine and nobody is waiting for it."""
        with self._cv:
            return self._holder is None and not self._line

    def contended(self, priority: Priority) -> bool:
        """Is a book older than `priority` waiting for this machine?"""
        with self._cv:
            return any(t.priority < priority for t in self._line)


class Gates:
    """One Gate per machine id, made on first use."""

    def __init__(self):
        self._lock = threading.Lock()
        self._gates: dict[str, Gate] = {}

    def gate(self, machine_id: str) -> Gate:
        with self._lock:
            return self._gates.setdefault(machine_id, Gate())
