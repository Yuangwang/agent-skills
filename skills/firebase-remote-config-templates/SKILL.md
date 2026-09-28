---
name: firebase-remote-config-templates
description: >-
  Manages server-side Firebase Remote Config templates (remote_config.json). Use
  ONLY when fetching, validating, or updating Remote Config parameters,
  targeting conditions, condition priority order, parameterGroups, or version
  rollbacks. For Android/iOS client SDKs, use firebase-remote-config-basics.
  Don't use for CLI login, Gemini/AI, Hosting, Auth, Firestore, or Xcode.
compatibility: >-
  Best used with the Firebase MCP Server (`remoteconfig_get_template`,
  `remoteconfig_update_template`), Firebase CLI
  (`npx -y firebase-tools@latest`), REST API v1, or Admin SDK.
metadata:
  category: ApplicationDevelopment
---

# Firebase Remote Config Server Template Management

> [!IMPORTANT] **Client SDK Setup & Specialist Subagents**
> - **Do NOT read `firebase-remote-config-basics`** for pure server template,
>   CLI, REST, or MCP tasks. Only read `firebase-remote-config-basics` when
>   adding Android/iOS `firebase-config` SDKs, XML/Plist defaults, or
>   `fetchAndActivate()`.
> - **Large Templates & Pre-Deploy Audits**: When subagents are enabled,
>   delegate large template surgery (`> 200` lines) to
>   `remote-config-template-architect` and pre-deploy checks to
>   `remote-config-auditor`.

This skill covers fetching, validating, authoring, deploying, and rolling back
Firebase Remote Config templates—including **parameters** (`parameters`),
**targeting conditions** (`conditions`), **condition evaluation order**, and
**parameter groups** (`parameterGroups`)—across the Firebase MCP Server,
Firebase CLI, REST API v1, and Firebase Admin SDK.

## Troubleshooting Execution

1. **Registry 403 Fallback**: If `npx -y firebase-tools@latest` fails with HTTP
   403, inform the user and fall back to the local `firebase` binary if
   installed globally (`npm install -g firebase-tools`).
1. **Missing Active Project**: Call `firebase_get_environment` (MCP) or run
   `npx -y firebase-tools@latest login:list`. If no project is active, ask the
   user for their `<PROJECT_ID>` and append `--project <PROJECT_ID>` to CLI
   commands.

## Existing Remote Config API Architecture (Atomic Read-Modify-Write)

> **CRITICAL**: The production Remote Config backend does **NOT** support
> standalone single-item CRUD endpoints or CLI subcommands (granular `v1`
> endpoints like `/v1/projects/.../remoteConfigParameters` or `.../parameters`
> return `HTTP 501 UNIMPLEMENTED`, and CLI commands like
> `firebase remoteconfig:parameters:create` do not exist). Instead, the entire
> configuration is a single versioned `RemoteConfig` template resource
> containing `"conditions"`, `"parameters"`, and `"parameterGroups"`.

Every modification to parameters, conditions, condition order, or parameter
groups **MUST** follow an **Atomic Read-Modify-Write (RMW)** workflow so that
existing configuration items and server-managed values are preserved:

### 1. Firebase CLI & MCP Server Workflow

- **If Firebase MCP Server is connected**: Use `remoteconfig_get_template` to
  retrieve the current template (or specific `version_number`), mutate the JSON
  in memory preserving all existing keys, ask the user for confirmation, and
  call `remoteconfig_update_template` to publish a new template or roll back.
- **Via Firebase CLI (`npx -y firebase-tools@latest`)**:
  1. **Fetch current template**:
     `npx -y firebase-tools@latest remoteconfig:get -o remote_config.json` (add
     `-v <VERSION>` for a specific version).
  1. **Modify & Validate in-place**: Mutate only the targeted entries in
     `"conditions"`, `"parameters"`, or `"parameterGroups"` while preserving all
     existing entries, then run `rc_template_validator.py --fix-order` on
     `remote_config.json` to verify `RC001`–`RC010` and sort keys.
  1. **MANDATORY: User Review and Verification**: STOP and ask the user to
     verify your changes before deployment:
     - *"I have prepared the changes in `remote_config.json`. Please review the
       file for accuracy. Once you are satisfied, tell me to 'deploy' to make
       the changes live."*
  1. **Deploy**: Ensure `firebase.json` maps `{ "remoteconfig": { "template":
     "remote_config.json" } }`, then run:
     `npx -y firebase-tools@latest deploy --only remoteconfig`
  1. **Inspect versions or Rollback**: List versions with
     `npx -y firebase-tools@latest remoteconfig:versions:list [--limit <N>]` or
     roll back with
     `npx -y firebase-tools@latest remoteconfig:rollback -v <VERSION_NUMBER>`.

### 2. REST API v1 & Admin SDK Workflow (Concurrency-Safe via `ETag`)

When automating via the REST API (`https://firebaseremoteconfig.googleapis.com`)
or Firebase Admin SDK (`firebase-admin`)—which is required for server templates
(`firebase-server` namespace) and dry-run validation:

- **Endpoints**:
  - Client (`firebase`): `/v1/projects/{project_id}/remoteConfig`
  - Server (`firebase-server`):
    `/v1/projects/{project_id}/namespaces/firebase-server/remoteConfig`
- **Step 1 (`GET` + Capture `ETag`)**: Send `GET` and record the `ETag` response
  header (or `admin.remoteConfig().getTemplate()`).
- **Step 2 (Pre-Flight Dry-Run Validation)**: Validate against server-side
  `InternalTemplateValidator` rules without publishing via
  `PUT ...?validate_only=true` with header `If-Match: <ETag>` (or
  `admin.remoteConfig().validateTemplate(template)`).
- **Step 3 (`PUT` with `If-Match`)**: Publish via `PUT` with header
  `If-Match: <ETag>` (or `publishTemplate(template)`). Never use `If-Match: *`
  unless intentionally overwriting concurrent edits.
- **Rollback / Defaults Export**: `POST .../remoteConfig:rollback` with body
  `{"version_number": "<VERSION>"}`, or export client defaults via
  `GET .../remoteConfig:downloadDefaults?format=XML|PLIST|JSON`.

## Critical User Journeys (CUJs) for Template Management

A complete Remote Config template has three top-level collections:
`"conditions": []`, `"parameters": {}`, and `"parameterGroups": {}` (plus
optional `"version": {"description": "..."}`).

### CUJ 1: Create, Update, and Delete Parameters (`parameters`)

Top-level ungrouped parameters live in `"parameters"` (`{ "<key>": <Param> }`).

#### Parameter Schema & Validation Rules

- **Parameter Key Format**: Must match `^[a-zA-Z_][0-9a-zA-Z_]*$` (starts with
  an ASCII letter or `_`, followed by ASCII letters, digits, or `_`; max `256`
  bytes). Hyphens (`-`), dots (`.`), spaces, and JavaScript `Object.prototype`
  keywords (`prototype`, `constructor`, `__proto__`, `toString`, `valueOf`,
  `hasOwnProperty`, `isPrototypeOf`, `propertyIsEnumerable`, `toLocaleString`,
  `__defineGetter__`, `__defineSetter__`, `__lookupGetter__`,
  `__lookupSetter__`) are **invalid**.
- **Global Key Uniqueness**: A parameter key must be unique across the entire
  template—it cannot exist in both top-level `"parameters"` and a group in
  `"parameterGroups"`, nor can it share the same name as any group key in
  `"parameterGroups"`.
- **`valueType` & Strict Format Validation (`PARAM_VALUE_TYPE_MISMATCH`)**:
  - `"STRING"` (default if omitted): Any UTF-8 string.
  - `"BOOLEAN"`: Case-insensitive `"true"` or `"false"`.
  - `"NUMBER"`: Must match `^-?(\d*\.)?\d+$` (integer or decimal; scientific
    notation like `"1e5"` or leading `"+"` fails validation).
  - `"JSON"`: Must be a string containing strictly valid JSON (trailing commas,
    unquoted keys, or comments are rejected).
- **String-Encoded Static Values**: Every static `"value"` inside `defaultValue`
  and `conditionalValues` **MUST** be a JSON string—even for booleans, numbers,
  and JSON objects (e.g., `"value": "true"`, `"value": "42"`,
  `"value": "{\"tier\":\"pro\"}"`). Raw non-string JSON primitives fail
  validation.
- **`RemoteConfigParameterValue` Oneof & `useInAppDefault` Rules**:
  - A value object is a `oneof` of static values (`{"value": "<string>"}` or
    `{"useInAppDefault": true}`) or dynamic server-managed values
    (`personalizationValue`, `rolloutValue`, `experimentValue`). Both
    `defaultValue` and individual `conditionalValues[<cond>]` entries can use
    `{"useInAppDefault": true}` (never `{"useInAppDefault": false}`, which fails
    with `INVALID_IN_APP_DEFAULT_VALUE`).
  - `defaultValue` **MUST** be a static value (`{"value": "..."}` or
    `{"useInAppDefault": true}`); setting `personalizationValue`,
    `rolloutValue`, or `experimentValue` on `defaultValue` fails with
    `INVALID_DEFAULT_PARAM_VALUE`.
  - When mutating a parameter, reordering conditions, or moving a parameter
    into/out of `parameterGroups`, **always preserve** any existing
    `personalizationValue`, `rolloutValue`, or `experimentValue` objects inside
    `conditionalValues`.
- **Limits**: Up to `3,000` parameters total (ungrouped + grouped),
  `description` up to `256` Unicode code points, `version.description` up to
  `1,024` code points, and `1,050,000` bytes total parameter value size.

#### Operations

1. **Create or Update a Parameter**: Add or update `<param_key>` under
   `"parameters"` (or directly under `"parameterGroups[<group>].parameters"`):

   ```json
   {
     "parameters": {
       "welcome_message": {
         "defaultValue": { "value": "Welcome to our app!" },
         "conditionalValues": { "ios_us_users": { "value": "Howdy!" } },
         "description": "Headline text on the onboarding screen",
         "valueType": "STRING"
       }
     }
   }
   ```

1. **Duplicate, Rename, Change `valueType`, or Edit `conditionalValues`**:
   - **Duplicate / Rename Parameter**: Deep-clone all fields under a new unique
     `<new_key>` to duplicate, or move `<old_key>` to `<new_key>` in its current
     container (`"parameters"` or its group) to rename in-place.
   - **Change `valueType`**: Update `valueType` and coerce `defaultValue` and
     all static `conditionalValues` to satisfy the new type's validation.
   - **Swap Condition or Delete Single `conditionalValue`**: Replace `<old_cond>`
     with `<new_cond>` in `conditionalValues` (preserving its value object) and
     re-sort keys by `"conditions"` priority, or delete a single condition key
     from `conditionalValues` without deleting the condition from `"conditions"`.
1. **Delete a Parameter**: Remove `<param_key>` from `"parameters"` (or from
   `"parameterGroups[<group>].parameters"`) and publish the updated template.

### CUJ 2: Create, Update, and Delete Conditions (`conditions`)

Targeting rules are defined in the top-level `"conditions"` list and referenced
by `name` inside parameter `"conditionalValues"` maps.

#### Condition Schema & Validation Rules

- **`name`**: Must match `^[-_0-9a-zA-Z '!%.]+$`, be unique, non-empty, max
  `256` characters, with **no leading or trailing spaces**. Up to `2,000`
  conditions per template.
- **`tagColor` (Optional)**: `"BLUE"`, `"BROWN"`, `"CYAN"`, `"DEEP_ORANGE"`,
  `"GREEN"`, `"INDIGO"`, `"LIME"`, `"ORANGE"`, `"PINK"`, `"PURPLE"`, or `"TEAL"`.
- **`expression` (Grammar & Conjunction-Only Structure)**:
  - **Conjunction Only (` && `)**: Remote Config enforces a
    `SINGLE_OR_MULTI_AND` condition tree structure (max 50 atoms). Combine
    clauses **only** with ` && ` (use spaces around `&&` to match server
    canonicalization). Top-level `||` (`Expected exactly one term in OR
    condition here`) and `!` negation (`NOT condition is not allowed here`) are
    **rejected**. Express OR logic via list operators (e.g.,
    `device.country in ['US', 'CA']`) or separate conditions in `"conditions"`,
    and negation via negative operators (`!=`, `notContains`,
    `notInAtLeastOne`, `notInAll`).
  - **Client templates (`firebase` namespace)** support:
    - Platform / Locale: `device.os == 'ios'` (`'android'`, `'ios'`, `'web'`),
      `device.country in ['US', 'CA']`, `device.language in ['en', 'es']`
    - App version / Build / ID / FID / First Open: `app.version.>=(['2.1.0'])`,
      `app.version.exactlyMatches(['2.1.0', '2.1.1'])`,
      `app.build.contains(['100'])`, `app.id == '1:123:android:abc'`,
      `firebaseInstallationId in ['fid_1']`,
      `firstOpenTimestamp >= dateTime('2025-01-01T00:00:00', 'UTC')`
    - Analytics Audiences & User Properties:
      `app.audiences.inAtLeastOne(['purchasers'])` (`inAll`, `notInAtLeastOne`,
      `notInAll`), `app.userProperty['tier'].exactlyMatches(['gold'])` (string
      methods: `exactlyMatches`, `contains`, `notContains`, `matches`; numeric
      infix: `app.userProperty['age'] >= 21`)
    - Custom Signals: String values **MUST** use method calls
      (`app.customSignal['vip'].exactlyMatches(['true'])`, `contains`,
      `notContains`, `matches`); infix `==`, `!=`, `<`, `<=`, `>`, `>=` only
      accept numeric literals (`app.customSignal['level'] >= 5`); semantic
      versions use `version(app.customSignal['client_ver']) >= '1.2.0'`
    - Time / Random Percentage:
      `dateTime >= dateTime('2025-06-01T00:00:00', 'America/Los_Angeles')`
      (top-level `dateTime`, **not** `device.dateTime`), `percent <= 10`,
      `percent('rollout_seed') between 0 and 25`
  - **Server templates (`firebase-server` namespace)** ONLY support `percent`
    and `app.customSignal` (always prefixed with `app.`, e.g.,
    `app.customSignal['region'].exactlyMatches(['us-east1']) && percent <= 20`).

#### Referential Integrity & Experiment Guardrail (`UNKNOWN_CONDITION_REFERENCE`)

- **Create or Duplicate a Condition**: Append or insert the condition into
  top-level `"conditions"` **before or in the same atomic update** that adds its
  `name` to any parameter's `"conditionalValues"`. To duplicate a condition,
  copy its `expression` and `tagColor` under a new unique `name`:

  ```json
  {
    "conditions": [
      { "name": "ios_us_users", "expression": "device.os == 'ios' && device.country in ['US']", "tagColor": "BLUE" }
    ]
  }
  ```

- **Rename a Condition**: If you change `condition.name` in `"conditions"`, you
  **MUST** atomically rename that key inside `"conditionalValues"` across
  **all** top-level `"parameters"` AND **all** `"parameterGroups[*].parameters"`.
- **Delete a Condition (and Experiment Guardrail)**: Never delete a condition
  referenced by an active `experimentValue` in any parameter's
  `"conditionalValues"`. Otherwise, when removing a condition from
  `"conditions"`, you **MUST** remove its key from `"conditionalValues"` in
  **every** parameter across `"parameters"` and
  `"parameterGroups[*].parameters"` (`UNKNOWN_CONDITION_REFERENCE`).

---

### CUJ 3: Condition Order & Evaluation Precedence

In Remote Config, the array order of `"conditions"` directly controls which
value a user receives when multiple conditions match.

#### 1. First-Match-Wins & Specific-Before-Broad Precedence

- The top-level `"conditions"` array is evaluated in **strict descending
  priority from index `0` (highest priority) to index `N - 1` (lowest
  priority)**, returning the value of the **first** matching condition in the
  parameter's `"conditionalValues"` (or `defaultValue` if none match).
- **Specific-Before-Broad Rule**: Always place narrower intersection conditions
  at a **lower index (earlier in `"conditions"`)** than broader superset
  conditions. If `"all_ios"` (`device.os == 'ios'`) were placed at index `0`
  above `"ios_us_vip"` (`device.os == 'ios' && device.country in ['US']`), any
  parameter defining both would always resolve to `"all_ios"`, shadowing
  `"ios_us_vip"`.

#### 2. Reordering Conditions & Preserving `conditionalValues` Key Order

> **Canonical `conditionalValues` Key Order (`CONDIITION_ORDERING_INVALID`)**:
> Internally, Remote Config stores `conditional_value` as an ordered list and
> enforces `CONDIITION_ORDERING_INVALID` on internal/UI template paths whenever
> a parameter's condition keys do not match the relative order of
> `"conditions"`. Always keep the keys of every `"conditionalValues"` map (in
> both `"parameters"` and `"parameterGroups[*].parameters"`) sorted in the
> **exact relative order** of `"conditions"` (while preserving each condition's
> value object, including `personalizationValue`, `rolloutValue`, or
> `experimentValue`):

```json
{
  "conditions": [
    { "name": "ios_us_vip", "expression": "device.os == 'ios' && device.country in ['US']" },
    { "name": "all_ios", "expression": "device.os == 'ios'" }
  ],
  "parameters": {
    "promo_discount_pct": {
      "defaultValue": { "value": "0" },
      "conditionalValues": {
        "ios_us_vip": { "value": "30" },
        "all_ios": { "value": "10" }
      },
      "valueType": "NUMBER"
    }
  }
}
```

---

### CUJ 4: Create and Manage Parameter Groups (`parameterGroups`)

Parameter groups organize related parameters into named folders in the Firebase
Console and template JSON under the top-level `"parameterGroups"` map.

#### Parameter Group Schema & Validation Rules

- **Structure**: Top-level `"parameterGroups"` maps each `<group_name>` (`1` to
  `256` chars, unique, no leading/trailing spaces, max `1,000` groups) to
  `{ "description": "...", "parameters": { "<param_key>": <Parameter> } }`.
- **Single-Location & Global Key Uniqueness (`DUPLICATE_KEY`)**:
  1. A `<param_key>` can exist in **at most ONE place** in the entire template:
     either in top-level `"parameters"` **OR** inside a single
     `"parameterGroups[<group_name>].parameters"`—**NEVER** both, and **NEVER**
     in multiple groups.
  1. Group names and parameter keys share the **same global namespace** on the
     backend validator: a parameter key **cannot** equal any group name in
     `"parameterGroups"` (`DUPLICATE_KEY`).
- **Client & Server SDK Access Is Always Flat**:
  Parameter groups are purely organizational in the template and Console. Client
  and server SDKs always read grouped parameters by their bare `<param_key>`
  (e.g., `remoteConfig.getString("checkout_button_color")`), **never** prefixed
  with the group name (`"checkout_flow.checkout_button_color"` is invalid).

#### Operations

1. **Create or Edit a Parameter Group**: Create `<group_name>` under
   `"parameterGroups"`, or rename `<old_group>` to `<new_group>` / update its
   `"description"` while keeping its child `"parameters"` intact:

   ```json
   {
     "parameterGroups": {
       "checkout_flow": {
         "description": "Parameters controlling cart and payment screens",
         "parameters": {
           "checkout_button_color": {
             "defaultValue": { "value": "#1A73E8" },
             "conditionalValues": { "ios_us_users": { "value": "#0D47A1" } },
             "valueType": "STRING"
           }
         }
       }
     }
   }
   ```

1. **Move Parameters into a Group or Between Groups**: In a **single atomic
   template update**, remove `<param_key>` from its current location
   (`"parameters"` or `"parameterGroups[<old_group>].parameters"`) and insert it
   (preserving all fields) into `"parameterGroups[<new_group>].parameters"`.
1. **Ungroup Single Parameter, Ungroup All, or Delete Group**: Move a single
   parameter (keeping the group intact) or all child parameters from
   `"parameterGroups[<group>].parameters"` back into top-level `"parameters"`,
   and remove `"<group>"` from `"parameterGroups"` when deleting the group.
