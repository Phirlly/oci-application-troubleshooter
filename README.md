# OCI Application Troubleshooter

See an agent diagnose a checkout failure using real OCI logs and metrics,
propose a repair, and verify recovery after your approval.

**Status: in development.** Deployment recovery and OCI/GitHub integrations have
offline tests. Terraform target inputs are defined; workload resources and the
GitHub workflow are not implemented. The demo is not runnable yet.

## Configure once

Use your own GitHub repository or a configured team repository. Supply:

- **OCI:** signing private key, user OCID, tenancy OCID, key fingerprint,
  region and compartment, with permission to deploy.
- **OpenAI:** an API key with the required Agents API and model access.
- **Demo:** a presenter password.

Keep region and compartment together using
[deployment.tfvars.json.example](terraform/deployment.tfvars.json.example).
For the planned GitHub workflow, replace the compartment placeholder and save
the JSON as Environment secret `DEPLOYMENT_TFVARS_JSON`. Store credentials
separately in GitHub Secrets; never put keys or passwords in tfvars or commit
your actual configuration. Region availability and permissions require preflight.

After setup, the intended experience uses only GitHub Actions and
the demo page—no local Terraform, Cloud Shell, SSH or manual service wiring.

## Deploy and demonstrate

1. Run **Deploy** in GitHub Actions. Automation provisions through OCI Resource
   Manager and tests login and the complete demo before publishing **Demo ready**
   and the URL.
2. Open the URL, sign in and follow the guided flow:
   **Start incident → Investigate → Approve repair → See recovery evidence.**
   One page shows application health, the explanation and supporting evidence.
3. Select **Reset demo** to prepare another presentation without redeploying.

The included sample app generates its own telemetry. No customer data or manual
data loading is needed. The agent cannot approve its own repair.

| Action | Purpose |
| --- | --- |
| Verify — GitHub | Run the full readiness test before a meeting; refuse active or paused presentations. |
| Destroy — GitHub | Remove owned resources, even if the app is unavailable; report pending cleanup. |

## Costs and cleanup

OCI and model usage incur charges. Synthetic evidence is sent to OpenAI.
Some OCI deletions take time; rerun **Destroy** to check or finish cleanup.

For credential problems, update GitHub Secrets and rerun **Deploy** for an active
demo, or **Destroy** if removal is underway.

Licensed under [MIT](LICENSE).
