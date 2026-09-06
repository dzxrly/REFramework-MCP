# AccessPlan validation and collections

Starting with 1.1.0, `bind_access_plan` binds inputs by node ID and returns a new
`plan_ref`. The original plan stays available. Supply primitive values directly;
supply `{"object_ref": "obj:<id>"}` for an existing object, array, List, or Dictionary.
Use the returned `unresolved_nodes` to find remaining inputs.

~~~json
{
  "plan_ref": "<plan returned by find_access_paths>",
  "bindings": {"arg_0": 2, "arg_1": 10, "arg_2": false}
}
~~~

Primitive binding checks Boolean/String types, integer widths, and finite floating
point values. Supply enum values as integers; live validation checks the enum's
underlying type. Existing ObjectRefs are checked against the expected runtime
type during live validation. Binding does not allocate or populate game objects.

## Validate before execution

`validate_access_plan` reports symbolic and live results separately. A symbolic
result checks structure, options, and indexed MemberRefs; it is not evidence that
a game object exists. Live validation rejects incompatible casts, wrong types,
out-of-range values, unresolved inputs, and unsupported operations.

Intermediate methods execute only with `allow_getters: true` and an exact
canonical signature in `getter_allowlist`. Method prefixes and wildcards do not
authorize calls in an AccessPlan. For example, after reviewing this getter:

~~~json
{
  "plan_ref": "<bound plan>",
  "live": true,
  "allow_getters": true,
  "getter_allowlist": [
    "app.SaveDataManager::getCurrentUserSaveData() -> app.savedata.cUserSaveParam"
  ]
}
~~~

Target methods are validated as dry runs. The validation record binds the exact
receiver, MemberRef, and argument values for a later `invoke_method` request.
It cannot authorize a different receiver or changed arguments. Field writes
also retain payload approval and expected-old-value checks. Older validation
records must be recreated to obtain typed mutation bindings.

## Read bounded collections

`inspect_object` accepts `collection_offset` and `collection_limit` (1-1000).
With the matching 1.1.0 runtime, its `collection` response supports System.Array,
generic List, and generic Dictionary layouts recognized through REFramework
field metadata. It returns:

- `adapter`, `logical_count`, `offset`, and `limit`.
- `items` with zero-based storage `index` and `value`; dictionaries also include `key`.
- `next_offset` and `truncated`. Pass the returned cursor to the next request;
  a null cursor means the end.

List length excludes spare capacity. Dictionary length excludes deleted entries;
its cursor is a storage slot, not the number of returned pairs. Each dictionary
request scans at most 1000 slots, so a page may be empty and still have a cursor.
Concurrent game changes can change page contents; this is not an atomic snapshot.
Response byte limits may shorten a page while preserving a resumable cursor.

Managed values use leased ObjectRefs; primitive and String elements are returned
as scalar values. Unknown layouts fail explicitly. Older paired runtimes report
the collection capability as unavailable.

## Iterate, filter, and generate Lua

`iterate` takes `options.offset` and `options.limit` and produces a page of values.
Downstream reads, calls, and assertions apply to each selected value. `filter`
accepts `field` (optional), `operator` (`eq`, `ne`, `lt`, `le`, `gt`, `ge`), and
`value`; ordered comparisons require numbers. Filtering an existing sequence
keeps its source cursor and only filters that page.

`index` selects a zero-based array/List position, or a position in an existing
sequence. Dictionary key lookup is not supported by `index`; use paging.
Offsets are bounded at 1,000,000 in plans and 100,000 in `inspect_object`.

Generated Lua uses the same collection helpers and maps every selected element.
It requires the matching runtime's `probe` helpers and fails on unresolved inputs.
Code generation does not execute the plan; running a draft may execute its methods
and retains the Lua probe approval policy.

`bind_access_plan` returns an operation support matrix. Collection constructors
can be drafted when their inputs are explicit, but are not supported by the
side-effect-free live validator; bind an existing collection ObjectRef to use one
as a validated argument. Arbitrary object construction remains unsupported.
Lua integer literals are limited to signed 64-bit values; larger UInt64 values
are rejected during drafting. Native String arguments support UTF-8 text without
embedded NUL characters. Validation checks their encoding without allocating VM
strings; actual calls pass temporary strings directly to the selected method.

## Manual Wilds regression

With the matching runtime loaded and a save open, run:

~~~powershell
.\.venv\Scripts\python.exe scripts/validate_wilds_readonly.py --output .tmp/wilds-readonly-validation.json
~~~

This developer check uses the native Bridge transport. It reads box slots,
allocates temporary collections, compares native plans with generated Lua,
and checks isolated Probe cycles and native String arguments. It then waits 90
seconds to verify that leases expire and the same game runtime remains responsive
(`--settle-seconds` can extend this observation). It does not call a save mutation method.
MCP protocol tests and the regular unit/integration suite remain separate.
