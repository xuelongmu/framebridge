# API evidence and limitations

Evidence dates: September 20–October 1, 2026. This implementation follows authenticated
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
| User, project, folder permissions | Read operations verified | Reduced selections live-tested September 21; account is under project.workspace; permissions require FolderAsset fragment |
| Folder listing | All-child offset pagination verified on October 2 | `matchingChildren` includes folders and files; metadata hydration remains bounded to 100 IDs |
| CreateTransferBatch | Tiny standalone upload verified | One batch per file |
| AddAssetsToTransferBatch | Tiny standalone upload verified | One existing destination folder per asset |
| Signed S3 PUT | Tiny single-part upload reached TRANSCODED | Streamed upload; bounded same-part retries |
| GetAssetUploadUrls | Seen in web-client source | Offset retrieval verified across the completed 35-part live transfer |
| UpdateTransferBatches | SUCCEEDED update verified | Only after server asset completion |
| Multipart upload | Part sizing seen in client source | 68.1 GB / 35-part upload completed; forced process restart retained the same asset and resumed; server status TRANSCODED |
| Proxy downloads | Signed media URLs and bounded GET ranges verified | Resumable range downloader; 1 MiB CLI live sample verified September 23; full remote proxy transfer pending |
| Account, workspace, project navigation | Reduced queries live-tested September 23 | Read-only commands |
| Comments and versions | Comment query captured from web app; empty export tested | Read-only export; populated comments and version stacks need live validation |
| Folder creation | CreateFolder document and input construction observed in client source | Journaled mkdir and recursive upload; five live test folders created October 1 |
| Same-project moves | MoveAssets document and input construction observed in client source | Single-asset/folder moves live-tested; restricted paths/subtrees and cross-project moves refused |
| Rich metadata, stats, permissions | Reduced selections from captured web documents | Live reads verified October 1, including 20 BRAW field values |
| Transcript export | Transcript formats and readiness selected by the web client | Paginated listing and bounded export implemented; empty live listing and offline export tests only |
| ETag comparison | One-byte original ranges and compatible MD5 ETags observed | Explicit verification mode, separate from full-download SHA-256 evidence |
| Sharing, deletion, rename | Outside the approved scope | Not implemented |

The two discovery upload tests were tiny disposable files and were cleaned up.
They establish the protocol, not end-to-end validation of this Python client.
On September 21, this client completed a 68.1 GB transfer across all 35 parts after
a forced process interruption and restart. The journal reached `complete`, the
process exited successfully, and an independent status query returned `TRANSCODED`.
No full-original download-and-checksum comparison was performed for that BRAW.
On October 1, its multipart ETag matched a complete local-source calculation
using the original 35-part boundaries. This is conditional MD5 integrity evidence,
not a remote SHA-256 comparison. These tests do not establish every failure mode
or storage backend.
The offline suite now includes a real loopback HTTP transfer whose reassembled
bytes match the synthetic source after interruption and journal reopening.

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

Opt-in concurrent folder/manifest uploads batch status reads for up to 100 asset
IDs, then perform the existing per-asset verification before completion. One
coordinator owns the API client and journal writes. Opt-in parallel parts
use a shared transfer cap and request windows of up to four signed URLs, checking
their part numbers before sending data. On the verified staged-object S3 flow,
the signed query uses `x-amz-meta-part_number` (one-based), not native S3
`partNumber`. The client accepts either field, rejects conflicting or repeated
values, and checks each number against the requested zero-based offset. Missing
numbers in multi-URL windows are refused. Regression coverage is in
`test_staged_part_metadata_numbers`.
Each file still has its own
transfer batch, and ambiguous creations are never automatically replayed.

On October 2, a three-file synthetic upload (11,542,736 bytes) was force-stopped
after a successful part was durably recorded. Concurrent resume retained all
three asset IDs, and a subsequent repeat sent zero upload bytes. Full original
downloads matched local SHA-256 hashes. Batched status reads succeeded live.
All three files received a single part. A subsequent 2,075,169,730-byte ZIP test
received 35 parts and verified multi-URL windows with two concurrent PUTs. After
a hard process exit with one acknowledged part and another in flight, resume
retained the asset ID, skipped the acknowledged part, and completed the remaining
34 parts. The original's 35-part ETag matched the local calculation, and a full
original download matched the source SHA-256. A repeat sent zero bytes and made
no new assets or batches. Part count
is server-assigned; do not infer a fixed part-size threshold from these fixtures.

### Folder listing must include folders

The legacy `folderAssets` query and `FolderAsset.assets` without an explicit
asset-type filter returned files but omitted child folders in the live fixture.
Do not use those default results for recursion or folder-name conflict checks.
The client uses `FolderAsset.matchingChildren(filters: [], flattenFolders: false,
page: {mode: OFFSET, afterOffset: ..., first: ...}, sortBys: [])` instead.
Its offset pagination was verified with two-item pages against a mixed folder.
Recursive traversal returned all three subfolders and three files, and an empty
journal discovered an existing folder for reuse. Tests enforce count continuity,
unique IDs, advancing offsets, and complete traversal. These checks detect some
concurrent changes, not an atomic snapshot; independent writers remain unsupported.

Completion now also requires matching asset size, project, and parent folder.
The private `UploadEvidence` query selects `id`, `name`, `status`, `filesize`,
`project { id }`, and `parent { id }`. It does not request signed URLs or require
original-download access. This query was verified against the existing BRAW and
a synthetic WAV on October 1, 2026. Size metadata is not a server-side checksum.

The public V4 API exposes [file metadata](https://next.developer.frame.io/platform/api-reference/files/show)
and [upload status](https://next.developer.frame.io/platform/api-reference/files/show-file-upload-status),
including `file_size` and `upload_complete`/`upload_failed`. Those documented
responses do not provide a file checksum. This adapter continues to use the
private GraphQL API and does not assume browser credentials work with public REST.

## Integrity validation and multipart release criteria

On October 1, 2026, the strengthened uploader transferred a synthetic WAV of
12,582,956 bytes. Frame.io assigned **one part**. Metadata verification succeeded;
the full original was then downloaded from `assets.frame.io` with bounded ranges.
Its SHA-256 matched the source:
`6dd590f74d1925d45886f2271d03b1c657cf4d4867bc04c6f2281cc9f6346a61`.
This establishes an original round trip for that file, not multipart coverage.
The synthetic test asset remains in the user-approved destination folder.

The earlier 68.1 GB BRAW's remote size was compared with the local file and matched
68,087,113,964 bytes. A tiny original range exposed an ETag matching the local
multipart MD5 calculation. Its full original was **not** downloaded for SHA-256
comparison. See [Upload integrity investigation](UPLOAD-INTEGRITY-INVESTIGATION.md).
The offline multipart HTTP test covers interruption and resume with reassembled
bytes matching the source; new fault tests cover remote metadata mismatch,
unavailable/deleted assets, source mutation with preserved size and timestamp,
short request consumption, and verification retry without duplicate creation.

S3 multipart is enabled by default following the October 2 ZIP test, which passes
the live multipart original-download hash, interruption/resume, parallel-transfer,
and duplicate-free repeat gates. The deprecated `--experimental-multipart` flag
and Python `experimental` keyword remain accepted as no-ops for compatibility.
Concurrency still requires explicit worker settings; CLI defaults remain
sequential. Broader account and file-type coverage remains a known limitation:
one ZIP on one account does not establish universal compatibility. Other storage
backends remain unsupported. Promotion does not change private-API risk, source
checks, journal recovery, permission checks, or refusal of ambiguous creations.

## Folder and move contracts

`CreateFolder(input: CreateFolderInput!)` receives `parentId`, `name`, and
`restricted: false`. The reduced response selects the created asset's ID, name,
type, project, and parent. This matches the web-client input construction; the
CLI confirms the returned folder identity with a fresh read before marking its
intent complete. A lost response is not replayed. Explicit adoption can reconcile
a reviewed folder ID after validating the same name, project, and parent.

`MoveAssets(input: MoveAssetsInput!)` receives one `assetIds` element, `parentId`,
and `movePrivateCommentsToNewWorkspace: false`. The CLI refuses cross-project
moves and checks project `canMoveAsset`, source-folder `canMoveChildren`, and
destination-folder creation/listing permissions. Restricted ancestry/subtrees,
cycles, root moves, and name conflicts fail preflight. A read of the resulting
parent confirms completion. Async/bulk move operations are not implemented.

These documents were found in the loaded October 1 client bundle
`/_next/static/chunks/3x4nloikizte_.js`. The reduced mutations were then validated
with newly created disposable test folders/files only. The web bundle name is
evidence, not a runtime dependency.

Recursive upload composes this folder contract with the existing per-file upload
contract. It does not use an unverified bulk-tree mutation. Folder IDs live in
`operations.sqlite3`; file upload IDs and parts remain in `uploads.sqlite3`.
The client has no verified remote idempotency key or unique-name constraint, so
it cannot prevent races with independent writers. Do not concurrently modify the
same remote destination tree through other profiles or applications.

## Recovery and scope

Creation mutations are never automatically retried. A write-ahead journal state
blocks duplicate creation after an uncertain response. Reads have bounded
transient retries; storage retries target the same asset and part. Neither
server-side cleanup nor automatic recovery of ambiguous creation is implemented.
Folder creation supports explicit reviewed-ID adoption, not automatic name-based
reconciliation. Move reconciliation checks the asset's parent before declaring a
previous request complete; unresolved moves are not automatically replayed.

Permission checks are authoritative for the current session only. Discovery
showed upload permission did not imply download, sharing, or permission-management
rights. Downloads require an authorized session; sharing and permission changes
are not implemented. GraphQL introspection
was disabled; do not depend on it for runtime discovery.

Public integration reference: [Adobe Frame.io developer documentation](https://developer.adobe.com/frameio/).
This private adapter is unsupported and must be revalidated when the web client
changes. Prefer supported public V4 OAuth if Developer Console setup becomes viable.

For download recovery, batch planning, safety restrictions, and validation limits,
see [Transfer and inspection commands](TRANSFERS.md). Downloads use the media
`videoTranscodes` fields, including `downloadUrl`, `encodeStatus`, key, dimensions,
and byte size. A HEAD request returned 404 in discovery; GET Range returned 206.
No credentials or signed media URLs are persisted in this documentation.
