# Web-client operation inventory

Evidence date: October 1, 2026.

This is a partial inventory of requests observed in an authorized Frame.io web
session, not a complete GraphQL schema or a supported public API contract.
Operation names identify client documents; they are not server endpoint names.
Different documents can use the same operation name.

## Collection method and scope

The inspection opened the home page, an existing project, nested folders, an
existing audio-classified BRAW viewer, and the viewer's **Fields** panel.
Listeners captured operation documents and variable names. The saved inventory
contains declarations, response root keys, HTTP status, and GraphQL error codes.
It excludes bearer tokens, cookies, signed URLs, variable values, asset IDs,
account IDs, user details, and response content.

There are 83 distinct observed operation names: 81 queries and two mutations.
Response evidence was captured for 80 operations. The other three were requested
before the response listener was installed; they are not marked successful here.
For captured responses, HTTP 200 and no GraphQL errors establish only that the
observed request succeeded, not that every optional selection returned data.

No upload, download button, comment submission, field edit, share creation, or
deletion was initiated. Ordinary UI navigation automatically emitted
`UpsertGdprConsentCategories` and `MarkAssetSeen`. These are recorded as
browser-generated side effects, not read operations or mutations to replay.

## Useful API boundaries

| Area | Observed operations | What the documents establish |
| --- | --- | --- |
| Identity and routing | Me, Authorization, GetAccountsForRouting | Identity and account routing reads |
| Workspace and project navigation | GetWorkspacesBasic, GetWorkspacesForNavigation, GetAllProjects, GetProjectRootIds | Navigation requests and their input types; some list responses were not captured |
| Folder listing | GetFolderAssets, HydrateScrolledAssets, HydrateSettledAssets | IDs and ordering arrive first; separate queries hydrate asset data |
| Permissions | GetProjectPermissions, GetFolderPermissions, GetFolderRestrictions, GetAccountPermissions | UI reads permissions and restrictions separately |
| Metadata | GetProjectFieldDefinitions, GetProjectFieldGroups, GetAggregateDataByAssetIds, GetAssetsForViewer | Field definitions, groups, aggregate data, and asset field selections |
| Viewer and versions | GetAssetsForViewer, GetAssetVersionIds, GetAssetParentId | Type-specific viewer fragments and VersionStackAsset version IDs |
| Comments | GetCommentsByAsset | Filters, ordering, replies, search, timestamps, attachments, and annotations are selected; populated content was not validated |
| Transcripts | GetTranscriptionStreamUrl, GetAssetsForViewer | Read paths for transcript resources; no transcription job was started |
| Collections and shares | GetCollectionsForProject, GetSharesForProjectNavigation | Existing collection and share-list reads, not creation or edit contracts |
| Upload history | GetTransferBatchesForAccount | Batch history reads; no new upload was initiated |

### Folder listing differs from the existing reduced client query

The October web document selects
`account(by: {id: $accountId}).asset(id: $folderId)`, then a
`FolderAsset.assets(assetType, customSort, page, sortBy)` connection.
Nodes contain `id` and `index`; `pageInfo` contains `endCursor` and
`hasNextPage`. The existing Python client uses a different, previously verified
`folderAssets(page, query)` root. Do not replace that implementation merely
because this newer document has the same operation name.

### Comments and versions need broader validation

The web comments document requests page 1 with a page size of 10,000. This does
not establish an unlimited export contract. The existing client uses bounded
pagination. The document includes nested reply selections, but this inspection
does not validate populated threads, annotations, or attachment export.

`GetAssetVersionIds` selects version IDs only inside a `VersionStackAsset`
fragment. A successful response for a non-stack asset does not validate a
populated stack.

## Observed declarations

The following declarations preserve variable types and defaults. They are not
complete executable queries; selection sets and fragments are intentionally
omitted. “200; no errors” means the last captured response had HTTP 200 and no
GraphQL error entries. Root keys are response keys and can include aliases.

### Queries

- `query Me`
  - Evidence: 200; no errors. Response roots: `me`.
- `query GetAccountsForRouting($first: NonNegativeInt = 500, $signup: Boolean)`
  - Evidence: 200; no errors. Response roots: `me`.
- `query GetHasSearchEnabled($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetTransferBatchesForAccount($accountId: ID!, $pagination: PaginationInput, $assetPagination: PageInput!)`
  - Evidence: 200; no errors. Response roots: `transferBatchesForAccount`.
- `query GetAccountLock($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetAllAccountsForPrimaryNavPanel`
  - Evidence: 200; no errors. Response roots: `me`.
- `query GetWorkspacesBasic($accountId: ID!, $page: PageInput!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetAccountLevelMetadataEnabled($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetAvailableCustomFieldCount($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetCanManageBillingPermission($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetAccountFioVersionMigrationStartedAt($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `fioVersionMigration`.
- `query GetAccountFioVersionMigrationStatus($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `fioVersionMigration`.
- `query GetAccountWelcomeProjectId($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetAccountGroupTraits($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetNotificationGroups($accountId: ID!, $page: PageInput!, $unreadOnly: Boolean)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetWhatsNewFeedLastUpdatedTime($locale: SupportedLocale)`
  - Evidence: 200; no errors. Response roots: `whatsNewFeed`.
- `query GetFeatureGateSuggestions($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetFeatureFlags($accountId: ID!, $flagNames: [String!]!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetAccessRequests($accountId: ID!, $page: PageInput!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetUnopenedNotificationGroupsCount($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetResourceControlPolicies($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetSubscriptionId($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetAccountUsage($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetAccountInfoForWorkspaces($accountId: ID!)`
  - Evidence: request observed; response not captured.
- `query GetTrial($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetWorkspacesForNavigation($accountId: ID!, $page: PageInput!, $filter: WorkspaceFilterInput, $order: WorkspaceOrderInput)`
  - Evidence: request observed; response not captured.
- `query GetAllProjects($accountId: ID!, $page: PageInput!, $filter: AccountProjectFilter, $orders: [ProjectOrderInput!])`
  - Evidence: request observed; response not captured.
- `query GetAccountIdByProject($projectId: ID!)`
  - Evidence: 200; no errors. Response roots: `project`.
- `query Authorization`
  - Evidence: 200; no errors. Response roots: `me`.
- `query GetProjectAssetLifecyclePolicy($projectId: ID!)`
  - Evidence: 200; no errors. Response roots: `project`.
- `query GetProjectForProjectSwitcher($projectId: ID!)`
  - Evidence: 200; no errors. Response roots: `project`.
- `query GetAiAssistantStatus($projectId: ID!)`
  - Evidence: 200; no errors. Response roots: `project`.
- `query GetAssetForHeader($assetId: ID!)`
  - Evidence: 200; no errors. Response roots: `asset`.
- `query GetFolderForInvites($id: ID!)`
  - Evidence: 200; no errors. Response roots: `asset`.
- `query GetProjectPermissions($projectId: ID!)`
  - Evidence: 200; no errors. Response roots: `project`.
- `query GetRootCollectionForProject($projectId: ID!)`
  - Evidence: 200; no errors. Response roots: `project`.
- `query GetProjectFieldGroups($projectId: ID!)`
  - Evidence: 200; no errors. Response roots: `project`.
- `query GetRolesForResources($accountId: ID!, $locale: SupportedLocale)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetProjectFieldDefinitions($projectId: ID!, $query: String, $excludeSystemFields: Boolean)`
  - Evidence: 200; no errors. Response roots: `project`.
- `query GetFolderPermissions($folderId: ID!)`
  - Evidence: 200; no errors. Response roots: `asset`.
- `query GetProjectForNavigation($projectId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetAssetForNavigation($assetId: ID!)`
  - Evidence: 200; no errors. Response roots: `asset`.
- `query GetAncestorFolders($assetId: ID!)`
  - Evidence: 200; no errors. Response roots: `asset`.
- `query GetProjectNotificationStatus($accountId: ID!, $projectId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetProjectNotificationOverrideStatus($accountId: ID!, $projectId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetDirectMembershipsForFacepile($resourceId: ID!)`
  - Evidence: 200; no errors. Response roots: `directMemberships`, `indirectMembershipsTotalCount`, `resource`.
- `query GetResource($id: ID!)`
  - Evidence: 200; no errors. Response roots: `resource`.
- `query GetMountedStorageSettings($accountId: ID!, $projectId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetProjectAppliedPolicies($projectId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetFolderRestrictions($id: ID!)`
  - Evidence: 200; no errors. Response roots: `asset`.
- `query GetPlans($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetProjectSelectMemberOptions($projectId: ID, $shareId: ID)`
  - Evidence: 200; no errors. Response roots: `projectSelectMemberOptions`.
- `query GetCollection($id: ID!)`
  - Evidence: 200; no errors. Response roots: `collection`.
- `query GetFolderStats($folderId: ID!)`
  - Evidence: 200; no errors. Response roots: `folder`.
- `query GetAccountPermissions($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetAccountFieldManagementPermission($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetWorkspaceActions($workspaceId: ID!, $pageInput: PageInput!, $orders: [ActionOrderInput!])`
  - Evidence: 200; no errors. Response roots: `account`.
- `query ProjectConnectedToLightroom($projectId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetAuthenticationMethod`
  - Evidence: 200; no errors. Response roots: `me`.
- `query GetProjectRootIds($projectId: ID!)`
  - Evidence: 200; no errors. Response roots: `project`.
- `query GetWorkspaceInteractiveAssetSettings($workspaceId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetWorkspacePermissionsForInteractiveEligibleDialog($workspaceId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query HydrateProjects($accountId: ID!, $projectIds: [ID!]!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetLabsExperimentEnrollments($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetFolderAssets($accountId: ID!, $folderId: ID!, $assetType: ChildAssetTypeInput, $customSort: Boolean, $page: PageInput!, $sortBy: [MetadataSortByInput!])`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetCollectionsForProject($projectId: ID!, $order: CollectionsOrderInput! = {field: INSERTED_AT, direction: DESC_NULLS_LAST}, $page: PageInput! = {first: 50})`
  - Evidence: 200; no errors. Response roots: `project`.
- `query GetUnopenedInboxItemCountForPrimaryNav($accountId: ID!)`
  - Evidence: 200; no errors. Response roots: `me`.
- `query GetProjectTemplates($workspaceId: ID!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetSharesForProjectNavigation($projectId: ID!, $page: PageInput!, $orderBy: [ShareOrderInput!], $filters: ShareFiltersInput)`
  - Evidence: 200; no errors. Response roots: `project`.
- `query HydrateScrolledAssets($ids: [ID!]!, $includeDeleted: Boolean, $visibleFieldIds: [ID!])`
  - Evidence: 200; no errors. Response roots: `assets`.
- `query HydrateSettledAssets($ids: [ID!]!)`
  - Evidence: 200; no errors. Response roots: `assets`.
- `query MountedStorageAsset($assetId: ID!)`
  - Evidence: 200; no errors. Response roots: `asset`.
- `query GetAssetVersionIds($assetId: ID!)`
  - Evidence: 200; no errors. Response roots: `asset`.
- `query GetCommentsByAsset($filters: CommentFilters, $orderBy: [CommentOrder!]!, $includeReplies: Boolean, $assetId: ID!, $searchTerm: String) @stewardship(stewards: [VIEWER])`
  - Evidence: 200; no errors. Response roots: `asset`.
- `query GetAggregateDataByAssetIds($assetIds: [ID]!)`
  - Evidence: 200; no errors. Response roots: `aggregateData`.
- `query GetAncestorFolderIds($id: ID!)`
  - Evidence: 200; no errors. Response roots: `asset`.
- `query GetAssetParentId($assetId: ID!)`
  - Evidence: 200; no errors. Response roots: `asset`.
- `query GetAssetsForViewer($assetIds: [ID!]!, $sortBys: [MetadataSortByInput!] = [], $locale: SupportedLocale) @stewardship(stewards: [VIEWER])`
  - Evidence: 200; no errors. Response roots: `assets`.
- `query GetTranscriptionStreamUrl($assetId: ID!) @stewardship(stewards: [VIEWER])`
  - Evidence: 200; no errors. Response roots: `asset`.
- `query GetAssetThumbnails($accountId: ID!, $assetIds: [ID!]!)`
  - Evidence: 200; no errors. Response roots: `account`.
- `query GetGeoFeatures`
  - Evidence: 200; no errors. Response roots: `me`.

### Browser-generated mutations

- `mutation UpsertGdprConsentCategories($input: UpsertGdprConsentCategoriesInput!)`
  - Evidence: 200; no errors. Response roots: `upsertGdprConsentCategories`.
- `mutation MarkAssetSeen($input: MarkAssetSeenInput!)`
  - Evidence: 200; no errors. Response roots: `markAssetSeen`.

## Coverage limits

Schema introspection was not performed in this pass. The inventory excludes
unvisited UI flows, feature-gated behavior, most mutation contracts, upload
worker-only operations, and operations loaded in other accounts or asset types.
A field appearing in a document is evidence of a selection, not a guarantee of
access or a non-null value.

Keep this inventory separate from the client's mutation allowlist. New writes
need explicit scope, implementation review, and controlled live validation.
See [Upload integrity investigation](UPLOAD-INTEGRITY-INVESTIGATION.md) for
the one-byte original probe and checksum findings.
