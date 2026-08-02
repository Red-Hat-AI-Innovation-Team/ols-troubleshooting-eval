import copy
import re

_SENSITIVE_ENV_SUFFIX = re.compile(r"_(PASSWORD|TOKEN|KEY|SECRET)$", re.IGNORECASE)

_STRIP_METADATA_KEYS = {
    "uid",
    "resourceVersion",
    "creationTimestamp",
    "managedFields",
    "ownerReferences",
}

_MINIMIZED_RESOURCES = {
    "requests": {"cpu": "100m", "memory": "128Mi"},
    "limits": {"cpu": "100m", "memory": "128Mi"},
}

_SCALABLE_KINDS = {"Deployment", "StatefulSet"}


def sanitize_manifest(manifest: dict) -> dict:
    out = copy.deepcopy(manifest)

    kind = out.get("kind", "")

    if kind == "Secret":
        out["data"] = {"REDACTED": "REDACTED"}

    _strip_metadata(out)
    out.pop("status", None)

    if kind in _SCALABLE_KINDS:
        spec = out.get("spec", {})
        spec["replicas"] = 1
        _sanitize_pod_template(spec.get("template", {}))

    if kind == "Pod":
        _sanitize_container_list(out.get("spec", {}).get("containers", []))
        _sanitize_container_list(out.get("spec", {}).get("initContainers", []))

    if kind == "PersistentVolumeClaim":
        spec = out.get("spec", {})
        spec["storageClassName"] = "standard"

    return out


def sanitize_manifests(manifests: list[dict]) -> list[dict]:
    return [sanitize_manifest(m) for m in manifests]


def _strip_metadata(obj: dict) -> None:
    metadata = obj.get("metadata", {})
    for key in _STRIP_METADATA_KEYS:
        metadata.pop(key, None)


def _sanitize_pod_template(template: dict) -> None:
    spec = template.get("spec", {})
    _sanitize_container_list(spec.get("containers", []))
    _sanitize_container_list(spec.get("initContainers", []))


def _sanitize_container_list(containers: list[dict]) -> None:
    for container in containers:
        _filter_sensitive_env(container)
        _minimize_resources(container)


def _filter_sensitive_env(container: dict) -> None:
    env = container.get("env")
    if env is None:
        return
    container["env"] = [
        e for e in env if not _SENSITIVE_ENV_SUFFIX.search(e.get("name", ""))
    ]


def _minimize_resources(container: dict) -> None:
    if "resources" in container:
        container["resources"] = copy.deepcopy(_MINIMIZED_RESOURCES)
