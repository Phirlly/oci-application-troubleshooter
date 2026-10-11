# OCI Application Troubleshooter

See an agent diagnose a checkout failure using real OCI logs and metrics,
propose a repair, and verify recovery after your approval.

**Status: in development.** GitHub bootstrap, fresh-runner reuse and recovery
after a cancelled journal step are live-tested. Workload resources, Deploy,
Verify and Destroy are not implemented. The demo is not runnable yet.

## Configure once

Use your own GitHub repository or a configured team repository. Supply:

- **OCI:** signing private key, user OCID, tenancy OCID, key fingerprint,
  region and compartment, with permission to deploy.
- **OpenAI:** an API key with the required Agents API and model access.
- **Demo:** a presenter password.

Keep region and compartment together using
[deployment.tfvars.json.example](terraform/deployment.tfvars.json.example).
Replace the compartment placeholder and save the JSON as `DEPLOYMENT_TFVARS_JSON`
in GitHub's `demo` Environment. Store `OCI_TENANCY_OCID`, `OCI_USER_OCID`,
`OCI_FINGERPRINT` and `OCI_PRIVATE_KEY` there separately. Restrict this environment
to the reviewed `main` branch; remove same-name repository/organization secret
fallbacks. Never commit actual configuration or put credentials in tfvars.
Region availability and permissions require preflight.

The development bootstrap workflow retains a Resource Manager stack for recovery
metadata and reports `BOOTSTRAP_VERIFIED`, not demo readiness. It creates no
workload. Retain its GitHub deployment history. Later reviewed releases can reuse
an empty journal after the previous run finishes; populated journals require the
same release. Missing journals or unconfirmed creation require the original
release, and expired, unconfirmed creation remains pending. This workflow does
not remove the retained stack or use OpenAI/presenter secrets.

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
