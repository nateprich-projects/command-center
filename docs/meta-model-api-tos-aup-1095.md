# Meta Model API ToS/AUP check (#1095)

**Reviewed:** 2026-09-18  
**Decision:** **FAIL for the proposed test week as currently scoped.** A
pay-per-use test remains possible with Standard Services, or with Discounted
Services restricted to inputs that are demonstrably public and non-confidential.

This is a reading of Meta's published product terms, not legal advice. The
reviewed Terms of Service page says it was last updated 2026-08-28.

## What the documents allow

The Meta Model API Terms of Service, §1, grants a right to access and use the
Services and to develop applications, tools, products, and integrations that
interface with them. The same section says the Services are intended for
commercial use. Section 13 expressly contemplates pay-per-use Services Fees.

The Terms distinguish two service tiers:

- **Standard Services:** §5.1 says Meta processes the data under the
  incorporated processor terms and will not use Standard Services content to
  train Meta Models.
- **Discounted Services:** §§6.1–6.2 say Meta may use content to train,
  develop, evaluate, and improve its systems, and expressly prohibit submitting
  sensitive, confidential, or personal information. Section 6.2 specifically
  says that software code which the user intends or is required to keep
  confidential must not be submitted to Discounted Services.

The Terms' §10.1 incorporates the Model API Acceptable Use Policy and also
prohibits bypassing rate limits or other safeguards (§10.1(xii)) and consuming
resources excessively, degrading the service, or using it inconsistently with
legitimate end-user application use (§10.1(xiii)).

The AUP's automated-systems rule is not a blanket ban on scheduled agents. It
prohibits operating AI agents or automated systems **in ways that evade human
oversight or accountability**, including bypassing review or monitoring,
concealing automation where disclosure is required, coordinating prohibited
actions, violating another service's terms, or taking consequential or
irreversible actions through connected services without appropriate
authorization, human oversight, review, and confirmation.

## Mapping to the proposed test

The normal `muse-implement` shape is compatible with the first part of the
Terms: one ticket per run, a fresh ticket branch, local tests, and a pull
request rather than a direct production change. Its spending meter and one-run
reserve also support §10.1(xii)–(xiii). The AUP automation condition can be met
only if PR review and authorization remain in place and the agent does not
merge, deploy, or take other consequential actions without those controls.

The proposed test is broader than that safe case. It routes a week of ordinary
funnel tickets through the contributor-rate/Discounted Services tier. The
funnel includes private repositories (for example, `nateprich-projects/The-League`),
so that workload can include confidential source code or issue context. That is
not permitted by §6.2. The test week therefore stays blocked as written; a
general “the AUP allows automation” pass would overlook the separate ToS data
restriction.

## Gate result

Record **FAIL** on the parent investigation and do not start contributor-rate
spending for the general funnel. The compliant choices are:

1. use Standard Services for private or otherwise confidential tickets; or
2. narrow Discounted Services to a documented public/non-confidential lane,
   scrub personal or sensitive inputs, and retain PR review plus authorization
   before any consequential action.

Choosing between those exposure models remains the plan's stated human decision.
This note does not authorize a model switch or a decision about dropping Codex
Plus.

## Official sources

- [Meta Model API Terms of Service](https://dev.meta.ai/legal/terms-of-service)
- [Meta Model API Acceptable Use Policy](https://dev.meta.ai/legal/acceptable-use-policy)

