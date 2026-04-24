# AWS Custom eLab Image Default (2026-04-24)

## Scope
Make the repo default to the custom eLabFTW image path so scheduler/event metadata changes from `hokanuSK/elabftw:hypernext` are not skipped during AWS deploys.

## Problem
- AWS deploy entrypoints were defaulting `BUILD_ELAB_IMAGE=false`.
- `.env.aws`-style configs could also point `ELAB_WEB_IMAGE` at a stock upstream tag.
- Result: deploys could come up on an official eLabFTW image even when this repo expected scheduler metadata/UI behavior from the custom fork.

## Repo Changes
1. `scripts/aws/deploy_ec2_stack.sh`
   - default `BUILD_ELAB_IMAGE=true`
2. `scripts/aws/deploy_cloudformation_stack.sh`
   - default `BUILD_ELAB_IMAGE=true`
3. `infrastructure/aws/cloudformation/fleetmanager-ec2.yml`
   - CloudFormation parameter `BuildElabImage` now defaults to `true`
4. `.env.aws.example`
   - default web image tag changed to `elabftw/elabimg:custom`
   - explicit custom source added:
     - `ELABFTW_OWNER=hokanuSK`
     - `ELABFTW_REPO=elabftw`
     - `ELABFTW_VERSION=hypernext`
5. `docs/runbooks/aws_ec2_deploy.md`
   - updated to document the new default behavior

## Validation Status
- Live AWS validation was intentionally not performed in this turn.
- Local stack was already confirmed to be running `elabftw/elabimg:custom`.
- The next validation step should be a deploy from `develop` with post-deploy verification of:
  - active `web` image tag
  - scheduler event metadata visible in the intended eLabFTW flow

## Expected Next Step
- Merge this change to `develop`.
- On the next AWS deploy, keep defaults unless there is a deliberate reason to set `BUILD_ELAB_IMAGE=false`.
