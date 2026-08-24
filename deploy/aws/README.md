# AWS

Runs the Knowledge Explorer on App Runner with the context graph snapshotted to a
single S3 object. Rationale for every rejected alternative is in
[docs/design/aws-integration](../../docs/design/aws-integration/README.md).

**App Runner stopped accepting new customers on 30 April 2026.** Existing services
keep running and keep receiving security and availability work, but no new
features. If this account has never held an App Runner service, none of the
commands below will work and AWS points you at Amazon ECS Express Mode instead.
Check before you start.

There is no `apprunner.yaml` here: that file "is applicable only to services that
are based on source code. You can't use configuration files with image-based
services." This repo ships a `Dockerfile`, so the service is image-based and is
declared as CloudFormation in `apprunner-service.yaml`.

**Before building, add `boto3` to the image.** The `Dockerfile` installs
`.[explorer]`, which does not include `boto3`; it lives in the `cloud` extra. An
`s3://` snapshot URI on the stock image fails at the first snapshot with
`S3 snapshots need boto3, an optional dependency: install semantica[cloud]`.
Change the one line:

```diff
-RUN pip install --no-cache-dir ".[explorer]" \
+RUN pip install --no-cache-dir ".[explorer,cloud]" \
```

```bash
export AWS_REGION=<REGION>
export ACCOUNT=<AWS-ACCOUNT-ID>
export BUCKET=<YOUR-BUCKET>
export KEY=graph/context-graph.json
export IMAGE="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/semantica-explorer:latest"

# 1. Bucket. Versioning is what turns each snapshot into a restore point.
#    Drop --create-bucket-configuration entirely in us-east-1.
aws s3api create-bucket --bucket "$BUCKET" --region "$AWS_REGION" \
  --create-bucket-configuration LocationConstraint="$AWS_REGION"
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

# 2. Shared secret, passed to the service by reference so it never lands in the stack.
aws secretsmanager create-secret --name semantica-api-key \
  --secret-string "$(openssl rand -hex 32)"

# 3. Instance role — the credential the container actually runs with.
#    Substitute <YOUR-BUCKET>, <KEY>, <REGION> and <AWS-ACCOUNT-ID> in
#    instance-role-policy.json first.
aws iam create-role --role-name semantica-explorer-instance \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "tasks.apprunner.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'
aws iam put-role-policy --role-name semantica-explorer-instance \
  --policy-name semantica-snapshot \
  --policy-document file://deploy/aws/instance-role-policy.json

# 4. Access role, so App Runner can pull from private ECR.
aws iam create-role --role-name semantica-explorer-access \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "build.apprunner.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'
aws iam attach-role-policy --role-name semantica-explorer-access \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess

# 5. Image, built from the repo root with the boto3 edit above applied.
aws ecr create-repository --repository-name semantica-explorer
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com"
docker build -t "$IMAGE" .
docker push "$IMAGE"

# 6. Service. ALLOWED_ORIGINS needs the public URL, which does not exist yet —
#    use your custom domain, or re-run this with the ServiceUrl output once it does.
aws cloudformation deploy \
  --template-file deploy/aws/apprunner-service.yaml \
  --stack-name semantica-explorer \
  --parameter-overrides \
    ImageIdentifier="$IMAGE" \
    AccessRoleArn="arn:aws:iam::$ACCOUNT:role/semantica-explorer-access" \
    InstanceRoleArn="arn:aws:iam::$ACCOUNT:role/semantica-explorer-instance" \
    SnapshotUri="s3://$BUCKET/$KEY" \
    AllowedOrigins="https://<YOUR-DOMAIN>" \
    ApiKeySecretArn="$(aws secretsmanager describe-secret \
      --secret-id semantica-api-key --query ARN --output text)"
```

`SEMANTICA_API_KEY` is required. Without it the Explorer refuses every protected
route with 503 rather than serving the graph anonymously, so a missing key is an
outage, not an open door. Pass the same value as the `X-API-Key` header from any
client that talks to the deployed API. Never set `SEMANTICA_ALLOW_ANONYMOUS` here;
it is the development opt-out and it disables that check.

`ALLOWED_ORIGINS` must name the real deployment domain. Left at its default it
names `localhost`, and the browser will refuse the deployed UI's own requests.

**The container gets an IAM role, not access keys.** App Runner has two service
roles and they are easy to conflate: the *access* role
(`build.apprunner.amazonaws.com`) pulls the image, which is deployment-time only,
while the *instance* role (`tasks.apprunner.amazonaws.com`,
`InstanceConfiguration.InstanceRoleArn`) supplies credentials to the running
container for "AWS service actions that your service's compute instances need".
The second one is what step 3 creates, so this deployment holds no long-lived
secret access key. Section 6 of the design doc says otherwise; the design doc is
wrong on this point and should be corrected.

That role is scoped to one object: `s3:GetObject` and `s3:PutObject` on
`arn:aws:s3:::<YOUR-BUCKET>/<KEY>`, not on the bucket and not on a prefix. It also
carries `s3:ListBucket` on the bucket, which is load-bearing rather than
convenience: without it S3 answers `GetObject` for a not-yet-existing key with
`403 Access Denied` instead of `404`, and the snapshot loader treats 403 as a real
failure and refuses to start rather than silently beginning with an empty graph.
The bucket holds one object, so listing it discloses nothing further. The third
statement reads the API-key secret, which App Runner requires of the instance role
before it will inject a `RuntimeEnvironmentSecrets` value. Encrypt the bucket with
a customer managed key instead of `AES256` and you must add `kms:Decrypt` and
`kms:GenerateDataKey`.

**Exactly one instance, enforced by `MaxSize: 1`** in the
`AWS::AppRunner::AutoScalingConfiguration`. Two containers would each hold a
private, diverging copy of the in-memory graph, answer requests from whichever
copy the platform picked, and overwrite each other's snapshots. `MaxConcurrency`
is left at 100 on purpose — it is the per-instance request count that triggers
scale-out, so lowering it on a service that cannot scale would throttle the UI
without adding safety.

That guarantee does not extend to deployments. App Runner "temporarily doubles the
number of provisioned instances during deployments, to maintain the same capacity
for both old and new code", so a redeploy briefly runs two graphs: the new
container loads the snapshot at start-up, the old one writes its final snapshot at
shutdown afterwards, and the new one's next interval write then replaces it. Edits
made after the new container started are lost. Redeploy when the graph is idle;
`AutoDeploymentsEnabled` is off so an ECR push alone will not trigger this.

The snapshot interval is a data-loss budget. At the default 30 seconds, an abrupt
kill — host failure, or a stop that skips the shutdown signal — loses up to 30
seconds of edits, because between snapshots the changes exist only in one
process's memory. Lower it to shrink the window; each snapshot reserialises the
whole graph under the mutation lock, so the floor is the serialisation cost, not S3.

**Delete the service, do not pause it.** App Runner bills a service whether it is
enabled or disabled and whether or not anything is deployed to it, so stopping it
does not stop the charge. A deployment needed only intermittently should be torn
down and recreated, which the S3 snapshot makes safe:

```bash
aws cloudformation delete-stack --stack-name semantica-explorer
```

The bucket, the instance role and the secret are outside the stack precisely so
this is non-destructive. Re-running step 6 against the same `SnapshotUri` restores
the graph from the snapshot the old service left behind.
