"""Minimal AWS Signature V4 S3/MinIO client using only stdlib.

Implements just enough of the S3 API to upload, download, list and head
objects against MinIO (or any S3-compatible endpoint). About ~120 LOC of
signing + ~80 LOC of API methods.

Reference: AWS docs, "Signing AWS API requests with SigV4"
            https://docs.aws.amazon.com/general/latest/gr/sigv4_signing.html

Design notes
------------
* Single-shot uploads only -- no multipart. For backups under a few GB
  this is fine; bigger objects should use multipart in production.
* `UNSIGNED-PAYLOAD` is NOT used. We always sign the body hash so that
  MinIO's per-request integrity check passes even with versioning on.
* We don't depend on the SDK's date math; we use `time.gmtime` + an
  explicit format string so the signing date stays UTC regardless of
  the host clock.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import http.client
import sys
import urllib.parse


def _log(msg: str) -> None:
    sys.stderr.write(f"[s3_client] {msg}\n")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _sign(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


class S3Error(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"S3 status={status} body={body[:400]}")
        self.status = status
        self.body = body


class S3Client:
    """Tiny SigV4 S3 client.

    endpoint examples:
       http://localhost:9000           (MinIO default)
       https://s3.us-east-1.amazonaws.com
    """

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        bucket: str | None = None,
        timeout_s: int = 60,
    ):
        if not endpoint:
            raise ValueError("endpoint required")
        u = urllib.parse.urlsplit(endpoint)
        if not u.scheme or not u.netloc:
            raise ValueError(f"endpoint must be a full URL, got {endpoint!r}")
        self.scheme = u.scheme
        self.host = u.netloc  # includes :port
        self.endpoint = f"{u.scheme}://{u.netloc}"
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.service = "s3"
        self.bucket = bucket
        self.timeout_s = timeout_s

    # ---- low level --------------------------------------------------
    def _new_conn(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, timeout=self.timeout_s)
        return http.client.HTTPConnection(self.host, timeout=self.timeout_s)

    def _request(
        self,
        method: str,
        bucket: str,
        key: str,
        body: bytes = b"",
        extra_headers: dict | None = None,
        query: dict | None = None,
    ) -> tuple[int, dict, bytes]:
        # Build canonical path: /<bucket>/<key>; key parts must be percent-encoded
        # individually so '/' inside keys is preserved.
        parts = key.split("/") if key else []
        enc_key = "/".join(urllib.parse.quote(p, safe="") for p in parts)
        path = f"/{urllib.parse.quote(bucket, safe='')}"
        if enc_key:
            path += "/" + enc_key

        canonical_query = ""
        if query:
            items = sorted(query.items(), key=lambda kv: kv[0])
            canonical_query = "&".join(
                f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
                for k, v in items
            )

        now = _dt.datetime.now(_dt.UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        body_hash = _hash(body)
        headers = {
            "Host": self.host,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": body_hash,
        }
        if extra_headers:
            for k, v in extra_headers.items():
                headers[k] = v

        # Canonical headers must be sorted by lowercase header name.
        canon_hdr_keys = sorted(headers.keys(), key=lambda s: s.lower())
        canonical_headers = "".join(
            f"{k.lower()}:{str(headers[k]).strip()}\n" for k in canon_hdr_keys
        )
        signed_headers = ";".join(k.lower() for k in canon_hdr_keys)

        canonical_request = "\n".join(
            [
                method,
                path,
                canonical_query,
                canonical_headers,
                signed_headers,
                body_hash,
            ]
        )

        credential_scope = f"{date_stamp}/{self.region}/{self.service}/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                _hash(canonical_request.encode("utf-8")),
            ]
        )

        sk = _signing_key(self.secret_key, date_stamp, self.region, self.service)
        signature = hmac.new(sk, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        authz = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        headers["Authorization"] = authz

        full_path = path + (f"?{canonical_query}" if canonical_query else "")
        conn = self._new_conn()
        try:
            conn.request(method, full_path, body=body, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            return resp.status, dict(resp.getheaders()), resp_body
        finally:
            conn.close()

    # ---- public ops -------------------------------------------------
    def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict | None = None,
        bucket: str | None = None,
    ) -> dict:
        bucket = bucket or self.bucket
        if not bucket:
            raise ValueError("bucket required")
        hdr = {"Content-Type": content_type, "Content-Length": str(len(data))}
        if metadata:
            for k, v in metadata.items():
                hdr[f"x-amz-meta-{k.lower()}"] = str(v)
        status, resp_hdr, body = self._request("PUT", bucket, key, body=data, extra_headers=hdr)
        if status not in (200, 201):
            raise S3Error(status, body.decode("utf-8", "replace"))
        return {
            "etag": (resp_hdr.get("ETag") or resp_hdr.get("etag") or "").strip('"'),
            "version_id": resp_hdr.get("x-amz-version-id") or resp_hdr.get("X-Amz-Version-Id"),
            "size": len(data),
        }

    def get_object(self, key: str, bucket: str | None = None) -> bytes:
        bucket = bucket or self.bucket
        status, _hdr, body = self._request("GET", bucket, key)
        if status != 200:
            raise S3Error(status, body.decode("utf-8", "replace"))
        return body

    def head_object(self, key: str, bucket: str | None = None) -> dict | None:
        bucket = bucket or self.bucket
        status, hdr, body = self._request("HEAD", bucket, key)
        if status == 404:
            return None
        if status != 200:
            raise S3Error(status, body.decode("utf-8", "replace"))
        return hdr

    def delete_object(self, key: str, bucket: str | None = None) -> bool:
        bucket = bucket or self.bucket
        status, _hdr, body = self._request("DELETE", bucket, key)
        if status not in (200, 204):
            raise S3Error(status, body.decode("utf-8", "replace"))
        return True

    def list_objects(
        self, prefix: str = "", bucket: str | None = None, max_keys: int = 1000
    ) -> list[dict]:
        """List objects with the given prefix. Returns a list of dicts:
        [{key, size, last_modified, etag}, ...]
        Handles pagination via continuation tokens.
        """
        bucket = bucket or self.bucket
        out: list[dict] = []
        token: str | None = None
        while True:
            q = {"list-type": "2", "max-keys": str(max_keys)}
            if prefix:
                q["prefix"] = prefix
            if token:
                q["continuation-token"] = token
            status, _hdr, body = self._request("GET", bucket, "", query=q)
            if status != 200:
                raise S3Error(status, body.decode("utf-8", "replace"))
            xml = body.decode("utf-8", "replace")
            out.extend(_parse_list_objects_v2(xml))
            # Look for NextContinuationToken to keep paging
            tok = _xml_field(xml, "NextContinuationToken")
            if not tok or _xml_field(xml, "IsTruncated") != "true":
                break
            token = tok
        return out

    def bucket_stats(self, prefix: str = "", bucket: str | None = None) -> dict:
        objs = self.list_objects(prefix=prefix, bucket=bucket, max_keys=1000)
        total = sum(int(o["size"]) for o in objs)
        oldest = min((o["last_modified"] for o in objs), default=None)
        return {"object_count": len(objs), "total_bytes": total, "oldest": oldest}

    def health(self) -> bool:
        """Hit the MinIO/S3 live endpoint. We try GET /minio/health/live;
        if that 404s (real S3), fall back to a list call against the
        configured bucket."""
        try:
            conn = self._new_conn()
            conn.request("GET", "/minio/health/live", headers={"Host": self.host})
            r = conn.getresponse()
            r.read()
            conn.close()
            if r.status == 200:
                return True
        except Exception as exc:  # noqa: BLE001
            _log(f"live endpoint health check failed: {exc!r}")
        try:
            self.list_objects(prefix="__healthcheck__", max_keys=1)
            return True
        except Exception:
            return False


# --- minimal XML helpers (avoid bringing in xml.etree where unnecessary) ---
def _xml_field(xml: str, name: str) -> str | None:
    open_tag = f"<{name}>"
    close_tag = f"</{name}>"
    i = xml.find(open_tag)
    if i == -1:
        return None
    i += len(open_tag)
    j = xml.find(close_tag, i)
    if j == -1:
        return None
    return xml[i:j]


def _parse_list_objects_v2(xml: str) -> list[dict]:
    """Very small XML parser tailored to ListObjectsV2 responses.
    Works around the fact that MinIO and S3 may differ in whitespace."""
    import xml.etree.ElementTree as ET

    out: list[dict] = []
    try:
        head = xml[:4096].upper()
        if "<!DOCTYPE" in head or "<!ENTITY" in head:
            return out
        root = ET.fromstring(xml)  # noqa: S314
    except ET.ParseError:
        return out
    # Strip namespace
    ns_uri = ""
    if root.tag.startswith("{"):
        ns_uri = root.tag.split("}", 1)[0][1:]

    def tag(n):
        return f"{{{ns_uri}}}{n}" if ns_uri else n

    for c in root.findall(tag("Contents")):
        k = c.findtext(tag("Key")) or ""
        sz = c.findtext(tag("Size")) or "0"
        lm = c.findtext(tag("LastModified")) or ""
        et = (c.findtext(tag("ETag")) or "").strip('"')
        out.append({"key": k, "size": int(sz), "last_modified": lm, "etag": et})
    return out


# --- CLI smoke test ---
if __name__ == "__main__":
    import os

    ep = os.environ.get("MINIO_ENDPOINT", "http://localhost:9000")
    ak = os.environ.get("MINIO_ACCESS_KEY", "zkcex")
    sk = os.environ.get("MINIO_SECRET_KEY", "zkcex-backup-demo")
    bk = os.environ.get("MINIO_BUCKET", "zkcex-backups")
    c = S3Client(ep, ak, sk, bucket=bk)
    print("live:", c.health())
    test_key = "__smoketest__/hello.txt"
    print("put:", c.put_object(test_key, b"hello zkcex backup", content_type="text/plain"))
    print("head:", c.head_object(test_key))
    got = c.get_object(test_key)
    if got != b"hello zkcex backup":
        raise AssertionError(got)
    print("get OK; deleting...")
    c.delete_object(test_key)
    print("list (first 5):", c.list_objects()[:5])
    print("stats:", c.bucket_stats())
