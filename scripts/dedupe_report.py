"""Generate a structured JSON report of duplicate formulas."""
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, '/home/admin/equations')
import importlib
fd = importlib.import_module('find_duplicates')
importlib.reload(fd)

FORMULAS_DIR = '/home/admin/equations/formulas'

def load_all():
    files = sorted(os.listdir(FORMULAS_DIR))
    file_data = {}
    errors = []

    for fname in files:
        if not fname.endswith('.yaml'):
            continue
        path = os.path.join(FORMULAS_DIR, fname)
        data = fd.parse_yaml_line_by_line(path)
        if 'equation' not in data or 'id' not in data:
            errors.append({"file": fname, "error": "missing equation or id"})
            continue
        tree = fd.parse_equation(data['equation'])
        if tree is None:
            errors.append({"file": fname, "error": "parse failed (None)"})
            continue
        if isinstance(tree, tuple):
            errors.append({"file": fname, "error": "parse returned tuple"})
            continue
        data['_tree'] = tree
        data['_norm'] = tree.normalized(sort_commutative=True)
        data['_vars'] = sorted(tree.all_leaves())
        data['_file'] = fname
        file_data[data['id']] = data

    return file_data, errors


def record(d):
    return {
        "id": d['id'],
        "file": d['_file'],
        "name": d.get('name', ''),
        "topic": d.get('topic', ''),
        "equation": d.get('equation', ''),
    }


def report():
    file_data, errors = load_all()

    # Level 1: exact equation string duplicates
    eq_groups = defaultdict(list)
    for fid, data in file_data.items():
        eq_groups[data['equation']].append(data)
    exact_dups = []
    for eq, group in sorted(eq_groups.items()):
        if len(group) > 1:
            exact_dups.append({
                "equation": eq,
                "files": [record(d) for d in group],
            })

    # Level 2: structural (normalized) duplicates
    norm_groups = defaultdict(list)
    for fid, data in file_data.items():
        norm_groups[data['_norm']].append(data)
    struct_dups = []
    for norm, group in sorted(norm_groups.items()):
        if len(group) > 1 and len(set(d['equation'] for d in group)) > 1:
            struct_dups.append({
                "normalized": norm,
                "files": [record(d) for d in group],
            })

    # Level 3: rearranged formulas (same vars, swapped sides)
    var_groups = defaultdict(list)
    for fid, data in file_data.items():
        var_groups[tuple(data['_vars'])].append(data)

    rearrangements = []
    for varset, group in var_groups.items():
        if len(group) < 2:
            continue
        entries = []
        for d in group:
            tree = d['_tree']
            if tree is None or tree.value != 'eq' or len(tree.children) < 2:
                continue
            left_norm = tree.children[0].normalized(True)
            right_norm = tree.children[1].normalized(True)
            entries.append((d, left_norm, right_norm))

        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                d1, l1, r1 = entries[i]
                d2, l2, r2 = entries[j]
                if l1 == r2 and r1 == l2:
                    rearrangements.append({
                        "vars": list(varset),
                        "a": record(d1),
                        "a_left": l1,
                        "a_right": r1,
                        "b": record(d2),
                        "b_left": l2,
                        "b_right": r2,
                    })

    # Level 4: same-topic rearrangements
    topic_groups = defaultdict(list)
    for fid, data in file_data.items():
        topic_groups[data.get('topic', 'unknown')].append(data)

    same_topic_rearrangements = []
    for topic, group in topic_groups.items():
        if len(group) < 2:
            continue
        entries = []
        for d in group:
            tree = d['_tree']
            if tree is None or tree.value != 'eq' or len(tree.children) < 2:
                continue
            lv = tree.children[0].leftmost_leaf()
            rv = tree.children[1].all_leaves()
            entries.append((d, lv, rv, tree.children[1].normalized(True)))

        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                d1, lv1, rv1, rn1 = entries[i]
                d2, lv2, rv2, rn2 = entries[j]
                if lv1 in rv2 and lv2 in rv1 and set(d1['_vars']) == set(d2['_vars']):
                    same_topic_rearrangements.append({
                        "topic": topic,
                        "a": record(d1),
                        "b": record(d2),
                    })

    # Level 5: duplicate names
    name_dups = []
    for topic, group in topic_groups.items():
        if len(group) < 2:
            continue
        entries = [(d, (d.get('name', '') or '').lower().replace('[', '').replace(']', '').strip())
                   for d in group]
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                d1, n1 = entries[i]
                d2, n2 = entries[j]
                if n1 == n2:
                    name_dups.append({
                        "topic": topic,
                        "name": d1.get('name', ''),
                        "a": record(d1),
                        "b": record(d2),
                    })

    report_data = {
        "summary": {
            "total_files": len(file_data),
            "errors": len(errors),
            "exact_duplicate_groups": len(exact_dups),
            "structural_duplicate_groups": len(struct_dups),
            "rearrangement_pairs": len(rearrangements),
            "same_topic_rearrangement_pairs": len(same_topic_rearrangements),
            "duplicate_name_pairs": len(name_dups),
        },
        "errors": errors,
        "exact_duplicates": exact_dups,
        "structural_duplicates": struct_dups,
        "rearrangements": rearrangements,
        "same_topic_rearrangements": same_topic_rearrangements,
        "duplicate_names": name_dups,
    }

    print(json.dumps(report_data, indent=2))


if __name__ == '__main__':
    report()
