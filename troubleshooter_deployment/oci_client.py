"""One explicit API-key configuration and transport boundary for OCI adapters."""
from oci.loggingsearch import LogSearchClient
from oci.circuit_breaker import NoCircuitBreakerStrategy
from oci.object_storage import ObjectStorageClient
from oci.resource_manager import ResourceManagerClient
from oci.retry import NoneRetryStrategy

from .codec import validate_scope
from .oci_transport import BoundedOciSession, TransportError


def bounded_client(config, scope, service):
    validate_scope(scope)
    if (type(config) is not dict or set(config) != {"tenancy", "user", "fingerprint", "region", "key_content"}
            or config["tenancy"] != scope.tenancy_id or config["region"] != scope.region
            or any(type(value) is not str or not value for value in config.values())):
        raise TransportError()
    client_type = {"objectstorage": ObjectStorageClient, "resourcemanager": ResourceManagerClient,
                   "loggingsearch": LogSearchClient}[service]
    host = "logging" if service == "loggingsearch" else service
    suffix = "oci.oraclecloud.com" if service == "loggingsearch" else "oraclecloud.com"
    origin = f"https://{host}.{scope.region}.{suffix}"
    read_seconds = 15 if service == "loggingsearch" else 5
    client = client_type(
        dict(config), service_endpoint=origin, retry_strategy=NoneRetryStrategy(), timeout=(5, read_seconds),
        circuit_breaker_strategy=NoCircuitBreakerStrategy(), enable_strict_url_encoding=True,
        skip_deserialization=True,
        client_level_realm_specific_endpoint_template_enabled=False,
        client_level_dualstack_endpoints_enabled=False,
    )
    client.base_client.session.close()
    client.base_client.session = BoundedOciSession(origin, read_seconds=read_seconds)
    client.base_client.logger.disabled = True
    return client
