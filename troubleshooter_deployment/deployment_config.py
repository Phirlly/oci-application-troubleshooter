"""One noncredential target document shared by the runner and Resource Manager.

Format validation proves no OCI access, ownership or deployment readiness.
The caller supplies the environment mapping; this module reads no local files
or credentials and performs no service requests.
"""
from dataclasses import dataclass
import json
import re

from .codec import RecordError, validate_scope
from .records import Scope

SETTING = "DEPLOYMENT_TFVARS_JSON"
MAX_BYTES = 4096
_FIELDS = {"region", "compartment_ocid"}


class ConfigurationError(ValueError):
    """Messages contain only fixed instructions, never supplied input."""

    def __init__(self, instruction):
        super().__init__(f"{SETTING}: {instruction}")


@dataclass(frozen=True, slots=True, repr=False)
class DeploymentTarget:
    region: str
    compartment_ocid: str

    def __post_init__(self):
        if (type(self.region) is not str or len(self.region) > 64
                or re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)+-[1-9][0-9]*", self.region) is None):
            raise ConfigurationError("set region to an OCI region identifier, such as us-ashburn-1.")
        if (type(self.compartment_ocid) is not str or len(self.compartment_ocid) > 256
                or re.fullmatch(r"ocid1\.compartment\.oc1\.\.[A-Za-z0-9]+", self.compartment_ocid) is None):
            raise ConfigurationError("set compartment_ocid to an OC1 compartment OCID, not its name or a tenancy OCID.")

    def resource_manager_variables(self):
        """Fresh string map; credentials and automation-owned variables excluded."""
        return {"region": self.region, "compartment_ocid": self.compartment_ocid}

    def scope(self, repository_id, environment, tenancy_id):
        scope = Scope(repository_id, environment, tenancy_id, self.compartment_ocid, self.region)
        try:
            validate_scope(scope)
        except RecordError:
            raise ConfigurationError("invalid repository, environment or tenancy context.") from None
        return scope


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError("remove duplicate JSON fields.")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ConfigurationError("use standard JSON values.")


def parse_tfvars(text):
    """Parse the complete two-field .tfvars.json document without coercion."""
    if type(text) is not str or not text or len(text) > MAX_BYTES:
        raise ConfigurationError("supply a JSON target document of at most 4096 UTF-8 bytes.")
    try:
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise ConfigurationError("supply a JSON target document of at most 4096 UTF-8 bytes.")
        data = json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    except ConfigurationError:
        raise
    except (ValueError, RecursionError):
        raise ConfigurationError("supply valid UTF-8 JSON without comments or trailing commas.") from None
    if type(data) is not dict or set(data) != _FIELDS:
        raise ConfigurationError("supply only region and compartment_ocid; keep credentials in separate Secrets.")
    return DeploymentTarget(**data)


def from_environment(environ):
    """Read only the configured document; per-field overrides are unsupported."""
    return parse_tfvars(environ.get(SETTING, ""))
