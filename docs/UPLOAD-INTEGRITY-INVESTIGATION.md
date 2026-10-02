# Upload integrity investigation

Evidence date: October 1, 2026.

## Result

The existing 68,087,113,964-byte, 35-part BRAW upload has a remote ETag that
exactly matches the multipart MD5 calculated from the complete local source
using the uploader's original part boundaries. The remote probe consumed one
byte. No new upload or full-original download was performed.

| Evidence | Result |
| --- | --- |
| Original metadata size | 68,087,113,964 bytes |
| Request | GET with Range: bytes=0-0 and Accept-Encoding: identity |
| HTTP status | 206 |
| Content-Range | bytes 0-0/68087113964 |
| Content-Length | 1 |
| Bytes consumed | 1 |
| Remote ETag | bbf8fa579932d9ab8a057a66d7e0136e-35 |
| Local multipart MD5 candidate | bbf8fa579932d9ab8a057a66d7e0136e-35 |
| Comparison | Exact match |
| Local size and modification timestamp before/after hashing | Unchanged |
| Explicit checksum headers | None returned among Content-MD5, Digest, Content-Digest, or the inspected x-amz-checksum headers |

The probe required HTTPS, an allowlisted Frame.io download hostname, no
redirects, an exact Content-Range, and a streaming response. It consumed no body
when range validation failed. It did not save or print the signed download URL.

## Interpret the match

The local calculation read every source byte and used the same boundaries as
the original upload:

1. Set the part width to max(5 MiB, ceil(file size / part count)).
2. Calculate each part's binary MD5 digest in order.
3. Concatenate those 35 binary digests.
4. Calculate MD5 over the concatenation and append "-35".

The width was 1,945,346,114 bytes. The final part was 1,945,346,088 bytes.

AWS documents this multipart ETag construction. A multipart ETag is not the
MD5 of the whole file. This exact match is strong evidence against accidental
transfer corruption for this asset under the multipart-MD5 interpretation.
Do not treat an arbitrary ETag as a checksum: encryption, storage behavior, or
object copying can change its meaning. MD5 is not collision-resistant, and this
test is not a cryptographic authenticity guarantee.
See [AWS upload integrity documentation](https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html).

This test did not download and SHA-256-compare the complete remote original,
prove media playability, or establish checksum behavior for every asset or
backend. Preserve a distinct result such as "multipart_etag_match"; do not
silently equate it with the existing full-download SHA-256 verification flag.

## Checksum-enforced uploads

### What was observed

The inspection read 161 application JavaScript resources referenced by the
loaded Frame.io page. The inspected resources had no literal Content-MD5,
content-md5, or x-amz-checksum references. This bounded search does not prove
that no other worker, lazy-loaded bundle, or backend supports checksums.

The uploader implementation in
`/_next/static/chunks/2rqo3wt_s757y.js` showed two PUT paths sending a sliced
Blob and a content-type header. Their part-size formula matched the existing
Python uploader. No explicit checksum header appeared at those call sites.
Another observed bundle, `/_next/static/chunks/0pz0_ubfbf-l_.js`, included
transfer-event selections for totalPartCount and uploadUrls.

A read-only GetAssetUploadUrls query against the completed 35-part asset
returned uploadUrls: null. Therefore this pass could not inspect a current
upload signature or test whether it permits a checksum header. No PUT was sent,
and no storage signature or query parameter was altered.

### What remains unverified

Amazon S3 supports Content-MD5 integrity checks for applicable PUT requests.
An incompatible signed request or unsupported storage path can reject added
headers. Generic S3 support alone does not establish support for Frame.io's
issued upload URLs.
See [AWS PutObject reference](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html).

Before enabling checksum-enforced uploads, run a separately authorized test
with a small disposable asset and fresh upload URLs:

1. Inspect the URL's operation shape and signed-header requirements without
   exposing signature values.
2. Test a deliberately incorrect checksum against only that disposable
   upload target. Confirm a checksum-specific rejection, not an unrelated
   signature or permission error.
3. Send the correct checksum with the same intended content and verify
   acceptance and completion.
4. Verify the resulting original and test multipart behavior separately.
5. Require checksum validation when enabled; never retry without the checksum
   merely to turn a rejected request into success.

Do not overwrite a completed asset or reuse a production part for the negative
test. This investigation does not authorize that future write test.

## Implementation status

The initial investigation added documentation only. A subsequent requested
implementation added `verify --etag` in the working checkout. It uses two
one-byte probes around the local hash pass, checks continuity, and reports a
distinct ETag result without setting the full-download `checksum_verified` flag.
See [ETag comparison](TRANSFERS.md#compare-the-original-etag) for the command
contract. Checksum-enforced upload headers remain unimplemented and unverified.

See [Web-client operation inventory](WEB-CLIENT-INVENTORY.md) for the separate
83-operation browser inspection.
