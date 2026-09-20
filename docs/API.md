# API evidence and limitations

Evidence date: September 20, 2026. This implementation follows authenticated
requests observed in the user's own Frame.io web session. It does not bypass
resource permissions or use another application's OAuth registration.

## Authentication

Requests use `POST https://api.frame.io/graphql`, with `operationName`, `query`,
and `variables`. Supply the bearer token plus `apollographql-client-name` and
`apollographql-client-version` observed from the web app. Bearer-only requests
returned HTTP 403 during discovery. HTTP 200 can contain GraphQL errors.

`cycleRefreshToken(input: CycleRefreshTokenInput!)` accepts `accessToken`,
`refreshToken`, and `sessionToken`, and returns rotated token values plus Unix
expiration seconds. Renewal was tested separately from this new CLI. The client
persists rotated credentials before continuing. It does not replay mutations on
an authentication failure. Browser session credentials were not accepted by the
public V4 REST endpoint in our tests.

## Operation coverage

| Operation | Discovery evidence | Implementation status |
| --- | --- | --- |
| User, project, folder permissions | Read operations verified | Reduced selections implemented; fresh live check pending |
| Folder cursor pagination | Filtered listing verified | General listing implemented; empty sort/filter variant pending |
| CreateTransferBatch | Tiny standalone upload verified | One batch per file |
| AddAssetsToTransferBatch | Tiny standalone upload verified | One existing destination folder per asset |
| Signed S3 PUT | Tiny single-part upload reached TRANSCODED | Streamed upload; bounded same-part retries |
| GetAssetUploadUrls | Seen in web-client source | Offset URL retrieval still needs live validation |
| UpdateTransferBatches | SUCCEEDED update verified | Only after server asset completion |
| Multipart upload | Part sizing seen in client source | Experimental opt-in; no live multipart test |
| Folder creation, sharing, downloads | Not established for this workflow | Not implemented |

The two discovery upload tests were tiny disposable files and were cleaned up.
They establish the protocol, not end-to-end validation of this Python client.

## Upload contract

Create a transfer batch with account ID, name, one top-level file, zero folders,
and `uploadedVia: BROWSER`. Add an asset using local ID `0`, filename, `type: file`,
parent folder ID, byte size, and MIME type. Read the returned asset ID and total
part count. Obtain signed part URLs without storing them in the journal.

Storage PUTs use `x-amz-acl: private` and the MIME type. They must not receive
Frame.io authorization headers. The client permits only HTTPS S3 hosts and
disables redirects. For multiple parts, the observed width is the greater of
5 MiB and `ceil(file_size / total_part_count)`; the final part contains the rest.

Default S3 uploads complete automatically. `FinalizeUpload` failed for our test
asset and belongs to another storage flow, so this client does not call it.
The client polls for UPLOADED or TRANSCODED, then marks the batch SUCCEEDED.

## Recovery and scope

Creation mutations are never automatically retried. A write-ahead journal state
blocks duplicate creation after an uncertain response. Reads have bounded
transient retries; storage retries target the same asset and part. Neither
server-side cleanup nor automatic recovery of ambiguous creation is implemented.

Permission checks are authoritative for the current session only. Discovery
showed upload permission did not imply download, sharing, or permission-management
rights. The implementation does not attempt those actions. GraphQL introspection
was disabled; do not depend on it for runtime discovery.

Public integration reference: [Adobe Frame.io developer documentation](https://developer.adobe.com/frameio/).
This private adapter is unsupported and must be revalidated when the web client
changes. Prefer supported public V4 OAuth if Developer Console setup becomes viable.
