import logging

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

logger = logging.getLogger(__name__)

DEFAULT_RESOURCE_TYPES = [
    "Deployment",
    "Service",
    "ConfigMap",
    "Secret",
    "StatefulSet",
    "Pod",
    "PersistentVolumeClaim",
]

_RESOURCE_LISTERS = {
    "Deployment": lambda apps, ns: apps.list_namespaced_deployment(ns),
    "StatefulSet": lambda apps, ns: apps.list_namespaced_stateful_set(ns),
    "Service": lambda core, ns: core.list_namespaced_service(ns),
    "ConfigMap": lambda core, ns: core.list_namespaced_config_map(ns),
    "Secret": lambda core, ns: core.list_namespaced_secret(ns),
    "Pod": lambda core, ns: core.list_namespaced_pod(ns),
    "PersistentVolumeClaim": lambda core, ns: core.list_namespaced_persistent_volume_claim(ns),
}

_APPS_RESOURCES = {"Deployment", "StatefulSet"}


def snapshot_namespace(
    kubeconfig: str,
    namespace: str,
    resource_types: list[str] | None = None,
) -> list[dict]:
    types_to_list = resource_types or DEFAULT_RESOURCE_TYPES

    try:
        config.load_kube_config(config_file=kubeconfig)
    except Exception:
        logger.warning(
            "Failed to load kubeconfig from %s — returning empty snapshot",
            kubeconfig,
        )
        return []

    core_v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()

    manifests: list[dict] = []

    for resource_type in types_to_list:
        lister = _RESOURCE_LISTERS.get(resource_type)
        if lister is None:
            logger.warning("Unknown resource type %s — skipping", resource_type)
            continue

        api_client = apps_v1 if resource_type in _APPS_RESOURCES else core_v1

        try:
            result = lister(api_client, namespace)
        except ApiException as exc:
            logger.warning(
                "Failed to list %s in namespace %s: %s — skipping",
                resource_type,
                namespace,
                exc.reason,
            )
            continue

        for item in result.items:
            manifest = client.ApiClient().sanitize_for_serialization(item)
            manifests.append(manifest)

    return manifests
