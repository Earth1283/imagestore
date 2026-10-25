"""Undo/redo history built from minimal patches.

Commands never snapshot the whole image:

* ``SparsePatch`` stores only the coordinates and before/after values of pixels that
  actually changed (pencil strokes, single-pixel edits).
* ``RectPatch`` stores before/after copies of one rectangle (fills, pastes).
* ``TransformCommand`` stores only the operation name; undo applies the exact inverse
  permutation, which is lossless by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count

import numpy as np

_ids = count(1)


@dataclass(eq=False)
class Command:
    label: str
    id: int = field(default_factory=lambda: next(_ids), init=False)

    @property
    def nbytes(self) -> int:
        return 0


@dataclass(eq=False)
class SparsePatch(Command):
    ys: np.ndarray = None  # int64 (N,)
    xs: np.ndarray = None  # int64 (N,)
    before: np.ndarray = None  # uint8 (N, C)
    after: np.ndarray = None  # uint8 (N, C)

    @property
    def nbytes(self) -> int:
        return self.ys.nbytes + self.xs.nbytes + self.before.nbytes + self.after.nbytes


@dataclass(eq=False)
class RectPatch(Command):
    x0: int = 0
    y0: int = 0
    before: np.ndarray = None  # uint8 (h, w, C)
    after: np.ndarray = None

    @property
    def nbytes(self) -> int:
        return self.before.nbytes + self.after.nbytes


@dataclass(eq=False)
class TransformCommand(Command):
    op: str = ""


class History:
    """Linear undo/redo stack with a memory budget.

    ``state_id`` identifies the current pixel state (the id of the last applied
    command), which makes "is the document saved?" an O(1) comparison.
    """

    def __init__(self, max_bytes: int = 1 << 30):
        self.max_bytes = max_bytes
        self._undo: list[Command] = []
        self._redo: list[Command] = []
        self._base_state = 0  # state id when the undo stack is empty

    @property
    def state_id(self) -> int:
        return self._undo[-1].id if self._undo else self._base_state

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def peek_undo(self) -> Command | None:
        return self._undo[-1] if self._undo else None

    def peek_redo(self) -> Command | None:
        return self._redo[-1] if self._redo else None

    @property
    def undo_label(self) -> str | None:
        return self._undo[-1].label if self._undo else None

    @property
    def redo_label(self) -> str | None:
        return self._redo[-1].label if self._redo else None

    @property
    def undo_stack(self) -> tuple[Command, ...]:
        """Applied commands, oldest first (read-only view for history displays)."""
        return tuple(self._undo)

    @property
    def redo_stack(self) -> tuple[Command, ...]:
        """Undone commands in the order redo would re-apply them."""
        return tuple(reversed(self._redo))

    @property
    def base_state(self) -> int:
        """State id of the buffer before the oldest command still in the stack."""
        return self._base_state

    @property
    def nbytes(self) -> int:
        return sum(c.nbytes for c in self._undo) + sum(c.nbytes for c in self._redo)

    def __len__(self) -> int:
        return len(self._undo)

    def push(self, command: Command) -> None:
        self._undo.append(command)
        self._redo.clear()
        while len(self._undo) > 1 and self.nbytes > self.max_bytes:
            dropped = self._undo.pop(0)
            self._base_state = dropped.id

    def pop_undo(self) -> Command | None:
        if not self._undo:
            return None
        command = self._undo.pop()
        self._redo.append(command)
        return command

    def pop_redo(self) -> Command | None:
        if not self._redo:
            return None
        command = self._redo.pop()
        self._undo.append(command)
        return command

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self._base_state = 0
