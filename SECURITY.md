# Security Policy

## Supported releases

Security fixes are made on the latest release and `main`. Upgrade to the newest supported release before reporting an issue.

## Reporting a vulnerability

Please do not open a public issue for an exploitable vulnerability. Use [GitHub's private vulnerability reporting](https://github.com/palrajjp/agentRamen/security/advisories/new) to contact the maintainers privately. Include affected versions, impact, reproduction steps, and any suggested mitigation. Do not include real credentials or customer data in the report.

If private vulnerability reporting is unavailable for your account, contact the repository maintainers through their GitHub profile and request a private security contact. The maintainers will acknowledge reports and coordinate a fix and disclosure with the reporter.

## Central service deployment

The local `stdio` server runs with the permissions of the process that launches it. The optional central MCP service verifies OIDC bearer tokens and serves a pinned, read-only snapshot; operators must still terminate TLS at a trusted ingress, restrict hostnames and repository groups, protect snapshot storage with repository-equivalent access controls, and rotate snapshots through a reviewed deployment pipeline. Snapshots contain indexed source files that pass heuristic credential checks, which are not a substitute for data-loss prevention review.

The local HTTP browser/API server has no authentication and binds to localhost by default. Do not expose it directly to a network.
