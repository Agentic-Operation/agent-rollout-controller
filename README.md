# agent-rollout-controller

Rollout/deploy controller for Spec-444: validate, per-environment deploy with `environments[]` override merge, model_bindings resolution via model-authority (C19), lifecycle hooks, §6.3 inventory record, rollback, and card environment-instance sync to Agent-Registry. API contract canonical in agent-bootstrap-management/api/openapi/bootstrap-api.yaml.\n\nRun: `python src/rollout_controller.py --port 8091`

Spec repos: agent-profile-management (Spec-443) · agent-bootstrap-management (Spec-444) · agent-discovery-card (Spec-445) · docs: agent-standard-docs · conformance: agent-conformance
License: Apache-2.0
