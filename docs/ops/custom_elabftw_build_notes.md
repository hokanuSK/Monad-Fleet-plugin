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

## Next Steps for AWS Deployment
1. Push the custom image to a registry (e.g., ECR)
2. Update AWS CloudFormation templates to use the custom image
3. Deploy to AWS using existing scripts in `scripts/aws/`
4. Verify functionality in AWS environment

## Notes
- elabftw submodule has merge conflicts in hypernext branch that should be resolved before further development
- Build required increased memory allocation due to large Yarn dependencies
- Stack uses custom image tag `elabftw/elabimg:custom` in docker-compose.yml

## Related Files
- `infrastructure/docker-compose.yml`: Updated to use custom image
- `src/elabftw/containers/elabimg/Dockerfile`: Modified for build fix
- Commit: 25a5cc7