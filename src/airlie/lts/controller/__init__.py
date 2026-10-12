from __future__ import annotations

from airlie.config import ControllerSpec
from airlie.engine.protocol import Engine
from airlie.lts.controller.heuristic import HeuristicController
from airlie.lts.controller.judge import JudgeController
from airlie.lts.controller.protocol import AdmissionContext, Decision, LTSAdmissionController
from airlie.lts.controller.simple import AlwaysAdmit, NeverAdmit


def build_controller(spec: ControllerSpec, engine: Engine | None = None) -> LTSAdmissionController:
    if spec.kind == "always":
        return AlwaysAdmit()
    if spec.kind == "never":
        return NeverAdmit()
    if spec.kind == "heuristic":
        return HeuristicController(spec.duplicate_jaccard)
    if spec.kind == "judge":
        if engine is None:
            raise ValueError("lts.controller.kind=judge needs an engine")
        return JudgeController(engine, spec.judge_role, spec.threshold)
    if spec.kind == "learned":
        raise NotImplementedError("lts.controller.kind=learned arrives in M5 (spec 03 §2 torch controller)")
    raise ValueError(f"unknown controller kind {spec.kind}")


__all__ = [
    "AdmissionContext",
    "AlwaysAdmit",
    "Decision",
    "HeuristicController",
    "JudgeController",
    "LTSAdmissionController",
    "NeverAdmit",
    "build_controller",
]
