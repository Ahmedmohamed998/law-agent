"""
Quick credential test -- runs three checks:
  1. AWS IAM keys (boto3 STS get-caller-identity)
  2. Bedrock embedding model (cohere.embed-multilingual-v3)
  3. Mantle Bearer token (a lightweight Bedrock /models list via REST)

Usage:
    python test_keys.py
"""

import base64
import json
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from dotenv import load_dotenv
import urllib.request

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg):     print(f"  {GREEN}+ {msg}{RESET}")
def fail(msg):   print(f"  {RED}x {msg}{RESET}")
def warn(msg):   print(f"  {YELLOW}! {msg}{RESET}")
def header(msg): print(f"\n{BOLD}{msg}{RESET}")

ACCESS_KEY    = os.environ.get("AWS_ACCESS_KEY_ID", "")
SECRET_KEY    = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
REGION        = os.environ.get("AWS_BEDROCK_REGION", "us-east-1")
EMBED_MODEL   = os.environ.get("AWS_BEDROCK_EMBEDDING_MODEL", "cohere.embed-multilingual-v3")
MANTLE_TOKEN  = os.environ.get("MANTLE_BEARER_TOKEN", "")

all_passed = True

# 1 --- STS
header("1/3  AWS IAM keys  (STS get-caller-identity)")
if not ACCESS_KEY or not SECRET_KEY:
    warn("Keys not set - skipping")
else:
    try:
        sts = boto3.client("sts", region_name=REGION,
                           aws_access_key_id=ACCESS_KEY,
                           aws_secret_access_key=SECRET_KEY)
        identity = sts.get_caller_identity()
        ok(f"Account : {identity['Account']}")
        ok(f"ARN     : {identity['Arn']}")
    except ClientError as e:
        fail(f"{e.response['Error']['Code']}: {e.response['Error']['Message']}")
        all_passed = False
    except Exception as e:
        fail(str(e)); all_passed = False

# 2 --- Bedrock embedding
header(f"2/3  Bedrock embedding  ({EMBED_MODEL})")
try:
    bedrock = boto3.client("bedrock-runtime", region_name=REGION,
                           aws_access_key_id=ACCESS_KEY,
                           aws_secret_access_key=SECRET_KEY)
    body = json.dumps({"texts": ["test"], "input_type": "search_document"})
    resp = bedrock.invoke_model(body=body, modelId=EMBED_MODEL,
                                accept="application/json",
                                contentType="application/json")
    data = json.loads(resp["body"].read())
    embeddings = data.get("embeddings", [])
    if embeddings:
        ok(f"Embedding vector length: {len(embeddings[0])}")
    else:
        warn(f"No embeddings in response keys: {list(data.keys())}")
except ClientError as e:
    code = e.response["Error"]["Code"]
    fail(f"{code}: {e.response['Error']['Message']}")
    if code == "UnrecognizedClientException":
        warn("Token invalid - keys may be expired or account lacks Bedrock access.")
    elif code == "AccessDeniedException":
        warn("Keys valid but IAM user lacks bedrock:InvokeModel permission, or model not enabled.")
    all_passed = False
except Exception as e:
    fail(str(e)); all_passed = False

# 3 --- Mantle token
header("3/3  Mantle Bearer token")
if not MANTLE_TOKEN:
    warn("MANTLE_BEARER_TOKEN not set - skipping")
else:
    try:
        decoded = base64.b64decode(MANTLE_TOKEN).decode("utf-8", errors="replace")
        ok(f"Token prefix: {decoded[:80]}")
    except Exception:
        warn("Could not base64-decode token")
    url = f"https://bedrock.{REGION}.amazonaws.com/foundation-models"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {MANTLE_TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            count = len(data.get("modelSummaries", []))
            ok(f"Mantle endpoint reachable; {count} models listed")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:300]
        fail(f"HTTP {e.code}")
        warn(f"Response: {body_text}")
        all_passed = False
    except Exception as e:
        fail(str(e)); all_passed = False

print()
if all_passed:
    print(f"{GREEN}{BOLD}All checks passed{RESET}")
else:
    print(f"{RED}{BOLD}One or more checks FAILED -- see details above.{RESET}")
    sys.exit(1)
