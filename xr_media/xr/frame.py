"""Tracking sample types exchanged between headset browser and host."""

from dataclasses import dataclass, field
import math
from typing import Any, Dict, Mapping, Optional


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _finite(value: Any, default: float = 0.0) -> float:
    """Like _f, but NaN and infinities also fall back to ``default``."""
    result = _f(value, default)
    return result if math.isfinite(result) else default


def _b(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _first(data: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


@dataclass(frozen=True)
class RigidPose:
    """Rigid transform: metres + unit quaternion (x, y, z, w)."""

    px: float = 0.0
    py: float = 0.0
    pz: float = 0.0
    qx: float = 0.0
    qy: float = 0.0
    qz: float = 0.0
    qw: float = 1.0

    @property
    def valid(self) -> bool:
        """True when every value is finite and the quaternion has a usable length."""
        values = (self.px, self.py, self.pz, self.qx, self.qy, self.qz, self.qw)
        return all(math.isfinite(v) for v in values) and (
            self.qx * self.qx + self.qy * self.qy + self.qz * self.qz + self.qw * self.qw) > 1e-12

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> "RigidPose":
        """Parse nested ``position``/``orientation`` dicts or flat ``px``/``x``... keys; missing values default."""
        if not data:
            return cls()
        pos = data.get("position")
        ori = data.get("orientation")
        if isinstance(pos, Mapping) or isinstance(ori, Mapping):
            pos = pos or {}
            ori = ori or {}
            return cls(
                px=_f(pos.get("x")),
                py=_f(pos.get("y")),
                pz=_f(pos.get("z")),
                qx=_f(ori.get("x")),
                qy=_f(ori.get("y")),
                qz=_f(ori.get("z")),
                qw=_f(ori.get("w"), 1.0),
            )
        return cls(
            px=_f(_first(data, "px", "x")),
            py=_f(_first(data, "py", "y")),
            pz=_f(_first(data, "pz", "z")),
            qx=_f(data.get("qx")),
            qy=_f(data.get("qy")),
            qz=_f(data.get("qz")),
            qw=_f(data.get("qw"), 1.0),
        )


@dataclass
class HandInput:
    """One-hand digital + analog controls (matches Joy layout)."""

    trigger_pressed: bool = False
    grip_pressed: bool = False
    stick_clicked: bool = False
    face_primary: bool = False
    face_secondary: bool = False
    trigger_analog: float = 0.0
    grip_analog: float = 0.0
    stick_x: float = 0.0
    stick_y: float = 0.0

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> "HandInput":
        """Parse a browser input dict; missing or invalid values use the field defaults."""
        if not data:
            return cls()
        trigger = _finite(data.get("trigger_analog"))
        grip = _finite(data.get("grip_analog"))
        return cls(
            trigger_pressed=_b(data.get("trigger_pressed")),
            grip_pressed=_b(data.get("grip_pressed")),
            stick_clicked=_b(data.get("stick_clicked")),
            face_primary=_b(data.get("face_primary")),
            face_secondary=_b(data.get("face_secondary")),
            trigger_analog=trigger,
            grip_analog=grip,
            stick_x=_finite(data.get("stick_x")),
            stick_y=_finite(data.get("stick_y")),
        )


@dataclass
class TrackedHand:
    """One hand or controller: pose, buttons, tracking flag, source type and hand joints.

    ``source_type`` is ``controller``, ``hand`` or ``none``.
    """
    pose: RigidPose = field(default_factory=RigidPose)
    input: HandInput = field(default_factory=HandInput)
    tracked: bool = False
    source_type: str = "none"
    joints: Dict[str, RigidPose] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> "TrackedHand":
        """Parse a browser hand dict; an empty dict is untracked, a missing ``tracked`` key means tracked.

        A non-finite or degenerate pose is reported as untracked; such hand joints are dropped.
        """
        if not data:
            return cls()
        joints_raw = data.get("joints") or {}
        pose = RigidPose.from_mapping(data)
        joints = {
            str(name): RigidPose.from_mapping(joint)
            for name, joint in joints_raw.items()
            if isinstance(joint, Mapping)
        }
        return cls(
            pose=pose,
            input=HandInput.from_mapping(data.get("input")),
            tracked=bool(data.get("tracked", True)) and pose.valid,
            source_type=str(data.get("source_type") or "controller"),
            joints={name: joint for name, joint in joints.items() if joint.valid},
        )


@dataclass
class XrSample:
    """One synchronized headset + dual-hand observation."""

    t_ms: int = 0
    headset_id: str = "headset"
    hmd: RigidPose = field(default_factory=RigidPose)
    left: TrackedHand = field(default_factory=TrackedHand)
    right: TrackedHand = field(default_factory=TrackedHand)
    hmd_tracked: bool = False
    passthrough: bool = False

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "XrSample":
        """Build a sample from a parsed JSON dict (not a string); defaults do not imply valid tracking."""
        hmd_raw = payload.get("hmd") or {}
        hmd = RigidPose.from_mapping(hmd_raw)
        t_ms = payload.get("t_ms") or 0
        if isinstance(t_ms, float) and not math.isfinite(t_ms):
            raise ValueError("non-finite t_ms")
        return cls(
            t_ms=int(t_ms),
            headset_id=str(payload.get("headset_id") or "headset"),
            hmd=hmd,
            left=TrackedHand.from_mapping(payload.get("left")),
            right=TrackedHand.from_mapping(payload.get("right")),
            hmd_tracked=bool(
                payload.get("hmd_tracked", isinstance(hmd_raw.get("position"), Mapping))
            ) and hmd.valid,
            passthrough=_b(payload.get("passthrough")),
        )
