"""The settings each model can be tuned with. The keys are the names the engines themselves take, so a value goes
straight through to them. This table is the one place that says what a model has: the page draws its form from it,
presets and jobs store the values, and a worker checks what it is sent against it."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

Value = float | int | None
Values = tuple[tuple[str, Value], ...]  # in the order of the model's table, every setting present


class ParamError(ValueError):
    pass


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    kind: str  # "int" or "float"
    default: Value  # None: leave it to the model (no value is sent)
    min: float
    max: float
    step: float
    help: str
    native: bool = True  # False: the engine has no such setting, so it is done to the audio afterwards (speed only)

    def coerce(self, raw: object) -> Value:
        if raw is None or raw == "":
            return self.default
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ParamError(f"{self.label} must be a number")
        value = int(raw) if self.kind == "int" else float(raw)
        if self.kind == "int" and value != raw:
            raise ParamError(f"{self.label} must be a whole number")
        if not self.min <= value <= self.max:
            raise ParamError(f"{self.label} must be between {self.min:g} and {self.max:g}")
        return value

    def view(self, default: Value) -> dict:
        return {"key": self.key, "label": self.label, "kind": self.kind, "default": default, "min": self.min,
                "max": self.max, "step": self.step, "help": self.help}


PARAMS: dict[str, tuple[Param, ...]] = {
    "nano": (
        Param("speed", "Speed", "float", 1.0, 0.5, 2.0, 0.05, "How fast it talks. 1 is normal."),
        Param("steps", "Quality steps", "int", None, 1, 64, 1,
              "More steps sound a little cleaner and take longer. Empty uses the model's own value."),
        Param("cfg", "Guidance", "float", None, 0, 10, 0.1,
              "How strongly the voice is held to its style. Empty uses the model's own value."),
        Param("sway", "Sway", "float", 0.0, -1, 1, 0.05, "Shifts where the model spends its steps. 0 is off."),
        Param("seed", "Seed", "int", None, 0, 2_147_483_647, 1,
              "The same seed reads the same text the same way. Empty gives a new reading each time."),
        Param("max_chars", "Longest piece", "int", 140, 40, 400, 10,
              "Text is spoken in pieces up to this many characters. Nano was trained on short clips."),
    ),
    "turbo": (
        Param("speed", "Speed", "float", 1.0, 0.5, 2.0, 0.05,
              "How fast it talks. Turbo has no speed of its own, so the finished audio is sped up or slowed down, "
              "which can sound less natural at the extremes.", native=False),
        Param("temperature", "Expressiveness", "float", 0.8, 0.05, 2.0, 0.05,
              "Higher is more varied and less steady (the sampling temperature)."),
        Param("top_k", "Top K", "int", 25, 1, 200, 1, "Each sound is picked from this many likeliest candidates."),
        Param("top_p", "Top P", "float", 0.95, 0.05, 1.0, 0.01, "Candidates are kept until their chances add up to this."),
        Param("repetition_penalty", "Repetition penalty", "float", 1.2, 1.0, 2.0, 0.05,
              "Above 1 discourages the voice from repeating itself or stalling."),
        Param("repetition_window", "Repetition window", "int", 64, 1, 512, 1,
              "How many recent sounds the repetition penalty looks back over."),
        Param("max_new_frames", "Longest piece (frames)", "int", 300, 50, 2000, 10,
              "Hard cap on the audio made for one piece, a guard against runaway output."),
        Param("max_chars", "Longest piece", "int", 256, 40, 600, 10, "Text is spoken in pieces up to this many characters."),
    ),
    "english": (
        Param("speed", "Speed", "float", 1.05, 0.5, 2.0, 0.05, "How fast it talks."),
        Param("total_steps", "Quality steps", "int", 8, 1, 64, 1, "More steps sound a little cleaner and take longer."),
        Param("silence_duration", "Pause between pieces", "float", 0.3, 0, 3.0, 0.05,
              "Seconds of silence where a long text is joined from pieces."),
        Param("max_chunk_length", "Longest piece", "int", 300, 50, 1000, 10,
              "Text is spoken in pieces up to this many characters."),
    ),
}


def specs(model: str) -> tuple[Param, ...]:
    try:
        return PARAMS[model]
    except KeyError:
        raise ParamError(f"unknown model {model!r}") from None


def resolve(model: str, given: Mapping[str, object] | None = None, defaults: Mapping[str, Value] | None = None) -> Values:
    """Every setting of the model, from what was given, else `defaults`, else the model's own. Checks the values
    and the names: a setting the model does not have is an error, not something to skip quietly."""
    table, given, defaults = specs(model), given or {}, defaults or {}
    unknown = sorted(set(given) - {p.key for p in table})
    if unknown:
        raise ParamError(f"the {model} model has no setting {', '.join(map(repr, unknown))}; "
                         f"it has {', '.join(p.key for p in table)}")
    out = []
    for p in table:
        raw = given.get(p.key)
        out.append((p.key, p.coerce(defaults.get(p.key, p.default) if raw is None or raw == "" else raw)))
    return tuple(out)


def stored(model: str, raw: Mapping[str, object] | None) -> Values:
    """Settings read back from a database or a manifest: names the model has no more are dropped, not refused."""
    table = {p.key for p in specs(model)}
    return resolve(model, {k: v for k, v in (raw or {}).items() if k in table})


def of_record(record: dict) -> dict[str, Value]:
    """The settings of a saved record (a preset, a library entry), which is taken apart: its `params` are read, and
    the `temperature` of records from before models had settings of their own is kept for Turbo, the only model that
    used it."""
    record = dict(record)
    old, given = record.pop("temperature", None), record.pop("params", None)
    if given is None:
        given = {"temperature": old} if old is not None and record["model"] == "turbo" else {}
    return dict(stored(record["model"], given))


def engine_kwargs(values: Values) -> dict[str, float | int]:
    """What goes to the engine: the settings that have a value."""
    return {k: v for k, v in values if v is not None}


def split_speed(model: str, options: Mapping[str, float | int]) -> tuple[dict[str, float | int], float]:
    """What the engine is given, and the speed to apply to its audio (1.0: none, the engine does speed itself)."""
    after = any(p.key == "speed" and not p.native for p in specs(model))
    rest = {k: v for k, v in options.items() if not (after and k == "speed")}
    return rest, (options.get("speed", 1.0) if after else 1.0)


def non_default(model: str, values: Values) -> Values:
    own = {p.key: p.default for p in specs(model)}
    return tuple((k, v) for k, v in values if own.get(k) != v)
