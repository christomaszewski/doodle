"""Read active Compose images and enforce Rig's invocation-specific runtime image pins.

Shipped with this service's standalone launch surface; no Rig installation is required.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys


def native_platform(env, platform=None):
    arch = env.get("RIG_TARGET_ARCH")
    if arch and arch not in ("arm64", "amd64"):
        raise ValueError(f"unsupported native replay architecture: {arch}")
    platform = platform or env.get("DOCKER_DEFAULT_PLATFORM")
    if arch and platform:
        allowed = {arch, f"linux/{arch}"}
        if arch == "arm64":
            allowed.add("linux/arm64/v8")
        if platform not in allowed:
            raise ValueError(f"runtime platform {platform} does not match native linux/{arch}")
    return f"linux/{arch}" if arch else None


def compose_model(options):
    result = subprocess.run(["docker", "compose", *options, "config", "--format", "json"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(f"Compose image inspection failed: {result.stderr.strip()}")
    model = json.loads(result.stdout)
    services = model.get("services") if isinstance(model, dict) else None
    if not isinstance(services, dict) or not services:
        raise ValueError("Compose image inspection returned no active services")
    for name, service in services.items():
        if not isinstance(service, dict) or not isinstance(service.get("image"), str) or not service["image"]:
            raise ValueError(f"active service {name} has no resolved runtime image")
        try:
            native_platform(os.environ, service.get("platform"))
        except ValueError as exc:
            raise ValueError(f"active service {name}: {exc}") from exc
    return model


def image_pins(raw, images=None):
    pins = json.loads(raw)
    if (not isinstance(pins, dict) or set(pins) != {"version", "images"}
            or type(pins.get("version")) is not int or pins["version"] != 1
            or not isinstance(pins.get("images"), dict)):
        raise ValueError("RIG_RUNTIME_IMAGE_PINS must contain version: 1 and an images mapping")
    mapping = pins["images"]
    if images is not None and set(mapping) != set(images):
        missing, extra = sorted(set(images) - set(mapping)), sorted(set(mapping) - set(images))
        raise ValueError(f"runtime image selection changed: missing pins={missing}, unexpected pins={extra}")
    if any(not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value)
           for value in mapping.values()):
        raise ValueError("runtime image pins must be full sha256 image IDs")
    return mapping


def pinned_image(image, env):
    """One-shot helpers may use one image from a previously verified active set."""
    if "RIG_RUNTIME_IMAGE_PINS" not in env:
        return image
    mapping = image_pins(env["RIG_RUNTIME_IMAGE_PINS"])
    if image not in mapping:
        raise ValueError(f"runtime image selection changed: no pin for {image}")
    return mapping[image]


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    mode = args.pop(0)
    split = args.index("--") if "--" in args else len(args)
    options, up_args = args[:split], args[split + 1:]
    if mode not in ("runtime-images", "images", "pinned-up", "validate-pins"):
        raise ValueError(f"unknown runtime image operation: {mode}")
    model = compose_model(options)
    images = sorted({service["image"] for service in model["services"].values()})
    if mode == "runtime-images":
        selected = {"version": 1, "images": images}
        selected["roles"] = {"doodle_radio": model["services"]["radio"]["image"]}
        print(json.dumps(selected))
        return 0
    if mode == "images":
        # JSON strings are valid YAML scalars, including registry ports and digests.
        print("\n".join("- " + json.dumps(image) for image in images))
        return 0
    pins = image_pins(os.environ["RIG_RUNTIME_IMAGE_PINS"], images)
    if set(images) - set(pins):
        raise ValueError(f"runtime image selection changed: missing pins={sorted(set(images) - set(pins))}")
    if mode in ("pinned-up", "validate-pins"):
        allowed = {"-d", "--detach", "--force-recreate", "--remove-orphans",
                   "--no-deps", "--wait", "--no-build", "--quiet-pull"}
        if any(arg not in allowed for arg in up_args):
            raise ValueError("pinned replay up only accepts launch flags; image policy and service selection are fixed")
    if mode == "validate-pins":
        return 0
    for service in model["services"].values():
        service["image"] = pins[service["image"]]
        service["pull_policy"] = "never"
        platform = native_platform(os.environ, service.get("platform"))
        if platform:
            service["platform"] = platform
        service.pop("build", None)
        service.pop("profiles", None)  # config already selected the active services
        # Compose retains optional dependencies on inactive profile services in `config`.
        # They have no definition in this frozen active model and must not be activated.
        dependencies = service.get("depends_on", {})
        for name, dependency in list(dependencies.items()):
            if name not in model["services"]:
                if dependency.get("required", True):
                    raise ValueError(f"active service requires missing dependency {name}")
                del dependencies[name]
        if not dependencies:
            service.pop("depends_on", None)
    # Compose already escapes literal dollars for round-tripping and resolves mount paths.
    # Keep those values verbatim; stdin avoids writing a rendered file.
    operation = ["up", "--no-build"]
    return subprocess.run(["docker", "compose", "-f", "-", *operation, "--pull", "never",
                           *up_args], input=json.dumps(model), text=True).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"runtime-images: {exc}\n")
        sys.exit(2)
