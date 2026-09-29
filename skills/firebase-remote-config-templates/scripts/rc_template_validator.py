#!/usr/bin/env python3
"""Offline validator and canonicalizer for Firebase Remote Config templates.

Enforces 100% parity with the Firebase Console web validators
(`config/ng2/common/constants.ts`, `data_type_validator.ts`,
`parameter_name_validator.ts`, `reserved_keywords.ts`,
`condition_name_editor.ts`, `parameter_group_edit.ts`,
`parameter_editor_store.ts`, `config_template_store.ts`,
`remote_config_service.ts`, and `shared/targeting/rx/details/*`) across
RC001-RC009 plus logical condition precedence shadowing detection (RC010).
Supports deterministic auto-remediation for conditionalValues key ordering
(--fix-order), raw JSON primitive value coercion (--fix-types), and dangling
condition reference removal (--prune-dangling).
"""

import argparse
import dataclasses
import json
import re
import sys
from typing import Any

# config/ng2/common/constants.ts & condition_name_editor.ts & data_type_validator.ts
PARAMETER_NAME_REGEX = re.compile(r'^[a-zA-Z_][0-9a-zA-Z_]*$')
CONDITION_NAME_REGEX = re.compile(r"^[-_0-9a-zA-Z '!%.]+$")
NUMERIC_REGEX = re.compile(r'^-?(\d*\.)?\d+$')

# config/ng2/common/reserved_keywords.ts (FORBIDDEN_KEYS)
RESERVED_JS_KEYWORDS = frozenset({
    '__proto__',
    'constructor',
    'prototype',
    'hasOwnProperty',
    'isPrototypeOf',
    'propertyIsEnumerable',
    'toLocaleString',
    'toString',
    'valueOf',
    '__defineGetter__',
    '__defineSetter__',
    '__lookupGetter__',
    '__lookupSetter__',
})

# config/ng2/common/models.ts (ConditionColor) & remote_config_service.ts
VALID_TAG_COLORS = frozenset({
    'BLUE',
    'BROWN',
    'CYAN',
    'DEEP_ORANGE',
    'DEEPORANGE',
    'GREEN',
    'INDIGO',
    'LIME',
    'ORANGE',
    'PINK',
    'PURPLE',
    'TEAL',
    'CONDITION_DISPLAY_COLOR_UNSPECIFIED',
})

VALID_VALUE_TYPES = frozenset({'STRING', 'BOOLEAN', 'NUMBER', 'JSON'})
VALUE_ONEOF_FIELDS = (
    'value',
    'useInAppDefault',
    'personalizationValue',
    'rolloutValue',
    'experimentValue',
)
DYNAMIC_VALUE_FIELDS = (
    'personalizationValue',
    'rolloutValue',
    'experimentValue',
)

# config/ng2/common/constants.ts
MAX_PARAMETER_NAME_LENGTH = 256
MAX_PARAMETER_DESCRIPTION_LENGTH = 256
MAX_PARAMETER_COUNT = 3000
MAX_PARAMETER_GROUP_KEY_LENGTH = 256
MAX_PARAMETER_GROUP_DESCRIPTION_LENGTH = 256
MAX_PARAMETER_GROUP_COUNT = 1000
MAX_CONDITION_COUNT = 2000
MAX_CONDITION_NAME_LENGTH = 256
MAX_VERSION_DESCRIPTION_LENGTH = 1024
MAX_CONDITION_ATOMS = 50
MAX_ROLLOUT_MICRO_PERCENT = 100_000_000
MAX_PERCENT_SEED_LENGTH = 32

# shared/targeting/rx/details/* regexes
PERCENT_SEED_REGEX = re.compile(r'^[\w.-]*$')
CUSTOM_OR_USER_PROP_NUMERIC_REGEX = re.compile(
    r'^[-+]?[0-9]{0,20}(?:\.[0-9]{0,10})?$'
)
CUSTOM_SIGNAL_SEMVER_REGEX = re.compile(r'^[0-9]+(?:\.[0-9]+){0,4}$')
NUMERIC_VERSION_REGEX = re.compile(r'^\d+(?:[\d.]*\d)?$')
FID_VALIDATION_REGEX = re.compile(
    r'^[cdef][\w-]{9}(?:[AEIMQUYcgkosw048]|[\w-]{12})$'
)

STRING_INFIX_CUSTOM_OR_USER_PROP_RE = re.compile(
    r"app\.(?:customSignal|userProperty)\['[^']+'\]\s*(?:==|!=|<|<=|>|>=)\s*'[^']*'"
)
NUMERIC_INFIX_CUSTOM_OR_USER_PROP_RE = re.compile(
    r"^app\.(?:customSignal|userProperty)\['[^']+'\]\s*(?:==|!=|<|<=|>|>=)\s*(.+)$"
)
SEMVER_CUSTOM_SIGNAL_RE = re.compile(
    r"^version\(app\.customSignal\['[^']+'\]\)\s*(?:==|!=|<|<=|>|>=)\s*'([^']*)'$"
)
APP_VERSION_OR_BUILD_NUMERIC_OP_RE = re.compile(
    r'^app\.(?:version|build)\.\s*(?:<|<=|>|>=)\s*\(\s*\[([^\]]*)\]\s*\)$'
)
FID_IN_CLAUSE_RE = re.compile(
    r'^(?:app\.)?firebaseInstallationId\s+in\s+\[([^\]]+)\]$'
)
COUNTRY_OR_LANGUAGE_NOT_IN_RE = re.compile(
    r'\bdevice\.(?:country|language)\b.*(?:\bnotIn\b|!=)'
)
PERCENT_CLAUSE_RE = re.compile(
    r"^percent(?:\('([^']*)'\))?\s*(?:(<=\s*([+-]?\d+(?:\.\d+)?))|(between\s+([+-]?\d+(?:\.\d+)?)\s+and\s+([+-]?\d+(?:\.\d+)?)))$"
)
HALLUCINATED_OS_RE = re.compile(r'\bapp\.operatingSystem\b')
HALLUCINATED_AUDIENCES_RE = re.compile(r'\bapp\.audiences\s*(?:in\b|==|!=)')
HALLUCINATED_PERCENT_FN_RE = re.compile(r'^percent\s*\([^)]*,[^)]*\)$')
HALLUCINATED_VERSION_BUILD_RE = re.compile(
    r'^app\.(?:version|build)\s*(?:(?:==|!=|<|<=|>|>=)|\.'
    r'(?:exactlyMatches|contains|startsWith|endsWith|regex|notContains|<|<=|>|>=)'
    r"\(\s*'[^']*'\s*\))"
)
ALLOWLISTED_TARGETING_CLAUSE_RE = re.compile(
    r'^(?:'
    r'true|false|'
    r"device\.os\s*(?:==|!=)\s*'[^']+'|"
    r'device\.os\s+in\s+\[[^\]]+\]|'
    r"device\.(?:country|language)\s*(?:in\s+\[[^\]]+\]|==\s*'[^']+')|"
    r"app\.id\s*(?:(?:==|!=)\s*'[^']+'|in\s+\[[^\]]+\])|"
    r'app\.(?:version|build)\.\s*(?:exactlyMatches|contains|startsWith|endsWith|regex|notContains|<|<=|>|>=)\s*\(\s*\[[^\]]*\]\s*\)|'
    r'app\.audiences\.\s*(?:inAtLeastOne|notInAtLeastOne|inAll|notInAll)\s*\(\s*\[[^\]]+\]\s*\)|'
    r"app\.(?:userProperty|customSignal)\['[^']+'\]\.\s*(?:exactlyMatches|contains|startsWith|endsWith|regex|notContains)\s*\(\s*\[[^\]]+\]\s*\)|"
    r"app\.(?:userProperty|customSignal)\['[^']+'\]\s*(?:==|!=|<|<=|>|>=)\s*.+|"
    r"version\(app\.customSignal\['[^']+'\]\)\s*(?:==|!=|<|<=|>|>=)\s*'[^']*'|"
    r"percent(?:\('[^']*'\))?\s*(?:(?:<|<=|>|>=)\s*[+-]?\d+(?:\.\d+)?|between\s+[+-]?\d+(?:\.\d+)?\s+and\s+[+-]?\d+(?:\.\d+)?)|"
    r"dateTime\s*(?:<|<=|>|>=|==)\s*(?:dateTime\('[^']+'\)|'[^']+')|"
    r"(?:app\.)?firstOpenTimestamp\s*(?:<|<=|>|>=|==)\s*(?:dateTime\('[^']+'\)|'[^']+'|\d+)|"
    r'(?:app\.)?firebaseInstallationId\s+in\s+\[[^\]]+\]'
    r')$'
)
CLIENT_ONLY_SIGNAL_RE = re.compile(
    r'\b(?:device\.(?:os|country|language)|'
    r'app\.(?:id|version|build|audiences|userProperty|firebaseInstallationId|firstOpenTimestamp)|'
    r'firebaseInstallationId|firstOpenTimestamp|dateTime)\b'
)
IN_LIST_CLAUSE_RE = re.compile(
    r'^([a-zA-Z0-9_.]+)\s+in\s+\[([^\]]+)\]$'
)
PERCENT_LE_CLAUSE_RE = re.compile(r'^percent\s*<=\s*(\d+(?:\.\d+)?)$')


@dataclasses.dataclass
class Finding:
  rule_id: str
  severity: str  # 'ERROR' or 'WARNING'
  path: str
  message: str


def _strip_single_quoted_strings(expr: str) -> str:
  return re.sub(r"'(?:\\'|[^'])*'", "''", expr)


def _split_and_clauses(expr: str) -> list[str]:
  """Splits a condition expression on top-level && operators."""
  clauses: list[str] = []
  current: list[str] = []
  in_quote = False
  bracket_depth = 0
  paren_depth = 0
  i = 0
  while i < len(expr):
    ch = expr[i]
    if ch == "'" and (i == 0 or expr[i - 1] != '\\'):
      in_quote = not in_quote
      current.append(ch)
      i += 1
    elif not in_quote and ch == '[':
      bracket_depth += 1
      current.append(ch)
      i += 1
    elif not in_quote and ch == ']':
      bracket_depth = max(0, bracket_depth - 1)
      current.append(ch)
      i += 1
    elif not in_quote and ch == '(':
      paren_depth += 1
      current.append(ch)
      i += 1
    elif not in_quote and ch == ')':
      paren_depth = max(0, paren_depth - 1)
      current.append(ch)
      i += 1
    elif (
        not in_quote
        and bracket_depth == 0
        and paren_depth == 0
        and expr[i : i + 2] == '&&'
    ):
      clauses.append(''.join(current).strip())
      current = []
      i += 2
    else:
      current.append(ch)
      i += 1
  tail = ''.join(current).strip()
  if tail:
    clauses.append(tail)
  return clauses


def _clause_subsumes(broad_clause: str, narrow_clause: str) -> bool:
  """Returns True if broad_clause is satisfied whenever narrow_clause holds."""
  norm_broad = ' '.join(broad_clause.split())
  norm_narrow = ' '.join(narrow_clause.split())
  if norm_broad == norm_narrow:
    return True

  # Check `field in ['US', 'CA']` subsuming `field in ['US']`
  m_broad = IN_LIST_CLAUSE_RE.match(norm_broad)
  m_narrow = IN_LIST_CLAUSE_RE.match(norm_narrow)
  if m_broad and m_narrow and m_broad.group(1) == m_narrow.group(1):
    broad_items = {
        item.strip().strip("'") for item in m_broad.group(2).split(',')
    }
    narrow_items = {
        item.strip().strip("'") for item in m_narrow.group(2).split(',')
    }
    if narrow_items and narrow_items.issubset(broad_items):
      return True

  # Check `percent <= 50` subsuming `percent <= 10`
  p_broad = PERCENT_LE_CLAUSE_RE.match(norm_broad)
  p_narrow = PERCENT_LE_CLAUSE_RE.match(norm_narrow)
  if p_broad and p_narrow:
    if float(p_broad.group(1)) >= float(p_narrow.group(1)):
      return True

  return False


def _condition_subsumes(broad_clauses: list[str], narrow_clauses: list[str]) -> bool:
  """Returns True if every clause in broad_clauses subsumes a narrow clause."""
  if not broad_clauses or not narrow_clauses:
    return False
  for b_clause in broad_clauses:
    if not any(_clause_subsumes(b_clause, n_clause) for n_clause in narrow_clauses):
      return False
  return True


def _validate_parameter_key(
    key: str, path: str, findings: list[Finding]
) -> None:
  """Validates parameter key per config/ng2/common/parameter_name_validator.ts."""
  if not key or len(key) > MAX_PARAMETER_NAME_LENGTH:
    findings.append(
        Finding(
            'RC001_KEY_FORMAT',
            'ERROR',
            path,
            f'Parameter key "{key}" must be 1..{MAX_PARAMETER_NAME_LENGTH} '
            'characters (MAX_PARAMETER_NAME_LENGTH).',
        )
    )
  elif not PARAMETER_NAME_REGEX.match(key):
    findings.append(
        Finding(
            'RC001_KEY_FORMAT',
            'ERROR',
            path,
            f'Parameter key "{key}" does not match PARAMETER_NAME_REGEX '
            '(^[a-zA-Z_][0-9a-zA-Z_]*$).',
        )
    )
  if key in RESERVED_JS_KEYWORDS:
    findings.append(
        Finding(
            'RC001_KEY_FORMAT',
            'ERROR',
            path,
            f'Parameter key "{key}" is in FORBIDDEN_KEYS '
            '(reserved_keywords.ts).',
        )
    )


def _validate_datatype_string(
    raw_val: str, value_type: str, path: str, findings: list[Finding]
) -> None:
  """Validates a string value against valueType per data_type_validator.ts."""
  if value_type == 'BOOLEAN':
    if raw_val.lower() not in ('true', 'false'):
      findings.append(
          Finding(
              'RC006_VALUE_TYPE',
              'ERROR',
              path,
              f'PARAM_VALUE_TYPE_MISMATCH: BOOLEAN value "{raw_val}" must be '
              '"true" or "false" (case-insensitive).',
          )
      )
  elif value_type == 'NUMBER':
    if not NUMERIC_REGEX.match(raw_val):
      findings.append(
          Finding(
              'RC006_VALUE_TYPE',
              'ERROR',
              path,
              f'PARAM_VALUE_TYPE_MISMATCH: NUMBER value "{raw_val}" does not '
              'match Console NUMERIC_REGEX ^-?(\\d*\\.)?\\d+$.',
          )
      )
  elif value_type == 'JSON':
    try:
      json.loads(raw_val)
    except ValueError as exc:
      findings.append(
          Finding(
              'RC006_VALUE_TYPE',
              'ERROR',
              path,
              f'PARAM_VALUE_TYPE_MISMATCH: invalid JSON string ({exc}).',
          )
      )


def _validate_value_object(
    val_obj: Any,
    value_type: str,
    is_default: bool,
    namespace: str,
    path: str,
    fix_types: bool,
    findings: list[Finding],
) -> None:
  """Validates ParameterValue per parameter_editor_store.ts & remote_config_service.ts."""
  if not isinstance(val_obj, dict):
    findings.append(
        Finding(
            'RC007_ONEOF_DEFAULT',
            'ERROR',
            path,
            'ParameterValue must be a JSON object.',
        )
    )
    return

  present_oneofs = [f for f in VALUE_ONEOF_FIELDS if f in val_obj]
  if len(present_oneofs) != 1:
    findings.append(
        Finding(
            'RC007_ONEOF_DEFAULT',
            'ERROR',
            path,
            f'ParameterValue must specify exactly one of {VALUE_ONEOF_FIELDS}; '
            f'found {present_oneofs}.',
        )
    )
    return

  active_field = present_oneofs[0]
  if active_field == 'useInAppDefault':
    if val_obj['useInAppDefault'] is not True:
      findings.append(
          Finding(
              'RC007_ONEOF_DEFAULT',
              'ERROR',
              path,
              'INVALID_IN_APP_DEFAULT_VALUE: useInAppDefault must be true '
              '(never false).',
          )
      )
    return

  if active_field in DYNAMIC_VALUE_FIELDS:
    if is_default:
      findings.append(
          Finding(
              'RC007_ONEOF_DEFAULT',
              'ERROR',
              path,
              f'INVALID_DEFAULT_PARAM_VALUE: defaultValue cannot use '
              f'{active_field}.',
          )
      )
    if namespace == 'firebase-server':
      findings.append(
          Finding(
              'RC007_ONEOF_DEFAULT',
              'ERROR',
              path,
              f'UNSUPPORTED_PARAM_VALUE: {active_field} is not supported in '
              'the firebase-server namespace.',
          )
      )
    # Validate managed value payloads & arm datatypes per parameter_editor_store.ts
    managed_obj = val_obj[active_field]
    if active_field == 'rolloutValue' and isinstance(managed_obj, dict):
      # Check rollout value datatype (REST `value` or Console `enabledVariantValue.value`)
      if isinstance(managed_obj.get('value'), str):
        _validate_datatype_string(
            managed_obj['value'],
            value_type,
            f'{path}.rolloutValue.value',
            findings,
        )
      enabled_var = managed_obj.get('enabledVariantValue')
      if isinstance(enabled_var, dict) and isinstance(
          enabled_var.get('value'), str
      ):
        _validate_datatype_string(
            enabled_var['value'],
            value_type,
            f'{path}.rolloutValue.enabledVariantValue.value',
            findings,
        )
      # Check rollout percentage range (REST `percent` 0..100 or microPercentRange 0..100_000_000)
      if 'percent' in managed_obj:
        pct = managed_obj['percent']
        if not isinstance(pct, (int, float)) or not (0 <= float(pct) <= 100):
          findings.append(
              Finding(
                  'RC008_ROLLOUT_RANGE',
                  'ERROR',
                  f'{path}.rolloutValue.percent',
                  f'INVALID_ROLLOUT_PERCENTAGE: rollout percent ({pct}) must '
                  'be between 0 and 100.',
              )
          )
      mpr = managed_obj.get('microPercentRange')
      if not isinstance(mpr, dict):
        epc = managed_obj.get('enabledPercentCondition')
        if isinstance(epc, dict):
          mpr = epc.get('microPercentRange')
      if isinstance(mpr, dict):
        start = mpr.get(
            'microPercentLowerBound', mpr.get('microPercentStart', 0)
        )
        end = mpr.get('microPercentUpperBound', mpr.get('microPercentEnd', 0))
        if (
            not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or start < 0
            or end <= start
            or end > MAX_ROLLOUT_MICRO_PERCENT
        ):
          findings.append(
              Finding(
                  'RC008_ROLLOUT_RANGE',
                  'ERROR',
                  f'{path}.rolloutValue.microPercentRange',
                  f'INVALID_ROLLOUT_PERCENTAGE: microPercentRange ({start}..'
                  f'{end}) must satisfy 0 <= lower < upper <= '
                  f'{MAX_ROLLOUT_MICRO_PERCENT} (100%).',
              )
          )
    elif active_field == 'experimentValue' and isinstance(managed_obj, dict):
      variants = managed_obj.get('variantValue', [])
      if isinstance(variants, list):
        for v_idx, var_obj in enumerate(variants):
          if (
              isinstance(var_obj, dict)
              and not var_obj.get('noChange')
              and isinstance(var_obj.get('value'), str)
          ):
            _validate_datatype_string(
                var_obj['value'],
                value_type,
                f'{path}.experimentValue.variantValue[{v_idx}].value',
                findings,
            )
    return

  # Static 'value' check
  raw_val = val_obj['value']
  if not isinstance(raw_val, str):
    if fix_types:
      if isinstance(raw_val, bool):
        val_obj['value'] = 'true' if raw_val else 'false'
      elif isinstance(raw_val, (int, float)):
        val_obj['value'] = str(raw_val)
      else:
        val_obj['value'] = json.dumps(raw_val)
      raw_val = val_obj['value']
    else:
      findings.append(
          Finding(
              'RC006_VALUE_TYPE',
              'ERROR',
              f'{path}.value',
              f'PARAM_VALUE_TYPE_MISMATCH: static value must be a JSON string, '
              f'got {type(raw_val).__name__}.',
          )
      )
      return

  _validate_datatype_string(raw_val, value_type, f'{path}.value', findings)


def _validate_targeting_clause(
    clause: str, c_path: str, findings: list[Finding]
) -> None:
  """Validates individual AND clauses per shared/targeting/rx/details/*."""
  norm = clause.strip()
  initial_finding_count = len(findings)
  if norm.startswith('!'):
    findings.append(
        Finding(
            'RC009_EXPRESSION_GRAMMAR',
            'ERROR',
            f'{c_path}.expression',
            f'Negation (!) in "{clause}" is rejected; use !=, '
            'notContains, notInAtLeastOne, or notInAll.',
        )
    )

  if HALLUCINATED_OS_RE.search(norm):
    findings.append(
        Finding(
            'RC009_EXPRESSION_GRAMMAR',
            'ERROR',
            f'{c_path}.expression',
            f'Invalid OS field in "{clause}": use '
            "`device.os == 'ios'` (`'ios'`, `'android'`, `'web'`) or "
            '`device.os in [...]`, not `app.operatingSystem`.',
        )
    )

  if HALLUCINATED_AUDIENCES_RE.search(norm):
    findings.append(
        Finding(
            'RC009_EXPRESSION_GRAMMAR',
            'ERROR',
            f'{c_path}.expression',
            f'Invalid audiences syntax in "{clause}": use '
            "`app.audiences.inAtLeastOne(['audience_id'])`, "
            '`.notInAtLeastOne([...])`, `.inAll([...])`, or `.notInAll([...])`, '
            'not `in [...]` or `==`.',
        )
    )

  if HALLUCINATED_PERCENT_FN_RE.match(norm):
    findings.append(
        Finding(
            'RC009_EXPRESSION_GRAMMAR',
            'ERROR',
            f'{c_path}.expression',
            f'Invalid percent() syntax in "{clause}": use '
            "`percent <= 10`, `percent('seed') <= 10`, or "
            "`percent('seed') between 0 and 10`, not `percent('seed', min, max)`.",
        )
    )

  if HALLUCINATED_VERSION_BUILD_RE.match(norm):
    findings.append(
        Finding(
            'RC009_EXPRESSION_GRAMMAR',
            'ERROR',
            f'{c_path}.expression',
            f'Invalid version/build syntax in "{clause}": use method syntax '
            "with a list argument, e.g., `app.version.>=(['2.4.0'])` or "
            "`app.version.exactlyMatches(['2.4.0'])`.",
        )
    )

  # user_percent.ts: seed pattern ^[\w.-]*$, maxLength(32), and 0..100 bounds
  m_pct = PERCENT_CLAUSE_RE.match(norm)
  if m_pct:
    seed = m_pct.group(1)
    if seed is not None:
      if (
          len(seed) > MAX_PERCENT_SEED_LENGTH
          or not PERCENT_SEED_REGEX.match(seed)
      ):
        findings.append(
            Finding(
                'RC009_EXPRESSION_GRAMMAR',
                'ERROR',
                f'{c_path}.expression',
                f'Percent seed "{seed}" in "{clause}" violates '
                'user_percent.ts seed rules (^[\\w.-]*$, max 32 chars).',
            )
        )
    if m_pct.group(3) is not None:
      upper = float(m_pct.group(3))
      if not (0 < upper <= 100):
        findings.append(
            Finding(
                'RC009_EXPRESSION_GRAMMAR',
                'ERROR',
                f'{c_path}.expression',
                f'Percentage upper bound {upper} in "{clause}" must satisfy '
                '0 < upper <= 100 (user_percent.ts).',
            )
        )
    elif m_pct.group(5) is not None and m_pct.group(6) is not None:
      lower = float(m_pct.group(5))
      upper = float(m_pct.group(6))
      if not (0 <= lower < upper <= 100):
        findings.append(
            Finding(
                'RC009_EXPRESSION_GRAMMAR',
                'ERROR',
                f'{c_path}.expression',
                f'Percentage range {lower}..{upper} in "{clause}" must '
                'satisfy 0 <= lower < upper <= 100 (user_percent.ts).',
            )
        )

  # custom_signal.ts & analytics_user_property.ts: numeric infix regex
  m_num = NUMERIC_INFIX_CUSTOM_OR_USER_PROP_RE.match(norm)
  if m_num:
    rhs = m_num.group(1).strip()
    if not rhs.startswith("'"):
      if not rhs or not CUSTOM_OR_USER_PROP_NUMERIC_REGEX.match(rhs):
        findings.append(
            Finding(
                'RC009_EXPRESSION_GRAMMAR',
                'ERROR',
                f'{c_path}.expression',
                f'Numeric operand "{rhs}" in "{clause}" violates '
                'VALID_NUMERIC_VALUE_REGEX '
                '(^[-+]?[0-9]{0,20}([.][0-9]{0,10})?$).',
            )
        )

  # custom_signal.ts: semantic version regex
  m_semver = SEMVER_CUSTOM_SIGNAL_RE.match(norm)
  if m_semver:
    ver_str = m_semver.group(1)
    if not CUSTOM_SIGNAL_SEMVER_REGEX.match(ver_str):
      findings.append(
          Finding(
              'RC009_EXPRESSION_GRAMMAR',
              'ERROR',
              f'{c_path}.expression',
              f'Semantic version "{ver_str}" in "{clause}" violates '
              'VALID_SEMANTIC_VERSION_VALUE_REGEX (^[0-9]+(\\.[0-9]+){0,4}$).',
          )
      )

  # version.ts, build_number.ts, common.ts: numeric version/build regex
  m_ver_op = APP_VERSION_OR_BUILD_NUMERIC_OP_RE.match(norm)
  if m_ver_op:
    items = [
        item.strip().strip("'")
        for item in m_ver_op.group(1).split(',')
        if item.strip()
    ]
    if len(items) != 1 or not NUMERIC_VERSION_REGEX.match(items[0]):
      findings.append(
          Finding(
              'RC009_EXPRESSION_GRAMMAR',
              'ERROR',
              f'{c_path}.expression',
              f'Numeric version/build comparison "{clause}" requires a single '
              'value matching NUMERIC_VERSION_REGEX (^\\d+([\\d.]*\\d)?$).',
          )
      )

  # firebase_installation_id.ts: FID_VALIDATION_REGEX
  m_fid = FID_IN_CLAUSE_RE.match(norm)
  if m_fid:
    fids = [
        item.strip().strip("'")
        for item in m_fid.group(1).split(',')
        if item.strip()
    ]
    for fid in fids:
      if not FID_VALIDATION_REGEX.match(fid):
        findings.append(
            Finding(
                'RC009_EXPRESSION_GRAMMAR',
                'ERROR',
                f'{c_path}.expression',
                f'Firebase Installation ID "{fid}" in "{clause}" violates '
                'FID_VALIDATION_REGEX '
                '(^[cdef][\\w-]{9}([AEIMQUYcgkosw048]|[\\w-]{12})$).',
            )
        )

  # countries.ts & languages.ts: supportsNotInOperator = false
  if COUNTRY_OR_LANGUAGE_NOT_IN_RE.search(norm):
    findings.append(
        Finding(
            'RC009_EXPRESSION_GRAMMAR',
            'ERROR',
            f'{c_path}.expression',
            f'Clause "{clause}" uses unsupported negative operator on '
            'device.country or device.language (supportsNotInOperator = false '
            'in countries.ts / languages.ts).',
        )
    )

  if (
      len(findings) == initial_finding_count
      and not ALLOWLISTED_TARGETING_CLAUSE_RE.match(norm)
  ):
    findings.append(
        Finding(
            'RC009_EXPRESSION_GRAMMAR',
            'ERROR',
            f'{c_path}.expression',
            f'Unrecognized targeting expression clause "{clause}".',
        )
    )


def validate_and_canonicalize(
    template: dict[str, Any],
    namespace: str = 'firebase',
    fix_order: bool = False,
    fix_types: bool = False,
    prune_dangling: bool = False,
) -> list[Finding]:
  """Validates template in-place and returns all findings."""
  findings: list[Finding] = []

  # constants.ts: MAX_VERSION_DESCRIPTION_LENGTH = 1024
  version_obj = template.get('version')
  if isinstance(version_obj, dict):
    v_desc = version_obj.get('description')
    if isinstance(v_desc, str) and len(v_desc) > MAX_VERSION_DESCRIPTION_LENGTH:
      findings.append(
          Finding(
              'RC001_KEY_FORMAT',
              'ERROR',
              'version.description',
              f'Version description length ({len(v_desc)}) exceeds '
              f'MAX_VERSION_DESCRIPTION_LENGTH ({MAX_VERSION_DESCRIPTION_LENGTH}).',
          )
      )

  conditions = template.get('conditions', [])
  if not isinstance(conditions, list):
    findings.append(
        Finding('RC003_CONDITION_NAME', 'ERROR', 'conditions', 'Must be a list.')
    )
    conditions = []

  if len(conditions) > MAX_CONDITION_COUNT:
    findings.append(
        Finding(
            'RC003_CONDITION_NAME',
            'ERROR',
            'conditions',
            f'Template has {len(conditions)} conditions '
            f'(max {MAX_CONDITION_COUNT}).',
        )
    )

  condition_names: list[str] = []
  condition_index: dict[str, int] = {}
  parsed_condition_clauses: list[tuple[str, list[str]]] = []

  for idx, cond in enumerate(conditions):
    c_path = f'conditions[{idx}]'
    if not isinstance(cond, dict):
      findings.append(
          Finding('RC003_CONDITION_NAME', 'ERROR', c_path, 'Must be an object.')
      )
      continue

    raw_name = cond.get('name', '')
    if not isinstance(raw_name, str) or not raw_name.strip():
      findings.append(
          Finding(
              'RC003_CONDITION_NAME',
              'ERROR',
              f'{c_path}.name',
              'Condition name must be a non-empty, non-whitespace string.',
          )
      )
      continue

    trimmed_name = raw_name.strip()
    if raw_name != trimmed_name:
      if fix_order or fix_types:
        raw_name = trimmed_name
        cond['name'] = raw_name
      else:
        findings.append(
            Finding(
                'RC003_CONDITION_NAME',
                'ERROR',
                f'{c_path}.name',
                f'Condition name "{raw_name}" has leading or trailing spaces.',
            )
        )

    if (
        len(raw_name) > MAX_CONDITION_NAME_LENGTH
        or not CONDITION_NAME_REGEX.match(raw_name)
    ):
      findings.append(
          Finding(
              'RC003_CONDITION_NAME',
              'ERROR',
              f'{c_path}.name',
              f'Condition name "{raw_name}" violates CONDITION_NAME_REGEX '
              f'(^[-_0-9a-zA-Z \'!%.]+$) or exceeds '
              f'{MAX_CONDITION_NAME_LENGTH} chars.',
          )
      )

    if trimmed_name in condition_index:
      findings.append(
          Finding(
              'RC003_CONDITION_NAME',
              'ERROR',
              f'{c_path}.name',
              f'Duplicate condition name "{trimmed_name}" '
              '(condition_name_editor.ts).',
          )
      )
    else:
      condition_index[trimmed_name] = len(condition_names)
      condition_names.append(trimmed_name)

    tag_color = cond.get('tagColor')
    if tag_color is not None:
      if (
          not isinstance(tag_color, str)
          or tag_color.upper() not in VALID_TAG_COLORS
      ):
        findings.append(
            Finding(
                'RC003_CONDITION_NAME',
                'ERROR',
                f'{c_path}.tagColor',
                f'Invalid tagColor "{tag_color}".',
            )
        )
      elif fix_types:
        upper_color = tag_color.upper()
        if upper_color == 'DEEPORANGE':
          upper_color = 'DEEP_ORANGE'
        cond['tagColor'] = upper_color

    expr = cond.get('expression', '')
    if not isinstance(expr, str) or not expr.strip():
      findings.append(
          Finding(
              'RC009_EXPRESSION_GRAMMAR',
              'ERROR',
              f'{c_path}.expression',
              'Condition expression must be a non-empty string.',
          )
      )
      parsed_condition_clauses.append((trimmed_name, []))
      continue

    stripped_quotes = _strip_single_quoted_strings(expr)
    if '||' in stripped_quotes:
      findings.append(
          Finding(
              'RC009_EXPRESSION_GRAMMAR',
              'ERROR',
              f'{c_path}.expression',
              'Top-level OR (||) is rejected by Console condition editor; use '
              '`in [...]` list operators or separate conditions.',
          )
      )

    clauses = _split_and_clauses(expr)
    if len(clauses) > MAX_CONDITION_ATOMS:
      findings.append(
          Finding(
              'RC009_EXPRESSION_GRAMMAR',
              'ERROR',
              f'{c_path}.expression',
              f'Condition has {len(clauses)} AND atoms (max '
              f'{MAX_CONDITION_ATOMS}).',
          )
      )

    for clause in clauses:
      _validate_targeting_clause(clause, c_path, findings)

    if 'device.dateTime' in stripped_quotes:
      findings.append(
          Finding(
              'RC009_EXPRESSION_GRAMMAR',
              'ERROR',
              f'{c_path}.expression',
              'Use top-level `dateTime`, not `device.dateTime` (date_time.ts).',
          )
      )
    if STRING_INFIX_CUSTOM_OR_USER_PROP_RE.search(expr):
      findings.append(
          Finding(
              'RC009_EXPRESSION_GRAMMAR',
              'ERROR',
              f'{c_path}.expression',
              'String comparison on app.customSignal or app.userProperty must '
              'use .exactlyMatches([...]), .contains([...]), etc., not '
              '==/!=/</<=/>/>= (custom_signal.ts / analytics_user_property.ts).',
          )
      )
    if namespace == 'firebase-server' and CLIENT_ONLY_SIGNAL_RE.search(
        stripped_quotes
    ):
      findings.append(
          Finding(
              'RC009_EXPRESSION_GRAMMAR',
              'ERROR',
              f'{c_path}.expression',
              'firebase-server templates only support `percent` and '
              '`app.customSignal` in condition expressions '
              '(condition_editor.ts).',
          )
      )

    parsed_condition_clauses.append((trimmed_name, clauses))

  # Collect all parameters across root and parameterGroups
  global_keys: dict[str, str] = {}
  all_param_entries: list[tuple[str, str, dict[str, Any]]] = []

  param_groups = template.get('parameterGroups', {})
  if not isinstance(param_groups, dict):
    findings.append(
        Finding(
            'RC002_GLOBAL_UNIQUE_KEY',
            'ERROR',
            'parameterGroups',
            'parameterGroups must be an object.',
        )
    )
    param_groups = {}

  if len(param_groups) > MAX_PARAMETER_GROUP_COUNT:
    findings.append(
        Finding(
            'RC002_GLOBAL_UNIQUE_KEY',
            'ERROR',
            'parameterGroups',
            f'Template has {len(param_groups)} groups (max '
            f'{MAX_PARAMETER_GROUP_COUNT}).',
        )
    )

  for group_name, group_obj in param_groups.items():
    g_path = f'parameterGroups.{group_name}'
    if (
        not group_name
        or not group_name.strip()
        or len(group_name) > MAX_PARAMETER_GROUP_KEY_LENGTH
        or group_name != group_name.strip()
    ):
      findings.append(
          Finding(
              'RC002_GLOBAL_UNIQUE_KEY',
              'ERROR',
              g_path,
              f'Group key "{group_name}" must be 1..'
              f'{MAX_PARAMETER_GROUP_KEY_LENGTH} chars with no leading/trailing '
              'spaces (parameter_group_edit.ts).',
          )
      )
    global_keys[group_name] = g_path
    if not isinstance(group_obj, dict):
      continue
    g_desc = group_obj.get('description')
    if (
        isinstance(g_desc, str)
        and len(g_desc) > MAX_PARAMETER_GROUP_DESCRIPTION_LENGTH
    ):
      findings.append(
          Finding(
              'RC002_GLOBAL_UNIQUE_KEY',
              'ERROR',
              f'{g_path}.description',
              f'Group description length ({len(g_desc)}) exceeds '
              'MAX_PARAMETER_GROUP_DESCRIPTION_LENGTH '
              f'({MAX_PARAMETER_GROUP_DESCRIPTION_LENGTH}).',
          )
      )
    g_params = group_obj.get('parameters', {})
    if isinstance(g_params, dict):
      for p_key, p_obj in g_params.items():
        p_path = f'{g_path}.parameters.{p_key}'
        all_param_entries.append((p_key, p_path, p_obj))

  root_params = template.get('parameters', {})
  if isinstance(root_params, dict):
    for p_key, p_obj in root_params.items():
      p_path = f'parameters.{p_key}'
      all_param_entries.append((p_key, p_path, p_obj))

  if len(all_param_entries) > MAX_PARAMETER_COUNT:
    findings.append(
        Finding(
            'RC001_KEY_FORMAT',
            'ERROR',
            'parameters',
            f'Template has {len(all_param_entries)} total parameters '
            f'(max {MAX_PARAMETER_COUNT}).',
        )
    )

  for p_key, p_path, p_obj in all_param_entries:
    _validate_parameter_key(p_key, p_path, findings)
    if p_key in global_keys:
      findings.append(
          Finding(
              'RC002_GLOBAL_UNIQUE_KEY',
              'ERROR',
              p_path,
              f'DUPLICATE_KEY: "{p_key}" collides with {global_keys[p_key]} '
              '(parameter_editor_store.ts / parameter_group_edit.ts).',
          )
      )
    else:
      global_keys[p_key] = p_path

    if not isinstance(p_obj, dict):
      continue

    p_desc = p_obj.get('description')
    if (
        isinstance(p_desc, str)
        and len(p_desc) > MAX_PARAMETER_DESCRIPTION_LENGTH
    ):
      findings.append(
          Finding(
              'RC001_KEY_FORMAT',
              'ERROR',
              f'{p_path}.description',
              f'Parameter description length ({len(p_desc)}) exceeds '
              'MAX_PARAMETER_DESCRIPTION_LENGTH '
              f'({MAX_PARAMETER_DESCRIPTION_LENGTH}).',
          )
      )

    value_type = p_obj.get('valueType', 'STRING')
    if value_type not in VALID_VALUE_TYPES:
      findings.append(
          Finding(
              'RC006_VALUE_TYPE',
              'ERROR',
              f'{p_path}.valueType',
              f'Invalid valueType "{value_type}".',
          )
      )
      value_type = 'STRING'

    if 'defaultValue' in p_obj:
      _validate_value_object(
          p_obj['defaultValue'],
          value_type,
          is_default=True,
          namespace=namespace,
          path=f'{p_path}.defaultValue',
          fix_types=fix_types,
          findings=findings,
      )

    cond_vals = p_obj.get('conditionalValues')
    if isinstance(cond_vals, dict):
      dangling_keys = [k for k in cond_vals if k not in condition_index]
      for d_key in dangling_keys:
        if prune_dangling:
          del cond_vals[d_key]
        else:
          findings.append(
              Finding(
                  'RC004_CONDITION_REF',
                  'ERROR',
                  f'{p_path}.conditionalValues.{d_key}',
                  f'UNKNOWN_CONDITION_REFERENCE: "{d_key}" does not exist in '
                  'top-level conditions (remote_config_service.ts).',
              )
          )

      valid_keys = [k for k in cond_vals if k in condition_index]
      expected_keys = sorted(valid_keys, key=lambda k: condition_index[k])
      if valid_keys != expected_keys:
        if fix_order:
          reordered = {k: cond_vals[k] for k in expected_keys}
          for k in cond_vals:
            if k not in reordered:
              reordered[k] = cond_vals[k]
          p_obj['conditionalValues'] = reordered
          cond_vals = reordered
        else:
          findings.append(
              Finding(
                  'RC005_CONDITION_ORDER',
                  'ERROR',
                  f'{p_path}.conditionalValues',
                  f'CONDITION_ORDER_MISMATCH: keys {valid_keys} do not '
                  f'match conditions weight order {expected_keys} '
                  '(config_template_store.ts / remote_config_service.ts).',
              )
          )

      for c_name, c_val_obj in cond_vals.items():
        _validate_value_object(
            c_val_obj,
            value_type,
            is_default=False,
            namespace=namespace,
            path=f'{p_path}.conditionalValues.{c_name}',
            fix_types=fix_types,
            findings=findings,
        )

  # RC010: Condition Precedence Shadowing Check
  for i in range(len(parsed_condition_clauses)):
    broad_name, broad_clauses = parsed_condition_clauses[i]
    for j in range(i + 1, len(parsed_condition_clauses)):
      narrow_name, narrow_clauses = parsed_condition_clauses[j]
      if _condition_subsumes(broad_clauses, narrow_clauses):
        affected_params = [
            p_key
            for p_key, _, p_obj in all_param_entries
            if isinstance(p_obj, dict)
            and isinstance(p_obj.get('conditionalValues'), dict)
            and broad_name in p_obj['conditionalValues']
            and narrow_name in p_obj['conditionalValues']
        ]
        severity = 'ERROR' if affected_params else 'WARNING'
        affected_msg = (
            f' Affected parameters: {affected_params}.'
            if affected_params
            else ' (No parameter currently references both.)'
        )
        findings.append(
            Finding(
                'RC010_CONDITION_SHADOWING',
                severity,
                f'conditions[{i}] -> conditions[{j}]',
                f'Broad condition "{broad_name}" at index {i} logically '
                f'subsumes narrower condition "{narrow_name}" at index {j}, '
                f'shadowing it under first-match-wins.{affected_msg}',
            )
        )

  return findings


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(
      description='Validate and canonicalize a Firebase Remote Config template.'
  )
  parser.add_argument('template_path', help='Path to remote_config.json file.')
  parser.add_argument(
      '--namespace',
      choices=('firebase', 'firebase-server'),
      default='firebase',
      help='Target Remote Config namespace (default: firebase).',
  )
  parser.add_argument(
      '--fix-order',
      action='store_true',
      help='Sort conditionalValues keys in-place to match conditions[0..N-1].',
  )
  parser.add_argument(
      '--fix-types',
      action='store_true',
      help='Coerce raw JSON bool/number values into strings in-place.',
  )
  parser.add_argument(
      '--prune-dangling',
      action='store_true',
      help='Remove unknown condition references from conditionalValues.',
  )
  parser.add_argument(
      '--json',
      action='store_true',
      help='Output validation report as structured JSON.',
  )
  parser.add_argument(
      '--format',
      choices=('text', 'json'),
      default='text',
      help='Output format for validation report (default: text).',
  )
  parser.add_argument(
      '--strict',
      action='store_true',
      help='Treat WARNING findings as fatal errors (exit code 1).',
  )
  parser.add_argument(
      '-o',
      '--output',
      default=None,
      help='Optional output file path for canonicalized template JSON.',
  )
  args = parser.parse_args(argv)

  with open(args.template_path, 'r', encoding='utf-8') as f:
    template = json.load(f)

  findings = validate_and_canonicalize(
      template,
      namespace=args.namespace,
      fix_order=args.fix_order,
      fix_types=args.fix_types,
      prune_dangling=args.prune_dangling,
  )

  if args.output or args.fix_order or args.fix_types or args.prune_dangling:
    out_path = args.output or args.template_path
    with open(out_path, 'w', encoding='utf-8') as f:
      json.dump(template, f, indent=2)
      f.write('\n')

  errors = [item for item in findings if item.severity == 'ERROR']
  is_valid = (not findings) if args.strict else (not errors)
  if args.json or args.format == 'json':
    print(
        json.dumps(
            {
                'valid': is_valid,
                'error_count': len(errors),
                'warning_count': len(findings) - len(errors),
                'findings': [dataclasses.asdict(item) for item in findings],
            },
            indent=2,
        )
    )
  else:
    for item in findings:
      print(f'[{item.severity}] {item.rule_id} at {item.path}: {item.message}')
    if not findings:
      print('OK: Template passed all RC001-RC010 validation checks.')

  return 0 if is_valid else 1


if __name__ == '__main__':
  sys.exit(main())