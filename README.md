# OCI Application Troubleshooter

See an agent diagnose a checkout failure using real OCI logs and metrics,
propose a repair, and verify recovery after your approval.

**Status: in development. No working Deploy to OCI button or runnable demo yet.**
The login page, application package and Terraform foundation have automated
checks. Direct Resource Manager deployment and the complete demo remain untested.

## Planned deployment

1. Select **Deploy to OCI** here to open **OCI Console → Resource Manager →
   Create stack**, with the release package selected.
2. Choose the region and compartment, review the configuration, and select
   **Create** with **Run apply** enabled.
3. Automation provisions and connects the application, permissions, storage and
   synthetic telemetry. The target is one verified **Demo ready** URL.

Credential setup is still being finalized. A successful Terraform Apply alone
will not count as demo readiness. The button will be added after the actual
deployment package passes end-to-end testing.

You will need OCI access, deployment permissions and an OpenAI API key with
the required API/model access. GitHub hosts the source and release package;
deployment starts in OCI. No repository fork, GitHub Secrets, local Terraform,
Cloud Shell, SSH or manual service wiring is part of the intended setup.

## Planned demonstration

Open the URL and sign in:
**Start incident → Investigate → Approve repair → See recovery evidence.**

The sample app generates its own telemetry; no customer data or manual loading
is needed. The agent cannot approve its own repair. **Verify** and **Reset demo**
are planned inside the page; **Destroy** starts from the Resource Manager stack.

OCI and model usage incur charges. Synthetic evidence is sent to OpenAI.
Cleanup must report unresolved resources and provider deletion delays.

Licensed under [MIT](LICENSE).
