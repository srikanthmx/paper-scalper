"""Hot-reloadable strategy parameters.

Each strategy holds a flat dict `self.p` of tunables (thresholds and exits only —
indicator periods need a re-warm and stay fixed per process). The dashboard saves
a new version to the journal; the engine applies it on the next candle and tags
every subsequent trade with the version, so the journal can compare versions.
"""

from __future__ import annotations


class TunableParams:
    p: dict[str, float | int]

    def apply_params(self, new: dict[str, float | int]) -> None:
        for key, value in new.items():
            if key not in self.p:
                continue  # unknown keys ignored (e.g. from an older version)
            current = self.p[key]
            self.p[key] = int(value) if isinstance(current, int) else float(value)
