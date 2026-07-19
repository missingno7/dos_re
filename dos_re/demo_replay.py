"""Persistent boundaries for hook-verification demo replays.

This is the machine-neutral phase-1 slice of the dos_re 3.0 replay index.  A
hook-verification driver supplies exact point stopping plus complete
continuation-state capture/restore for the original interpreter or the
hooked/lifted candidate.  This module owns stable points, cache identities,
base-relative changed-page persistence, interval selection, and lightweight
function visits.

It intentionally does not claim that today's real-mode or PM snapshot codecs
already capture complete deterministic continuation state.  See
``docs/dos_re_3_0/demo_replay_boundaries.md``.

Tick demos, frontend timelines, viewer recordings, and other playback artifacts
are outside this module's workstream.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

FORMAT_VERSION = 1
DEFAULT_PAGE_SIZE = 4096
INDEX_PATH = Path("replay") / "index.json"


class ReplayArtifactError(RuntimeError):
    """Base class for invalid, corrupt, or incompatible replay artifacts."""


class StaleReplayCacheError(ReplayArtifactError):
    """The persisted cache was made for different deterministic inputs."""


@dataclass(frozen=True)
class DemoPoint:
    """One stable position on a demo's canonical monotonic timeline."""

    timeline_id: str
    ordinal: int

    def __post_init__(self) -> None:
        if not self.timeline_id:
            raise ValueError("demo point timeline_id must not be empty")
        if int(self.ordinal) < 0:
            raise ValueError("demo point ordinal must be non-negative")
        object.__setattr__(self, "ordinal", int(self.ordinal))

    @property
    def key(self) -> str:
        timeline = hashlib.sha256(self.timeline_id.encode("utf-8")).hexdigest()[:16]
        return f"{timeline}-{self.ordinal:016x}"

    def to_json(self) -> dict[str, Any]:
        return {"timeline_id": self.timeline_id, "ordinal": self.ordinal, "key": self.key}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "DemoPoint":
        point = cls(str(raw["timeline_id"]), int(raw["ordinal"]))
        key = raw.get("key")
        if key is not None and key != point.key:
            raise ReplayArtifactError(f"demo point key mismatch: {key!r} != {point.key!r}")
        return point


@dataclass(frozen=True)
class MachineImage:
    """Complete opaque continuation state split into metadata and memory regions.

    ``state`` must contain every non-memory field that can affect continuation.
    The demo input cursor is explicit because omitting it is an especially easy
    way to restore a plausible but non-deterministic checkpoint.
    """

    state: Mapping[str, Any]
    memory_regions: Mapping[str, bytes]
    event_cursor: int

    def normalized(self) -> "MachineImage":
        state = _json_roundtrip(self.state, what="machine state")
        regions: dict[str, bytes] = {}
        for name, data in self.memory_regions.items():
            name = str(name)
            if not name:
                raise ValueError("memory region name must not be empty")
            if name in regions:
                raise ValueError(f"duplicate memory region name: {name!r}")
            regions[name] = bytes(data)
        cursor = int(self.event_cursor)
        if cursor < 0:
            raise ValueError("event cursor must be non-negative")
        return MachineImage(state=state, memory_regions=regions, event_cursor=cursor)


@dataclass(frozen=True)
class CacheIdentity:
    """All identities that make a persisted boundary safe to restore."""

    event_stream_sha256: str
    base_snapshot_sha256: str
    executable_image: str
    runtime_implementation: str
    device_model: str
    snapshot_format: str

    @classmethod
    def build(
        cls,
        *,
        event_stream: Any,
        base_image: MachineImage,
        executable_image: str,
        runtime_implementation: str,
        device_model: str,
        snapshot_format: str,
    ) -> "CacheIdentity":
        return cls(
            event_stream_sha256=hash_event_stream(event_stream),
            base_snapshot_sha256=machine_image_sha256(base_image),
            executable_image=str(executable_image),
            runtime_implementation=str(runtime_implementation),
            device_model=str(device_model),
            snapshot_format=str(snapshot_format),
        )

    @property
    def digest(self) -> str:
        return _sha256(_canonical_json(self.to_json()))

    def to_json(self) -> dict[str, str]:
        return {
            "event_stream_sha256": self.event_stream_sha256,
            "base_snapshot_sha256": self.base_snapshot_sha256,
            "executable_image": self.executable_image,
            "runtime_implementation": self.runtime_implementation,
            "device_model": self.device_model,
            "snapshot_format": self.snapshot_format,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "CacheIdentity":
        return cls(
            event_stream_sha256=str(raw["event_stream_sha256"]),
            base_snapshot_sha256=str(raw["base_snapshot_sha256"]),
            executable_image=str(raw["executable_image"]),
            runtime_implementation=str(raw["runtime_implementation"]),
            device_model=str(raw["device_model"]),
            snapshot_format=str(raw["snapshot_format"]),
        )


def hash_event_stream(events: Any) -> str:
    """Hash event bytes directly, or structured events as canonical JSON."""
    if isinstance(events, (bytes, bytearray, memoryview)):
        payload = bytes(events)
    else:
        payload = _canonical_json(_json_roundtrip(events, what="event stream"))
    return _sha256(payload)


def machine_image_sha256(image: MachineImage) -> str:
    """Content identity of complete metadata, cursor, region names, and bytes."""
    image = image.normalized()
    h = hashlib.sha256()
    h.update(_canonical_json({"state": image.state, "event_cursor": image.event_cursor}))
    for name in sorted(image.memory_regions):
        name_bytes = name.encode("utf-8")
        data = image.memory_regions[name]
        h.update(len(name_bytes).to_bytes(4, "little"))
        h.update(name_bytes)
        h.update(len(data).to_bytes(8, "little"))
        h.update(data)
    return h.hexdigest()


@dataclass
class FunctionVisit:
    function_id: str
    invocation_count: int = 0
    first_entry: DemoPoint | None = None
    last_exit: DemoPoint | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "function_id": self.function_id,
            "invocation_count": self.invocation_count,
            "first_entry": None if self.first_entry is None else self.first_entry.to_json(),
            "last_exit": None if self.last_exit is None else self.last_exit.to_json(),
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "FunctionVisit":
        first = raw.get("first_entry")
        last = raw.get("last_exit")
        return cls(
            function_id=str(raw["function_id"]),
            invocation_count=int(raw.get("invocation_count", 0)),
            first_entry=None if first is None else DemoPoint.from_json(first),
            last_exit=None if last is None else DemoPoint.from_json(last),
        )


class FunctionVisitIndex:
    """Streaming, recursion-safe function interval recorder."""

    def __init__(self) -> None:
        self._visits: dict[str, FunctionVisit] = {}
        self._active_depth: dict[str, int] = {}

    def observe_entry(self, function_id: str, point_before: DemoPoint) -> None:
        function_id = str(function_id)
        if not function_id:
            raise ValueError("function identity must not be empty")
        visit = self._visits.setdefault(function_id, FunctionVisit(function_id))
        depth = self._active_depth.get(function_id, 0)
        if depth == 0 and visit.first_entry is None:
            visit.first_entry = point_before
        elif visit.first_entry is not None:
            _require_same_timeline(visit.first_entry, point_before)
        visit.invocation_count += 1
        self._active_depth[function_id] = depth + 1

    def observe_exit(self, function_id: str, point_after: DemoPoint) -> None:
        function_id = str(function_id)
        depth = self._active_depth.get(function_id, 0)
        if depth <= 0:
            raise ValueError(f"function exit without active invocation: {function_id!r}")
        visit = self._visits[function_id]
        if visit.first_entry is not None:
            _require_same_timeline(visit.first_entry, point_after)
            if point_after.ordinal < visit.first_entry.ordinal:
                raise ValueError("function exit precedes its first entry")
        depth -= 1
        self._active_depth[function_id] = depth
        if depth == 0:
            visit.last_exit = point_after

    def active_depth(self, function_id: str) -> int:
        return self._active_depth.get(str(function_id), 0)

    def visits(self) -> tuple[FunctionVisit, ...]:
        return tuple(self._visits[key] for key in sorted(self._visits))

    def to_json(self) -> list[dict[str, Any]]:
        return [visit.to_json() for visit in self.visits()]

    @classmethod
    def from_json(cls, raw: list[Mapping[str, Any]]) -> "FunctionVisitIndex":
        out = cls()
        for item in raw:
            visit = FunctionVisit.from_json(item)
            if visit.function_id in out._visits:
                raise ReplayArtifactError(f"duplicate function visit: {visit.function_id!r}")
            out._visits[visit.function_id] = visit
            out._active_depth[visit.function_id] = 0
        return out


class ReplayDriver(Protocol):
    """Machine-specific exact replay/capture seam."""

    @property
    def current_point(self) -> DemoPoint: ...

    def capture(self) -> MachineImage: ...

    def restore(self, image: MachineImage, point: DemoPoint) -> None: ...

    def replay_to(self, point: DemoPoint) -> None: ...


@dataclass(frozen=True)
class ReplayResult:
    restored_from: DemoPoint
    start: DemoPoint
    end: DemoPoint
    start_was_cached: bool
    endpoint: MachineImage


class ReplayArtifact:
    """Persistent base snapshot, delta boundaries, and demo metadata index."""

    def __init__(self, bundle_dir: Path, index: dict[str, Any], identity: CacheIdentity):
        self.bundle_dir = Path(bundle_dir)
        self.replay_dir = self.bundle_dir / "replay"
        self.index_path = self.replay_dir / "index.json"
        self._index = index
        self.identity = identity

    @property
    def page_size(self) -> int:
        return int(self._index["page_size"])

    @property
    def base_point(self) -> DemoPoint:
        return DemoPoint.from_json(self._index["base_point"])

    @classmethod
    def create(
        cls,
        bundle_dir: str | Path,
        *,
        base_point: DemoPoint,
        base_image: MachineImage,
        identity: CacheIdentity,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> "ReplayArtifact":
        bundle = Path(bundle_dir)
        replay = bundle / "replay"
        index_path = replay / "index.json"
        if index_path.exists():
            raise FileExistsError(f"replay index already exists: {index_path}")
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        base = base_image.normalized()
        actual_base_hash = machine_image_sha256(base)
        if identity.base_snapshot_sha256 != actual_base_hash:
            raise ValueError(
                "cache identity base_snapshot_sha256 does not match the supplied base image")

        replay.mkdir(parents=True, exist_ok=True)
        base_dir = replay / "base"
        regions_dir = base_dir / "regions"
        regions_dir.mkdir(parents=True, exist_ok=False)
        region_records = []
        for index, name in enumerate(sorted(base.memory_regions)):
            data = base.memory_regions[name]
            rel = Path("regions") / f"{index:04d}.bin.zlib"
            (base_dir / rel).write_bytes(zlib.compress(data, 6))
            region_records.append({
                "name": name,
                "size": len(data),
                "sha256": _sha256(data),
                "file": rel.as_posix(),
            })
        _write_json_atomic(base_dir / "state.json", {
            "format_version": FORMAT_VERSION,
            "point": base_point.to_json(),
            "identity_digest": identity.digest,
            "state": base.state,
            "event_cursor": base.event_cursor,
            "regions": region_records,
        })
        index = {
            "format_version": FORMAT_VERSION,
            "identity": identity.to_json(),
            "identity_digest": identity.digest,
            "page_size": int(page_size),
            "base_point": base_point.to_json(),
            "base": (Path("replay") / "base" / "state.json").as_posix(),
            "boundaries": {},
            "point_metadata": {},
            "function_visits": [],
        }
        _write_json_atomic(index_path, index)
        artifact = cls(bundle, index, identity)
        artifact._attach_to_demo_manifest()
        return artifact

    @classmethod
    def open(
        cls,
        bundle_dir: str | Path,
        *,
        expected_identity: CacheIdentity,
    ) -> "ReplayArtifact":
        bundle = Path(bundle_dir)
        index_path = bundle / INDEX_PATH
        index = _read_json(index_path)
        if int(index.get("format_version", 0)) != FORMAT_VERSION:
            raise ReplayArtifactError(
                f"unsupported replay index version: {index.get('format_version')!r}")
        stored = CacheIdentity.from_json(index["identity"])
        stored_digest = str(index.get("identity_digest", ""))
        if stored.digest != stored_digest:
            raise ReplayArtifactError("replay index cache identity digest is corrupt")
        if stored != expected_identity:
            raise StaleReplayCacheError(
                f"replay cache identity mismatch: stored {stored.digest}, "
                f"expected {expected_identity.digest}")
        artifact = cls(bundle, index, stored)
        artifact._validate_index()
        return artifact

    def has_boundary(self, point: DemoPoint) -> bool:
        self._check_point(point)
        return point == self.base_point or point.key in self._index["boundaries"]

    def cached_points(self) -> tuple[DemoPoint, ...]:
        points = [self.base_point]
        points.extend(
            DemoPoint.from_json(record["point"])
            for record in self._index["boundaries"].values()
        )
        return tuple(sorted(points, key=lambda p: p.ordinal))

    def nearest_cached_at_or_before(self, point: DemoPoint) -> DemoPoint:
        self._check_point(point)
        eligible = [cached for cached in self.cached_points()
                    if cached.ordinal <= point.ordinal]
        if not eligible:
            raise ReplayArtifactError(f"no cached boundary at or before {point.ordinal}")
        return max(eligible, key=lambda p: p.ordinal)

    def load_boundary(self, point: DemoPoint) -> MachineImage:
        self._check_point(point)
        base = self._load_base()
        if point == self.base_point:
            return base
        record = self._index["boundaries"].get(point.key)
        if record is None:
            raise KeyError(f"demo point is not cached: {point.key}")
        manifest_path = self._resolve_relative(record["manifest"])
        manifest = _read_json(manifest_path)
        stored_point = DemoPoint.from_json(manifest["point"])
        if stored_point != point:
            raise ReplayArtifactError("boundary manifest point does not match index")
        if manifest.get("identity_digest") != self.identity.digest:
            raise StaleReplayCacheError("boundary cache identity does not match replay index")
        if int(manifest.get("page_size", 0)) != self.page_size:
            raise ReplayArtifactError("boundary page size does not match replay index")

        regions = {name: bytearray(data) for name, data in base.memory_regions.items()}
        seen: set[tuple[str, int]] = set()
        for page in manifest.get("changed_pages", []):
            name = str(page["region"])
            page_index = int(page["page_index"])
            key = (name, page_index)
            if key in seen:
                raise ReplayArtifactError(f"duplicate changed page: {name}[{page_index}]")
            seen.add(key)
            if name not in regions or page_index < 0:
                raise ReplayArtifactError(f"invalid changed page: {name}[{page_index}]")
            start = page_index * self.page_size
            expected_size = min(self.page_size, len(regions[name]) - start)
            if expected_size <= 0 or int(page["size"]) != expected_size:
                raise ReplayArtifactError(f"changed page is outside region: {name}[{page_index}]")
            payload = _read_zlib(
                self._resolve_relative(page["file"]),
                what=f"changed page {name}[{page_index}]",
            )
            if len(payload) != expected_size or _sha256(payload) != page["sha256"]:
                raise ReplayArtifactError(f"changed page payload mismatch: {name}[{page_index}]")
            regions[name][start:start + expected_size] = payload
        image = MachineImage(
            state=manifest["state"],
            memory_regions={name: bytes(data) for name, data in regions.items()},
            event_cursor=int(manifest["event_cursor"]),
        ).normalized()
        expected_image_hash = manifest.get("machine_image_sha256")
        if expected_image_hash != machine_image_sha256(image):
            raise ReplayArtifactError("reconstructed boundary machine image hash mismatch")
        return image

    def cache_boundary(
        self,
        point: DemoPoint,
        image: MachineImage,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        """Persist ``point`` against the original base. Return False if present."""
        self._check_point(point)
        if self.has_boundary(point):
            return False
        image = image.normalized()
        base = self._load_base()
        if set(image.memory_regions) != set(base.memory_regions):
            raise ValueError("boundary memory-region set differs from base")
        for name in base.memory_regions:
            if len(image.memory_regions[name]) != len(base.memory_regions[name]):
                raise ValueError(f"boundary memory-region size differs from base: {name!r}")

        boundaries_dir = self.replay_dir / "boundaries"
        boundaries_dir.mkdir(parents=True, exist_ok=True)
        final_dir = boundaries_dir / point.key
        temp_dir = Path(tempfile.mkdtemp(prefix=f".{point.key}-", dir=boundaries_dir))
        changed_pages = []
        try:
            for region_no, name in enumerate(sorted(base.memory_regions)):
                base_data = base.memory_regions[name]
                data = image.memory_regions[name]
                for page_index, start in enumerate(range(0, len(data), self.page_size)):
                    payload = data[start:start + self.page_size]
                    if payload == base_data[start:start + self.page_size]:
                        continue
                    rel = Path("pages") / f"{region_no:04d}" / f"{page_index:08x}.bin.zlib"
                    path = temp_dir / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(zlib.compress(payload, 6))
                    changed_pages.append({
                        "region": name,
                        "page_index": page_index,
                        "size": len(payload),
                        "sha256": _sha256(payload),
                        "file": (Path("replay") / "boundaries" / point.key / rel).as_posix(),
                    })
            _write_json_atomic(temp_dir / "state.json", {
                "format_version": FORMAT_VERSION,
                "point": point.to_json(),
                "identity_digest": self.identity.digest,
                "page_size": self.page_size,
                "state": image.state,
                "event_cursor": image.event_cursor,
                "machine_image_sha256": machine_image_sha256(image),
                "metadata": _json_roundtrip(metadata or {}, what="boundary metadata"),
                "changed_pages": changed_pages,
            })
            if final_dir.exists():
                raise ReplayArtifactError(
                    f"boundary directory exists but is not indexed: {final_dir}")
            os.replace(temp_dir, final_dir)
            temp_dir = None
        finally:
            if temp_dir is not None and temp_dir.exists():
                shutil.rmtree(temp_dir)

        self._index["boundaries"][point.key] = {
            "point": point.to_json(),
            "manifest": (Path("replay") / "boundaries" / point.key / "state.json").as_posix(),
        }
        self._write_index()
        return True

    def annotate_point(self, point: DemoPoint, **metadata: Any) -> None:
        self._check_point(point)
        current = dict(self._index["point_metadata"].get(point.key, {}))
        current.update(_json_roundtrip(metadata, what="point metadata"))
        self._index["point_metadata"][point.key] = current
        self._write_index()

    def update_function_visits(self, visits: FunctionVisitIndex) -> None:
        for visit in visits.visits():
            if visit.first_entry is not None:
                self._check_point(visit.first_entry)
            if visit.last_exit is not None:
                self._check_point(visit.last_exit)
        self._index["function_visits"] = visits.to_json()
        self._write_index()
        self._attach_to_demo_manifest()

    def function_visits(self) -> FunctionVisitIndex:
        return FunctionVisitIndex.from_json(self._index.get("function_visits", []))

    def _load_base(self) -> MachineImage:
        manifest_path = self._resolve_relative(self._index["base"])
        manifest = _read_json(manifest_path)
        if manifest.get("identity_digest") != self.identity.digest:
            raise StaleReplayCacheError("base cache identity does not match replay index")
        if DemoPoint.from_json(manifest["point"]) != self.base_point:
            raise ReplayArtifactError("base point does not match replay index")
        regions: dict[str, bytes] = {}
        for record in manifest["regions"]:
            name = str(record["name"])
            if name in regions:
                raise ReplayArtifactError(f"duplicate base memory region: {name!r}")
            data = _read_zlib(
                self._resolve_relative(Path("replay") / "base" / record["file"]),
                what=f"base memory region {name!r}",
            )
            if len(data) != int(record["size"]) or _sha256(data) != record["sha256"]:
                raise ReplayArtifactError(f"base memory region payload mismatch: {name!r}")
            regions[name] = data
        image = MachineImage(
            state=manifest["state"],
            memory_regions=regions,
            event_cursor=int(manifest["event_cursor"]),
        ).normalized()
        if machine_image_sha256(image) != self.identity.base_snapshot_sha256:
            raise ReplayArtifactError("base snapshot content does not match cache identity")
        return image

    def _check_point(self, point: DemoPoint) -> None:
        _require_same_timeline(self.base_point, point)
        if point.ordinal < self.base_point.ordinal:
            raise ValueError("demo point precedes the artifact base")

    def _resolve_relative(self, relative: str | Path) -> Path:
        path = (self.bundle_dir / Path(relative)).resolve()
        root = self.bundle_dir.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ReplayArtifactError(f"artifact path escapes demo bundle: {relative!r}") from exc
        return path

    def _validate_index(self) -> None:
        if self.page_size <= 0:
            raise ReplayArtifactError("invalid replay index page size")
        base = self.base_point
        keys: set[str] = set()
        for key, record in self._index.get("boundaries", {}).items():
            point = DemoPoint.from_json(record["point"])
            self._check_point(point)
            if key != point.key or key in keys or point == base:
                raise ReplayArtifactError(f"invalid replay boundary index key: {key!r}")
            keys.add(key)

    def _write_index(self) -> None:
        _write_json_atomic(self.index_path, self._index)

    def _attach_to_demo_manifest(self) -> None:
        manifest_path = self.bundle_dir / "input_demo.json"
        if not manifest_path.exists():
            return
        manifest = _read_json(manifest_path)
        metadata = dict(manifest.get("metadata", {}))
        metadata["replay_index"] = {
            "path": INDEX_PATH.as_posix(),
            "format_version": FORMAT_VERSION,
            "identity_digest": self.identity.digest,
        }
        metadata["function_visits"] = list(self._index.get("function_visits", []))
        manifest["metadata"] = metadata
        _write_json_atomic(manifest_path, manifest)


def replay_interval(
    artifact: ReplayArtifact,
    driver: ReplayDriver,
    x: DemoPoint,
    y: DemoPoint,
    *,
    cache_y: bool = False,
    x_metadata: Mapping[str, Any] | None = None,
    y_metadata: Mapping[str, Any] | None = None,
) -> ReplayResult:
    """Restore nearest boundary, lazily cache X, and execute exactly X through Y."""
    _require_same_timeline(x, y)
    artifact._check_point(x)
    artifact._check_point(y)
    if y.ordinal < x.ordinal:
        raise ValueError("replay interval end precedes start")

    restored_from = artifact.nearest_cached_at_or_before(x)
    start_was_cached = artifact.has_boundary(x)
    driver.restore(artifact.load_boundary(restored_from), restored_from)
    _require_driver_point(driver, restored_from, "restore")
    driver.replay_to(x)
    _require_driver_point(driver, x, "replay to X")
    if not start_was_cached:
        artifact.cache_boundary(x, driver.capture(), metadata=x_metadata)
    driver.replay_to(y)
    _require_driver_point(driver, y, "replay to Y")
    endpoint = driver.capture().normalized()
    if cache_y and not artifact.has_boundary(y):
        artifact.cache_boundary(y, endpoint, metadata=y_metadata)
    return ReplayResult(
        restored_from=restored_from,
        start=x,
        end=y,
        start_was_cached=start_was_cached,
        endpoint=endpoint,
    )


def _require_driver_point(driver: ReplayDriver, expected: DemoPoint, operation: str) -> None:
    actual = driver.current_point
    if actual != expected:
        raise ReplayArtifactError(
            f"driver did not stop exactly after {operation}: {actual!r} != {expected!r}")


def _require_same_timeline(a: DemoPoint, b: DemoPoint) -> None:
    if a.timeline_id != b.timeline_id:
        raise ValueError(
            f"demo points use different timelines: {a.timeline_id!r} != {b.timeline_id!r}")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _json_roundtrip(value: Any, *, what: str) -> Any:
    try:
        return json.loads(_canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} must be JSON-serializable: {exc}") from exc


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayArtifactError(f"cannot read replay artifact JSON {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ReplayArtifactError(f"replay artifact JSON is not an object: {path}")
    return raw


def _read_zlib(path: Path, *, what: str) -> bytes:
    try:
        return zlib.decompress(path.read_bytes())
    except (OSError, zlib.error) as exc:
        raise ReplayArtifactError(f"cannot read {what} from {path}: {exc}") from exc


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
