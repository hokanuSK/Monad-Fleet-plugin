# Custom eLabFTW Image Build and Deployment Notes

## Date: April 25, 2026

## Summary
Successfully built and deployed a custom eLabFTW image from the forked repo (hokanuSK/elabftw) hypernext branch.

## Changes Made
- **Dockerfile Modification**: Added `ENV NODE_OPTIONS=--max-old-space-size=6144` to `src/elabftw/containers/elabimg/Dockerfile` to prevent Node.js out-of-memory errors during Yarn asset compilation.
- **Image Build**: Built `elabftw/elabimg:custom` from local source.
- **Stack Deployment**: Updated Docker Compose stack to use the custom image and verified all services are running.

## Current Status
- eLabFTW web UI accessible at https://localhost:8443
- All services (web, mysql, fleet-service, model-device, grafana, mimir) running and healthy
- Custom image size: 915MB

## AWS Deployment Steps
To deploy the custom image to AWS:

1. **Push Image to ECR**:
   - Run `scripts/aws/push_elabimg_to_ecr.sh` (requires AWS CLI configured)
   - This creates ECR repo `fleetmanager-elabimg`, tags and pushes the image
   - Outputs the ECR URI (e.g., `123456789012.dkr.ecr.eu-north-1.amazonaws.com/fleetmanager-elabimg:custom`)

2. **Update .env.aws**:
   - Set `ELAB_WEB_IMAGE=123456789012.dkr.ecr.eu-north-1.amazonaws.com/fleetmanager-elabimg:custom`
   - Ensure other required variables are set (see .env.aws.example)

3. **Deploy to AWS**:
   - Run `scripts/aws/deploy_cloudformation_stack.sh` with:
     - `BUILD_ELAB_IMAGE=false`
     - `REPO_REF=aws-deploy-custom-elabftw-image`
     - Other parameters as needed (VPC, subnet, etc.)

4. **Verification**:
   - Run `scripts/aws/verify_cloudformation_stack.sh` after deployment

## Notes
- elabftw submodule has merge conflicts in hypernext branch that should be resolved before further development
- Build required increased memory allocation due to large Yarn dependencies
- Stack uses custom image tag `elabftw/elabimg:custom` in docker-compose.yml
- AWS deployment uses branch `aws-deploy-custom-elabftw-image`

## Related Files
- `infrastructure/docker-compose.yml`: Updated to use custom image
- `src/elabftw/containers/elabimg/Dockerfile`: Modified for build fix
- `scripts/aws/push_elabimg_to_ecr.sh`: New script for ECR push
- Commit: 25a5cc7 (initial), plus new commits on aws-deploy-custom-elabftw-image