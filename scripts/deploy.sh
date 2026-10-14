#!/usr/bin/env bash
# Packages the app, uploads it and the static files to the stack's S3 bucket, and
# rolls the running instances onto the new release. Run from your own machine with
# AWS credentials; it never touches an instance directly.
#
#   scripts/deploy.sh STACK_NAME [RELEASE_LABEL]
#
# On the first deploy no instance is healthy yet: they are waiting for the release
# object this script uploads, so there is nothing to refresh.
set -euo pipefail

STACK="${1:?usage: scripts/deploy.sh STACK_NAME [RELEASE_LABEL]}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="${2:-$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"

for tool in aws tar; do
  command -v "$tool" >/dev/null 2>&1 || { echo "error: $tool is not installed" >&2; exit 1; }
done

output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
BUCKET=$(output AppBucketName)
ASG=$(output AutoScalingGroupName)
[ -n "$BUCKET" ] && [ "$BUCKET" != None ] || { echo "error: stack $STACK has no AppBucketName output" >&2; exit 1; }

mkdir -p "$ROOT/dist"
BUNDLE="$ROOT/dist/webapp-$LABEL.tgz"
echo "packaging $BUNDLE"
COPYFILE_DISABLE=1 tar -czf "$BUNDLE" -C "$ROOT" \
  --exclude='__pycache__' --exclude='*.pyc' \
  app config scripts requirements.txt

echo "uploading release and static files to s3://$BUCKET"
aws s3 cp "$BUNDLE" "s3://$BUCKET/releases/webapp-$LABEL.tgz" --only-show-errors
aws s3 cp "$BUNDLE" "s3://$BUCKET/releases/webapp-latest.tgz" --only-show-errors
aws s3 sync "$ROOT/app/static" "s3://$BUCKET/static" --delete --cache-control "public,max-age=3600" --only-show-errors

IN_SERVICE=$(aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ASG" \
  --query "length(AutoScalingGroups[0].Instances[?LifecycleState=='InService'])" --output text)
if [ "${IN_SERVICE:-0}" -gt 0 ]; then
  echo "rolling $IN_SERVICE running instance(s) onto $LABEL"
  aws autoscaling start-instance-refresh --auto-scaling-group-name "$ASG" \
    --preferences MinHealthyPercentage=50,InstanceWarmup=300 >/dev/null
else
  echo "no running instances yet; new ones pick the release up when they boot"
fi
echo "done: $(output LoadBalancerUrl)"
