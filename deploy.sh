#!/bin/bash
#
# Deploy the curmon24 rate-monitor Lambda to the Stage account.
#
# Steps: pip install deps -> zip -> upload to the CI S3 bucket -> cloudformation deploy.
# No Docker / ant — this is a single small Python Lambda.
#
# Usage:  ./deploy.sh
#
set -euo pipefail

# ----------------------------------------------------------------- config (Stage)
AWS_PROFILE="${AWS_PROFILE:-ven-dev}"
REGION="${REGION:-us-east-1}"
STACK_NAME="Curmon24-Stage"
export AWS_PROFILE

cd "$(dirname "$0")"

# ----------------------------------------------------------------- resolve target
echo "Resolving AWS account (profile: $AWS_PROFILE)..."
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)" || {
  echo "ERROR: Please fix AWS credentials. You may need to run 'aws sso login --profile $AWS_PROFILE'" >&2
  exit 1
}

DEPLOY_BUCKET="ven-ci-${AWS_ACCOUNT_ID}-${REGION}"
TIMESTAMP="$(date +'%Y%m%d-%H%M%S')"
DEPLOY_KEY="${STACK_NAME}/${TIMESTAMP}"
DATA_BUCKET_NAME="curmon24-stage-data-${AWS_ACCOUNT_ID}"

# ----------------------------------------------------------------- build the zip
echo "Building Lambda package..."
rm -rf dist
mkdir -p dist/package
python3 -m pip install -r requirements.txt --target dist/package --quiet
cp monitor.py dist/package/
( cd dist/package && zip -qr ../Lambda.zip . )

# ----------------------------------------------------------------- upload the zip
echo "Uploading Lambda.zip to s3://${DEPLOY_BUCKET}/${DEPLOY_KEY}/ ..."
aws s3 cp --no-progress --region "$REGION" \
  dist/Lambda.zip "s3://${DEPLOY_BUCKET}/${DEPLOY_KEY}/Lambda.zip"

# ----------------------------------------------------------------- stack parameters
# Read init.stage.args, dropping comments and blank lines, into an array.
PARAMS=()
while IFS= read -r line; do
  line="${line%%#*}"                    # strip trailing comments
  line="$(echo "$line" | xargs || true)"  # trim whitespace
  [ -n "$line" ] && PARAMS+=("$line")
done < init.stage.args

# ----------------------------------------------------------------- deploy
echo "Deploying CloudFormation stack $STACK_NAME to $REGION..."
aws cloudformation deploy \
  --region "$REGION" \
  --template-file CloudFormation/Template.yaml \
  --stack-name "$STACK_NAME" \
  --parameter-overrides \
    DeploymentS3Bucket="$DEPLOY_BUCKET" \
    DeploymentS3Key="$DEPLOY_KEY" \
    S3DataBucketName="$DATA_BUCKET_NAME" \
    "${PARAMS[@]}" \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --tags Partner=VenCloud LoadType=stage

echo "Done. Stack $STACK_NAME deployed."
