import copy

import pytest

from simulate_mcp.sanitize import sanitize_manifest, sanitize_manifests


def _make_secret():
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "db-credentials",
            "namespace": "app-ns",
            "uid": "abc-123",
            "resourceVersion": "999",
            "creationTimestamp": "2025-01-01T00:00:00Z",
            "managedFields": [{"manager": "kubectl"}],
            "ownerReferences": [{"kind": "Deployment", "name": "app"}],
        },
        "data": {
            "username": "YWRtaW4=",
            "password": "c3VwZXJzZWNyZXQ=",
        },
        "status": {"phase": "Active"},
    }


def _make_deployment():
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "web-app",
            "namespace": "app-ns",
            "uid": "def-456",
            "resourceVersion": "100",
            "creationTimestamp": "2025-06-01T00:00:00Z",
            "managedFields": [],
            "ownerReferences": [],
            "labels": {"app": "web"},
        },
        "spec": {
            "replicas": 5,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": "app:latest",
                            "env": [
                                {"name": "APP_NAME", "value": "web"},
                                {"name": "DATABASE_HOST", "value": "db.svc"},
                                {"name": "DB_PASSWORD", "value": "hunter2"},
                                {"name": "API_TOKEN", "value": "tok-abc"},
                                {"name": "ENCRYPTION_KEY", "value": "k3y"},
                                {"name": "CLIENT_SECRET", "value": "s3c"},
                            ],
                            "resources": {
                                "requests": {"cpu": "500m", "memory": "1Gi"},
                                "limits": {"cpu": "2", "memory": "4Gi"},
                            },
                        }
                    ]
                }
            },
        },
        "status": {"availableReplicas": 5},
    }


def _make_pvc():
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": "data-pvc",
            "namespace": "app-ns",
            "uid": "pvc-789",
            "resourceVersion": "50",
            "creationTimestamp": "2025-03-01T00:00:00Z",
            "managedFields": [],
        },
        "spec": {
            "storageClassName": "gp3-encrypted",
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "100Gi"}},
        },
        "status": {"phase": "Bound"},
    }


def _make_statefulset():
    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": "db", "namespace": "app-ns"},
        "spec": {
            "replicas": 3,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "postgres",
                            "image": "postgres:16",
                            "env": [
                                {"name": "PGDATA", "value": "/data"},
                                {"name": "POSTGRES_PASSWORD", "value": "pg-pass"},
                            ],
                        }
                    ]
                }
            },
        },
    }


class TestSecretRedaction:
    def test_secret_data_is_stripped(self):
        result = sanitize_manifest(_make_secret())
        assert result["data"] == {"REDACTED": "REDACTED"}

    def test_secret_kind_preserved(self):
        result = sanitize_manifest(_make_secret())
        assert result["kind"] == "Secret"
        assert result["metadata"]["name"] == "db-credentials"


class TestSensitiveEnvVars:
    def test_password_env_removed(self):
        result = sanitize_manifest(_make_deployment())
        envs = result["spec"]["template"]["spec"]["containers"][0]["env"]
        names = [e["name"] for e in envs]
        assert "DB_PASSWORD" not in names

    def test_token_env_removed(self):
        result = sanitize_manifest(_make_deployment())
        envs = result["spec"]["template"]["spec"]["containers"][0]["env"]
        names = [e["name"] for e in envs]
        assert "API_TOKEN" not in names

    def test_key_env_removed(self):
        result = sanitize_manifest(_make_deployment())
        envs = result["spec"]["template"]["spec"]["containers"][0]["env"]
        names = [e["name"] for e in envs]
        assert "ENCRYPTION_KEY" not in names

    def test_secret_env_removed(self):
        result = sanitize_manifest(_make_deployment())
        envs = result["spec"]["template"]["spec"]["containers"][0]["env"]
        names = [e["name"] for e in envs]
        assert "CLIENT_SECRET" not in names

    def test_safe_env_vars_kept(self):
        result = sanitize_manifest(_make_deployment())
        envs = result["spec"]["template"]["spec"]["containers"][0]["env"]
        names = [e["name"] for e in envs]
        assert "APP_NAME" in names
        assert "DATABASE_HOST" in names

    def test_db_password_redacted(self):
        manifest = _make_deployment()
        manifest["spec"]["template"]["spec"]["containers"][0]["env"] = [
            {"name": "APP_NAME", "value": "web"},
            {"name": "DB_PASSWORD", "value": "supersecret123"},
        ]
        result = sanitize_manifest(manifest)
        envs = result["spec"]["template"]["spec"]["containers"][0]["env"]
        names = [e["name"] for e in envs]
        assert "DB_PASSWORD" not in names
        assert "APP_NAME" in names


class TestMetadataStripping:
    def test_uid_stripped(self):
        result = sanitize_manifest(_make_secret())
        assert "uid" not in result["metadata"]

    def test_resource_version_stripped(self):
        result = sanitize_manifest(_make_secret())
        assert "resourceVersion" not in result["metadata"]

    def test_creation_timestamp_stripped(self):
        result = sanitize_manifest(_make_secret())
        assert "creationTimestamp" not in result["metadata"]

    def test_managed_fields_stripped(self):
        result = sanitize_manifest(_make_secret())
        assert "managedFields" not in result["metadata"]

    def test_owner_references_stripped(self):
        result = sanitize_manifest(_make_secret())
        assert "ownerReferences" not in result["metadata"]

    def test_status_stripped(self):
        result = sanitize_manifest(_make_secret())
        assert "status" not in result

    def test_name_and_namespace_kept(self):
        result = sanitize_manifest(_make_secret())
        assert result["metadata"]["name"] == "db-credentials"
        assert result["metadata"]["namespace"] == "app-ns"


class TestReplicaScaling:
    def test_deployment_replicas_scaled_to_1(self):
        result = sanitize_manifest(_make_deployment())
        assert result["spec"]["replicas"] == 1

    def test_statefulset_replicas_scaled_to_1(self):
        result = sanitize_manifest(_make_statefulset())
        assert result["spec"]["replicas"] == 1


class TestResourceMinimization:
    def test_resources_minimized(self):
        result = sanitize_manifest(_make_deployment())
        resources = result["spec"]["template"]["spec"]["containers"][0]["resources"]
        assert resources["requests"]["cpu"] == "100m"
        assert resources["requests"]["memory"] == "128Mi"
        assert resources["limits"]["cpu"] == "100m"
        assert resources["limits"]["memory"] == "128Mi"


class TestPVCSanitization:
    def test_storage_class_set_to_standard(self):
        result = sanitize_manifest(_make_pvc())
        assert result["spec"]["storageClassName"] == "standard"

    def test_access_modes_preserved(self):
        result = sanitize_manifest(_make_pvc())
        assert result["spec"]["accessModes"] == ["ReadWriteOnce"]


class TestImmutability:
    def test_input_manifest_not_mutated(self):
        original = _make_deployment()
        frozen = copy.deepcopy(original)
        sanitize_manifest(original)
        assert original == frozen

    def test_secret_input_not_mutated(self):
        original = _make_secret()
        frozen = copy.deepcopy(original)
        sanitize_manifest(original)
        assert original == frozen

    def test_pvc_input_not_mutated(self):
        original = _make_pvc()
        frozen = copy.deepcopy(original)
        sanitize_manifest(original)
        assert original == frozen


class TestSanitizeManifests:
    def test_batch_sanitization(self):
        manifests = [_make_secret(), _make_deployment(), _make_pvc()]
        results = sanitize_manifests(manifests)
        assert len(results) == 3
        assert results[0]["data"] == {"REDACTED": "REDACTED"}
        assert results[1]["spec"]["replicas"] == 1
        assert results[2]["spec"]["storageClassName"] == "standard"

    def test_empty_list(self):
        assert sanitize_manifests([]) == []

    def test_original_list_not_mutated(self):
        manifests = [_make_secret()]
        frozen = copy.deepcopy(manifests)
        sanitize_manifests(manifests)
        assert manifests == frozen
