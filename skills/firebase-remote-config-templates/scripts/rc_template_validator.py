#!/usr/bin/env python3
"""Offline validator and canonicalizer for Firebase Remote Config templates.

Enforces production InternalTemplateValidator, ReservedKeywordsValidator, and
ConditionValidator invariants (RC001-RC009) plus logical condition precedence
shadowing detection (RC010). Supports deterministic auto-remediation for
conditionalValues key ordering (--fix-order), raw JSON primitive value coercion
(--fix-types), and dangling condition reference removal (--prune-dangling).
"""

import argparse
import dataclasses
import json
import re
import sys
from typing import Any

PARAMETER_KEY_REGEX = re.compile(r'^[a-zA-Z_][0-9a-zA-Z_]*$')
CONDITION_NAME_REGEX = re.compile(r"^[-_0-9a-zA-Z '!%.]+$")
NUMBER_VALUE_REGEX = re.compile(r'^-?(\d*\.)?\d+$')

RESERVED_JS_KEYWORDS = frozenset({
    'prototype',
    'constructor',
    '__proto__',
    'toString',
    'valueOf',
    'hasOwnProperty',
    'isPrototypeOf',
    'propertyIsEnumerable',
    'toLocaleString',
    '__defineGetter__',
    '__defineSetter__',
    '__lookupGetter__',
    '__lookupSetter__',
})

VALID_TAG_COLORS = frozenset({
    'BLUE',
    'BROWN',
    'CYAN',
    'DEEP_ORANGE',
    'GREEN',
    'INDIGO',
    'LIME',
    'ORANGE',
    'PINK',
    'PURPLE',
    'TEAL',
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

MAX_PARAMETERS = 3000
MAX_CONDITIONS = 2000
MAX_PARAMETER_GROUPS = 1000
MAX_KEY_BYTES = 256
MAX_NAME_CHARS = 256
MAX_CONDITION_ATOMS = 50
MAX_PARTIAL_ROLLOUT_MICRO_PERCENT = 50_000_000
FULL_ROLLOUT_MICRO_PERCENT = 100_000_000

STRING_INFIX_CUSTOM_OR_USER_PROP_RE = re.compile(
    r"app\.(?:customSignal|userProperty)\['[^']+'\]\s*(?:==|!=)\s*'[^']*'"
)
CLIENT_ONLY_SIGNAL_RE = re.compile(
    r'\b(?:device\.(?:os|country|language)|'
    r'app\.(?:id|version|build|audiences|userProperty)|dateTime)\b'
)
IN_LIST_CLAUSE_RE = re.compile(
    r"^([a-zA-Z0-9_.]+)\s+in\s+\[([^\]]+)\]$"
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
  if not key or len(key.encode('utf-8')) > MAX_KEY_BYTES:
    findings.append(
        Finding(
            'RC001_KEY_FORMAT',
            'ERROR',
            path,
            f'Parameter key "{key}" must be 1..{MAX_KEY_BYTES} UTF-8 bytes.',
        )
    )
  elif not PARAMETER_KEY_REGEX.match(key):
    findings.append(
        Finding(
            'RC001_KEY_FORMAT',
            'ERROR',
            path,
            f'Parameter key "{key}" does not match ^[a-zA-Z_][0-9a-zA-Z_]*$.',
        )
    )
  if key in RESERVED_JS_KEYWORDS:
    findings.append(
        Finding(
            'RC001_KEY_FORMAT',
            'ERROR',
            path,
            f'Parameter key "{key}" is a prohibited JS Object.prototype keyword.',
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
    if active_field == 'rolloutValue' and isinstance(val_obj['rolloutValue'], dict):
      mpr = val_obj['rolloutValue'].get('microPercentRange')
      if isinstance(mpr, dict):
        start = mpr.get('microPercentStart', 0)
        end = mpr.get('microPercentEnd', 0)
        valid_end = (
            0 < end <= MAX_PARTIAL_ROLLOUT_MICRO_PERCENT
            or end == FULL_ROLLOUT_MICRO_PERCENT
        )
        if start != 0 or not valid_end:
          findings.append(
              Finding(
                  'RC008_ROLLOUT_RANGE',
                  'ERROR',
                  f'{path}.rolloutValue.microPercentRange',
                  f'INVALID_ROLLOUT_PERCENTAGE: microPercentStart ({start}) '
                  'must be 0 and microPercentEnd ({end}) must be <= '
                  '50_000_000 (50%) or == 100_000_000 (100%).',
              )
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

  if value_type == 'BOOLEAN':
    if raw_val.lower() not in ('true', 'false'):
      findings.append(
          Finding(
              'RC006_VALUE_TYPE',
              'ERROR',
              f'{path}.value',
              f'PARAM_VALUE_TYPE_MISMATCH: BOOLEAN value "{raw_val}" must be '
              '"true" or "false".',
          )
      )
  elif value_type == 'NUMBER':
    if not NUMBER_VALUE_REGEX.match(raw_val):
      findings.append(
          Finding(
              'RC006_VALUE_TYPE',
              'ERROR',
              f'{path}.value',
              f'PARAM_VALUE_TYPE_MISMATCH: NUMBER value "{raw_val}" does not '
              'match ^-?(\\d*\\.)?\\d+$.',
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
              f'{path}.value',
              f'PARAM_VALUE_TYPE_MISMATCH: invalid JSON string ({exc}).',
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

  conditions = template.get('conditions', [])
  if not isinstance(conditions, list):
    findings.append(
        Finding('RC003_CONDITION_NAME', 'ERROR', 'conditions', 'Must be a list.')
    )
    conditions = []

  if len(conditions) > MAX_CONDITIONS:
    findings.append(
        Finding(
            'RC003_CONDITION_NAME',
            'ERROR',
            'conditions',
            f'Template has {len(conditions)} conditions (max {MAX_CONDITIONS}).',
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
    if not isinstance(raw_name, str) or not raw_name:
      findings.append(
          Finding(
              'RC003_CONDITION_NAME',
              'ERROR',
              f'{c_path}.name',
              'Condition name must be a non-empty string.',
          )
      )
      continue

    if raw_name != raw_name.strip():
      if fix_order or fix_types:
        raw_name = raw_name.strip()
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

    if len(raw_name) > MAX_NAME_CHARS or not CONDITION_NAME_REGEX.match(raw_name):
      findings.append(
          Finding(
              'RC003_CONDITION_NAME',
              'ERROR',
              f'{c_path}.name',
              f'Condition name "{raw_name}" violates ^[-_0-9a-zA-Z \'!%.]+$ '
              f'or exceeds {MAX_NAME_CHARS} chars.',
          )
      )

    if raw_name in condition_index:
      findings.append(
          Finding(
              'RC003_CONDITION_NAME',
              'ERROR',
              f'{c_path}.name',
              f'Duplicate condition name "{raw_name}".',
          )
      )
    else:
      condition_index[raw_name] = len(condition_names)
      condition_names.append(raw_name)

    tag_color = cond.get('tagColor')
    if tag_color is not None and tag_color not in VALID_TAG_COLORS:
      findings.append(
          Finding(
              'RC003_CONDITION_NAME',
              'ERROR',
              f'{c_path}.tagColor',
              f'Invalid tagColor "{tag_color}".',
          )
      )

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
      parsed_condition_clauses.append((raw_name, []))
      continue

    stripped_quotes = _strip_single_quoted_strings(expr)
    if '||' in stripped_quotes:
      findings.append(
          Finding(
              'RC009_EXPRESSION_GRAMMAR',
              'ERROR',
              f'{c_path}.expression',
              'Top-level OR (||) is rejected by SINGLE_OR_MULTI_AND; use '
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
      if clause.lstrip().startswith('!'):
        findings.append(
            Finding(
                'RC009_EXPRESSION_GRAMMAR',
                'ERROR',
                f'{c_path}.expression',
                f'Negation (!) in "{clause}" is rejected; use !=, '
                'notContains, notInAtLeastOne, or notInAll.',
            )
        )
    if 'device.dateTime' in stripped_quotes:
      findings.append(
          Finding(
              'RC009_EXPRESSION_GRAMMAR',
              'ERROR',
              f'{c_path}.expression',
              'Use top-level `dateTime`, not `device.dateTime`.',
          )
      )
    if STRING_INFIX_CUSTOM_OR_USER_PROP_RE.search(expr):
      findings.append(
          Finding(
              'RC009_EXPRESSION_GRAMMAR',
              'ERROR',
              f'{c_path}.expression',
              'String comparison on app.customSignal or app.userProperty must '
              'use .exactlyMatches([...]), .contains([...]), etc., not ==/!=.',
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
              '`app.customSignal` in condition expressions.',
          )
      )

    parsed_condition_clauses.append((raw_name, clauses))

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

  if len(param_groups) > MAX_PARAMETER_GROUPS:
    findings.append(
        Finding(
            'RC002_GLOBAL_UNIQUE_KEY',
            'ERROR',
            'parameterGroups',
            f'Template has {len(param_groups)} groups (max '
            f'{MAX_PARAMETER_GROUPS}).',
        )
    )

  for group_name, group_obj in param_groups.items():
    g_path = f'parameterGroups.{group_name}'
    if (
        not group_name
        or len(group_name) > MAX_NAME_CHARS
        or group_name != group_name.strip()
    ):
      findings.append(
          Finding(
              'RC002_GLOBAL_UNIQUE_KEY',
              'ERROR',
              g_path,
              f'Group name "{group_name}" must be 1..{MAX_NAME_CHARS} chars '
              'with no leading/trailing spaces.',
          )
      )
    global_keys[group_name] = g_path
    if not isinstance(group_obj, dict):
      continue
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

  if len(all_param_entries) > MAX_PARAMETERS:
    findings.append(
        Finding(
            'RC001_KEY_FORMAT',
            'ERROR',
            'parameters',
            f'Template has {len(all_param_entries)} total parameters '
            f'(max {MAX_PARAMETERS}).',
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
              f'DUPLICATE_KEY: "{p_key}" collides with {global_keys[p_key]}.',
          )
      )
    else:
      global_keys[p_key] = p_path

    if not isinstance(p_obj, dict):
      continue

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
                  'top-level conditions.',
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
                  f'CONDIITION_ORDERING_INVALID: keys {valid_keys} do not '
                  f'match conditions priority order {expected_keys}.',
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

  if args.fix_order or args.fix_types or args.prune_dangling:
    with open(args.template_path, 'w', encoding='utf-8') as f:
      json.dump(template, f, indent=2)
      f.write('\n')

  errors = [item for item in findings if item.severity == 'ERROR']
  if args.json:
    print(
        json.dumps(
            {
                'valid': not errors,
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

  return 1 if errors else 0


if __name__ == '__main__':
  sys.exit(main())
