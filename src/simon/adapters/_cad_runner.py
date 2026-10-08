"""Fixed CAD operations, executed inside an offline, resource-limited Linux lease."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

READ_OPERATIONS = frozenset({"cad.mesh_inspect"})
WRITE_OPERATIONS = frozenset({"cad.openscad_export", "cad.render_mesh", "cad.blender_script"})
FILE_LIMIT = 48 * 1024 * 1024


def relative_file(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 240
        or value.startswith(("/", "-"))
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(
            part in {"", ".", ".."} or part.casefold().startswith((".git", ".simon"))
            for part in value.split("/")
        )
    ):
        raise ValueError("Use a relative workspace filename without traversal or internal paths")
    return value


def validate_arguments(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "cad.openscad_export": {"input", "output"},
        "cad.blender_script": {"input", "output"},
        "cad.mesh_inspect": {"input", "vase_checks"},
        "cad.render_mesh": {"input", "output", "material", "resolution", "samples", "save_scene"},
    }
    if operation not in fields or set(arguments) - fields[operation]:
        raise ValueError("Unsupported CAD operation or arguments")
    result = dict(arguments)
    result["input"] = relative_file(arguments.get("input"))
    expected = (
        {".py"}
        if operation == "cad.blender_script"
        else {".scad"}
        if operation == "cad.openscad_export"
        else {".stl", ".3mf"}
    )
    if Path(result["input"]).suffix.lower() not in expected:
        raise ValueError("Unsupported CAD input extension")
    if operation in WRITE_OPERATIONS:
        result["output"] = relative_file(arguments.get("output"))
        allowed = (
            {".blend"}
            if operation == "cad.blender_script"
            else {".stl", ".3mf"}
            if operation == "cad.openscad_export"
            else {".png"}
        )
        if Path(result["output"]).suffix.lower() not in allowed:
            raise ValueError("Unsupported CAD output extension")
    if operation == "cad.mesh_inspect":
        result["vase_checks"] = arguments.get("vase_checks", False)
        if type(result["vase_checks"]) is not bool:
            raise ValueError("vase_checks must be boolean")
    if operation == "cad.render_mesh":
        for name, default, minimum, maximum in (
            ("resolution", 960, 256, 1600),
            ("samples", 32, 8, 128),
        ):
            value = arguments.get(name, default)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} is outside its supported range")
            result[name] = value
        result["material"] = arguments.get("material", "celadon")
        if not isinstance(result["material"], str) or result["material"] not in {
            "celadon",
            "ivory",
            "terracotta",
            "charcoal",
        }:
            raise ValueError("Unknown studio material")
        result["save_scene"] = arguments.get("save_scene", False)
        if type(result["save_scene"]) is not bool:
            raise ValueError("save_scene must be boolean")
    return result


def workspace_file(workspace: Path, value: str, *, output: bool = False) -> Path:
    current = workspace
    for component in relative_file(value).split("/"):
        current /= component
        if current.is_symlink():
            raise ValueError("CAD files cannot contain symbolic links")
    if not current.resolve().is_relative_to(workspace.resolve()):
        raise ValueError("CAD file escaped its workspace")
    if output:
        if current.exists():
            raise ValueError("Output exists; choose a new revision filename")
    else:
        info = current.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > FILE_LIMIT:
            raise ValueError("CAD input must be a regular file up to 48 MiB")
    return current


def load_mesh(source: Path) -> Any:
    # Geometry dependencies are installed in the CAD image, not the API environment.
    np = importlib.import_module("numpy")
    trimesh = importlib.import_module("trimesh")

    if source.suffix.lower() == ".3mf":
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if (
                len(members) > 64
                or sum(item.file_size for item in members) > 128 * 1024 * 1024
                or any(item.flag_bits & 1 for item in members)
            ):
                raise ValueError("3MF package exceeds inspection bounds")
    mesh = trimesh.load(source, force="mesh", process=True, allow_remote=False)
    if (
        not isinstance(mesh, trimesh.Trimesh)
        or not 1 <= len(mesh.faces) <= 500000
        or not np.isfinite(mesh.vertices).all()
    ):
        raise ValueError("Expected a finite triangle mesh with at most 500,000 faces")
    return mesh


def inspect_mesh(source: Path, *, vase_checks: bool) -> dict[str, Any]:
    np = importlib.import_module("numpy")

    mesh = load_mesh(source)
    report: dict[str, Any] = {
        "units": "mm",
        "vertices": len(mesh.vertices),
        "triangles": len(mesh.faces),
        "bounds_mm": mesh.bounds.tolist(),
        "dimensions_mm": mesh.extents.tolist(),
        "watertight": bool(mesh.is_watertight),
        "winding_consistent": bool(mesh.is_winding_consistent),
        "positive_volume": bool(mesh.is_volume),
        "volume_mm3": float(mesh.volume),
        "surface_area_mm2": float(mesh.area),
        "euler_number": int(mesh.euler_number),
        "connected_components": len(mesh.split(only_watertight=False)),
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "note": "STL has no units; values assume millimeters. No automatic mesh repair applied.",
    }
    if not vase_checks:
        return report
    if not mesh.is_volume:
        raise ValueError("Vase checks require a closed, consistently wound solid")
    low, high = mesh.bounds[:, 2]
    # Centerline crossings establish a solid floor and an unobstructed mouth.
    origin = np.array([[0.0, 0.0, high + 1]])
    locations, _, _ = mesh.ray.intersects_location(origin, [[0, 0, -1]], multiple_hits=True)
    levels = sorted({round(float(point[2]), 5) for point in locations})
    centers = mesh.triangles_center
    normals = mesh.face_normals
    radial_dot = np.sum(centers[:, :2] * normals[:, :2], axis=1)
    candidates = np.flatnonzero(
        (radial_dot > 2) & (centers[:, 2] > low + 8) & (centers[:, 2] < high - 8)
    )
    if len(candidates) < 64:
        raise ValueError("Insufficient outer surface for vase wall sampling")
    samples = candidates[np.linspace(0, len(candidates) - 1, min(512, len(candidates))).astype(int)]
    directions = -normals[samples]
    starts = centers[samples] + directions * 0.0001
    hits, indices, _ = mesh.ray.intersects_location(starts, directions, multiple_hits=False)
    thickness = np.linalg.norm(hits - starts[indices], axis=1) + 0.0001
    report["vase"] = {
        "centerline_crossings_z_mm": levels,
        "open_mouth": bool(len(levels) == 2 and levels[-1] < low + (high - low) * 0.1),
        "closed_base": bool(len(levels) == 2 and abs(levels[0] - low) < 0.001),
        "base_thickness_mm": levels[1] - levels[0] if len(levels) == 2 else None,
        "wall_samples": len(thickness),
        "wall_min_mm": float(np.min(thickness)) if len(thickness) else None,
        "wall_median_mm": float(np.median(thickness)) if len(thickness) else None,
        "wall_max_mm": float(np.max(thickness)) if len(thickness) else None,
        "method": "512 deterministic outer-face inward-normal rays, excluding rim/base transitions",
        "limitation": "Sampled wall evidence is not an exhaustive manufacturing certification",
    }
    return report


BLENDER_SOURCE = r"""
import bpy, json, math, sys
from mathutils import Vector
params = json.loads(sys.argv[sys.argv.index('--') + 1])
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.stl_import(filepath=params['input'])
obj = bpy.context.selected_objects[0]
obj.name = 'Spiral fluted vase' if params['label'] == 'vase' else 'CAD model'
bpy.context.view_layer.objects.active = obj
bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
# Imported meshes use millimeters. Scale the studio to meters for lighting.
obj.scale = (0.001, 0.001, 0.001)
bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
for polygon in obj.data.polygons:
    polygon.use_smooth = abs(polygon.normal.z) < 0.9
colors = {'celadon': (0.18, 0.39, 0.34, 1), 'ivory': (0.72, 0.68, 0.57, 1),
          'terracotta': (0.42, 0.14, 0.075, 1), 'charcoal': (0.045, 0.065, 0.07, 1)}
material = bpy.data.materials.new('Satin ceramic / ' + params['material'])
material.use_nodes = True
nodes = material.node_tree.nodes
bsdf = nodes.get('Principled BSDF')
bsdf.inputs['Base Color'].default_value = colors[params['material']]
bsdf.inputs['Roughness'].default_value = 0.29
bsdf.inputs['Metallic'].default_value = 0
noise = nodes.new('ShaderNodeTexNoise')
noise.inputs['Scale'].default_value = 750
bump = nodes.new('ShaderNodeBump')
bump.inputs['Strength'].default_value = 0.09
bump.inputs['Distance'].default_value = 0.00013
material.node_tree.links.new(noise.outputs['Fac'], bump.inputs['Height'])
material.node_tree.links.new(bump.outputs['Normal'], bsdf.inputs['Normal'])
obj.data.materials.append(material)
height = obj.dimensions.z
extent = max(obj.dimensions)
floor_z = min((obj.matrix_world @ Vector(corner)).z for corner in obj.bound_box)
bpy.ops.mesh.primitive_plane_add(size=extent * 200, location=(0, 0, floor_z - 0.0002))
floor = bpy.context.object
floor.name = 'Warm stone studio floor'
floor_mat = bpy.data.materials.new('Warm grey backdrop')
floor_mat.diffuse_color = (0.36, 0.33, 0.285, 1)
floor.data.materials.append(floor_mat)
target = Vector((0, 0, floor_z + height * 0.49))
def aim(thing):
    thing.rotation_euler = (target - thing.location).to_track_quat('-Z', 'Y').to_euler()
bpy.ops.object.camera_add(location=(extent * 1.8, -extent * 2.8, floor_z + height * 2.05))
camera = bpy.context.object
aim(camera)
camera.data.type = 'ORTHO'
camera.data.ortho_scale = extent * 1.52
bpy.context.scene.camera = camera
for name, location, watts, size in (
    ('Large softbox', (-1.5, -1.8, 2.8), 50, 2.0),
    ('Rim light', (1.2, 0.7, 2.0), 35, 1.25),
    ('Fill card', (1.8, -1.5, 0.7), 12, 1.5),
):
    bpy.ops.object.light_add(type='AREA', location=tuple(value * extent for value in location))
    light = bpy.context.object
    light.name = name
    light.data.energy = watts * (extent / 0.18) ** 2
    light.data.shape = 'DISK'
    light.data.size = size * extent
    aim(light)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.device = 'CPU'
scene.cycles.samples = params['samples']
# Debian's portable Blender package does not include OpenImageDenoise.
scene.cycles.use_denoising = False
scene.render.threads_mode = 'FIXED'
scene.render.threads = 2
scene.render.resolution_x = scene.render.resolution_y = params['resolution']
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'PNG'
scene.render.filepath = params['output']
scene.world = bpy.data.worlds.new('Soft studio world')
scene.world.use_nodes = True
scene.world.node_tree.nodes['Background'].inputs[0].default_value = (0.55, 0.60, 0.65, 1)
scene.world.node_tree.nodes['Background'].inputs[1].default_value = 0.2
scene.unit_settings.system = 'METRIC'
scene.unit_settings.length_unit = 'MILLIMETERS'
scene.view_settings.view_transform = 'AgX'
scene.view_settings.exposure = -2.4
if params.get('scene'):
    bpy.ops.wm.save_as_mainfile(filepath=params['scene'])
bpy.ops.render.render(write_still=True)
"""


def run(request: dict[str, Any], workspace: Path) -> int:
    operation = request["operation"]
    arguments = validate_arguments(operation, request["arguments"])
    source = workspace_file(workspace, arguments["input"])
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_LIMIT, FILE_LIMIT))
    else:
        raise ValueError("CAD runner requires the isolated Linux image")
    if operation == "cad.mesh_inspect":
        print(json.dumps(inspect_mesh(source, vase_checks=arguments["vase_checks"])))
        return 0
    target = workspace_file(workspace, arguments["output"], output=True)
    scene_target = None
    if arguments.get("save_scene"):
        scene_target = workspace_file(
            workspace,
            str(Path(arguments["output"]).with_suffix(".blend")),
            output=True,
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "QT_QPA_PLATFORM": "offscreen",
        "OMP_NUM_THREADS": "2",
        "OPENBLAS_NUM_THREADS": "2",
    }
    with tempfile.TemporaryDirectory(prefix=".simon-cad-", dir=target.parent) as temporary:
        staging = Path(temporary)
        staged = staging / ("result" + target.suffix.lower())
        outputs = [(staged, target)]
        if operation == "cad.openscad_export":
            command = ["/usr/bin/openscad", "--hardwarnings", "-o", str(staged), str(source)]
            if target.suffix.lower() == ".stl":
                command[1:1] = ["--export-format", "binstl"]
        elif operation == "cad.blender_script":
            if source.stat().st_size > 1024 * 1024:
                raise ValueError("Blender scripts must be at most one MiB")
            script = staging / "script.py"
            # The Python program has the full Blender API inside the offline
            # container. Isolation is enforced by the lease, not Python filtering.
            script.write_text(
                "import bpy, runpy\n"
                + f"runpy.run_path({str(source)!r}, run_name='__main__')\n"
                + f"bpy.ops.wm.save_as_mainfile(filepath={str(staged)!r})\n",
                encoding="utf-8",
            )
            command = [
                "/usr/bin/blender",
                "--background",
                "--factory-startup",
                "--disable-autoexec",
                "--threads",
                "2",
                "--python-exit-code",
                "2",
                "--python",
                str(script),
            ]
        else:
            mesh = load_mesh(source)
            staged_mesh = staging / "input.stl"
            mesh.export(staged_mesh)
            script = staging / "render.py"
            script.write_text(BLENDER_SOURCE, encoding="utf-8")
            scene = staging / "scene.blend" if scene_target else None
            command = [
                "/usr/bin/blender",
                "--background",
                "--factory-startup",
                "--disable-autoexec",
                "--threads",
                "2",
                "--python-exit-code",
                "2",
                "--python",
                str(script),
                "--",
                json.dumps(
                    {
                        **arguments,
                        "input": str(staged_mesh),
                        "output": str(staged),
                        "scene": str(scene) if scene else None,
                        "label": "vase",
                    }
                ),
            ]
            if scene is not None and scene_target is not None:
                outputs.append((scene, scene_target))
        try:
            result = subprocess.run(
                command,
                cwd=workspace,
                env=environment,
                stdin=subprocess.DEVNULL,
                timeout=request["timeout_seconds"],
                check=False,
            )
        except subprocess.TimeoutExpired:
            print("CAD operation timed out; inspect its lease before retrying", file=sys.stderr)
            return 124
        if result.returncode:
            return result.returncode
        for output, _ in outputs:
            if (
                output.is_symlink()
                or not stat.S_ISREG(output.stat().st_mode)
                or output.stat().st_size > FILE_LIMIT
            ):
                raise ValueError("CAD did not produce a valid bounded file")
        # Every destination is no-overwrite; a partial publication on an I/O
        # failure is inspectable and never silently replaced on a later attempt.
        published = []
        for output, destination in outputs:
            os.link(output, destination, follow_symlinks=False)
            published.append(
                {
                    "path": destination.relative_to(workspace).as_posix(),
                    "bytes": destination.stat().st_size,
                    "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                }
            )
        print(json.dumps({"files": published}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run(json.loads(sys.argv[1]), Path("/workspace")))
    except (ValueError, TypeError, KeyError, IndexError, OSError, zipfile.BadZipFile):
        print(
            "CAD rejected: invalid file, unsupported arguments, or output already exists",
            file=sys.stderr,
        )
        sys.exit(2)
