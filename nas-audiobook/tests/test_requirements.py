"""The requirements files must list what the code really imports, so a fresh install of a worker or a server starts."""
import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
# import name -> the requirement that provides it. Packages that come in with another are listed under that one.
PROVIDER = {
    "numpy": "numpy", "fastapi": "fastapi", "starlette": "fastapi", "pydantic": "fastapi", "uvicorn": "uvicorn",
    "multipart": "python-multipart", "httpx": "httpx", "ebooklib": "ebooklib", "bs4": "beautifulsoup4",
    "vieneu": "vieneu", "vieneu_utils": "vieneu", "supertonic": "supertonic", "onnxruntime": "onnxruntime",
    "soundfile": "soundfile", "huggingface_hub": "vieneu",
}
# a requirement that installs another module too: onnxruntime-gpu ships the onnxruntime module
ALSO_PROVIDES = {"onnxruntime-gpu": "onnxruntime"}


def requirements(name: str) -> dict[str, str]:
    """{package: pinned version} for a requirements file and the files it includes."""
    found: dict[str, str] = {}
    for line in (ROOT / name).read_text().splitlines():
        line = line.split("#")[0].strip()
        if line.startswith("-r "):
            found.update(requirements(line[3:].strip()))
        elif "==" in line:
            package, version = line.split("==")
            found[package.split("[")[0].strip().lower()] = version.strip()
    return found


def imports_of(entry: str) -> set[str]:
    """Every third-party module imported anywhere (lazy imports too) by an app module and the app modules it imports."""
    seen: set[str] = set()
    todo = [entry]
    modules: set[str] = set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        path = APP / (name.replace(".", "/") + ".py")
        if not path.exists():
            path = APP / name.replace(".", "/") / "__init__.py"
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                targets = [(alias.name, 0, None) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                targets = [(node.module or "", node.level, [a.name for a in node.names])]
            else:
                continue
            for module, level, names in targets:
                if level:  # a relative import of another app module
                    base = name.rsplit(".", 1)[0] if "." in name else ""
                    candidates = [f"{base}.{module}".strip(".")] if module else [f"{base}.{n}".strip(".") for n in names]
                    todo += [c for c in candidates if (APP / (c.replace(".", "/") + ".py")).exists()
                             or (APP / c.replace(".", "/") / "__init__.py").exists()]
                else:
                    top = module.split(".")[0]
                    if top and top not in sys.stdlib_module_names and top != "app":
                        modules.add(top)
    return modules


def missing(entry: str, files: list[str]) -> set[str]:
    installed: set[str] = set()
    for f in files:
        installed |= {ALSO_PROVIDES.get(p, p) for p in requirements(f)}
    return {PROVIDER.get(m, m) for m in imports_of(entry)} - installed


def test_a_worker_installed_from_either_machine_file_has_everything_it_imports():
    assert missing("worker", ["requirements-mac-cpu.txt"]) == set()
    assert missing("worker", ["requirements-nvidia-gpu.txt"]) == set()


def test_the_shared_worker_file_alone_lacks_only_the_onnx_runtime_each_machine_file_adds():
    assert missing("worker", ["requirements-worker-common.txt"]) == {"onnxruntime"}


def test_a_server_installed_from_the_documented_files_has_everything_it_imports():
    assert missing("__main__", ["requirements.txt", "requirements-mac-cpu.txt"]) == set()


def test_a_server_without_a_machine_file_only_lacks_the_speech_packages_that_are_imported_lazily():
    assert missing("__main__", ["requirements.txt"]) <= {"vieneu", "supertonic", "onnxruntime"}


def test_the_same_package_is_never_pinned_to_two_versions_in_different_files():
    pins: dict[str, set[str]] = {}
    for f in ("requirements.txt", "requirements-worker-common.txt", "requirements-mac-cpu.txt",
              "requirements-nvidia-gpu.txt", "requirements-dev.txt"):
        for package, version in requirements(f).items():
            pins.setdefault(package, set()).add(version)
    assert {p: v for p, v in pins.items() if len(v) > 1} == {}


def test_the_mac_file_has_no_pytorch_and_the_nvidia_file_has_the_cuda_build():
    assert "torch" not in requirements("requirements-mac-cpu.txt")
    assert "torch" in requirements("requirements-nvidia-gpu.txt")
