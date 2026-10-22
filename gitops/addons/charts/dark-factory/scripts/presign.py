#!/usr/bin/env python3
"""SigV4 query presigner for a single S3 object (GET or PUT).

Stdlib only — `aws s3 presign` can mint GET URLs but has no PUT equivalent, and
the aws-cli container ships a self-contained binary with no importable boto3. The
alternative was installing boto3 at step start, which puts a package index on the
critical path of every run.

Credentials are read from `aws configure export-credentials --format process`
output rather than resolved here, so EKS Pod Identity keeps working without this
script knowing anything about the credential chain.

The returned URL inherits the credential's own lifetime: SigV4 rejects a
presigned request once the TEMPORARY credentials that signed it expire, no matter
what X-Amz-Expires says. The effective expiry is reported on stderr.
"""
import argparse
import datetime
import hashlib
import hmac
import json
import re
import sys
import urllib.parse

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"
TERMINATOR = "aws4_request"
# Presigned requests carry no body hash — S3 accepts this literal instead.
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
# Only `host` is signed. Signing Content-Type would force the sandbox to send a
# byte-identical header or get a 403 it cannot diagnose; S3 records whatever
# Content-Type the PUT actually sends either way.
SIGNED_HEADERS = "host"
SIGV4_MAX_EXPIRES_SECONDS = 604800
ALLOWED_METHODS = ("GET", "PUT")
# S3 keys this pipeline is allowed to sign. Matches the Sensor's own key filter:
# anything outside it cannot have reached the workflow, so signing it would mean
# the key came from somewhere unexpected.
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_./-]+$")


def _hmac_sha256(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_access_key, datestamp, region):
    key = _hmac_sha256(f"AWS4{secret_access_key}".encode("utf-8"), datestamp)
    key = _hmac_sha256(key, region)
    key = _hmac_sha256(key, SERVICE)
    return _hmac_sha256(key, TERMINATOR)


def _canonical_query(params):
    return "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
        for k, v in sorted(params.items())
    )


def presign(method, bucket, key, region, expires_in, credentials):
    host = f"{bucket}.s3.{region}.amazonaws.com"
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    scope = f"{datestamp}/{region}/{SERVICE}/{TERMINATOR}"

    params = {
        "X-Amz-Algorithm": ALGORITHM,
        "X-Amz-Credential": f"{credentials['AccessKeyId']}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires_in),
        "X-Amz-SignedHeaders": SIGNED_HEADERS,
    }
    session_token = credentials.get("SessionToken")
    if session_token:
        params["X-Amz-Security-Token"] = session_token

    canonical_uri = "/" + urllib.parse.quote(key, safe="/~")
    canonical_query = _canonical_query(params)
    canonical_request = "\n".join([
        method,
        canonical_uri,
        canonical_query,
        f"host:{host}\n",
        SIGNED_HEADERS,
        UNSIGNED_PAYLOAD,
    ])
    string_to_sign = "\n".join([
        ALGORITHM,
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(
        _signing_key(credentials["SecretAccessKey"], datestamp, region),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"https://{host}{canonical_uri}?{canonical_query}&X-Amz-Signature={signature}"


def load_credentials(path):
    with open(path, "r", encoding="utf-8") as handle:
        credentials = json.load(handle)
    for field in ("AccessKeyId", "SecretAccessKey"):
        if not credentials.get(field):
            raise ValueError(f"credential file {path} has no {field}")
    return credentials


def report_effective_expiry(credentials, expires_in):
    expiration = credentials.get("Expiration")
    if not expiration:
        return
    print(
        f"[presign] requested expiry {expires_in}s; signing credentials expire at "
        f"{expiration} — the URL dies at whichever comes first",
        file=sys.stderr,
    )


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", required=True, choices=ALLOWED_METHODS)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expires-in", required=True, type=int)
    parser.add_argument("--creds", required=True, help="aws configure export-credentials --format process output")
    args = parser.parse_args(argv)
    if not KEY_PATTERN.match(args.key):
        parser.error(f"key has characters this pipeline does not sign: {args.key}")
    if ".." in args.key:
        parser.error(f"key must not traverse: {args.key}")
    if not 0 < args.expires_in <= SIGV4_MAX_EXPIRES_SECONDS:
        parser.error(f"--expires-in must be 1..{SIGV4_MAX_EXPIRES_SECONDS}")
    return args


def main(argv):
    args = parse_args(argv)
    credentials = load_credentials(args.creds)
    report_effective_expiry(credentials, args.expires_in)
    print(presign(args.method, args.bucket, args.key, args.region, args.expires_in, credentials))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
